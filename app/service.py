"""业务服务层：借还、消毒、充电、维修、押金复核的全部规则。

所有写方法在 db._lock 保护的单连接事务中执行，保证设备状态与订单的一致性。
"""
import json
import time
import uuid

from . import adapters, config
from .db import get_conn

# ---------- 工具 ----------

class ServiceError(Exception):
    def __init__(self, message, code=400):
        super().__init__(message)
        self.code = code


def now():
    return time.time()


def q(sql, args=()):
    return get_conn().execute(sql, args)


def d(row):
    return dict(row) if row is not None else None


def audit(role, action, target=None, detail=None):
    q("INSERT INTO audits(ts, role, action, target, detail) VALUES (?,?,?,?,?)",
      (now(), role, action, target, json.dumps(detail, ensure_ascii=False) if detail else None))


# ---------- 权限 ----------

STAFF_ROLES = {"clerk", "technician", "repair"}


def require_role(actor, allowed):
    role = (actor or {}).get("role")
    if role not in allowed:
        raise ServiceError(f"当前角色无权此操作（需要 {'/'.join(sorted(allowed))}）", 403)


def require_site(actor, site_id):
    """馆区权限：主管可跨馆，其余人员仅限本馆。"""
    if actor.get("role") == "supervisor":
        return
    if actor.get("site_id") != site_id:
        raise ServiceError("跨馆操作被拒绝：设备不属于你所在馆区", 403)


# ---------- 初始化 / 清单 ----------

def init_db():
    conn = get_conn()
    with open(config.BASE_DIR / "app" / "schema.sql", encoding="utf-8") as f:
        conn.executescript(f.read())
    conn.commit()


def lang_manifest():
    """读取本地离线语言包清单（离线样例目录）。"""
    path = config.LANG_DIR / "manifest.json"
    if path.exists():
        return json.loads(path.read_text(encoding="utf-8"))
    return {"packs": {}}


def version_lt(device_v, required_v):
    def parse(v):
        out = []
        for part in str(v).split("."):
            num = "".join(ch for ch in part if ch.isdigit())
            out.append(int(num or 0))
        return out
    a, b = parse(device_v), parse(required_v)
    n = max(len(a), len(b))
    a += [0] * (n - len(a)); b += [0] * (n - len(b))
    return a < b


# ---------- 借出 ----------

def checkout_precheck(device_id):
    """借出前校验：语言包版本、电量、配件、设备排他。返回 (device, problems[])。"""
    dev = q("SELECT * FROM devices WHERE id=?", (device_id,)).fetchone()
    if not dev:
        raise ServiceError("设备编号不存在", 404)
    dev = d(dev)
    problems = []

    locked = q("SELECT id FROM orders WHERE device_id=? AND status IN ('active','overdue')",
               (device_id,)).fetchone()
    if locked:
        problems.append(f"设备已被租借订单 {locked['id']} 锁定，不能重复借出")
    elif dev["status"] != "available":
        problems.append(f"设备当前不可借（状态：{config.DEVICE_STATUS.get(dev['status'], dev['status'])}），无在借订单")
    if not dev["lang_ok"]:
        problems.append("语言包损坏，需重新灌装后才能借出")
    else:
        required = lang_manifest()["packs"].get(dev["lang_code"], {}).get("version")
        if required and version_lt(dev["lang_version"], required):
            problems.append(f"语言包版本过旧（本机 {dev['lang_version']} / 要求 {required}），请先升级")
    if dev["battery"] < config.READY_BATTERY:
        problems.append(f"电量不足（{dev['battery']}% < {config.READY_BATTERY}%）")
    if not dev["earphone"]:
        problems.append("耳机缺失")
    if not dev["strap"]:
        problems.append("挂绳缺失")
    return dev, problems


def checkout(actor, device_id, visitor_id, confirm=True):
    require_role(actor, {"clerk", "supervisor"})
    dev, problems = checkout_precheck(device_id)
    require_site(actor, dev["current_site_id"])
    if problems:
        # 馆区校验后再暴露阻断项
        raise ServiceError("；".join(problems), 409)
    visitor = q("SELECT * FROM visitors WHERE id=?", (visitor_id,)).fetchone()
    if not visitor:
        raise ServiceError("参观者不存在", 404)

    conn = get_conn()
    with conn:  # 事务
        # 行级复核：防止两个订单同时锁定同一设备
        row = conn.execute(
            "SELECT status, site_id FROM devices WHERE id=?", (device_id,)).fetchone()
        lock = conn.execute(
            "SELECT id FROM orders WHERE device_id=? AND status IN ('active','overdue')",
            (device_id,)).fetchone()
        if row["status"] != "available" or lock:
            raise ServiceError("设备已被其他订单锁定，无法重复借出", 409)

        oid = f"O-{int(now())}-{device_id[-4:]}"
        ts = now()
        hold = adapters.payment_hold(oid, config.DEPOSIT_DEFAULT)
        conn.execute(
            """INSERT INTO orders(id, device_id, visitor_id, site_id, lang_code,
               issued_at, due_at, status, deposit_amount, deposit_status, note)
               VALUES (?,?,?,?,?,?,?, 'active', ?, 'held', ?)""",
            (oid, device_id, visitor_id, dev["site_id"], dev["lang_code"],
             ts, ts + config.LOAN_HOURS * 3600, config.DEPOSIT_DEFAULT,
             f"预授权 {hold['receipt']}"))
        conn.execute("UPDATE devices SET status='on_loan', slot_no=NULL WHERE id=?", (device_id,))
        conn.execute("UPDATE charging_slots SET device_id=NULL WHERE device_id=?", (device_id,))
    audit(actor.get("role"), "checkout", device_id, {"order": oid, "visitor": visitor_id})
    return {"order_id": oid, "deposit": config.DEPOSIT_DEFAULT, "hold_receipt": hold["receipt"]}


# ---------- 归还 ----------

def find_active_order(device_id):
    row = q("""SELECT o.*, v.name visitor_name, v.phone visitor_phone
               FROM orders o JOIN visitors v ON v.id=o.visitor_id
               WHERE o.device_id=? AND o.status IN ('active','overdue')
               ORDER BY o.issued_at DESC LIMIT 1""", (device_id,)).fetchone()
    return d(row) if row else None


def begin_return(actor, device_id):
    """柜台开始归还检查：设备锁定在 return_check，防止借出/重复操作。"""
    require_role(actor, {"clerk", "supervisor"})
    dev = q("SELECT status, site_id FROM devices WHERE id=?", (device_id,)).fetchone()
    if not dev:
        raise ServiceError("设备编号不存在", 404)
    # 允许在非归属馆区开始检查——这正是跨馆归还流程
    order = find_active_order(device_id)
    if not order:
        raise ServiceError("该设备没有进行中的租借订单", 404)
    if dev["status"] == "on_loan":
        with get_conn() as conn:
            conn.execute("UPDATE devices SET status='return_check' WHERE id=?", (device_id,))
        audit(actor.get("role"), "return_begin", device_id, {"order": order["id"]})
    elif dev["status"] != "return_check":
        raise ServiceError("设备当前不在借出状态，不能进入归还检查", 409)
    return order


def assess_return(order, flags):
    """根据勾选结果计算扣款与去向（纯计算，供预览与落库）。"""
    fees = {"overdue": 0, "earphone": 0, "strap": 0, "damage": 0}
    ts = now()
    if order["status"] == "overdue" or ts > order["due_at"]:
        fees["overdue"] = config.OVERDUE_FEE
    if not flags.get("earphone", True):
        fees["earphone"] = config.EARPHONE_FEE
    if not flags.get("strap", True):
        fees["strap"] = config.STRAP_FEE
    if not flags.get("shell_ok", True) or not flags.get("button_ok", True):
        fees["damage"] = config.DAMAGE_FEE
    needs_repair = (not flags.get("shell_ok", True) or not flags.get("button_ok", True)
                    or not flags.get("lang_ok", True)
                    or not flags.get("earphone", True) or not flags.get("strap", True))
    return fees, needs_repair


def return_device(actor, device_id, return_site_id, flags, dispute=False, note=""):
    require_role(actor, {"clerk", "supervisor"})
    dev_row = q("SELECT * FROM devices WHERE id=?", (device_id,)).fetchone()
    if not dev_row:
        raise ServiceError("设备编号不存在", 404)
    dev = d(dev_row)
    require_site(actor, return_site_id)  # 只能在本人所在馆区办理归还
    order = find_active_order(device_id)
    if not order:
        past = q("SELECT id, deposit_status, refunded_amount FROM orders WHERE device_id=? "
                 "ORDER BY returned_at DESC LIMIT 1", (device_id,)).fetchone()
        if past:
            raise ServiceError(
                f"重复归还被拦截：最近订单 {past['id']} 已结案，押金状态 {past['deposit_status']}，"
                "不会重复退款", 409)
        raise ServiceError("该设备没有进行中的租借订单", 404)
    if dev["status"] not in ("on_loan", "return_check"):
        raise ServiceError("设备当前不在借出状态，不能归还", 409)

    cross_site = return_site_id != order["site_id"]
    fees, needs_repair = assess_return(order, flags)
    total_fees = sum(fees.values())

    conn = get_conn()
    with conn:
        # 幂等复核
        again = conn.execute(
            "SELECT status, deposit_status FROM orders WHERE id=?", (order["id"],)).fetchone()
        if again["status"] == "returned":
            raise ServiceError("重复归还被拦截：订单已结案，押金不重复退还", 409)

        conn.execute("""UPDATE devices SET battery=?, earphone=?, strap=?, shell_ok=?, button_ok=?,
                        lang_ok=?, status=?, note=?, pending_transfer=?, current_site_id=? WHERE id=?""",
                     (flags.get("battery", dev["battery"]),
                      1 if flags.get("earphone", True) else 0,
                      1 if flags.get("strap", True) else 0,
                      1 if flags.get("shell_ok", True) else 0,
                      1 if flags.get("button_ok", True) else 0,
                      1 if flags.get("lang_ok", True) else 0,
                      "repair" if needs_repair else "pending_disinfection",
                      note, 1 if cross_site else 0, return_site_id, device_id))

        deposit_status, refunded, receipt = "held", 0, None
        if total_fees == 0 and not dispute:
            r = adapters.payment_refund(order["id"], order["deposit_amount"])
            deposit_status, refunded, receipt = "refunded", order["deposit_amount"], r["receipt"]

        conn.execute("""UPDATE orders SET status='returned', returned_at=?,
             deposit_status=?, refunded_amount=?, refunded_at=?, refund_receipt=?,
             fee_overdue=?, fee_earphone=?, fee_strap=?, fee_damage=?,
             earnings=?, dispute=?, return_site_id=?, cross_site=?, note=?
             WHERE id=?""",
             (now(), deposit_status, refunded, now() if receipt else None, receipt,
              fees["overdue"], fees["earphone"], fees["strap"], fees["damage"],
              0 if deposit_status == "held" else total_fees,
              1 if dispute else 0, return_site_id, 1 if cross_site else 0, note, order["id"]))

        if needs_repair:
            cats = []
            if not flags.get("shell_ok", True): cats.append(("shell", "外壳损坏"))
            if not flags.get("button_ok", True): cats.append(("button", "按键故障"))
            if not flags.get("lang_ok", True): cats.append(("lang", "语言包损坏"))
            if not flags.get("earphone", True): cats.append(("accessory", "耳机缺失"))
            if not flags.get("strap", True): cats.append(("accessory", "挂绳缺失"))
            for cat, label in cats:
                conn.execute("""INSERT INTO repair_tickets
                    (device_id, site_id, reported_by, issue, category, status, created_at)
                    VALUES (?,?,?,?,?, 'open', ?)""",
                    (device_id, return_site_id, actor.get("role", "clerk"),
                     f"归还检查：{label}" + (f"（{note}）" if note else ""), cat, now()))
    audit(actor.get("role"), "return", device_id,
          {"order": order["id"], "fees": fees, "cross_site": cross_site,
           "route": "repair" if needs_repair else "pending_disinfection", "dispute": dispute})
    return {"order_id": order["id"], "fees": fees, "total_fees": total_fees,
            "cross_site": cross_site,
            "route": "repair" if needs_repair else "pending_disinfection",
            "deposit_status": deposit_status, "refunded": refunded, "receipt": receipt,
            "message": ("无扣款，押金已原路退还" if deposit_status == "refunded"
                        else ("存在争议/扣款，押金冻结，等待主管复核" if dispute or total_fees
                              else ""))}


# ---------- 消毒组批 ----------

def list_batches(site_id):
    rows = q("SELECT * FROM disinfect_batches WHERE site_id=? ORDER BY id DESC", (site_id,)).fetchall()
    out = []
    for b in rows:
        b = d(b)
        b["devices"] = [d(x) for x in q(
            """SELECT dv.id, dv.battery, dv.pending_transfer, dv.status
               FROM disinfect_items i JOIN devices dv ON dv.id=i.device_id
               WHERE i.batch_id=?""", (b["id"],)).fetchall()]
        out.append(b)
    return out


def create_batch(actor, device_ids):
    require_role(actor, {"technician", "supervisor"})
    if not device_ids:
        raise ServiceError("请至少选择一台设备")
    conn = get_conn()
    with conn:
        site = None
        for did in device_ids:
            row = conn.execute("SELECT status, current_site_id FROM devices WHERE id=?", (did,)).fetchone()
            if not row:
                raise ServiceError(f"设备 {did} 不存在", 404)
            require_site(actor, row["current_site_id"])
            if row["status"] != "pending_disinfection":
                raise ServiceError(f"设备 {did} 不在待消毒队列，不能组批", 409)
            in_batch = conn.execute(
                "SELECT 1 FROM disinfect_items WHERE device_id=?", (did,)).fetchone()
            if in_batch:
                raise ServiceError(f"设备 {did} 已在消毒批次中", 409)
            site = row["current_site_id"]
        cur = conn.execute(
            "INSERT INTO disinfect_batches(site_id, created_at, status) VALUES (?,?,'sterilizing')",
            (site, now()))
        bid = cur.lastrowid
        conn.executemany("INSERT INTO disinfect_items(batch_id, device_id) VALUES (?,?)",
                         [(bid, x) for x in device_ids])
    audit(actor.get("role"), "batch_create", str(bid), {"devices": device_ids})
    return {"batch_id": bid}


def sterilize_batch(actor, batch_id):
    require_role(actor, {"technician", "supervisor"})
    conn = get_conn()
    with conn:
        b = conn.execute("SELECT * FROM disinfect_batches WHERE id=?", (batch_id,)).fetchone()
        if not b:
            raise ServiceError("批次不存在", 404)
        require_site(actor, b["site_id"])
        if b["status"] != "sterilizing":
            raise ServiceError("批次已放行", 409)
        if b["sterilized_at"]:
            raise ServiceError("该批次已灭菌，等待放行", 409)
        conn.execute("UPDATE disinfect_batches SET sterilized_at=?, note='灭菌完成，等待放行' "
                     "WHERE id=?", (now(), batch_id))
    audit(actor.get("role"), "batch_sterilized", str(batch_id))
    return {"ok": True}


def release_batch(actor, batch_id):
    """放行：设备按 跨馆调拨 / 待充电 / 可借入柜 分流。未消毒完成的批次不得放行。"""
    require_role(actor, {"technician", "supervisor"})
    conn = get_conn()
    routed = []
    with conn:
        b = conn.execute("SELECT * FROM disinfect_batches WHERE id=?", (batch_id,)).fetchone()
        if not b:
            raise ServiceError("批次不存在", 404)
        require_site(actor, b["site_id"])
        if b["status"] != "sterilizing":
            raise ServiceError("批次已放行，不能重复放行", 409)
        if not b["sterilized_at"]:
            raise ServiceError("消毒阻断：批次尚未完成灭菌，设备不能放行回柜", 409)
        items = conn.execute("SELECT device_id FROM disinfect_items WHERE batch_id=?", (batch_id,)).fetchall()
        for it in items:
            did = it["device_id"]
            dev = conn.execute("SELECT * FROM devices WHERE id=?", (did,)).fetchone()
            if dev["pending_transfer"]:
                conn.execute("UPDATE devices SET status='in_transit' WHERE id=?", (did,))
                routed.append((did, "in_transit"))
            elif dev["battery"] < config.READY_BATTERY:
                conn.execute("UPDATE devices SET status='pending_charge' WHERE id=?", (did,))
                routed.append((did, "pending_charge"))
            else:
                slot = conn.execute(
                    "SELECT id FROM charging_slots WHERE site_id=? AND ok=1 AND device_id IS NULL "
                    "ORDER BY label LIMIT 1", (dev["current_site_id"],)).fetchone()
                conn.execute("UPDATE devices SET status='available', slot_no=?, last_release_batch=? "
                             "WHERE id=?", (slot["id"] if slot else None, batch_id, did))
                if slot:
                    conn.execute("UPDATE charging_slots SET device_id=? WHERE id=?", (did, slot["id"]))
                routed.append((did, "available"))
        conn.execute("UPDATE disinfect_batches SET status='released', released_at=?, released_by=? "
                     "WHERE id=?", (now(), actor.get("role"), batch_id))
    audit(actor.get("role"), "batch_release", str(batch_id), {"routed": routed})
    return {"routed": routed}


def receive_transfer(actor, device_id):
    """主管接收跨馆归还设备，回到本馆周转流程。"""
    require_role(actor, {"supervisor"})
    conn = get_conn()
    with conn:
        dev = conn.execute("SELECT * FROM devices WHERE id=?", (device_id,)).fetchone()
        if not dev:
            raise ServiceError("设备不存在", 404)
        require_site(actor, dev["site_id"])
        if dev["status"] != "in_transit" or not dev["pending_transfer"]:
            raise ServiceError("设备不在跨馆调拨状态", 409)
        target = "pending_charge" if dev["battery"] < config.READY_BATTERY else "pending_disinfection"
        conn.execute("UPDATE devices SET status=?, pending_transfer=0, current_site_id=? WHERE id=?",
                     (target, dev["site_id"], device_id))
    audit(actor.get("role"), "transfer_receive", device_id, {"target": target})
    return {"target": target}


# ---------- 充电槽 ----------

def list_slots(site_id):
    rows = q("""SELECT s.*, d.battery, d.status device_status, d.lang_code
                FROM charging_slots s LEFT JOIN devices d ON d.id=s.device_id
                WHERE s.site_id=? ORDER BY s.label""", (site_id,)).fetchall()
    return [d(r) for r in rows]


def set_slot_fault(actor, slot_id, ok, note=""):
    require_role(actor, {"technician", "supervisor"})
    conn = get_conn()
    with conn:
        s = conn.execute("SELECT * FROM charging_slots WHERE id=?", (slot_id,)).fetchone()
        if not s:
            raise ServiceError("槽位不存在", 404)
        require_site(actor, s["site_id"])
        if not ok and s["device_id"]:
            # 故障槽上的设备退出：充电中转待充电（转投其他槽），可借设备离槽
            dev = conn.execute("SELECT status FROM devices WHERE id=?", (s["device_id"],)).fetchone()
            if dev and dev["status"] == "charging":
                conn.execute("UPDATE devices SET status='pending_charge', slot_no=NULL WHERE id=?",
                             (s["device_id"],))
                conn.execute("""UPDATE charging_events SET ended_at=?, result='slot_fault'
                                WHERE device_id=? AND ended_at IS NULL""", (now(), s["device_id"]))
            elif dev:
                conn.execute("UPDATE devices SET slot_no=NULL WHERE id=?", (s["device_id"],))
            conn.execute("UPDATE charging_slots SET device_id=NULL WHERE id=?", (slot_id,))
        conn.execute("UPDATE charging_slots SET ok=?, note=? WHERE id=?",
                     (1 if ok else 0, note, slot_id))
    audit(actor.get("role"), "slot_fault", slot_id, {"ok": ok})
    return {"ok": bool(ok)}


def dock(actor, device_id, slot_id):
    require_role(actor, {"technician", "supervisor"})
    conn = get_conn()
    with conn:
        dev = conn.execute("SELECT * FROM devices WHERE id=?", (device_id,)).fetchone()
        sl = conn.execute("SELECT * FROM charging_slots WHERE id=?", (slot_id,)).fetchone()
        if not dev or not sl:
            raise ServiceError("设备或槽位不存在", 404)
        require_site(actor, dev["current_site_id"])
        if dev["current_site_id"] != sl["site_id"]:
            raise ServiceError("设备与充电槽不属于同一馆区", 403)
        if not sl["ok"]:
            raise ServiceError("充电槽故障，无法接入", 409)
        if sl["device_id"]:
            raise ServiceError("槽位已被占用", 409)
        if dev["status"] != "pending_charge":
            raise ServiceError("仅待充电设备可入槽充电", 409)
        conn.execute("UPDATE devices SET status='charging', slot_no=? WHERE id=?", (slot_id, device_id))
        conn.execute("UPDATE charging_slots SET device_id=? WHERE id=?", (device_id, slot_id))
        conn.execute("""INSERT INTO charging_events(slot_id, device_id, started_at)
                        VALUES (?,?,?)""", (slot_id, device_id, now()))
        adapters.cabinet_dock(slot_id, device_id)
    audit(actor.get("role"), "dock", device_id, {"slot": slot_id})
    return {"ok": True}


def tick_charging(actor=None):
    """后台任务：推进充电；达到阈值后设备在槽位上转为可借。"""
    conn = get_conn()
    done = []
    with conn:
        rows = conn.execute("SELECT * FROM devices WHERE status='charging'").fetchall()
        for r in rows:
            nb = min(100, r["battery"] + config.CHARGE_STEP)
            if nb >= config.READY_BATTERY:
                conn.execute("UPDATE devices SET battery=?, status='available' WHERE id=?", (nb, r["id"]))
                conn.execute("""UPDATE charging_events SET ended_at=?, result='charged'
                                WHERE device_id=? AND ended_at IS NULL""", (now(), r["id"]))
                done.append(r["id"])
            else:
                conn.execute("UPDATE devices SET battery=? WHERE id=?", (nb, r["id"]))
    return done


def undock(actor, device_id):
    """人工取出（维修/调拨）。电量未达阈值不得作为可借取出。"""
    require_role(actor, {"technician", "supervisor"})
    conn = get_conn()
    with conn:
        dev = conn.execute("SELECT * FROM devices WHERE id=?", (device_id,)).fetchone()
        if not dev:
            raise ServiceError("设备不存在", 404)
        require_site(actor, dev["current_site_id"])
        if dev["status"] == "charging" and dev["battery"] < config.READY_BATTERY:
            raise ServiceError(f"电量 {dev['battery']}% 未达阈值，不能取出入柜", 409)
        conn.execute("UPDATE charging_slots SET device_id=NULL WHERE device_id=?", (device_id,))
        conn.execute("UPDATE devices SET slot_no=NULL WHERE id=?", (device_id,))
    audit(actor.get("role"), "undock", device_id)
    return {"ok": True}


# ---------- 维修 ----------

def list_repairs(site_id=None, status=None):
    sql = """SELECT t.*, d.battery, d.lang_version, d.lang_code, d.earphone, d.strap
             FROM repair_tickets t JOIN devices d ON d.id=t.device_id WHERE 1=1"""
    args = []
    if site_id:
        sql += " AND t.site_id=?"; args.append(site_id)
    if status:
        sql += " AND t.status=?"; args.append(status)
    sql += " ORDER BY t.status, t.id"
    return [d(r) for r in q(sql, args).fetchall()]


def report_fault(actor, device_id, issue, category="other"):
    """讲解员可报故障（无需维修角色），但无权批准赔付。"""
    require_role(actor, {"clerk", "technician", "repair", "supervisor"})
    dev = q("SELECT * FROM devices WHERE id=?", (device_id,)).fetchone()
    if not dev:
        raise ServiceError("设备不存在", 404)
    require_site(actor, dev["current_site_id"])
    conn = get_conn()
    with conn:
        cur = conn.execute("""INSERT INTO repair_tickets
            (device_id, site_id, reported_by, issue, category, status, created_at)
            VALUES (?,?,?,?, ?, 'open', ?)""",
            (device_id, dev["current_site_id"], actor.get("role"), issue, category, now()))
        if dev["status"] == "available":
            conn.execute("UPDATE devices SET status='repair', slot_no=? WHERE id=?",
                         (None, device_id))
            conn.execute("UPDATE charging_slots SET device_id=NULL WHERE device_id=?", (device_id,))
        tid = cur.lastrowid
    audit(actor.get("role"), "repair_report", device_id, {"ticket": tid, "issue": issue})
    return {"ticket_id": tid}


def resolve_repair(actor, ticket_id, resolution="已修复", replace=None):
    require_role(actor, {"repair", "supervisor"})
    conn = get_conn()
    with conn:
        t = conn.execute("SELECT * FROM repair_tickets WHERE id=?", (ticket_id,)).fetchone()
        if not t:
            raise ServiceError("维修单不存在", 404)
        require_site(actor, t["site_id"])
        if t["status"] == "resolved":
            raise ServiceError("维修单已结案", 409)
        did = t["device_id"]
        sets, args = [], []
        if t["category"] == "lang":
            dev = conn.execute("SELECT lang_code FROM devices WHERE id=?", (did,)).fetchone()
            ver = lang_manifest()["packs"].get(dev["lang_code"], {}).get("version", "3.2.1")
            sets += ["lang_ok=1", "lang_version=?"]; args.append(ver)
        elif t["category"] == "accessory":
            repl = replace or {}
            if repl.get("earphone", True):
                sets.append("earphone=1")
            if repl.get("strap", True):
                sets.append("strap=1")
        elif t["category"] == "shell":
            sets.append("shell_ok=1")
        elif t["category"] == "button":
            sets.append("button_ok=1")
        # 修复后必须重新消毒才能回柜
        sets.append("status='pending_disinfection'")
        conn.execute(f"UPDATE devices SET {', '.join(sets)} WHERE id=?", args + [did])
        conn.execute("UPDATE devices SET slot_no=? WHERE id=?", (None, did))
        conn.execute("UPDATE charging_slots SET device_id=NULL WHERE device_id=?", (did,))
        conn.execute("""UPDATE repair_tickets SET status='resolved', resolved_at=?, resolution=?
                        WHERE id=?""", (now(), resolution, ticket_id))
    audit(actor.get("role"), "repair_resolve", str(ticket_id), {"device": did})
    return {"ok": True}


# ---------- 押金复核 ----------

def list_deposit_queue(site_id=None):
    sql = """SELECT o.*, v.name visitor_name, v.phone visitor_phone, d.site_id device_site,
                    (o.fee_overdue+o.fee_earphone+o.fee_strap+o.fee_damage) total_fees
             FROM orders o JOIN visitors v ON v.id=o.visitor_id
             JOIN devices d ON d.id=o.device_id
             WHERE o.status='returned' AND o.deposit_status='held'"""
    args = []
    if site_id:
        sql += " AND o.site_id=?"; args.append(site_id)
    sql += " ORDER BY o.dispute DESC, o.returned_at"
    return [d(r) for r in q(sql, args).fetchall()]


def settle_deposit(actor, order_id, action):
    """action=approve 按扣款退余额；waive 全额退。仅主管可批准。"""
    require_role(actor, {"supervisor"})  # 讲解员/维修员均无权批准赔付
    conn = get_conn()
    with conn:
        o = conn.execute("SELECT * FROM orders WHERE id=?", (order_id,)).fetchone()
        if not o:
            raise ServiceError("订单不存在", 404)
        require_site(actor, o["site_id"])
        if o["deposit_status"] != "held":
            raise ServiceError(
                f"订单已处理（{o['deposit_status']}，回执 {o['refund_receipt']}），退款幂等拦截", 409)
        fees = (o["fee_overdue"] + o["fee_earphone"] + o["fee_strap"] + o["fee_damage"])
        refund = o["deposit_amount"] - (fees if action == "approve" else 0)
        r = adapters.payment_refund(order_id, refund)
        conn.execute("""UPDATE orders SET deposit_status=?, refunded_amount=?, refunded_at=?,
                        refund_receipt=?, earnings=?, dispute=0 WHERE id=?""",
                     ("partially_refunded" if action == "approve" and fees else "refunded",
                      refund, now(), r["receipt"], fees if action == "approve" else 0, order_id))
    audit(actor.get("role"), f"deposit_{action}", order_id,
          {"refund": refund, "receipt": r["receipt"]})
    return {"refund": refund, "receipt": r["receipt"]}


# ---------- 逾期 ----------

def scan_overdue(actor=None):
    """后台任务：active 订单过期转 overdue（逾期罚金只计一次）。"""
    conn = get_conn()
    flipped = []
    with conn:
        rows = conn.execute("SELECT * FROM orders WHERE status='active' AND due_at<?", (now(),)).fetchall()
        for o in rows:
            conn.execute("UPDATE orders SET status='overdue', fee_overdue=? WHERE id=? AND fee_overdue=0",
                         (config.OVERDUE_FEE, o["id"]))
            flipped.append(o["id"])
        conn.execute("INSERT INTO audits(ts, role, action, target, detail) VALUES (?,?,?,?,?)",
                     (now(), "system", "overdue_scan", None, json.dumps(flipped)))
    return flipped


def remind(actor, order_id):
    require_role(actor, {"clerk", "supervisor"})
    o = q("SELECT * FROM orders WHERE id=?", (order_id,)).fetchone()
    if not o:
        raise ServiceError("订单不存在", 404)
    require_site(actor, o["site_id"])
    audit(actor.get("role"), "overdue_remind", order_id)
    return {"ok": True}


# ---------- 查询 ----------

def list_sites():
    return [d(r) for r in q("SELECT * FROM sites ORDER BY id").fetchall()]


def list_devices(site_id=None, status=None):
    sql = """SELECT dv.*, s.name site_name,
                    (SELECT id FROM orders WHERE device_id=dv.id AND status IN ('active','overdue')
                     ORDER BY issued_at DESC LIMIT 1) active_order
             FROM devices dv JOIN sites s ON s.id=dv.site_id WHERE 1=1"""
    args = []
    if site_id:
        sql += " AND dv.current_site_id=?"; args.append(site_id)
    if status:
        sql += " AND dv.status=?"; args.append(status)
    sql += " ORDER BY dv.id"
    return [d(r) for r in q(sql, args).fetchall()]


def device_detail(device_id):
    dev = q("SELECT * FROM devices WHERE id=?", (device_id,)).fetchone()
    if not dev:
        return None
    dev = d(dev)
    dev["orders"] = [d(r) for r in q(
        "SELECT * FROM orders WHERE device_id=? ORDER BY issued_at DESC LIMIT 5", (device_id,)).fetchall()]
    return dev


def pending_devices(site_id):
    return [d(r) for r in q(
        "SELECT * FROM devices WHERE current_site_id=? AND status='pending_disinfection' ORDER BY id",
        (site_id,)).fetchall()]


def list_orders(status=None, site_id=None):
    sql = """SELECT o.*, v.name visitor_name, v.phone visitor_phone
             FROM orders o JOIN visitors v ON v.id=o.visitor_id WHERE 1=1"""
    args = []
    if status:
        sql += " AND o.status=?"; args.append(status)
    if site_id:
        sql += " AND o.site_id=?"; args.append(site_id)
    sql += " ORDER BY o.issued_at DESC"
    return [d(r) for r in q(sql, args).fetchall()]


def list_visitors():
    return [d(r) for r in q("SELECT * FROM visitors ORDER BY id").fetchall()]


def add_visitor(name, phone):
    vid = f"V-{uuid.uuid4().hex[:6].upper()}"
    with get_conn() as conn:
        conn.execute("INSERT INTO visitors(id,name,phone) VALUES (?,?,?)", (vid, name, phone))
    return {"visitor_id": vid}


def dashboard(site_id=None):
    w = "WHERE current_site_id=?" if site_id else ""
    args = [site_id] if site_id else []
    counts = {r["status"]: r["n"] for r in q(
        f"SELECT status, COUNT(*) n FROM devices {w} GROUP BY status", args).fetchall()}
    held = q("SELECT COALESCE(SUM(deposit_amount-refunded_amount),0) s FROM orders "
             "WHERE status='returned' AND deposit_status='held'" +
             (" AND site_id=?" if site_id else ""), args).fetchone()["s"]
    earnings = q("SELECT COALESCE(SUM(earnings),0) s FROM orders" +
                 (" WHERE site_id=?" if site_id else ""), args).fetchone()["s"]
    overdue = q("SELECT COUNT(*) n FROM orders WHERE status='overdue'" +
                (" AND site_id=?" if site_id else ""), args).fetchone()["n"]
    faulty = q("SELECT COUNT(*) n FROM charging_slots WHERE ok=0" +
               (" AND site_id=?" if site_id else ""), args).fetchone()["n"]
    return {"devices": counts, "held_deposit": held, "earnings": earnings,
            "overdue": overdue, "faulty_slots": faulty}

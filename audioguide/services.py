"""业务服务层:借还、消毒、充电、维修、押金复核与权限。

所有写操作都在 db.tx() 事务内完成;设备排他靠
"条件 UPDATE + 部分唯一索引" 双保险,退款幂等靠
deposits.order_id 唯一约束与状态机单向往返。
"""
import json
import math
import sqlite3
from datetime import datetime, timedelta

from . import config
from .adapters import PaymentAdapter
from .db import all_, now_str, one, tx


# ---------- 异常 ----------
class DomainError(Exception):
    """业务异常,message 可直接展示给操作员。"""


class ValidationError(DomainError):
    """借出前校验失败(电量/语言包/配件等)。"""

    def __init__(self, problems):
        self.problems = problems  # [(code, message), ...]
        super().__init__(";".join(m for _, m in problems))


class PermissionDenied(DomainError):
    pass


class StateError(DomainError):
    """状态不允许的操作(如设备已被锁定、重复放行)。"""
    pass


class NotFound(DomainError):
    pass


# ---------- 小助手 ----------
def log_event(conn, kind, device_id=None, order_id=None, actor="", detail=""):
    conn.execute(
        "INSERT INTO events(ts,actor,kind,device_id,order_id,detail) VALUES(?,?,?,?,?,?)",
        (now_str(), actor, kind, device_id, order_id, detail),
    )


def get_staff(conn, staff_id):
    st = one(conn, "SELECT * FROM staff WHERE id=?", (staff_id,))
    if not st:
        raise PermissionDenied("请先选择工作人员身份")
    return st


def require_role(staff, *roles):
    if staff["role"] not in roles and staff["role"] != "admin":
        names = "/".join(config.ROLES[r] for r in roles)
        raise PermissionDenied(f"当前身份({config.ROLES[staff['role']]})无权操作,需要:{names}")


def require_zone(staff, museum_id):
    """馆区权限:本馆员工只能操作本馆资源,跨馆需授权或管理员。"""
    if staff["role"] == "admin" or staff["cross_zone"]:
        return
    if staff["museum_id"] != museum_id:
        raise PermissionDenied("跨馆操作需要具备跨馆授权的员工处理")


def get_device(conn, device_id_or_code):
    if isinstance(device_id_or_code, int) or str(device_id_or_code).isdigit():
        d = one(conn, "SELECT * FROM devices WHERE id=?", (int(device_id_or_code),))
    else:
        d = one(conn, "SELECT * FROM devices WHERE code=?", (device_id_or_code,))
    if not d:
        raise NotFound("设备不存在")
    return d


def required_version(conn, lang):
    row = one(conn, "SELECT required_version FROM langpack_requirements WHERE lang=?", (lang,))
    return row["required_version"] if row else None


# ---------- 借出 ----------
def validate_lendable(conn, device, lang):
    """借出前校验:状态、电量、语言包(损坏/缺失/版本)、配件。返回问题列表。"""
    problems = []
    if device["status"] != "available":
        problems.append(("state", f"设备当前状态为「{config.DEVICE_STATUS[device['status']]}」,不可借出"))
    if device["battery"] < config.LEND_MIN_BATTERY:
        problems.append(("battery", f"电量不足(当前 {device['battery']}%,借出需 ≥{config.LEND_MIN_BATTERY}%)"))
    if not device["langpack_ok"]:
        problems.append(("pack_corrupt", "语言包损坏,需送修重刷"))
    else:
        packs = json.loads(device["packs"] or "{}")
        req = required_version(conn, lang)
        if lang not in packs:
            problems.append(("pack_missing", f"未安装「{config.LANGS[lang]}」语言包"))
        elif req and packs[lang] != req:
            problems.append(("pack_version", f"语言包版本不符(当前 v{packs[lang]},要求 v{req})"))
    if not device["earphone"]:
        problems.append(("earphone", "配件缺失:耳机"))
    if not device["lanyard"]:
        problems.append(("lanyard", "配件缺失:挂绳"))
    return problems


def find_candidate(conn, museum_id, lang):
    """为柜台挑选一台可借设备(电量最高者优先)。"""
    rows = all_(
        conn,
        "SELECT * FROM devices WHERE museum_id=? AND status='available' ORDER BY battery DESC",
        (museum_id,),
    )
    for d in rows:
        if not validate_lendable(conn, d, lang):
            return d
    return None


def _next_code(conn, table, prefix):
    seq = one(conn, f"SELECT COUNT(*) AS c FROM {table}")["c"] + 1
    return "%s%s-%04d" % (prefix, datetime.now().strftime("%y%m%d"), seq)


def create_order(conn, staff, museum_id, visitor_name, visitor_phone, lang,
                 device_id=None, payment=None):
    """借出:校验→锁定设备→建单→冻结押金。同一设备不能被两个订单锁定。"""
    require_role(staff, "counter")
    require_zone(staff, museum_id)
    if lang not in config.LANGS:
        raise ValidationError([("lang", "未知语种")])
    payment = payment or PaymentAdapter()
    with tx(conn):
        if device_id:
            device = get_device(conn, device_id)
            if device["museum_id"] != museum_id:
                raise PermissionDenied("该设备不属于本馆")
        else:
            device = find_candidate(conn, museum_id, lang)
            if not device:
                raise StateError("本馆暂无满足条件的可借设备")
        problems = validate_lendable(conn, device, lang)
        if problems:
            raise ValidationError(problems)
        # 条件更新即排他锁:只有仍处 available 才能锁定成功
        cur = conn.execute(
            "UPDATE devices SET status='on_loan', updated_at=? WHERE id=? AND status='available'",
            (now_str(), device["id"]),
        )
        if cur.rowcount != 1:
            raise StateError("设备已被其他订单锁定,请重新选择")
        now = datetime.now()
        code = _next_code(conn, "orders", "OD")
        cur = conn.execute(
            "INSERT INTO orders(code,device_id,museum_id,visitor_name,visitor_phone,lang,"
            " status,deposit_amount,staff_id,created_at,due_at)"
            " VALUES(?,?,?,?,?,?, 'active',?,?,?,?)",
            (code, device["id"], museum_id, visitor_name, visitor_phone, lang,
             config.DEPOSIT_AMOUNT, staff["id"],
             now.strftime("%Y-%m-%d %H:%M:%S"),
             (now + timedelta(hours=config.LOAN_HOURS)).strftime("%Y-%m-%d %H:%M:%S")),
        )
        order_id = cur.lastrowid
        receipt = payment.hold(conn, order_id, code, config.DEPOSIT_AMOUNT)
        conn.execute(
            "INSERT INTO deposits(order_id,amount,status) VALUES(?,?, 'held')",
            (order_id, config.DEPOSIT_AMOUNT),
        )
        log_event(conn, "借出", device["id"], order_id, staff["name"],
                  f"订单 {code},押金冻结回执 {receipt}")
        return one(conn, "SELECT * FROM orders WHERE id=?", (order_id,))


# ---------- 归还 ----------
def find_open_order(conn, code):
    """按订单号或设备号找到未完结订单;若已归还则返回 ('done', order)。"""
    o = one(conn, "SELECT * FROM orders WHERE code=?", (code,))
    if not o:
        d = one(conn, "SELECT * FROM devices WHERE code=?", (code,))
        if d:
            o = one(conn,
                    "SELECT * FROM orders WHERE device_id=? ORDER BY id DESC LIMIT 1",
                    (d["id"],))
    if not o:
        raise NotFound("未找到该订单/设备的租借记录")
    return o


def _late_fee(order, now):
    due = datetime.strptime(order["due_at"], "%Y-%m-%d %H:%M:%S")
    if now <= due:
        return 0.0, 0
    hours = math.ceil((now - due).total_seconds() / 3600)
    return min(hours * config.LATE_FEE_PER_HOUR, config.LATE_FEE_CAP), hours


def get_or_create_open_batch(conn, museum_id):
    b = one(conn,
            "SELECT * FROM disinfection_batches WHERE museum_id=? AND status='open'"
            " ORDER BY id DESC LIMIT 1", (museum_id,))
    if b:
        return b
    code = _next_code(conn, "disinfection_batches", "DB")
    cur = conn.execute(
        "INSERT INTO disinfection_batches(code,museum_id,status,created_at) VALUES(?,?, 'open',?)",
        (code, museum_id, now_str()))
    return one(conn, "SELECT * FROM disinfection_batches WHERE id=?", (cur.lastrowid,))


def complete_return(conn, staff, code, shell_ok=True, buttons_ok=True,
                    earphone_back=True, lanyard_back=True,
                    deduction_mode="accept", notes="", payment=None):
    """归还检查:外观/按键/配件→定损→押金处理→设备流转。

    幂等:同一订单重复提交直接返回首次结果,不会重复退款。
    deduction_mode: accept=游客认可扣款; dispute=游客有异议→转押金复核。
    """
    require_role(staff, "counter")
    payment = payment or PaymentAdapter()
    order = find_open_order(conn, code)
    if order["status"] == "returned":
        return _return_summary(conn, order, already=True)
    require_zone(staff, order["museum_id"])  # 跨馆归还仅授权员工可办
    cross_museum = staff["museum_id"] != order["museum_id"]

    with tx(conn):
        # 事务内复查,防并发重复归还
        order = one(conn, "SELECT * FROM orders WHERE id=?", (order["id"],))
        if order["status"] == "returned":
            return _return_summary(conn, order, already=True)
        device = one(conn, "SELECT * FROM devices WHERE id=?", (order["device_id"],))
        now = datetime.now()
        fee, hours = _late_fee(order, now)

        deductions = []
        if not earphone_back:
            deductions.append(("耳机缺失", config.FEE_EARPHONE))
        if not lanyard_back:
            deductions.append(("挂绳缺失", config.FEE_LANYARD))
        if not shell_ok:
            deductions.append(("外壳破损", config.FEE_SHELL))
        if not buttons_ok:
            deductions.append(("按键失灵", config.FEE_BUTTONS))
        if fee > 0:
            deductions.append((f"逾期 {hours} 小时", fee))
        total = min(sum(a for _, a in deductions), order["deposit_amount"])

        try:
            conn.execute(
                "INSERT INTO return_checks(order_id,device_id,shell_ok,buttons_ok,"
                " earphone_ok,lanyard_ok,notes,result,checked_by,checked_at)"
                " VALUES(?,?,?,?,?,?,?,?,?,?)",
                (order["id"], device["id"], int(shell_ok), int(buttons_ok),
                 int(earphone_back), int(lanyard_back), notes,
                 "扣款 ¥%.0f" % total if total else "完好",
                 staff["id"], now.strftime("%Y-%m-%d %H:%M:%S")),
            )
        except sqlite3.IntegrityError:
            return _return_summary(conn, order, already=True)

        # 设备流转
        damaged = not shell_ok or not buttons_ok
        return_museum = staff["museum_id"]
        conn.execute(
            "UPDATE devices SET earphone=?, lanyard=?, disinfected=0, slot_id=NULL,"
            " status='pending_disinfection', museum_id=?, updated_at=? WHERE id=?",
            (int(earphone_back), int(lanyard_back), return_museum, now_str(), device["id"]))
        if damaged:
            conn.execute("UPDATE devices SET status='repair' WHERE id=?", (device["id"],))
            desc = "、".join(n for n, _ in deductions if n in ("外壳破损", "按键失灵"))
            conn.execute(
                "INSERT INTO repairs(device_id,source,description,status,reported_by,created_at)"
                " VALUES(?,?,?,?,?,?)",
                (device["id"], "return_check", f"归还检查:{desc}", "queued",
                 staff["id"], now_str()))
        else:
            batch = get_or_create_open_batch(conn, return_museum)
            conn.execute(
                "INSERT OR IGNORE INTO disinfection_items(batch_id,device_id) VALUES(?,?)",
                (batch["id"], device["id"]))
            refresh_device(conn, device["id"])

        # 押金处理(只在此发生一次)
        dep = one(conn, "SELECT * FROM deposits WHERE order_id=?", (order["id"],))
        receipts = []
        if total <= 0:
            r = payment.refund(conn, order["id"], order["code"], dep["amount"])
            receipts.append(r)
            conn.execute(
                "UPDATE deposits SET status='refunded', deduction=0, refund_receipt=? WHERE id=?",
                (r, dep["id"]))
        elif deduction_mode == "dispute":
            reason = "、".join(f"{n} ¥{a:.0f}" for n, a in deductions)
            conn.execute(
                "UPDATE deposits SET status='disputed', deduction=?, dispute_reason=? WHERE id=?",
                (total, reason, dep["id"]))
        else:
            back = dep["amount"] - total
            if total > 0:
                receipts.append(payment.collect(conn, order["id"], order["code"], total))
            if back > 0:
                receipts.append(payment.refund(conn, order["id"], order["code"], back))
            status = "forfeited" if back <= 0 else "partial"
            conn.execute(
                "UPDATE deposits SET status=?, deduction=?, refund_receipt=? WHERE id=?",
                (status, total, receipts[-1] if back > 0 else None, dep["id"]))

        conn.execute(
            "UPDATE orders SET status='returned', returned_at=?, return_staff_id=?,"
            " return_museum_id=?, late_fee=? WHERE id=?",
            (now.strftime("%Y-%m-%d %H:%M:%S"), staff["id"], return_museum, fee, order["id"]))
        if cross_museum:
            log_event(conn, "跨馆归还", device["id"], order["id"], staff["name"],
                      f"订单 {order['code']} 于非借出馆归还")
        log_event(conn, "归还", device["id"], order["id"], staff["name"],
                  f"检查完成,定损 ¥{total:.0f}")
        return _return_summary(conn, one(conn, "SELECT * FROM orders WHERE id=?", (order["id"],)))


def _return_summary(conn, order, already=False):
    check = one(conn, "SELECT * FROM return_checks WHERE order_id=?", (order["id"],))
    dep = one(conn, "SELECT * FROM deposits WHERE order_id=?", (order["id"],))
    device = one(conn, "SELECT * FROM devices WHERE id=?", (order["device_id"],))
    pays = all_(conn, "SELECT * FROM payments WHERE order_id=? ORDER BY id", (order["id"],))
    return {
        "already": already, "order": order, "check": check,
        "deposit": dep, "device": device, "payments": pays,
    }


# ---------- 设备状态机 ----------
def refresh_device(conn, device_id):
    """按标志位重算设备状态:待消毒→待充电→可借。"""
    d = one(conn, "SELECT * FROM devices WHERE id=?", (device_id,))
    if d["status"] in ("on_loan", "repair", "scrapped"):
        return d["status"]
    if not d["disinfected"]:
        new = "pending_disinfection"
        if d["slot_id"]:
            _release_slot(conn, d["slot_id"])
    elif d["battery"] < config.SHELF_MIN_BATTERY:
        new = "pending_charging"
        if not d["slot_id"]:
            _assign_slot(conn, d)
    else:
        new = "available"
        if d["slot_id"]:
            _release_slot(conn, d["slot_id"])
    if new != d["status"]:
        conn.execute("UPDATE devices SET status=?, updated_at=? WHERE id=?",
                     (new, now_str(), device_id))
        log_event(conn, "状态变更", device_id, None, "系统",
                  f"{config.DEVICE_STATUS[d['status']]} → {config.DEVICE_STATUS[new]}")
    return new


def _assign_slot(conn, device):
    slot = one(conn,
               "SELECT * FROM charging_slots WHERE museum_id=? AND status='empty'"
               " ORDER BY code LIMIT 1", (device["museum_id"],))
    if not slot:
        return None
    conn.execute("UPDATE charging_slots SET status='charging', device_id=? WHERE id=?",
                 (device["id"], slot["id"]))
    conn.execute("UPDATE devices SET slot_id=? WHERE id=?", (slot["id"], device["id"]))
    log_event(conn, "入槽充电", device["id"], None, "系统", f"槽位 {slot['code']}")
    return slot


def _release_slot(conn, slot_id):
    conn.execute("UPDATE charging_slots SET status='empty', device_id=NULL WHERE id=?", (slot_id,))
    conn.execute("UPDATE devices SET slot_id=NULL WHERE slot_id=?", (slot_id,))


# ---------- 消毒 ----------
def release_batch(conn, staff, batch_id):
    """放行消毒批次:批次内设备标记已消毒,随后按电量进入待充电或可借。"""
    require_role(staff, "counter", "technician")
    with tx(conn):
        b = one(conn, "SELECT * FROM disinfection_batches WHERE id=?", (batch_id,))
        if not b:
            raise NotFound("批次不存在")
        if b["status"] != "open":
            raise StateError("该批次已放行,请勿重复操作")
        require_zone(staff, b["museum_id"])
        conn.execute(
            "UPDATE disinfection_batches SET status='released', released_at=?, released_by=?"
            " WHERE id=?", (now_str(), staff["id"], batch_id))
        items = all_(conn, "SELECT device_id FROM disinfection_items WHERE batch_id=?", (batch_id,))
        for it in items:
            conn.execute("UPDATE devices SET disinfected=1, updated_at=? WHERE id=?",
                         (now_str(), it["device_id"]))
            refresh_device(conn, it["device_id"])
        log_event(conn, "消毒放行", None, None, staff["name"],
                  f"批次 {b['code']} 放行 {len(items)} 台")
        return len(items)


# ---------- 充电 ----------
def set_slot_fault(conn, staff, slot_id, faulty):
    """注入/清除充电槽故障。故障槽停止充电,设备保留在槽内待修。"""
    require_role(staff, "counter", "technician")
    with tx(conn):
        slot = one(conn, "SELECT * FROM charging_slots WHERE id=?", (slot_id,))
        if not slot:
            raise NotFound("槽位不存在")
        require_zone(staff, slot["museum_id"])
        if faulty:
            if slot["status"] == "fault":
                raise StateError("该槽位已处于故障状态")
            conn.execute("UPDATE charging_slots SET status='fault' WHERE id=?", (slot_id,))
            log_event(conn, "槽位故障", slot["device_id"], None, staff["name"],
                      f"槽位 {slot['code']} 故障,暂停充电")
        else:
            if slot["status"] != "fault":
                raise StateError("该槽位不在故障状态")
            new = "charging" if slot["device_id"] else "empty"
            conn.execute("UPDATE charging_slots SET status=? WHERE id=?", (new, slot_id))
            log_event(conn, "槽位修复", slot["device_id"], None, staff["name"],
                      f"槽位 {slot['code']} 恢复{new}")


# ---------- 维修 ----------
def report_fault(conn, staff, device_id_or_code, description):
    """故障上报——讲解员可报故障。"""
    with tx(conn):
        device = get_device(conn, device_id_or_code)
        cur = conn.execute(
            "INSERT INTO repairs(device_id,source,description,status,reported_by,created_at)"
            " VALUES(?,?,?,?,?,?)",
            (device["id"], "fault_report", description, "queued", staff["id"], now_str()))
        if device["status"] not in ("on_loan", "repair", "scrapped"):
            conn.execute("UPDATE devices SET status='repair', slot_id=NULL, updated_at=?"
                         " WHERE id=?", (now_str(), device["id"]))
        log_event(conn, "故障上报", device["id"], None, staff["name"], description)
        return cur.lastrowid


def resolve_repair(conn, staff, repair_id, action):
    """维修处理:start=开始维修, fixed=修复完成(重新消毒), scrap=报废。"""
    require_role(staff, "technician")
    with tx(conn):
        r = one(conn, "SELECT * FROM repairs WHERE id=?", (repair_id,))
        if not r:
            raise NotFound("维修单不存在")
        device = one(conn, "SELECT * FROM devices WHERE id=?", (r["device_id"],))
        require_zone(staff, device["museum_id"])
        if action == "start":
            if r["status"] != "queued":
                raise StateError("仅待处理维修单可开始维修")
            conn.execute("UPDATE repairs SET status='in_repair' WHERE id=?", (repair_id,))
        elif action == "fixed":
            if r["status"] not in ("queued", "in_repair"):
                raise StateError("该维修单已完结")
            conn.execute(
                "UPDATE repairs SET status='fixed', resolved_at=?, resolved_by=?,"
                " resolution='已修复' WHERE id=?",
                (now_str(), staff["id"], repair_id))
            # 修复后重新走消毒流程,语言包损坏标记一并清除(重刷完成)
            conn.execute(
                "UPDATE devices SET status='pending_disinfection', disinfected=0,"
                " langpack_ok=1, earphone=1, lanyard=1, updated_at=? WHERE id=?",
                (now_str(), device["id"]))
            batch = get_or_create_open_batch(conn, device["museum_id"])
            conn.execute("INSERT OR IGNORE INTO disinfection_items(batch_id,device_id)"
                         " VALUES(?,?)", (batch["id"], device["id"]))
        elif action == "scrap":
            if r["status"] not in ("queued", "in_repair"):
                raise StateError("该维修单已完结")
            conn.execute(
                "UPDATE repairs SET status='scrapped', resolved_at=?, resolved_by=?,"
                " resolution='报废' WHERE id=?", (now_str(), staff["id"], repair_id))
            conn.execute("UPDATE devices SET status='scrapped', slot_id=NULL, updated_at=?"
                         " WHERE id=?", (now_str(), device["id"]))
        else:
            raise StateError("未知维修操作")
        log_event(conn, "维修处理", device["id"], None, staff["name"],
                  f"维修单 #{repair_id} {action}")


# ---------- 押金复核 ----------
def review_deposit(conn, staff, deposit_id, decision, deduction=0.0, payment=None):
    """押金复核:仅复核员/管理员可批准——讲解员报故障可以,批准赔付不行。"""
    require_role(staff, "reviewer")
    payment = payment or PaymentAdapter()
    with tx(conn):
        dep = one(conn, "SELECT * FROM deposits WHERE id=?", (deposit_id,))
        if not dep:
            raise NotFound("押金记录不存在")
        if dep["status"] != "disputed":
            raise StateError("该押金不在复核队列,请勿重复处理")
        order = one(conn, "SELECT * FROM orders WHERE id=?", (dep["order_id"],))
        if decision == "refund_full":
            r = payment.refund(conn, order["id"], order["code"], dep["amount"])
            conn.execute(
                "UPDATE deposits SET status='refunded', deduction=0, refund_receipt=?,"
                " reviewed_by=?, reviewed_at=? WHERE id=?",
                (r, staff["id"], now_str(), dep["id"]))
            detail = f"复核:全额退还 ¥{dep['amount']:.0f},回执 {r}"
        elif decision == "deduct":
            deduction = max(0.0, min(float(deduction), dep["amount"]))
            back = dep["amount"] - deduction
            receipts = []
            if deduction > 0:
                receipts.append(payment.collect(conn, order["id"], order["code"], deduction))
            if back > 0:
                receipts.append(payment.refund(conn, order["id"], order["code"], back))
            status = "forfeited" if back <= 0 else "partial"
            conn.execute(
                "UPDATE deposits SET status=?, deduction=?, refund_receipt=?,"
                " reviewed_by=?, reviewed_at=? WHERE id=?",
                (status, deduction, receipts[-1] if back > 0 else None,
                 staff["id"], now_str(), dep["id"]))
            detail = f"复核:扣款 ¥{deduction:.0f},退还 ¥{back:.0f}"
        else:
            raise StateError("未知复核结论")
        log_event(conn, "押金复核", order["device_id"], order["id"], staff["name"], detail)
        return one(conn, "SELECT * FROM deposits WHERE id=?", (dep["id"],))


# ---------- 配件补齐 ----------
def restock_accessory(conn, staff, device_id):
    """补齐耳机/挂绳等配件。"""
    require_role(staff, "counter", "technician")
    with tx(conn):
        d = get_device(conn, device_id)
        require_zone(staff, d["museum_id"])
        conn.execute("UPDATE devices SET earphone=1, lanyard=1, updated_at=? WHERE id=?",
                     (now_str(), d["id"]))
        log_event(conn, "配件补齐", d["id"], None, staff["name"], "耳机/挂绳已补齐")

"""预置演示数据:两馆设备、在借/逾期/争议订单、消毒批次、充电槽、维修单。

同时生成本地资产:设备外观照片(SVG)与离线语言包样例文件。
首次启动时自动执行,也可用 `python3 app.py --reseed` 重置。
"""
import json
import os
from datetime import datetime, timedelta

from . import config
from .adapters import PaymentAdapter
from .db import now_str, one

FULL_PACKS = {"zh": "3.2", "en": "2.8", "ja": "1.9", "fr": "1.4"}
REQUIREMENTS = [("zh", "中文", "3.2"), ("en", "English", "2.8"),
                ("ja", "日本語", "1.9"), ("fr", "Français", "1.4")]

PACK_SAMPLE_ENTRIES = {
    "zh": [("hall-1", "第一展厅 · 青铜之光", "欢迎来到青铜之光展厅……"),
           ("hall-2", "第二展厅 · 丝路遗珍", "本展厅展出丝绸之路沿线文物……"),
           ("hall-3", "第三展厅 · 瓷器之美", "请欣赏宋代青瓷……"),
           ("exit", "出口导览", "感谢参观,请沿指示牌离馆……")],
    "en": [("hall-1", "Hall 1 · Bronze Age", "Welcome to the Bronze Age gallery..."),
           ("hall-2", "Hall 2 · Silk Road", "This gallery presents..."),
           ("hall-3", "Hall 3 · Porcelain", "Please enjoy the Song celadon..."),
           ("exit", "Exit Guide", "Thank you for visiting...")],
    "ja": [("hall-1", "第一展示室 · 青銅の光", "青銅の光展示室へようこそ……"),
           ("hall-2", "第二展示室 · シルクロード", "この展示室では……"),
           ("hall-3", "第三展示室 · 磁器の美", "宋代の青磁をご覧ください……"),
           ("exit", "出口案内", "ご来館ありがとうございました……")],
    "fr": [("hall-1", "Salle 1 · Âge du bronze", "Bienvenue dans la galerie..."),
           ("hall-2", "Salle 2 · Route de la soie", "Cette salle présente..."),
           ("hall-3", "Salle 3 · Porcelaine", "Veuillez admirer le céladon..."),
           ("exit", "Sortie", "Merci de votre visite...")],
}


def device_photo_svg(code, ok=True):
    """生成设备外观示意照片(SVG)。"""
    body = "#38695a" if ok else "#7a6a55"
    return f"""<svg xmlns="http://www.w3.org/2000/svg" width="240" height="160" viewBox="0 0 240 160">
  <rect x="0" y="0" width="240" height="160" rx="10" fill="#efe9da"/>
  <rect x="70" y="14" width="100" height="132" rx="14" fill="{body}" stroke="#274b40" stroke-width="2"/>
  <rect x="80" y="26" width="80" height="58" rx="6" fill="#f6f3ec"/>
  <text x="120" y="60" font-size="18" text-anchor="middle" fill="#274b40" font-family="monospace">{code}</text>
  <circle cx="120" cy="104" r="10" fill="#a3802a"/>
  <circle cx="94" cy="104" r="5" fill="#c9ae5d"/>
  <circle cx="146" cy="104" r="5" fill="#c9ae5d"/>
  <rect x="96" y="126" width="48" height="8" rx="4" fill="#274b40"/>
  <path d="M60 30 q-16 50 0 100" stroke="#a3802a" stroke-width="3" fill="none"/>
  <circle cx="60" cy="132" r="6" fill="#38695a"/>
</svg>"""


def write_assets(data_dir):
    """写设备照片与离线语言包样例文件。"""
    photo_dir = os.path.join(data_dir, "photos")
    pack_dir = os.path.join(data_dir, "langpacks")
    os.makedirs(photo_dir, exist_ok=True)
    os.makedirs(pack_dir, exist_ok=True)
    codes = [f"AG-1{i:02d}" for i in range(1, 13)] + [f"AG-2{i:02d}" for i in range(1, 7)]
    for c in codes:
        with open(os.path.join(photo_dir, c + ".svg"), "w", encoding="utf-8") as f:
            f.write(device_photo_svg(c, ok=(c != "AG-107")))
    for lang, _label, ver in REQUIREMENTS:
        entries = [{"id": i, "title": t, "text": x} for i, t, x in PACK_SAMPLE_ENTRIES[lang]]
        payload = {"lang": lang, "version": ver, "entries": entries}
        with open(os.path.join(pack_dir, f"{lang}_v{ver}.pack"), "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, indent=1)
    # 旧版英文包样例(对应 AG-109 上的过期版本)
    with open(os.path.join(pack_dir, "en_v2.7.pack"), "w", encoding="utf-8") as f:
        json.dump({"lang": "en", "version": "2.7",
                   "entries": [{"id": "hall-1", "title": "Hall 1", "text": "Old sample."}]},
                  f, ensure_ascii=False, indent=1)


def seed_all(conn, data_dir):
    """向空库写入演示数据。幂等性由调用方(仅空库时调用)保证。"""
    write_assets(data_dir)
    pay = PaymentAdapter(os.path.join(data_dir, "receipts"))
    now = datetime.now()
    ts = lambda dt: dt.strftime("%Y-%m-%d %H:%M:%S")
    ymd = now.strftime("%y%m%d")

    conn.execute("INSERT INTO museums(code,name) VALUES('M1','主馆'),('M2','东馆')")
    staff_rows = [
        ("王柜员", "counter", 1, 0), ("李柜员", "counter", 2, 0),
        ("赵讲解员", "docent", 1, 0), ("钱维修", "technician", 1, 0),
        ("孙复核", "reviewer", 1, 1), ("周管理员", "admin", 1, 1),
    ]
    conn.executemany("INSERT INTO staff(name,role,museum_id,cross_zone) VALUES(?,?,?,?)",
                     staff_rows)
    conn.executemany(
        "INSERT INTO langpack_requirements(lang,label,required_version,file) VALUES(?,?,?,?)",
        [(l, lb, v, f"{l}_v{v}.pack") for l, lb, v in REQUIREMENTS])

    # (code, museum, status, battery, disinfected, packs, pack_ok, earphone, lanyard, slot)
    P = json.dumps(FULL_PACKS)
    P_OLD_EN = json.dumps({**FULL_PACKS, "en": "2.7"})
    devices = [
        ("AG-101", 1, "on_loan", 78, 1, P, 1, 1, 1, None),
        ("AG-102", 1, "on_loan", 66, 1, P, 1, 1, 1, None),
        ("AG-103", 1, "pending_charging", 45, 1, P, 1, 1, 1, "S1-03"),
        ("AG-104", 1, "available", 92, 1, P, 1, 1, 1, None),
        ("AG-105", 1, "pending_disinfection", 84, 0, P, 1, 1, 1, None),
        ("AG-106", 1, "available", 88, 1, P, 1, 0, 1, None),   # 耳机缺失待补齐
        ("AG-107", 1, "repair", 55, 1, P, 1, 1, 1, None),      # 按键失灵
        ("AG-108", 1, "available", 90, 1, P, 0, 1, 1, None),   # 语言包损坏
        ("AG-109", 1, "available", 95, 1, P_OLD_EN, 1, 1, 1, None),  # 英文包过期
        ("AG-110", 1, "available", 61, 1, P, 1, 1, 1, None),
        ("AG-111", 1, "available", 100, 1, P, 1, 1, 1, None),
        ("AG-112", 1, "pending_charging", 30, 1, P, 1, 1, 1, "S1-05"),
        ("AG-201", 2, "on_loan", 82, 1, P, 1, 1, 1, None),
        ("AG-202", 2, "available", 97, 1, P, 1, 1, 1, None),
        ("AG-203", 2, "available", 88, 1, P, 1, 1, 1, None),
        ("AG-204", 2, "available", 73, 1, P, 1, 1, 1, None),
        ("AG-205", 2, "available", 91, 1, P, 1, 1, 1, None),
        ("AG-206", 2, "pending_charging", 35, 1, P, 1, 1, 1, "S2-02"),
    ]
    slot_ids = {}
    for m, n in ((1, 12), (2, 8)):
        for i in range(1, n + 1):
            code = f"S{m}-{i:02d}"
            cur = conn.execute(
                "INSERT INTO charging_slots(code,museum_id,status) VALUES(?,?, 'empty')",
                (code, m))
            slot_ids[code] = cur.lastrowid
    for code, m, status, bat, dis, packs, ok, ear, lan, slot in devices:
        cur = conn.execute(
            "INSERT INTO devices(code,museum_id,status,battery,disinfected,packs,langpack_ok,"
            " earphone,lanyard,photo,slot_id,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
            (code, m, status, bat, dis, packs, ok, ear, lan, f"{code}.svg",
             slot_ids.get(slot), now_str()))
        if slot:
            conn.execute("UPDATE charging_slots SET status='charging', device_id=? WHERE id=?",
                         (cur.lastrowid, slot_ids[slot]))
    conn.execute("UPDATE charging_slots SET status='fault' WHERE code='S1-07'")

    def add_order(seq, dev_code, museum, name, phone, lang, created, due,
                  status="active", returned=None):
        dev = one(conn, "SELECT id FROM devices WHERE code=?", (dev_code,))["id"]
        code = f"OD{ymd}-{seq:04d}"
        cur = conn.execute(
            "INSERT INTO orders(code,device_id,museum_id,visitor_name,visitor_phone,lang,"
            " status,deposit_amount,staff_id,created_at,due_at,returned_at)"
            " VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
            (code, dev, museum, name, phone, lang, status, config.DEPOSIT_AMOUNT,
             1, ts(created), ts(due), ts(returned) if returned else None))
        oid = cur.lastrowid
        conn.execute("INSERT INTO deposits(order_id,amount,status) VALUES(?,?, 'held')",
                     (oid, config.DEPOSIT_AMOUNT))
        pay.hold(conn, oid, code, config.DEPOSIT_AMOUNT)
        return oid, code

    h = timedelta(hours=1)
    add_order(1, "AG-101", 1, "张伟", "13800001111", "zh", now - 1 * h, now + 3 * h)
    add_order(2, "AG-102", 1, "李娜", "13800002222", "en", now - 7 * h, now - 3 * h)  # 已逾期
    add_order(3, "AG-201", 2, "王强", "13800003333", "ja", now - 2 * h, now + 2 * h)
    # 历史正常归还(已退押金)
    oid4, code4 = add_order(4, "AG-104", 1, "陈晓", "13800004444", "zh",
                            now - timedelta(days=1, hours=5), now - timedelta(days=1, hours=1),
                            status="returned", returned=now - timedelta(days=1, hours=2))
    r = pay.refund(conn, oid4, code4, config.DEPOSIT_AMOUNT)
    conn.execute("UPDATE deposits SET status='refunded', refund_receipt=? WHERE order_id=?",
                 (r, oid4))
    conn.execute(
        "INSERT INTO return_checks(order_id,device_id,shell_ok,buttons_ok,earphone_ok,"
        " lanyard_ok,notes,result,checked_by,checked_at) VALUES(?,?,1,1,1,1,'','完好',1,?)",
        (oid4, one(conn, "SELECT id FROM devices WHERE code='AG-104'")["id"], now_str()))
    # 历史归还:耳机缺失,押金争议待复核
    oid5, code5 = add_order(5, "AG-106", 1, "刘洋", "13800005555", "en",
                            now - timedelta(hours=9), now - timedelta(hours=5),
                            status="returned", returned=now - timedelta(hours=6))
    conn.execute(
        "INSERT INTO return_checks(order_id,device_id,shell_ok,buttons_ok,earphone_ok,"
        " lanyard_ok,notes,result,checked_by,checked_at) VALUES(?,?,1,1,0,1,'耳机未随机归还','扣款 ¥30',1,?)",
        (oid5, one(conn, "SELECT id FROM devices WHERE code='AG-106'")["id"], now_str()))
    conn.execute(
        "UPDATE deposits SET status='disputed', deduction=30, dispute_reason='耳机缺失 ¥30'"
        " WHERE order_id=?", (oid5,))

    # 消毒批次:一个已放行(历史),一个在组批中
    conn.execute(
        "INSERT INTO disinfection_batches(code,museum_id,status,created_at,released_at,released_by)"
        " VALUES(?,?,?,?,?,?)",
        (f"DB{ymd}-0001", 1, "released", ts(now - timedelta(days=1, hours=8)),
         ts(now - timedelta(days=1, hours=6)), 5))
    for code in ("AG-104", "AG-106"):
        conn.execute("INSERT INTO disinfection_items(batch_id,device_id) VALUES(1,"
                     " (SELECT id FROM devices WHERE code=?))", (code,))
    conn.execute(
        "INSERT INTO disinfection_batches(code,museum_id,status,created_at) VALUES(?,?, 'open',?)",
        (f"DB{ymd}-0002", 1, ts(now - timedelta(hours=2))))
    conn.execute("INSERT INTO disinfection_items(batch_id,device_id) VALUES(2,"
                 " (SELECT id FROM devices WHERE code='AG-105'))")

    # 维修单:讲解员上报的按键失灵
    conn.execute(
        "INSERT INTO repairs(device_id,source,description,status,reported_by,created_at)"
        " VALUES((SELECT id FROM devices WHERE code='AG-107'),'fault_report',"
        " '按键失灵:音量键无响应','queued',3,?)", (now_str(),))

    for kind, detail in [
        ("系统初始化", "演示数据装载完成"),
        ("借出", "OD 前三单为在借/逾期演示数据"),
        ("消毒放行", f"批次 DB{ymd}-0001 已放行(历史)"),
    ]:
        conn.execute("INSERT INTO events(ts,actor,kind,detail) VALUES(?,?,?,?)",
                     (now_str(), "系统", kind, detail))
    conn.commit()

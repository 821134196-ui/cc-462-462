"""预置演示数据：馆区、设备（含各类异常）、参观者、槽位、进行中/逾期订单、
待消毒批次、维修单、押金复核队列，并生成本地语言包样例与外观照片占位图。"""
import json
import sqlite3
import time

from . import config
from .db import get_conn
from .service import init_db

LANGS = {"zh": "中文", "en": "English", "ja": "日本語", "fr": "Français"}
REQUIRED_VERSIONS = {"zh": "3.2.1", "en": "3.2.1", "ja": "3.1.0", "fr": "3.0.4"}


def _svg(device_id, label, tint):
    return f"""<svg xmlns='http://www.w3.org/2000/svg' width='320' height='220' viewBox='0 0 320 220'>
<rect width='320' height='220' rx='18' fill='#f6f4ee'/>
<rect x='92' y='24' width='136' height='172' rx='16' fill='{tint}' stroke='#8a6d2f' stroke-width='2'/>
<rect x='108' y='44' width='104' height='64' rx='6' fill='#12322b'/>
<text x='160' y='82' font-family='sans-serif' font-size='20' fill='#c9a86a' text-anchor='middle'>{label}</text>
<circle cx='128' cy='136' r='9' fill='#c9a86a'/><circle cx='160' cy='136' r='9' fill='#c9a86a'/>
<circle cx='192' cy='136' r='9' fill='#c9a86a'/><circle cx='160' cy='166' r='11' fill='#c9a86a'/>
<text x='160' y='210' font-family='monospace' font-size='14' fill='#5a4a2a' text-anchor='middle'>{device_id}</text>
</svg>"""


def seed(reset=True):
    config.DATA_DIR.mkdir(parents=True, exist_ok=True)
    if reset and config.DB_PATH.exists():
        for p in (config.DB_PATH,
                  config.DB_PATH.with_suffix(".db-wal"),
                  config.DB_PATH.with_suffix(".db-shm")):
            p.unlink(missing_ok=True)

    init_db()
    conn = get_conn()
    ts = time.time()

    sites = [("S1", "青铜主馆", "主馆区"), ("S2", "东翼分馆", "东翼")]
    conn.executemany("INSERT INTO sites(id,name,branch) VALUES(?,?,?)", sites)

    visitors = [
        ("V-100001", "林晚", "138****0011"),
        ("V-100002", "Noah Carter", "139****0022"),
        ("V-100003", "佐藤绫", "137****0033"),
        ("V-100004", "赵一山", "136****0044"),
        ("V-100005", "Camille Roux", "135****0055"),
        ("V-100006", "何乐", "134****0066"),
    ]
    conn.executemany("INSERT INTO visitors(id,name,phone) VALUES(?,?,?)", visitors)

    # 设备：含正常可借、低电量、旧版本、语言包损坏、缺耳机、在借、逾期、待消毒、维修等
    devices = [
        # id, site, battery, status, lang, ver, lang_ok, ear, strap, shell, btn
        ("AG-001", "S1", 96, "available", "zh", "3.2.1", 1, 1, 1, 1, 1),
        ("AG-002", "S1", 88, "available", "en", "3.2.1", 1, 1, 1, 1, 1),
        ("AG-003", "S1", 72, "available", "zh", "3.2.1", 1, 1, 1, 1, 1),   # 低电量不可借
        ("AG-004", "S1", 95, "available", "ja", "3.0.2", 1, 1, 1, 1, 1),   # 版本过旧
        ("AG-005", "S1", 90, "available", "zh", "3.2.1", 0, 1, 1, 1, 1),   # 语言包损坏
        ("AG-006", "S1", 93, "available", "en", "3.2.1", 1, 0, 1, 1, 1),   # 缺耳机
        ("AG-007", "S1", 64, "on_loan", "fr", "3.0.4", 1, 1, 1, 1, 1),     # 在借
        ("AG-008", "S1", 41, "on_loan", "zh", "3.2.1", 1, 1, 1, 1, 1),     # 逾期未还
        ("AG-009", "S1", 58, "pending_disinfection", "en", "3.2.1", 1, 1, 1, 1, 1),
        ("AG-010", "S1", 30, "pending_charge", "zh", "3.2.1", 1, 1, 1, 1, 1),
        ("AG-011", "S1", 85, "repair", "zh", "3.2.1", 1, 1, 1, 0, 1),      # 外壳破损
        ("AG-012", "S1", 91, "available", "ja", "3.1.0", 1, 1, 1, 1, 1),
        ("AG-101", "S2", 97, "available", "zh", "3.2.1", 1, 1, 1, 1, 1),
        ("AG-102", "S2", 82, "available", "en", "3.2.1", 1, 1, 1, 1, 1),
        ("AG-103", "S2", 76, "on_loan", "zh", "3.2.1", 1, 1, 1, 1, 1),
        ("AG-104", "S2", 55, "pending_disinfection", "fr", "3.0.4", 1, 0, 1, 1, 1),
    ]
    for row in devices:
        did, site, bat, status, lc, ver, ok, ear, strap, shell, btn = row
        conn.execute("""INSERT INTO devices(id, site_id, current_site_id, battery, status,
            lang_code, lang_version, lang_ok, earphone, strap, shell_ok, button_ok)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
            (did, site, site, bat, status, lc, ver, ok, ear, strap, shell, btn))
        (config.PHOTO_DIR / f"{did}.svg").write_text(
            _svg(did, LANGS[lc], "#e9efe9" if shell else "#f3e3dd"), encoding="utf-8")

    # AG-104：归属东翼(S2)，被跨馆归还到主馆(S1)，等待消毒放行后调拨回馆
    conn.execute("UPDATE devices SET current_site_id='S1', pending_transfer=1 WHERE id='AG-104'")

    # 槽位：每馆 8 个，其中一个故障
    slots = []
    for site in ("S1", "S2"):
        for i in range(1, 9):
            sid = f"{site}-C{i:02d}"
            slots.append((sid, site, f"{site[-1]}-{i:02d}", 0 if sid == "S1-C07" else 1,
                          "接触不良，已停用" if sid == "S1-C07" else ""))
    conn.executemany("INSERT INTO charging_slots(id,site_id,label,ok,note) VALUES(?,?,?,?,?)", slots)

    # 进行中订单 + 逾期订单
    conn.execute("""INSERT INTO orders(id,device_id,visitor_id,site_id,lang_code,issued_at,due_at,
        status,deposit_amount,deposit_status,note)
        VALUES(?,?,?,?,?,?,?, 'active',200,'held','正常借用')""",
        ("O-SEED-007", "AG-007", "V-100005", "S1", "fr",
         ts - 3600, ts + 3 * 3600))
    conn.execute("""INSERT INTO orders(id,device_id,visitor_id,site_id,lang_code,issued_at,due_at,
        status,deposit_amount,deposit_status,fee_overdue,note)
        VALUES(?,?,?,?,?,?,?, 'overdue',200,'held',20,'逾期未还，等待催还')""",
        ("O-SEED-008", "AG-008", "V-100006", "S1", "zh",
         ts - 8 * 3600, ts - 4 * 3600))
    conn.execute("""INSERT INTO orders(id,device_id,visitor_id,site_id,lang_code,issued_at,due_at,
        status,deposit_amount,deposit_status,note)
        VALUES(?,?,?,?,?,?,?, 'active',200,'held','正常借用')""",
        ("O-SEED-103", "AG-103", "V-100004", "S2", "zh",
         ts - 1800, ts + 3.5 * 3600))

    # 已归还但押金待复核：缺耳机扣款争议 + 无争议逾期扣款
    conn.execute("""INSERT INTO orders(id,device_id,visitor_id,site_id,lang_code,issued_at,due_at,
        returned_at,status,deposit_amount,deposit_status,fee_earphone,dispute,cross_site,
        return_site_id,note)
        VALUES(?,?,?,?,?,?,?,?,'returned',200,'held',50,1,1,?, '跨馆归还，耳机缺失，游客主张借出时即缺')""",
        ("O-SEED-R1", "AG-104", "V-100002", "S2", "fr",
         ts - 5 * 3600, ts - 3600, ts - 1800, "S1"))
    conn.execute("""INSERT INTO orders(id,device_id,visitor_id,site_id,lang_code,issued_at,due_at,
        returned_at,status,deposit_amount,deposit_status,fee_overdue,dispute,return_site_id,note)
        VALUES(?,?,?,?,?,?,?,?,'returned',200,'held',20,0,?,'逾期归还，无争议')""",
        ("O-SEED-R2", "AG-012", "V-100003", "S1", "ja",
         ts - 9 * 3600, ts - 3600, ts - 900, "S1"))

    # 维修单
    conn.execute("""INSERT INTO repair_tickets(device_id,site_id,reported_by,issue,category,
        status,created_at) VALUES(?,?,?,?, 'shell','open',?)""",
        ("AG-011", "S1", "clerk", "归还检查：外壳开裂", ts - 7200))

    # 待消毒小批次（未放行）
    bid = conn.execute("INSERT INTO disinfect_batches(site_id,created_at,status,note) "
                       "VALUES(?,?,'sterilizing','灭菌中')", ("S1", ts - 600)).lastrowid
    conn.execute("INSERT INTO disinfect_items(batch_id,device_id) VALUES(?,?)", (bid, "AG-009"))

    conn.commit()

    # 离线语言包样例
    manifest = {"required_versions": REQUIRED_VERSIONS,
                "packs": {k: {"version": v, "label": LANGS[k],
                              "file": f"{k}.lpak", "size_kb": 1280 + i * 120}
                          for i, (k, v) in enumerate(REQUIRED_VERSIONS.items())}}
    (config.LANG_DIR / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    for code, ver in REQUIRED_VERSIONS.items():
        (config.LANG_DIR / f"{code}.lpak").write_text(
            f"OFFLINE LANG PACK {code} v{ver}\n（演示用离线语言包样例，不可用于真实导览）\n",
            encoding="utf-8")

    return {"sites": len(sites), "devices": len(devices), "visitors": len(visitors)}


if __name__ == "__main__":
    print(seed())

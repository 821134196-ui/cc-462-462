"""进程内后台任务:逾期扫描与充电完成处理。

以守护线程运行,每个任务使用独立 SQLite 连接;
*_once() 函数同时供测试直接调用。
"""
import threading
import time

from . import config
from .adapters import ChargingCabinetAdapter
from .db import all_, connect, now_str, tx
from .services import log_event, refresh_device


def overdue_scan_once(data_dir=None):
    """把超过应还时间的在借订单标记为逾期。"""
    conn = connect(data_dir)
    try:
        with tx(conn):
            rows = all_(conn,
                        "SELECT * FROM orders WHERE status='active' AND due_at < ?",
                        (now_str(),))
            for r in rows:
                conn.execute("UPDATE orders SET status='overdue' WHERE id=?", (r["id"],))
                log_event(conn, "逾期", r["device_id"], r["id"], "系统",
                          f"订单 {r['code']} 超过应还时间 {r['due_at']}")
            return len(rows)
    finally:
        conn.close()


def charging_tick_once(data_dir=None, adapter=None):
    """充电柜上报一轮:电量推进;达到入柜阈值且已消毒的设备出槽转为可借。"""
    adapter = adapter or ChargingCabinetAdapter()
    conn = connect(data_dir)
    try:
        with tx(conn):
            rows = all_(
                conn,
                "SELECT s.id AS slot_id, s.code AS slot_code, s.status AS slot_status,"
                " d.id AS dev_id, d.code AS dev_code, d.battery"
                " FROM charging_slots s JOIN devices d ON d.slot_id = s.id"
                " WHERE s.status='charging'")
            done = 0
            for r in rows:
                level = adapter.read_level(
                    {"code": r["slot_code"], "status": r["slot_status"]}, r["battery"])
                conn.execute("UPDATE devices SET battery=?, updated_at=? WHERE id=?",
                             (level, now_str(), r["dev_id"]))
                if level >= config.SHELF_MIN_BATTERY:
                    conn.execute(
                        "UPDATE charging_slots SET status='empty', device_id=NULL WHERE id=?",
                        (r["slot_id"],))
                    conn.execute("UPDATE devices SET slot_id=NULL WHERE id=?", (r["dev_id"],))
                    refresh_device(conn, r["dev_id"])
                    done += 1
            # 等待中的设备补位空槽
            waiting = all_(
                conn,
                "SELECT * FROM devices WHERE status='pending_charging' AND slot_id IS NULL"
                " AND disinfected=1 ORDER BY battery")
            for d in waiting:
                slot = all_(conn,
                            "SELECT * FROM charging_slots WHERE museum_id=? AND status='empty'"
                            " ORDER BY code LIMIT 1", (d["museum_id"],))
                if not slot:
                    break
                conn.execute("UPDATE charging_slots SET status='charging', device_id=?"
                             " WHERE id=?", (d["id"], slot[0]["id"]))
                conn.execute("UPDATE devices SET slot_id=? WHERE id=?",
                             (slot[0]["id"], d["id"]))
            return done
    finally:
        conn.close()


def start_background(data_dir=None):
    """启动两个守护线程:逾期扫描、充电推进。返回 stop_event 便于停止。"""
    stop = threading.Event()

    def loop(fn, interval):
        while not stop.is_set():
            try:
                fn(data_dir)
            except Exception as e:  # 后台任务不拖垮主进程
                print(f"[tasks] {fn.__name__} 出错: {e}")
            stop.wait(interval)

    t1 = threading.Thread(target=loop, args=(overdue_scan_once, config.OVERDUE_SCAN_SECONDS),
                          daemon=True, name="overdue-scan")
    t2 = threading.Thread(target=loop, args=(charging_tick_once, config.CHARGE_TICK_SECONDS),
                          daemon=True, name="charging-tick")
    t1.start()
    t2.start()
    return stop

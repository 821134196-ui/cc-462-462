"""SQLite 数据访问层:连接、schema、事务与查询小助手。"""
import os
import sqlite3
from contextlib import contextmanager
from datetime import datetime

from . import config

SCHEMA = """
CREATE TABLE IF NOT EXISTS museums(
  id INTEGER PRIMARY KEY,
  code TEXT UNIQUE NOT NULL,
  name TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS staff(
  id INTEGER PRIMARY KEY,
  name TEXT NOT NULL,
  role TEXT NOT NULL,            -- counter/docent/technician/reviewer/admin
  museum_id INTEGER NOT NULL REFERENCES museums(id),
  cross_zone INTEGER NOT NULL DEFAULT 0   -- 1=允许跨馆操作
);
CREATE TABLE IF NOT EXISTS devices(
  id INTEGER PRIMARY KEY,
  code TEXT UNIQUE NOT NULL,     -- 设备编号 AG-101
  museum_id INTEGER NOT NULL REFERENCES museums(id),
  status TEXT NOT NULL,          -- available/on_loan/pending_disinfection/pending_charging/repair/scrapped
  battery INTEGER NOT NULL DEFAULT 100,
  disinfected INTEGER NOT NULL DEFAULT 1,  -- 0=待消毒(需批次放行)
  packs TEXT NOT NULL DEFAULT '{}',        -- JSON: {lang: version}
  langpack_ok INTEGER NOT NULL DEFAULT 1,  -- 0=语言包损坏
  earphone INTEGER NOT NULL DEFAULT 1,
  lanyard INTEGER NOT NULL DEFAULT 1,
  photo TEXT,
  slot_id INTEGER,
  updated_at TEXT
);
CREATE TABLE IF NOT EXISTS langpack_requirements(
  lang TEXT PRIMARY KEY,
  label TEXT NOT NULL,
  required_version TEXT NOT NULL,
  file TEXT                      -- 离线语言包样例文件
);
CREATE TABLE IF NOT EXISTS orders(
  id INTEGER PRIMARY KEY,
  code TEXT UNIQUE NOT NULL,
  device_id INTEGER NOT NULL REFERENCES devices(id),
  museum_id INTEGER NOT NULL REFERENCES museums(id),   -- 借出馆
  visitor_name TEXT NOT NULL,
  visitor_phone TEXT,
  lang TEXT NOT NULL,
  status TEXT NOT NULL,          -- active/overdue/returned
  deposit_amount REAL NOT NULL,
  late_fee REAL NOT NULL DEFAULT 0,
  staff_id INTEGER,
  return_staff_id INTEGER,
  return_museum_id INTEGER,      -- 实际归还馆(跨馆归还时不同于借出馆)
  created_at TEXT,
  due_at TEXT,
  returned_at TEXT
);
-- 同一设备同一时间最多一个未完结订单(排他锁的数据库兜底)
CREATE UNIQUE INDEX IF NOT EXISTS uq_orders_active_device
  ON orders(device_id) WHERE status IN ('active','overdue');
CREATE TABLE IF NOT EXISTS deposits(
  id INTEGER PRIMARY KEY,
  order_id INTEGER UNIQUE NOT NULL REFERENCES orders(id),  -- 退款幂等锚点
  amount REAL NOT NULL,
  status TEXT NOT NULL,          -- held/refunded/partial/forfeited/disputed
  deduction REAL NOT NULL DEFAULT 0,
  dispute_reason TEXT,
  reviewed_by INTEGER,
  reviewed_at TEXT,
  refund_receipt TEXT
);
CREATE TABLE IF NOT EXISTS payments(
  id INTEGER PRIMARY KEY,
  order_id INTEGER NOT NULL REFERENCES orders(id),
  kind TEXT NOT NULL,            -- hold/refund/collect
  amount REAL NOT NULL,
  receipt TEXT UNIQUE NOT NULL,  -- 付款回执编号
  created_at TEXT
);
CREATE TABLE IF NOT EXISTS return_checks(
  id INTEGER PRIMARY KEY,
  order_id INTEGER UNIQUE NOT NULL REFERENCES orders(id),  -- 重复归还兜底
  device_id INTEGER NOT NULL,
  shell_ok INTEGER NOT NULL,
  buttons_ok INTEGER NOT NULL,
  earphone_ok INTEGER NOT NULL,
  lanyard_ok INTEGER NOT NULL,
  notes TEXT,
  result TEXT,
  checked_by INTEGER,
  checked_at TEXT
);
CREATE TABLE IF NOT EXISTS disinfection_batches(
  id INTEGER PRIMARY KEY,
  code TEXT UNIQUE NOT NULL,
  museum_id INTEGER NOT NULL REFERENCES museums(id),
  status TEXT NOT NULL,          -- open/released
  created_at TEXT,
  released_at TEXT,
  released_by INTEGER
);
CREATE TABLE IF NOT EXISTS disinfection_items(
  id INTEGER PRIMARY KEY,
  batch_id INTEGER NOT NULL REFERENCES disinfection_batches(id),
  device_id INTEGER NOT NULL,
  UNIQUE(batch_id, device_id)
);
CREATE TABLE IF NOT EXISTS charging_slots(
  id INTEGER PRIMARY KEY,
  code TEXT UNIQUE NOT NULL,     -- 槽位编号 S1-03
  museum_id INTEGER NOT NULL REFERENCES museums(id),
  status TEXT NOT NULL,          -- empty/charging/fault
  device_id INTEGER
);
CREATE TABLE IF NOT EXISTS repairs(
  id INTEGER PRIMARY KEY,
  device_id INTEGER NOT NULL REFERENCES devices(id),
  source TEXT NOT NULL,          -- return_check/fault_report
  description TEXT,
  status TEXT NOT NULL,          -- queued/in_repair/fixed/scrapped
  reported_by INTEGER,
  created_at TEXT,
  resolved_at TEXT,
  resolved_by INTEGER,
  resolution TEXT
);
CREATE TABLE IF NOT EXISTS events(
  id INTEGER PRIMARY KEY,
  ts TEXT,
  actor TEXT,
  kind TEXT,
  device_id INTEGER,
  order_id INTEGER,
  detail TEXT
);
"""


def now_str():
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def connect(data_dir=None):
    path = config.db_path() if data_dir is None else os.path.join(data_dir, "museum.db")
    conn = sqlite3.connect(path, timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def init_db(conn):
    conn.executescript(SCHEMA)
    conn.commit()


@contextmanager
def tx(conn):
    """BEGIN IMMEDIATE:进入即取写锁,保证校验-更新原子完成。"""
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise


def one(conn, sql, args=()):
    return conn.execute(sql, args).fetchone()


def all_(conn, sql, args=()):
    return conn.execute(sql, args).fetchall()

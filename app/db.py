"""SQLite 连接与 schema。所有写操作在单连接上加锁串行化，保证设备排他。"""
import sqlite3
import threading
from contextlib import contextmanager

from . import config

_lock = threading.RLock()
_conn = None


class _LockedConnection:
    """连接代理：with conn: 时持全局锁，退出提交/回滚，
    使「复查状态→写订单/设备→提交」整段临界区跨线程串行。"""

    def __init__(self, raw):
        self._raw = raw

    def __getattr__(self, name):
        return getattr(self._raw, name)

    def __enter__(self):
        _lock.acquire()
        return self._raw

    def __exit__(self, et, ev, tb):
        try:
            if et is None:
                self._raw.commit()
            else:
                self._raw.rollback()
        finally:
            _lock.release()
        return False


def get_conn():
    global _conn
    with _lock:
        if _conn is None:
            config.DATA_DIR.mkdir(parents=True, exist_ok=True)
            config.PHOTO_DIR.mkdir(parents=True, exist_ok=True)
            config.LANG_DIR.mkdir(parents=True, exist_ok=True)
            raw = sqlite3.connect(config.DB_PATH, check_same_thread=False)
            raw.row_factory = sqlite3.Row
            raw.execute("PRAGMA foreign_keys = ON")
            raw.execute("PRAGMA journal_mode = WAL")
            _conn = _LockedConnection(raw)
        return _conn


@contextmanager
def locked():
    """显式持锁事务（等价于 with get_conn() as conn）。"""
    with get_conn() as conn:
        yield conn


def tx():
    """返回事务上下文（持锁直到提交）。"""
    return locked()

#!/usr/bin/env python3
"""一条命令启动:python3 app.py [--port 8000] [--reseed]

- 首次启动自动建库并写入预置借还演示数据(两馆、在借/逾期/争议订单);
- 进程内后台任务:逾期扫描 + 充电柜推进;
- 本地目录 data/ 保存 SQLite 库、外观照片、离线语言包样例与付款回执。
"""
import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from audioguide import config, tasks, web
from audioguide.db import connect, init_db, one
from audioguide.seed import seed_all


def main():
    ap = argparse.ArgumentParser(description="博物馆语音导览器借还与充电周转系统")
    ap.add_argument("--port", type=int, default=8000)
    ap.add_argument("--reseed", action="store_true", help="清空并重建演示数据")
    args = ap.parse_args()

    config.ensure_dirs()
    if args.reseed and os.path.exists(config.db_path()):
        os.remove(config.db_path())
    conn = connect()
    init_db(conn)
    if not one(conn, "SELECT COUNT(*) AS c FROM museums")["c"]:
        seed_all(conn, config.data_dir())
        print(f"已写入演示数据 → {config.data_dir()}")
    conn.close()

    tasks.start_background()
    web.serve(args.port)


if __name__ == "__main__":
    main()

"""python -m app [--reseed] [--port N]"""
import argparse

from .server import run

if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="博物馆语音导览器借还周转系统")
    ap.add_argument("--reseed", action="store_true", help="重置并重新生成演示数据")
    ap.add_argument("--host", default=None)
    ap.add_argument("--port", type=int, default=None)
    a = ap.parse_args()
    run(host=a.host, port=a.port, reseed=a.reseed)

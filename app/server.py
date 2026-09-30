"""零依赖 HTTP 服务：静态前端 + JSON API + 进程内后台任务（逾期扫描 / 充电推进）。

身份：请求头 X-Actor-Role / X-Actor-Site 标识当前柜台人员与所属馆区，
前端顶栏切换角色，便于演示权限差异。
"""
import json
import mimetypes
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

from . import config, service
from .db import get_conn
from .seed import seed

_STOP = threading.Event()


# ---------------- 后台任务 ----------------

def _overdue_loop():
    while not _STOP.wait(config.OVERDUE_TICK):
        try:
            service.scan_overdue()
        except Exception as e:  # 演示环境：后台任务不致命
            print("[overdue]", e)


def _charge_loop():
    while not _STOP.wait(config.CHARGE_TICK):
        try:
            done = service.tick_charging()
            if done:
                print("[charge] 充满入柜:", ",".join(done))
        except Exception as e:
            print("[charge]", e)


# ---------------- HTTP ----------------

SAFE_FILE_PREFIX = (config.PHOTO_DIR, config.LANG_DIR)


def _safe_join(base, name):
    p = (base / name).resolve()
    if base.resolve() not in p.parents and p != base.resolve():
        return None
    return p


class Handler(BaseHTTPRequestHandler):
    server_version = "MuseumGuide/1.0"

    def log_message(self, fmt, *args):
        pass  # 静默；需要时可打开

    # ---- 基础响应 ----
    def _send(self, code, body, ctype="application/json; charset=utf-8"):
        if isinstance(body, (dict, list)):
            body = json.dumps(body, ensure_ascii=False).encode("utf-8")
        elif isinstance(body, str):
            body = body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _actor(self):
        return {
            "role": self.headers.get("X-Actor-Role", "clerk"),
            "site_id": self.headers.get("X-Actor-Site", "S1"),
        }

    def _body(self):
        n = int(self.headers.get("Content-Length", 0) or 0)
        if not n:
            return {}
        raw = self.rfile.read(n)
        try:
            return json.loads(raw.decode("utf-8"))
        except Exception:
            raise service.ServiceError("请求体不是合法 JSON", 400)

    def _ok(self, result=None):
        self._send(200, {"ok": True, "result": result or {}})

    def _err(self, exc):
        self._send(exc.code, {"ok": False, "error": str(exc)})

    # ---- 路由 ----
    def do_GET(self):
        try:
            u = urlparse(self.path)
            path, qs = u.path, parse_qs(u.query)
            g = lambda k, d=None: qs.get(k, [d])[0]

            if path.startswith("/api/"):
                return self._api_get(path, g)
            if path.startswith("/files/"):
                return self._serve_file(path)
            return self._serve_static(path)
        except service.ServiceError as e:
            self._err(e)
        except FileNotFoundError:
            self._send(404, {"ok": False, "error": "资源不存在"})
        except Exception as e:
            self._send(500, {"ok": False, "error": f"服务器错误: {e}"})

    def do_POST(self):
        try:
            self._api_post(urlparse(self.path).path)
        except service.ServiceError as e:
            self._err(e)
        except Exception as e:
            self._send(500, {"ok": False, "error": f"服务器错误: {e}"})

    # ---- GET API ----
    def _api_get(self, path, g):
        actor = self._actor()

        if path == "/api/sites":
            return self._ok({"sites": service.list_sites(), "roles": config.ROLE_NAMES,
                             "actor": actor, "thresholds": {
                                 "ready_battery": config.READY_BATTERY,
                                 "deposit": config.DEPOSIT_DEFAULT,
                                 "overdue_fee": config.OVERDUE_FEE,
                                 "earphone_fee": config.EARPHONE_FEE,
                                 "strap_fee": config.STRAP_FEE,
                                 "damage_fee": config.DAMAGE_FEE}})
        if path == "/api/dashboard":
            return self._ok({"dashboard": service.dashboard(g("site")),
                             "status_labels": config.DEVICE_STATUS})
        if path == "/api/devices":
            return self._ok({"devices": service.list_devices(g("site"), g("status")),
                             "lang": service.lang_manifest()})
        if path.startswith("/api/precheck/"):
            dev, problems = service.checkout_precheck(path.rsplit("/", 1)[1])
            return self._ok({"device": dev, "problems": problems,
                             "required": service.lang_manifest()["packs"].get(dev["lang_code"])})
        if path.startswith("/api/device/"):
            detail = service.device_detail(path.rsplit("/", 1)[1])
            if not detail:
                raise service.ServiceError("设备不存在", 404)
            return self._ok({"device": detail})
        if path.startswith("/api/return/"):
            order = service.begin_return(actor, path.rsplit("/", 1)[1])
            return self._ok({"order": order})
        if path == "/api/orders":
            return self._ok({"orders": service.list_orders(g("status"), g("site"))})
        if path == "/api/visitors":
            return self._ok({"visitors": service.list_visitors()})
        if path == "/api/pending":
            return self._ok({"devices": service.pending_devices(g("site", "S1"))})
        if path == "/api/batches":
            return self._ok({"batches": service.list_batches(g("site", "S1"))})
        if path == "/api/slots":
            return self._ok({"slots": service.list_slots(g("site", "S1")),
                             "pending_charge": service.list_devices(g("site"), "pending_charge")})
        if path == "/api/repairs":
            return self._ok({"repairs": service.list_repairs(g("site"), g("status"))})
        if path == "/api/deposits":
            return self._ok({"deposits": service.list_deposit_queue(g("site"))})
        if path == "/api/langpacks":
            return self._ok({"manifest": service.lang_manifest()})
        if path == "/api/audits":
            rows = get_conn().execute(
                "SELECT * FROM audits ORDER BY id DESC LIMIT 60").fetchall()
            return self._ok({"audits": [dict(r) for r in rows]})
        raise service.ServiceError("未知接口", 404)

    # ---- POST API ----
    def _api_post(self, path):
        actor, b = self._actor(), self._body()

        if path == "/api/checkout":
            return self._ok(service.checkout(actor, b["device_id"], b["visitor_id"]))
        if path == "/api/return":
            flags = b.get("flags", {})
            return self._ok(service.return_device(
                actor, b["device_id"], b.get("return_site_id", actor["site_id"]),
                flags, bool(b.get("dispute")), b.get("note", "")))
        if path == "/api/visitors":
            return self._ok(service.add_visitor(b["name"], b["phone"]))
        if path == "/api/batches":
            return self._ok(service.create_batch(actor, b["device_ids"]))
        if path.endswith("/sterilize"):
            return self._ok(service.sterilize_batch(actor, int(path.split("/")[-2])))
        if path.endswith("/release"):
            return self._ok(service.release_batch(actor, int(path.split("/")[-2])))
        if path.startswith("/api/transfer/") and path.endswith("/receive"):
            return self._ok(service.receive_transfer(actor, path.split("/")[-2]))
        if path == "/api/dock":
            return self._ok(service.dock(actor, b["device_id"], b["slot_id"]))
        if path == "/api/undock":
            return self._ok(service.undock(actor, b["device_id"]))
        if path.startswith("/api/slots/") and path.endswith("/fault"):
            return self._ok(service.set_slot_fault(
                actor, path.split("/")[-2], bool(b.get("ok")), b.get("note", "")))
        if path == "/api/repairs":
            return self._ok(service.report_fault(
                actor, b["device_id"], b["issue"], b.get("category", "other")))
        if path.startswith("/api/repairs/") and path.endswith("/resolve"):
            return self._ok(service.resolve_repair(
                actor, int(path.split("/")[-2]), b.get("resolution", "已修复"),
                b.get("replace")))
        if path.startswith("/api/deposits/"):
            return self._ok(service.settle_deposit(actor, path.rsplit("/", 1)[1], b["action"]))
        if path == "/api/remind":
            return self._ok(service.remind(actor, b["order_id"]))
        raise service.ServiceError("未知接口", 404)

    # ---- 静态资源 ----
    def _serve_static(self, path):
        rel = "index.html" if path in ("/", "") else path.lstrip("/")
        p = _safe_join(config.WEB_DIR, rel)
        if not p or not p.is_file():
            return self._send(404, "Not found", "text/plain; charset=utf-8")
        ctype = mimetypes.guess_type(str(p))[0] or "application/octet-stream"
        self._send(200, p.read_bytes(), ctype + ("; charset=utf-8" if ctype.startswith("text") or
                                                 ctype.endswith("javascript") else ""))

    def _serve_file(self, path):
        parts = path.split("/")  # /files/photos/<name> 或 /files/lang/<name>
        kind, name = parts[2], "/".join(parts[3:])
        base = config.PHOTO_DIR if kind == "photos" else config.LANG_DIR if kind == "lang" else None
        if base is None:
            raise service.ServiceError("未知文件区", 404)
        p = _safe_join(base, name)
        if not p or not p.is_file():
            raise FileNotFoundError(name)
        ctype = "image/svg+xml" if p.suffix == ".svg" else "text/plain"
        self._send(200, p.read_bytes(), ctype + "; charset=utf-8")


def run(host=None, port=None, reseed=False):
    if reseed or not config.DB_PATH.exists():
        info = seed(reset=True)
        print("已预置演示数据:", info)
    else:
        service.init_db()
    get_conn()
    threading.Thread(target=_overdue_loop, daemon=True).start()
    threading.Thread(target=_charge_loop, daemon=True).start()
    httpd = ThreadingHTTPServer((host or config.HTTP_HOST, port or config.HTTP_PORT), Handler)
    url = f"http://{httpd.server_address[0]}:{httpd.server_address[1]}"
    print(f"博物馆语音导览器周转系统已启动：{url}")
    print("提示：顶栏可切换 讲解员/消毒充电员/维修员/主管 与馆区，验证权限差异。")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        _STOP.set()
        httpd.server_close()

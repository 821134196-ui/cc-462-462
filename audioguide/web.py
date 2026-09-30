"""Web 层:基于标准库 http.server 的轻量页面服务(零第三方依赖)。

页面:借用柜台 / 设备可用表 / 归还检查 / 消毒组批 / 充电槽视图 / 维修队列 / 押金复核。
"""
import json
import os
import re
import traceback
from html import escape as esc
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, quote, urlparse

from . import config
from . import services as svc
from .db import all_, connect, one

ROUTES = []


def route(method, pattern):
    def deco(fn):
        ROUTES.append((method, re.compile("^" + pattern + "$"), fn))
        return fn
    return deco


# ---------- 请求/响应 ----------
class Req:
    def __init__(self, handler):
        self.h = handler
        u = urlparse(handler.path)
        self.path = u.path
        self.query = {k: v[0] for k, v in parse_qs(u.query).items()}
        self.cookies = SimpleCookie(handler.headers.get("Cookie", ""))
        self.db = connect()
        self._form = None

    @property
    def form(self):
        if self._form is None:
            n = int(self.h.headers.get("Content-Length") or 0)
            body = self.h.rfile.read(n).decode("utf-8") if n else ""
            self._form = {k: v[0] for k, v in parse_qs(body).items()}
        return self._form

    @property
    def staff(self):
        m = self.cookies.get("staff_id")
        row = one(self.db, "SELECT * FROM staff WHERE id=?", (m.value,)) if m else None
        if row:
            return row
        # 演示默认:柜台身份(游客 kiosk 即开即用),可在页头切换
        return one(self.db, "SELECT * FROM staff WHERE role='counter' ORDER BY id LIMIT 1")


class Resp:
    def __init__(self, body="", status=200, headers=None, ctype="text/html; charset=utf-8"):
        self.body, self.status, self.headers, self.ctype = body, status, headers or {}, ctype


def redirect(loc):
    return Resp("", 303, {"Location": loc})


def money(x):
    return f"¥{x:.0f}"


def batt_bar(pct):
    cls = "low" if pct < config.LEND_MIN_BATTERY else ("mid" if pct < config.SHELF_MIN_BATTERY else "")
    return (f'<span class="batt {cls}"><i style="width:{pct}%"></i></span>'
            f'<span class="batt-v">{pct}%</span>')


def badge(status):
    return f'<span class="badge st-{status}">{config.DEVICE_STATUS.get(status, status)}</span>'


def pack_tags(device, req_db):
    if not device["langpack_ok"]:
        return '<span class="tag bad">语言包损坏</span>'
    packs = json.loads(device["packs"] or "{}")
    reqs = {r["lang"]: r["required_version"]
            for r in all_(req_db, "SELECT * FROM langpack_requirements")}
    out = []
    for lang, ver in packs.items():
        bad = " bad" if reqs.get(lang) and reqs[lang] != ver else ""
        out.append(f'<span class="tag{bad}">{config.LANG_SHORT.get(lang, lang)} v{ver}</span>')
    return " ".join(out)


# ---------- 布局 ----------
NAV = [("/counter", "借用柜台"), ("/devices", "设备可用"), ("/return", "归还检查"),
       ("/disinfection", "消毒组批"), ("/charging", "充电槽"), ("/repair", "维修队列"),
       ("/deposits", "押金复核"), ("/events", "日志")]


def layout(req, title, body, main_cls=""):
    staff = req.staff
    museums = {m["id"]: m["name"] for m in all_(req.db, "SELECT * FROM museums")}
    opts = "".join(
        f'<option value="{s["id"]}"{" selected" if staff and s["id"] == staff["id"] else ""}>'
        f'{esc(s["name"])} · {config.ROLES[s["role"]]} · {museums[s["museum_id"]]}'
        f'{" · 跨馆" if s["cross_zone"] else ""}</option>'
        for s in all_(req.db, "SELECT * FROM staff ORDER BY id"))
    nav = "".join(
        f'<a href="{p}"{" class=on" if req.path == p else ""}>{t}</a>' for p, t in NAV)
    notice = req.query.get("notice")
    error = req.query.get("error")
    flash = (f'<div class="flash ok">{esc(notice)}</div>' if notice else "") + \
            (f'<div class="flash err">{esc(error)}</div>' if error else "")
    role = config.ROLES[staff["role"]] if staff else "-"
    return f"""<!doctype html><html lang="zh"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{esc(title)} · 语音导览周转台</title>
<link rel="stylesheet" href="/static/style.css"></head>
<body><header>
<a class="logo" href="/">导览周转台</a><nav>{nav}</nav>
<form class="who" method="get" action="/switch">
<select name="staff_id" onchange="this.form.submit()">{opts}</select>
<input type="hidden" name="back" value="{esc(req.path)}">
<span class="role">{role}</span></form></header>
<main class="{main_cls}">{flash}{body}</main>
<footer>博物馆语音导览器借还与充电周转系统 · 演示数据 · SQLite 本地库</footer>
</body></html>"""


def page(req, title, body, main_cls=""):
    return Resp(layout(req, title, body, main_cls))


def error_page(req, e):
    body = f"""<div class="card errcard"><h2>操作未完成</h2>
<p class="big-err">{esc(str(e))}</p>
<p><a class="btn" href="javascript:history.back()">返回上一步</a>
<a class="btn ghost" href="/">回到首页</a></p></div>"""
    return Resp(layout(req, "操作未完成", body), 400)


# ---------- 首页 ----------
@route("GET", "/")
def home(req):
    db = req.db
    c = lambda sql: one(db, sql)["c"]
    stats = [
        ("可借设备", c("SELECT COUNT(*) c FROM devices WHERE status='available'"), "available", "/devices?status=available"),
        ("在借", c("SELECT COUNT(*) c FROM devices WHERE status='on_loan'"), "on_loan", "/devices?status=on_loan"),
        ("待消毒", c("SELECT COUNT(*) c FROM devices WHERE status='pending_disinfection'"), "pending_disinfection", "/disinfection"),
        ("待充电", c("SELECT COUNT(*) c FROM devices WHERE status='pending_charging'"), "pending_charging", "/charging"),
        ("维修中", c("SELECT COUNT(*) c FROM devices WHERE status='repair'"), "repair", "/repair"),
        ("逾期订单", c("SELECT COUNT(*) c FROM orders WHERE status='overdue'"), "overdue", "/return"),
        ("押金待复核", c("SELECT COUNT(*) c FROM deposits WHERE status='disputed'"), "disputed", "/deposits"),
    ]
    cards = "".join(f'<a class="stat st-{k}" href="{link}"><b>{n}</b><span>{t}</span></a>'
                    for t, n, k, link in stats)
    events = "".join(
        f'<tr><td class="mono">{e["ts"]}</td><td>{esc(e["actor"] or "")}</td>'
        f'<td>{esc(e["kind"])}</td><td>{esc(e["detail"] or "")}</td></tr>'
        for e in all_(db, "SELECT * FROM events ORDER BY id DESC LIMIT 10"))
    body = f"""<h1>周转总览</h1><div class="stats">{cards}</div>
<div class="card"><h2>最近动态</h2>
<table><tr><th>时间</th><th>操作人</th><th>事件</th><th>详情</th></tr>{events}</table></div>"""
    return page(req, "总览", body)


# ---------- 借用柜台(游客向:大字号、少步骤) ----------
@route("GET", "/counter")
def counter(req):
    lang = req.query.get("lang")
    staff = req.staff
    if not lang:
        btns = "".join(
            f'<a class="lang-btn" href="/counter?lang={k}"><b>{v}</b><span>{k.upper()}</span></a>'
            for k, v in config.LANGS.items())
        body = f"""<div class="kiosk"><h1>语音导览租借</h1>
<p class="step">第 1 步 / 共 2 步 · 请选择讲解语言</p>
<div class="langs">{btns}</div></div>"""
        return page(req, "借用柜台", body, "counter")
    museum = one(req.db, "SELECT * FROM museums WHERE id=?", (staff["museum_id"],))
    dev = svc.find_candidate(req.db, staff["museum_id"], lang)
    if not dev:
        body = f"""<div class="kiosk"><h1>语音导览租借</h1>
<p class="big-err">抱歉,{esc(museum["name"])}暂时没有可借出的{config.LANGS[lang]}设备。</p>
<p class="step">设备可能正在消毒或充电,请稍后再试或咨询工作人员。</p>
<p><a class="btn big" href="/counter">重新选择语言</a></p></div>"""
        return page(req, "借用柜台", body, "counter")
    body = f"""<div class="kiosk"><h1>语音导览租借</h1>
<p class="step">第 2 步 / 共 2 步 · 确认设备并登记</p>
<div class="card kiosk-dev">
<img class="dev-photo" src="/photos/{esc(dev['photo'])}" alt="设备照片">
<div><p class="dev-code">{esc(dev['code'])}</p>
<p>电量 {batt_bar(dev['battery'])}</p>
<p>语言包 {pack_tags(dev, req.db)}</p>
<p class="depo">押金 {money(config.DEPOSIT_AMOUNT)} · 归还即退 · 租期 {config.LOAN_HOURS} 小时</p></div></div>
<form method="post" action="/counter" class="kiosk-form">
<input type="hidden" name="lang" value="{esc(lang)}">
<label>姓名 <input name="name" required placeholder="参观者姓名"></label>
<label>手机号 <input name="phone" placeholder="选填"></label>
<button class="btn big">确认借出</button></form>
<p><a href="/counter">‹ 重新选择语言</a></p></div>"""
    return page(req, "借用柜台", body, "counter")


@route("POST", "/counter")
def counter_post(req):
    f = req.form
    staff = req.staff
    order = svc.create_order(req.db, staff, staff["museum_id"],
                             f.get("name", "参观者"), f.get("phone", ""), f["lang"])
    dev = one(req.db, "SELECT * FROM devices WHERE id=?", (order["device_id"],))
    dep = one(req.db, "SELECT * FROM deposits WHERE order_id=?", (order["id"],))
    pay = one(req.db, "SELECT * FROM payments WHERE order_id=? AND kind='hold'", (order["id"],))
    body = f"""<div class="kiosk"><h1>借出成功</h1>
<div class="card okcard">
<p class="dev-code">{esc(dev['code'])}</p>
<p class="bigline">订单号 <b class="mono">{esc(order['code'])}</b></p>
<p class="bigline">请于 <b>{esc(order['due_at'][5:16])}</b> 前归还至任意柜台</p>
<p>押金 {money(dep['amount'])} 已冻结 · 回执 <span class="mono">{esc(pay['receipt'])}</span></p>
</div><p><a class="btn big" href="/counter">继续下一笔</a></p></div>"""
    return page(req, "借出成功", body, "counter")


# ---------- 设备可用表 ----------
@route("GET", "/devices")
def devices(req):
    db = req.db
    where, args = [], []
    if req.query.get("museum"):
        where.append("d.museum_id=?"); args.append(req.query["museum"])
    if req.query.get("status"):
        where.append("d.status=?"); args.append(req.query["status"])
    sql = ("SELECT d.*, m.name AS mname, s.code AS slot_code FROM devices d"
           " JOIN museums m ON m.id=d.museum_id"
           " LEFT JOIN charging_slots s ON s.id=d.slot_id")
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY d.museum_id, d.code"
    rows = all_(db, sql, args)
    museums = all_(db, "SELECT * FROM museums")
    mopts = '<option value="">全部馆区</option>' + "".join(
        f'<option value="{m["id"]}"{" selected" if req.query.get("museum")==str(m["id"]) else ""}>{m["name"]}</option>'
        for m in museums)
    sopts = '<option value="">全部状态</option>' + "".join(
        f'<option value="{k}"{" selected" if req.query.get("status")==k else ""}>{v}</option>'
        for k, v in config.DEVICE_STATUS.items())
    trs = []
    for d in rows:
        acc = (f'{"耳机✓" if d["earphone"] else "<b class=miss>耳机✗</b>"} · '
               f'{"挂绳✓" if d["lanyard"] else "<b class=miss>挂绳✗</b>"}')
        ops = [f'<a href="/repair?device={d["code"]}">报故障</a>']
        if not d["earphone"] or not d["lanyard"]:
            ops.append(f'''<form method="post" action="/devices/restock" class="inline">
<input type="hidden" name="id" value="{d["id"]}"><button>补齐配件</button></form>''')
        trs.append(
            f'<tr><td class="mono"><b>{d["code"]}</b></td><td>{d["mname"]}</td>'
            f'<td>{badge(d["status"])}</td><td>{batt_bar(d["battery"])}</td>'
            f'<td>{pack_tags(d, db)}</td><td>{acc}</td>'
            f'<td>{"已消毒" if d["disinfected"] else "<b class=miss>待消毒</b>"}</td>'
            f'<td class="mono">{d["slot_code"] or "—"}</td><td class="ops">{" ".join(ops)}</td></tr>')
    body = f"""<h1>设备可用表</h1>
<form class="filters" method="get"><select name="museum">{mopts}</select>
<select name="status">{sopts}</select><button class="btn">筛选</button></form>
<div class="card"><table><tr><th>编号</th><th>馆区</th><th>状态</th><th>电量</th>
<th>语言包</th><th>配件</th><th>消毒</th><th>槽位</th><th>操作</th></tr>
{''.join(trs)}</table></div>"""
    return page(req, "设备可用表", body)


@route("POST", "/devices/restock")
def restock(req):
    svc.restock_accessory(req.db, req.staff, int(req.form["id"]))
    return redirect("/devices?notice=" + quote("配件已补齐"))


# ---------- 归还检查 ----------
@route("GET", "/return")
def return_home(req):
    rows = all_(req.db,
                "SELECT o.*, d.code AS dcode, m.name AS mname FROM orders o"
                " JOIN devices d ON d.id=o.device_id JOIN museums m ON m.id=o.museum_id"
                " WHERE o.status IN ('active','overdue') ORDER BY o.due_at")
    trs = "".join(
        f'<tr><td class="mono"><a href="/return/{o["code"]}">{o["code"]}</a></td>'
        f'<td class="mono">{o["dcode"]}</td><td>{o["mname"]}</td>'
        f'<td>{esc(o["visitor_name"])}</td><td>{config.LANGS[o["lang"]]}</td>'
        f'<td class="mono">{o["due_at"][5:16]}</td>'
        f'<td><span class="badge st-{o["status"]}">{config.ORDER_STATUS[o["status"]]}</span></td></tr>'
        for o in rows)
    body = f"""<h1>归还检查</h1>
<div class="card"><form class="filters" method="get" action="/return/lookup">
<input name="code" placeholder="输入订单号或设备号,如 OD…-0001 / AG-101" required>
<button class="btn">查找订单</button></form></div>
<div class="card"><h2>在借订单</h2>
<table><tr><th>订单号</th><th>设备</th><th>借出馆</th><th>参观者</th><th>语言</th>
<th>应还时间</th><th>状态</th></tr>{trs or '<tr><td colspan=7>暂无在借订单</td></tr>'}</table></div>"""
    return page(req, "归还检查", body)


@route("GET", "/return/lookup")
def return_lookup(req):
    o = svc.find_open_order(req.db, req.query.get("code", ""))
    return redirect(f"/return/{o['code']}")


@route("GET", r"/return/([A-Za-z0-9-]+)")
def return_check(req, code):
    db = req.db
    o = one(db, "SELECT * FROM orders WHERE code=?", (code,))
    if not o:
        raise svc.NotFound("订单不存在")
    d = one(db, "SELECT * FROM devices WHERE id=?", (o["device_id"],))
    m = one(db, "SELECT * FROM museums WHERE id=?", (o["museum_id"],))
    if o["status"] == "returned":
        s = svc._return_summary(db, o, already=True)
        return page(req, "归还结果", _return_result_html(req, s))
    cross = req.staff["museum_id"] != o["museum_id"] and not (
        req.staff["cross_zone"] or req.staff["role"] == "admin")
    warn = (f'<div class="flash err">本单属于「{m["name"]}」,当前身份无跨馆权限,'
            f'提交将被拒绝。请切换具备跨馆授权的员工(如周管理员)。</div>') if cross else ""
    body = f"""<h1>归还检查 · <span class="mono">{esc(o['code'])}</span></h1>{warn}
<div class="return-grid">
<div class="card photo-card"><img class="dev-photo-lg" src="/photos/{esc(d['photo'])}" alt="设备外观">
<p class="mono center">{esc(d['code'])} · {esc(m['name'])}借出</p>
<p class="center">参观者:{esc(o['visitor_name'])} · 应还 {esc(o['due_at'][5:16])}</p></div>
<form class="card check-card" method="post" action="/return/{esc(o['code'])}">
<h2>外观与配件检查</h2>
<label class="chk"><input type="checkbox" name="earphone" value="1" checked> 耳机已归还</label>
<label class="chk"><input type="checkbox" name="lanyard" value="1" checked> 挂绳已归还</label>
<div class="radio-row"><span>外壳:</span>
<label><input type="radio" name="shell" value="ok" checked> 完好</label>
<label><input type="radio" name="shell" value="bad"> 破损(赔 {money(config.FEE_SHELL)})</label></div>
<div class="radio-row"><span>按键:</span>
<label><input type="radio" name="buttons" value="ok" checked> 正常</label>
<label><input type="radio" name="buttons" value="bad"> 失灵(赔 {money(config.FEE_BUTTONS)})</label></div>
<p class="hint">耳机缺失赔 {money(config.FEE_EARPHONE)} · 挂绳缺失赔 {money(config.FEE_LANYARD)}
· 逾期每小时 {money(config.LATE_FEE_PER_HOUR)}(封顶 {money(config.LATE_FEE_CAP)})</p>
<label>备注 <input name="notes" placeholder="选填"></label>
<div class="radio-row"><span>扣款方式:</span>
<label><input type="radio" name="mode" value="accept" checked> 游客认可,现场结算</label>
<label><input type="radio" name="mode" value="dispute"> 游客有异议,转押金复核</label></div>
<button class="btn big">完成检查并结算押金</button></form></div>"""
    return page(req, "归还检查", body)


@route("POST", r"/return/([A-Za-z0-9-]+)")
def return_submit(req, code):
    f = req.form
    s = svc.complete_return(
        req.db, req.staff, code,
        shell_ok=f.get("shell") == "ok", buttons_ok=f.get("buttons") == "ok",
        earphone_back=f.get("earphone") == "1", lanyard_back=f.get("lanyard") == "1",
        deduction_mode=f.get("mode", "accept"), notes=f.get("notes", ""))
    return page(req, "归还结果", _return_result_html(req, s))


def _return_result_html(req, s):
    o, dep, d = s["order"], s["deposit"], s["device"]
    pays = "".join(f'<li>{ {"hold":"押金冻结","refund":"退款","collect":"扣款"}[p["kind"]] } '
                   f'{money(p["amount"])} · 回执 <span class="mono">{p["receipt"]}</span></li>'
                   for p in s["payments"])
    again = '<p class="flash ok">该订单此前已完成归还,以下为首次处理结果(未重复退款)。</p>' \
            if s["already"] else ""
    body = f"""<h1>归还结果</h1>{again}
<div class="card okcard">
<p>订单 <span class="mono">{esc(o['code'])}</span> · 设备 <b class="mono">{esc(d['code'])}</b>
→ {badge(d['status'])}</p>
<p>检查结论:{esc(s['check']['result'] if s['check'] else '—')}
{(' · 备注:' + esc(s['check']['notes'])) if s['check'] and s['check']['notes'] else ''}</p>
<p>押金 {money(dep['amount'])} · 状态 <b>{config.DEPOSIT_STATUS[dep['status']]}</b>
· 扣款 {money(dep['deduction'])}</p>
<ul>{pays}</ul>
<p><a class="btn" href="/return">继续归还</a>
<a class="btn ghost" href="/deposits">押金复核</a></p></div>"""
    return body


# ---------- 消毒组批 ----------
@route("GET", "/disinfection")
def disinfection(req):
    db = req.db
    museums = {m["id"]: m["name"] for m in all_(db, "SELECT * FROM museums")}
    open_batches = all_(db, "SELECT * FROM disinfection_batches WHERE status='open' ORDER BY id")
    cards = []
    for b in open_batches:
        items = all_(db,
                     "SELECT d.* FROM disinfection_items i JOIN devices d ON d.id=i.device_id"
                     " WHERE i.batch_id=? ORDER BY d.code", (b["id"],))
        devs = " ".join(f'<span class="tag">{d["code"]}</span>' for d in items) or "暂无设备"
        cards.append(f"""<div class="card"><h2>批次 <span class="mono">{b['code']}</span>
<small>{museums[b['museum_id']]} · 组批中</small></h2>
<p>{devs}</p>
<form method="post" action="/disinfection/release">
<input type="hidden" name="id" value="{b['id']}">
<button class="btn">消毒完成,放行 {len(items)} 台</button></form></div>""")
    hist = all_(db, "SELECT * FROM disinfection_batches WHERE status='released' ORDER BY id DESC LIMIT 8")
    htrs = "".join(
        f'<tr><td class="mono">{b["code"]}</td><td>{museums[b["museum_id"]]}</td>'
        f'<td class="mono">{b["released_at"]}</td></tr>' for b in hist)
    body = f"""<h1>消毒组批</h1>
<p class="hint">归还设备自动进入本馆开放批次;批次放行后设备才允许充电入柜、重新可借。</p>
{''.join(cards) or '<div class="card">当前没有组批中的批次。</div>'}
<div class="card"><h2>放行历史</h2>
<table><tr><th>批次号</th><th>馆区</th><th>放行时间</th></tr>{htrs}</table></div>"""
    return page(req, "消毒组批", body)


@route("POST", "/disinfection/release")
def disinfection_release(req):
    n = svc.release_batch(req.db, req.staff, int(req.form["id"]))
    return redirect("/disinfection?notice=" + quote(f"批次已放行,{n} 台设备进入下一环节"))


# ---------- 充电槽视图 ----------
@route("GET", "/charging")
def charging(req):
    db = req.db
    museums = all_(db, "SELECT * FROM museums")
    sections = []
    for m in museums:
        slots = all_(db,
                     "SELECT s.*, d.code AS dcode, d.battery FROM charging_slots s"
                     " LEFT JOIN devices d ON d.id=s.device_id"
                     " WHERE s.museum_id=? ORDER BY s.code", (m["id"],))
        cells = []
        for s in slots:
            if s["status"] == "charging":
                inner = (f'<div class="slot-dev mono">{s["dcode"]}</div>'
                         f'{batt_bar(s["battery"])}')
                btn = '<button name="op" value="fault" class="mini warn">模拟故障</button>'
            elif s["status"] == "fault":
                inner = (f'<div class="slot-dev">{"— " + s["dcode"] if s["dcode"] else "空"}</div>'
                         f'<div class="fault">故障</div>')
                btn = '<button name="op" value="fix" class="mini">修复</button>'
            else:
                inner = '<div class="slot-dev empty">空槽</div>'
                btn = '<button name="op" value="fault" class="mini warn">模拟故障</button>'
            cells.append(f"""<div class="slot st-{s['status']}">
<div class="slot-no mono">{s['code']}</div>{inner}
<form method="post" action="/charging/slot">
<input type="hidden" name="id" value="{s['id']}">{btn}</form></div>""")
        waiting = all_(db,
                       "SELECT * FROM devices WHERE museum_id=? AND status='pending_charging'"
                       " AND slot_id IS NULL", (m["id"],))
        wait = (f'<p class="hint">等待空槽:' +
                " ".join(f'<span class="tag">{w["code"]}</span>' for w in waiting) + "</p>"
                if waiting else "")
        sections.append(f'<div class="card"><h2>{m["name"]} · 充电柜</h2>'
                        f'<div class="slots">{"".join(cells)}</div>{wait}</div>')
    body = f"""<h1>充电槽视图</h1>
<p class="hint">模拟充电柜每 {config.CHARGE_TICK_SECONDS} 秒上报一次,电量 ≥{config.SHELF_MIN_BATTERY}%
且消毒放行后自动出槽转为可借;故障槽位暂停充电。</p>{''.join(sections)}"""
    return page(req, "充电槽视图", body)


@route("POST", "/charging/slot")
def charging_slot(req):
    svc.set_slot_fault(req.db, req.staff, int(req.form["id"]),
                       faulty=req.form.get("op") == "fault")
    msg = "槽位已标记故障,暂停充电" if req.form.get("op") == "fault" else "槽位已修复"
    return redirect("/charging?notice=" + quote(msg))


# ---------- 维修队列 ----------
@route("GET", "/repair")
def repair(req):
    db = req.db
    rows = all_(db,
                "SELECT r.*, d.code AS dcode, s.name AS rname FROM repairs r"
                " JOIN devices d ON d.id=r.device_id LEFT JOIN staff s ON s.id=r.reported_by"
                " ORDER BY CASE r.status WHEN 'queued' THEN 0 WHEN 'in_repair' THEN 1 ELSE 2 END, r.id DESC")
    st_name = {"queued": "待处理", "in_repair": "维修中", "fixed": "已修复", "scrapped": "已报废"}
    trs = []
    for r in rows:
        ops = ""
        if r["status"] == "queued":
            ops = (f'<form class="inline" method="post" action="/repair/action">'
                   f'<input type="hidden" name="id" value="{r["id"]}">'
                   f'<button name="op" value="start">开始维修</button>'
                   f'<button name="op" value="fixed">修复完成</button>'
                   f'<button name="op" value="scrap" class="warn">报废</button></form>')
        elif r["status"] == "in_repair":
            ops = (f'<form class="inline" method="post" action="/repair/action">'
                   f'<input type="hidden" name="id" value="{r["id"]}">'
                   f'<button name="op" value="fixed">修复完成</button>'
                   f'<button name="op" value="scrap" class="warn">报废</button></form>')
        src = "归还检查" if r["source"] == "return_check" else "故障上报"
        trs.append(f'<tr><td>#{r["id"]}</td><td class="mono">{r["dcode"]}</td><td>{src}</td>'
                   f'<td>{esc(r["description"] or "")}</td><td>{esc(r["rname"] or "")}</td>'
                   f'<td><span class="badge st-rp-{r["status"]}">{st_name[r["status"]]}</span></td>'
                   f'<td class="ops">{ops}</td></tr>')
    pre = req.query.get("device", "")
    body = f"""<h1>维修队列</h1>
<div class="card"><h2>故障上报</h2>
<p class="hint">讲解员可报故障;赔付审批请走「押金复核」(需复核员/管理员)。</p>
<form class="filters" method="post" action="/repair/report">
<input name="device" value="{esc(pre)}" placeholder="设备编号,如 AG-108" required>
<input name="desc" placeholder="故障描述,如 语言包损坏无法播放" required size="40">
<button class="btn">提交报障</button></form></div>
<div class="card"><table><tr><th>单号</th><th>设备</th><th>来源</th><th>描述</th>
<th>上报人</th><th>状态</th><th>操作</th></tr>{''.join(trs)}</table></div>"""
    return page(req, "维修队列", body)


@route("POST", "/repair/report")
def repair_report(req):
    svc.report_fault(req.db, req.staff, req.form["device"].strip(), req.form["desc"].strip())
    return redirect("/repair?notice=" + quote("故障已上报,进入维修队列"))


@route("POST", "/repair/action")
def repair_action(req):
    svc.resolve_repair(req.db, req.staff, int(req.form["id"]), req.form["op"])
    return redirect("/repair?notice=" + quote("维修单已更新"))


# ---------- 押金复核 ----------
@route("GET", "/deposits")
def deposits(req):
    db = req.db
    can_review = req.staff["role"] in ("reviewer", "admin")
    rows = all_(db,
                "SELECT dep.*, o.code AS ocode, o.visitor_name, d.code AS dcode FROM deposits dep"
                " JOIN orders o ON o.id=dep.order_id JOIN devices d ON d.id=o.device_id"
                " WHERE dep.status='disputed' ORDER BY dep.id")
    cards = []
    for dep in rows:
        actions = ""
        if can_review:
            actions = f"""<form class="inline" method="post" action="/deposits/review">
<input type="hidden" name="id" value="{dep['id']}">
<button name="decision" value="refund_full">全额退还 {money(dep['amount'])}</button>
<input type="number" name="deduction" min="0" max="{dep['amount']:.0f}" value="{dep['deduction']:.0f}" step="1">
<button name="decision" value="deduct" class="warn">按此金额扣款</button></form>"""
        cards.append(f"""<div class="card dispute"><h2>订单 <span class="mono">{dep['ocode']}</span>
<small>{esc(dep['visitor_name'])} · 设备 {dep['dcode']}</small></h2>
<p>押金 {money(dep['amount'])} · 争议事项:{esc(dep['dispute_reason'] or '')}</p>{actions}</div>""")
    if not cards:
        cards.append('<div class="card">当前没有待复核的押金争议。</div>')
    note = "" if can_review else \
        '<div class="flash err">当前身份无复核权限(讲解员可报故障,但不能批准赔付)。</div>'
    hist = all_(db,
                "SELECT dep.*, o.code AS ocode FROM deposits dep JOIN orders o ON o.id=dep.order_id"
                " WHERE dep.status!='disputed' ORDER BY dep.id DESC LIMIT 12")
    htrs = "".join(
        f'<tr><td class="mono">{h["ocode"]}</td><td>{money(h["amount"])}</td>'
        f'<td>{config.DEPOSIT_STATUS[h["status"]]}</td><td>{money(h["deduction"])}</td>'
        f'<td class="mono">{h["refund_receipt"] or "—"}</td></tr>' for h in hist)
    body = f"""<h1>押金复核</h1>{note}{''.join(cards)}
<div class="card"><h2>押金台账</h2>
<table><tr><th>订单</th><th>押金</th><th>状态</th><th>扣款</th><th>退款回执</th></tr>{htrs}</table></div>"""
    return page(req, "押金复核", body)


@route("POST", "/deposits/review")
def deposits_review(req):
    svc.review_deposit(req.db, req.staff, int(req.form["id"]), req.form["decision"],
                       float(req.form.get("deduction") or 0))
    return redirect("/deposits?notice=" + quote("复核完成,退款回执已生成"))


# ---------- 日志 ----------
@route("GET", "/events")
def events(req):
    rows = all_(req.db, "SELECT * FROM events ORDER BY id DESC LIMIT 100")
    trs = "".join(
        f'<tr><td class="mono">{e["ts"]}</td><td>{esc(e["actor"] or "")}</td>'
        f'<td>{esc(e["kind"])}</td><td>{esc(e["detail"] or "")}</td></tr>' for e in rows)
    return page(req, "事件日志",
                f'<h1>事件日志</h1><div class="card"><table><tr><th>时间</th><th>操作人</th>'
                f'<th>事件</th><th>详情</th></tr>{trs}</table></div>')


# ---------- 身份切换 ----------
@route("GET", "/switch")
def switch(req):
    sid = req.query.get("staff_id", "")
    back = req.query.get("back", "/")
    resp = redirect(back)
    resp.headers["Set-Cookie"] = f"staff_id={sid}; Path=/"
    return resp


# ---------- 静态资源 ----------
STATIC_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static")


def serve_file(path, ctype):
    if not os.path.isfile(path):
        return None
    with open(path, "rb") as f:
        data = f.read()
    return Resp(data, 200, ctype=ctype)


def dispatch(req):
    p = req.path
    if p == "/static/style.css":
        return serve_file(os.path.join(STATIC_DIR, "style.css"), "text/css; charset=utf-8")
    m = re.match(r"^/(photos|packs)/([\w.-]+)$", p)
    if m:
        base = config.photo_dir() if m.group(1) == "photos" else config.langpack_dir()
        ctype = "image/svg+xml" if m.group(1) == "photos" else "application/octet-stream"
        r = serve_file(os.path.join(base, m.group(2)), ctype)
        if r:
            return r
        return Resp("not found", 404)
    for method, rx, fn in ROUTES:
        if method != req.h.command:
            continue
        m = rx.match(p)
        if m:
            return fn(req, *m.groups())
    return Resp(layout(req, "404", '<div class="card"><h2>页面不存在</h2>'
                                   '<p><a class="btn" href="/">回到首页</a></p></div>'), 404)


def make_handler():
    class Handler(BaseHTTPRequestHandler):
        def _go(self):
            req = Req(self)
            try:
                resp = dispatch(req)
            except svc.DomainError as e:
                resp = error_page(req, e)
            except Exception:
                traceback.print_exc()
                resp = Resp("<h1>500 内部错误</h1><pre>" + esc(traceback.format_exc()) + "</pre>",
                            500)
            finally:
                req.db.close()
            body = resp.body if isinstance(resp.body, bytes) else resp.body.encode("utf-8")
            self.send_response(resp.status)
            self.send_header("Content-Type", resp.ctype)
            self.send_header("Content-Length", str(len(body)))
            for k, v in resp.headers.items():
                self.send_header(k, v)
            self.end_headers()
            self.wfile.write(body)

        do_GET = do_POST = _go

        def log_message(self, *args):
            pass

    return Handler


def serve(port=8000):
    server = ThreadingHTTPServer(("0.0.0.0", port), make_handler())
    print(f"导览周转台已启动: http://127.0.0.1:{port}  (Ctrl+C 停止)")
    server.serve_forever()

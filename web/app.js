/* 博物馆导览器周转台 —— 单页前端（无框架、无构建） */
"use strict";

const STATE = { actor: { role: "clerk", site_id: "S1" }, sites: [], roles: {}, cfg: {}, lang: { packs: {} } };
const $ = (s, el = document) => el.querySelector(s);
const $$ = (s, el = document) => [...el.querySelectorAll(s)];
const h = (tag, attrs = {}, ...kids) => {
  const e = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs || {})) {
    if (k === "class") e.className = v;
    else if (k === "html") e.innerHTML = v;
    else if (k.startsWith("on") && typeof v === "function") e.addEventListener(k.slice(2), v);
    else if (v !== null && v !== undefined && v !== false) e.setAttribute(k, v === true ? "" : v);
  }
  for (const kid of kids.flat()) {
    if (kid == null || kid === false) continue;
    e.append(kid.nodeType ? kid : document.createTextNode(String(kid)));
  }
  return e;
};

async function api(path, opts = {}) {
  const res = await fetch(path, {
    method: opts.method || "GET",
    headers: {
      "Content-Type": "application/json",
      "X-Actor-Role": STATE.actor.role,
      "X-Actor-Site": STATE.actor.site_id,
    },
    body: opts.body ? JSON.stringify(opts.body) : undefined,
  });
  const data = await res.json().catch(() => ({ ok: false, error: "响应解析失败" }));
  if (!data.ok) { const e = new Error(data.error || "请求失败"); e.code = res.status; throw e; }
  return data.result;
}

let toastTimer;
function toast(msg, isErr = false) {
  const t = $("#toast");
  t.textContent = msg; t.className = "toast show" + (isErr ? " err" : "");
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => (t.className = "toast"), 3600);
}
async function guard(fn) { try { return await fn(); } catch (e) { toast(e.message, true); } }

const STATUS_LABEL = {
  available: "在柜可借", on_loan: "在借", return_check: "归还检查",
  pending_disinfection: "待消毒", pending_charge: "待充电", charging: "充电中",
  repair: "维修中", in_transit: "跨馆调拨",
};
const LANG_SHORT = { zh: "中", en: "EN", ja: "日", fr: "FR" };
const tag = (s, label) => h("span", { class: `tag ${s}` }, label || STATUS_LABEL[s] || s);
const esc = s => String(s ?? "").replace(/[&<>"]/g, c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));

function battery(dev, cls = "") {
  const low = dev.battery < STATE.cfg.ready_battery;
  return h("span", { class: `bat ${cls}${low ? " low" : ""}` },
    h("span", { class: "bar" }, h("i", { style: `width:${dev.battery}%` })),
    h("span", { class: "num" }, `${dev.battery}%`));
}
function verTag(dev) {
  const req = STATE.lang.packs[dev.lang_code];
  const old = dev.lang_ok && req && cmpVer(dev.lang_version, req.version) < 0;
  const broken = !dev.lang_ok;
  return h("span", {
    class: `tag ver ${old || broken ? "old" : ""}`,
    title: broken ? "语言包损坏" : old ? `要求 ≥ ${req.version}` : "版本达标",
  }, `${LANG_SHORT[dev.lang_code] || dev.lang_code} ${dev.lang_version}${broken ? " 损坏" : old ? " 旧" : ""}`);
}
function cmpVer(a, b) {
  const pa = a.split(".").map(Number), pb = b.split(".").map(Number);
  for (let i = 0; i < Math.max(pa.length, pb.length); i++) {
    if ((pa[i] || 0) !== (pb[i] || 0)) return (pa[i] || 0) < (pb[i] || 0) ? -1 : 1;
  }
  return 0;
}
const isStaff = () => true;
const can = (roles) => roles.includes(STATE.actor.role);
const siteName = id => (STATE.sites.find(s => s.id === id) || {}).name || id;
const money = n => `¥${n}`;
const fmtTime = ts => ts ? new Date(ts * 1000).toLocaleString("zh-CN", { hour12: false }) : "—";

/* ---------------- 顶栏 / KPI ---------------- */

async function initShell() {
  const r = await api("/api/sites");
  STATE.sites = r.sites; STATE.roles = r.roles; STATE.cfg = r.thresholds;
  $("#thrNote").textContent = r.thresholds.ready_battery + "%";
  const saved = JSON.parse(localStorage.getItem("actor") || "null");
  if (saved) STATE.actor = saved;

  const roleSel = $("#actorRole"), siteSel = $("#actorSite");
  for (const [k, v] of Object.entries(r.roles)) roleSel.append(h("option", { value: k }, v));
  for (const s of r.sites) siteSel.append(h("option", { value: s.id }, s.name));
  roleSel.value = STATE.actor.role; siteSel.value = STATE.actor.site_id;
  const persist = () => {
    localStorage.setItem("actor", JSON.stringify(STATE.actor));
    location.reload();
  };
  roleSel.onchange = () => { STATE.actor.role = roleSel.value; persist(); };
  siteSel.onchange = () => { STATE.actor.site_id = siteSel.value; persist(); };

  const lang = await api("/api/langpacks");
  STATE.lang = lang.manifest;
  await refreshKpi();
  setInterval(refreshKpi, 4000);
}

async function refreshKpi() {
  const d = (await api(`/api/dashboard?site=${STATE.actor.site_id}`)).dashboard;
  const box = $("#kpis");
  const items = [
    ["可借入柜", d.devices.available || 0, ""],
    ["在借", (d.devices.on_loan || 0) + (d.devices.return_check || 0), ""],
    ["待消毒/充电", (d.devices.pending_disinfection || 0) + (d.devices.pending_charge || 0) + (d.devices.charging || 0), "gold"],
    ["维修/调拨", (d.devices.repair || 0) + (d.devices.in_transit || 0), "red"],
    ["逾期未还", d.overdue, "red"],
    ["冻结押金", money(d.held_deposit), "gold"],
    ["故障槽位", d.faulty_slots, d.faulty_slots ? "red" : ""],
  ];
  box.replaceChildren(...items.map(([l, n, c]) =>
    h("div", { class: `kpi ${c}` }, h("div", { class: "n" }, String(n)), h("div", { class: "l" }, l))));
}

/* ---------------- 路由 ---------------- */

const routes = {};
function go(route) { location.hash = `#/${route}`; }
async function renderRoute() {
  const name = (location.hash.replace(/^#\//, "") || "counter").split("?")[0];
  $$("#tabs a").forEach(a => a.classList.toggle("active", a.dataset.route === name));
  const view = $("#view");
  view.replaceChildren();
  await guard(() => (routes[name] || routes.counter)(view));
}
window.addEventListener("hashchange", renderRoute);

/* ---------------- 视图 1：借用柜台（大字号 · 少步骤） ---------------- */

routes.counter = async function (root) {
  let pre = null;
  const panel = h("div", { class: "panel counter" });
  root.append(panel);

  const visitors = (await api("/api/visitors")).visitors;

  function paint() {
    const dots = [1, 2, 3].map(i => h("i", { class: pre ? (i === 1 ? "on" : "") : i === 1 ? "on" : "" }));
    panel.replaceChildren(
      h("div", { class: "step-dots" }, ...dots),
      h("div", { class: "big-step" }, "第 1 步 · 扫描或输入设备编号"),
      h("div", { class: "hint mt" }, "系统将自动校验语言包版本、电量与配件，确认后设备立即锁定为「在借」。"),
      h("div", { class: "row mt" },
        h("input", {
          class: "big-input", id: "devInput", placeholder: "如 AG-001",
          onkeydown: e => e.key === "Enter" && doPrecheck(),
        }),
        h("button", { class: "gold sm", onclick: () => { $("#devInput").value = "AG-001"; doPrecheck(); } }, "用 AG-001 试"),
        h("button", { class: "ghost sm", onclick: () => { $("#devInput").value = "AG-004"; doPrecheck(); } }, "试旧版本"),
        h("button", { class: "ghost sm", onclick: () => { $("#devInput").value = "AG-003"; doPrecheck(); } }, "试低电量"),
      ),
      h("div", { id: "preBox", class: "mt" }),
    );
    $("#devInput").focus();
    if (pre) renderPrecheck();
  }

  async function doPrecheck() {
    const id = ($("#devInput").value || "").trim().toUpperCase();
    if (!id) return;
    await guard(async () => { pre = await api(`/api/precheck/${encodeURIComponent(id)}`); renderPrecheck(); });
  }

  function renderPrecheck() {
    const { device: d, problems, required } = pre;
    const checks = [
      [`语言包 ${LANG_SHORT[d.lang_code]} · 版本 ${d.lang_version}`,
        d.lang_ok && (!required || cmpVer(d.lang_version, required.version) >= 0),
        !d.lang_ok ? "语言包损坏" : required ? `要求 ≥ ${required.version}` : ""],
      [`电量 ${d.battery}%（阈值 ${STATE.cfg.ready_battery}%）`, d.battery >= STATE.cfg.ready_battery],
      ["耳机在位", !!d.earphone],
      ["挂绳在位", !!d.strap],
      ["设备未被其他订单锁定", d.status === "available"],
    ];
    const box = $("#preBox");
    const ok = problems.length === 0;
    box.replaceChildren(
      h("div", { class: `pass-line ${ok ? "ok" : "bad"}` },
        ok ? "✓ 校验通过，可以借出" : "✕ 暂不可借出"),
      h("ul", { class: "checklist" }, ...checks.map(([t, pass, extra]) =>
        h("li", { class: pass ? "" : "bad" }, t + (pass ? "" : ` —— ${extra || "不达标"}`)))),
      !ok ? h("div", { class: "alert danger" }, "阻断原因：" + problems.join("；")) : null,
      h("div", { class: "grid2 mt" },
        h("label", { class: "fld" }, "参观者（演示数据）",
          (() => {
            const s = h("select", { id: "visitorSel" },
              ...visitors.map(v => h("option", { value: v.id }, `${v.name} · ${v.phone}`)));
            return s;
          })()),
        h("div", { class: "fee-box" }, "押金预授权", h("div", { class: "total" }, money(STATE.cfg.deposit)),
          h("div", { class: "small muted" }, "模拟支付网关冻结，归还无争议即原路退回")),
      ),
      h("button", {
        class: "big-btn mt", disabled: !ok,
        onclick: () => guard(doCheckout),
      }, ok ? "第 2 步 · 确认借出并锁定设备" : "存在阻断项，无法借出"),
    );
  }

  async function doCheckout() {
    const deviceId = pre.device.id, visitorId = $("#visitorSel").value;
    const r = await api("/api/checkout", { method: "POST", body: { device_id: deviceId, visitor_id: visitorId } });
    panel.replaceChildren(
      h("div", { class: "step-dots" }, h("i", { class: "on" }), h("i", { class: "on" }), h("i", { class: "on" })),
      h("div", { class: "big-step" }, "借出成功"),
      h("div", { class: "alert info mt" }, `设备 ${deviceId} 已进入「在借」状态，不能被其他订单锁定。`),
      h("div", { class: "grid2 mt" },
        h("div", { class: "panel", style: "margin:0" },
          h("h2", "租借订单"), h("div", { class: "mono", style: "font-size:20px" }, r.order_id)),
        h("div", { class: "fee-box" }, "押金预授权回执",
          h("div", { class: "mono mt" }, r.hold_receipt),
          h("div", { class: "total mt" }, money(r.deposit))),
      ),
      h("div", { class: "row mt" },
        h("button", { class: "gold", onclick: () => { pre = null; paint(); } }, "再借一台"),
        h("button", { class: "ghost", onclick: () => go("return") }, "去归还检查")),
    );
    refreshKpi();
  }

  paint();
};

/* ---------------- 视图 2：设备可用表（编号槽位阵列） ---------------- */

routes.devices = async function (root) {
  let filter = "";
  const panel = h("div", { class: "panel" });
  const filters = [["", "全部"], ["available", "可借"], ["on_loan", "在借"],
    ["pending_disinfection", "待消毒"], ["pending_charge", "待充电"], ["charging", "充电中"],
    ["repair", "维修"], ["in_transit", "调拨"]];

  async function load() {
    const { devices } = await api(`/api/devices?site=${STATE.actor.site_id}${filter ? `&status=${filter}` : ""}`);
    const grid = h("div", { class: "slot-grid" }, ...devices.map(d => h("div", {
      class: `cell s-${d.status}`,
      title: `${d.site_name}${d.active_order ? " · 订单 " + d.active_order : ""}`,
      onclick: () => showDetail(d.id),
    },
      h("div", { class: "meta" }, h("span", { class: "cid" }, d.id), verTag(d)),
      h("div", { class: "meta" }, tag(d.status), h("span", { class: "lang" }, LANG_SHORT[d.lang_code])),
      h("div", { class: "meta" },
        battery(d),
        h("span", { class: "lang" },
          (d.earphone ? "" : "无耳机 ") + (d.strap ? "" : "无挂绳") +
          (!d.shell_ok ? "外壳伤 " : "") + (!d.button_ok ? "按键坏" : ""))),
    )));
    panel.querySelector(".body").replaceChildren(
      devices.length ? grid : h("div", { class: "muted" }, "当前筛选下没有设备"),
      h("div", { class: "hint mt" }, `共 ${devices.length} 台 · 点选设备可查看租借与维修记录`),
    );
  }

  root.append(panel);
  panel.append(
    h("h2", "设备可用表 · 编号槽位阵列"),
    h("p", { class: "hint" }, `当前馆区：${siteName(STATE.actor.site_id)}。细条即电量，低于 ${STATE.cfg.ready_battery}% 变红不可借。`),
    h("div", { class: "row", style: "margin-bottom:14px" },
      ...filters.map(([v, l]) => h("button", {
        class: `sm ${filter === v ? "" : "ghost"}`,
        onclick: () => { filter = v; load(); },
      }, l))),
    h("div", { class: "body" }),
  );
  await load();

  async function showDetail(id) {
    const d = (await api(`/api/device/${id}`)).device;
    const dlg = h("div", { class: "panel" },
      h("h2", `${d.id} · 设备详情`),
      h("div", { class: "row mt" }, tag(d.status), verTag(d), battery(d)),
      h("table", { class: "mt" }, h("tbody",
        ...[
          ["归属馆区", d.site_name], ["当前所在馆", siteName(d.current_site_id)],
          ["耳机", d.earphone ? "在" : "缺失"], ["挂绳", d.strap ? "在" : "缺失"],
          ["外壳", d.shell_ok ? "完好" : "损伤"], ["按键", d.button_ok ? "正常" : "故障"],
          ["槽位", d.slot_no || "—"], ["上次放行批次", d.last_release_batch || "—"],
        ].map(([k, v]) => h("tr", h("th", k), h("td", String(v)))))),
      h("h3", {}, "近期订单"),
      h("table", {}, h("tbody",
        h("tr", {}, ...["订单", "状态", "借出", "归还", "押金"].map(x => h("th", {}, x))),
        ...(d.orders.length ? d.orders : [null]).map(o => o
          ? h("tr", {}, h("td", { class: "mono" }, o.id), h("td", {}, tag(o.status)),
            h("td", { class: "small" }, fmtTime(o.issued_at)), h("td", { class: "small" }, fmtTime(o.returned_at)),
            h("td", {}, tag(o.deposit_status)))
          : h("tr", {}, h("td", { colspan: 5, class: "muted" }, "暂无订单"))))),
      h("div", { class: "row mt" },
        d.status === "available" ? h("button", { onclick: () => { go("counter"); } }, "去柜台借出") : null,
        ["on_loan", "return_check"].includes(d.status)
          ? h("button", { class: "gold", onclick: () => { location.hash = `#/return?d=${d.id}`; renderRoute(); } }, "去归还检查")
          : null,
        h("button", { class: "ghost", onclick: () => dlg.remove() }, "关闭")),
    );
    root.append(dlg);
    dlg.scrollIntoView({ behavior: "smooth" });
  }
};

/* ---------------- 视图 3：归还检查（照片旁勾选配件） ---------------- */

routes.return = async function (root) {
  const panel = h("div", { class: "panel" });
  root.append(panel);
  const preset = (location.hash.split("?d=")[1] || "").trim();

  panel.append(
    h("h2", "归还检查"),
    h("p", { class: "hint" }, "扫入设备编号后开始检查：外观照片在左，配件与功能勾选在右；提交即计算扣款并决定去向。"),
    h("div", { class: "row" },
      h("input", { id: "retId", placeholder: "设备编号，如 AG-007", value: preset, style: "font-family:monospace;font-size:18px;letter-spacing:2px" }),
      h("button", { class: "gold", onclick: () => guard(load) }, "开始检查"),
      h("span", { class: "small muted" }, "设备将先锁定为「归还检查」")),
    h("div", { id: "retBody", class: "mt" }),
  );
  if (preset) guard(load);

  async function load() {
    const id = ($("#retId").value || "").trim().toUpperCase();
    const { order } = await api(`/api/return/${encodeURIComponent(id)}`, { method: "GET" });
    const d = (await api(`/api/device/${id}`)).device;
    const body = $("#retBody");
    const flags = {
      earphone: true, strap: true, shell_ok: true, button_ok: true, lang_ok: true,
      battery: Math.max(20, d.battery - 20),
    };
    let dispute = false;

    const feeBox = h("div");
    const siteSel = h("select", {},
      ...STATE.sites.map(s => h("option", { value: s.id, selected: s.id === STATE.actor.site_id }, s.name)));

    function feePreview() {
      const overdue = order.status === "overdue" || Date.now() / 1000 > order.due_at;
      const rows = [
        ["逾期费", overdue ? STATE.cfg.overdue_fee : 0, order.status === "overdue" ? "订单已逾期" : ""],
        ["耳机缺失", flags.earphone ? 0 : STATE.cfg.earphone_fee, ""],
        ["挂绳缺失", flags.strap ? 0 : STATE.cfg.strap_fee, ""],
        ["外壳/按键损伤", (flags.shell_ok && flags.button_ok) ? 0 : STATE.cfg.damage_fee, ""],
      ];
      const total = rows.reduce((a, r) => a + r[1], 0);
      const cross = siteSel.value !== order.site_id;
      feeBox.replaceChildren(
        h("div", { class: "fee-box" },
          h("div", { class: "small muted" }, "应扣合计（押金 ¥" + order.deposit_amount + "）"),
          h("div", { class: "total" }, money(total)),
          h("table", { class: "mt small" }, ...rows.map(([l, v, note]) =>
            h("tr", h("td", l + (note ? `（${note}）` : "")), h("td", { class: "right" }, money(v))))),
          total === 0 && !dispute
            ? h("div", { class: "small mt" }, "✓ 无扣款，提交后押金立即原路退回")
            : h("div", { class: "small danger-text mt" }, "押金将冻结，进入主管押金复核队列")),
        cross ? h("div", { class: "alert warn mt" },
          `跨馆归还：设备归属 ${siteName(order.site_id)}，将在消毒放行后调拨回馆。`) : null,
      );
      return total;
    }

    const check = (key, label, bad) => h("label", { class: "check-item" },
      h("input", {
        type: "checkbox", checked: flags[key],
        onchange: e => { flags[key] = e.target.checked; e.target.closest(".check-item").classList.toggle("bad", !e.target.checked); feePreview(); },
      }), h("span", {}, label), bad ? h("span", { class: "tag repair" }, bad) : null);

    const batInput = h("input", { type: "number", min: 0, max: 100, value: flags.battery, style: "width:90px",
      oninput: e => { flags.battery = Number(e.target.value); feePreview(); } });

    const disputeBox = h("label", { class: "check-item" },
      h("input", { type: "checkbox", onchange: e => { dispute = e.target.checked; feePreview(); } }),
      h("span", {}, "游客对扣款有争议（押金转主管复核，不自动退款）"));
    const noteInput = h("textarea", { rows: 2, placeholder: "备注：损伤位置、争议说明等" });

    body.replaceChildren(
      h("div", { class: "return-layout" },
        h("div", {},
          h("div", { class: "photo-box" },
            h("img", { src: `/files/photos/${id}.svg`, alt: "设备外观照片",
              onerror: e => e.target.replaceWith(h("div", { style: "padding:40px 0" }, "（无外观照片）")) }),
            h("div", { class: "small muted mt" }, `外观照片 · ${id} · 归还时拍摄留档`)),
          h("div", { class: "panel mt", style: "margin:0" },
            h("h2", "订单信息"),
            h("div", { class: "mono" }, order.id),
            h("div", { class: "mt" }, `${order.visitor_name} · ${order.visitor_phone}`),
            h("div", { class: "small muted" },
              `借出 ${fmtTime(order.issued_at)} · 应还 ${fmtTime(order.due_at)}`),
            order.status === "overdue" ? h("div", { class: "mt" }, tag("overdue", "逾期未还")) : null),
        ),
        h("div", {},
          h("div", { class: "check-list" },
            check("shell_ok", "外壳完好", null),
            check("button_ok", "按键功能正常", null),
            check("earphone", "耳机随设备归还", null),
            check("strap", "挂绳随设备归还", null),
            check("lang_ok", "语言包可正常加载", "损坏转维修")),
          h("label", { class: "fld mt" }, `归还实测电量（满电阈值 ${STATE.cfg.ready_battery}%，之后自动充电）`, batInput),
          h("label", { class: "fld mt" }, "归还办理馆区（跨馆归还走调拨流程）", siteSel),
          h("div", { class: "mt" }, disputeBox),
          noteInput,
          h("div", { class: "mt" }, feeBox),
          h("button", {
            class: "gold mt", style: "width:100%;font-size:18px;padding:15px",
            onclick: () => guard(async () => {
              const r = await api("/api/return", {
                method: "POST",
                body: {
                  device_id: id, return_site_id: siteSel.value,
                  flags: { ...flags, battery: Number(flags.battery) },
                  dispute, note: noteInput.value,
                },
              });
              body.replaceChildren(
                h("div", { class: "alert info" },
                  `设备已进入「${r.route === "repair" ? "维修" : "待消毒"}」` +
                  (r.cross_site ? "（跨馆归还，放行后调拨回馆）" : "")),
                r.receipt
                  ? h("div", { class: "panel" }, h("h2", "押金已退还"),
                    h("div", { class: "mono" }, r.receipt), h("div", { class: "gold-text" }, `退还 ${money(r.refunded)}`),
                    h("div", { class: "small muted" }, "重复归还不会再次退款"))
                  : h("div", { class: "panel" }, h("h2", "押金冻结"),
                    h("div", {}, `合计待核 ${money(r.total_fees)}，等待主管在「押金复核」处理${r.route === "repair" ? "；设备已转维修队列" : ""}`)),
                h("div", { class: "row mt" },
                  h("button", { class: "ghost", onclick: () => { body.replaceChildren(); $("#retId").value = ""; } }, "检查下一台"),
                  r.route === "repair" ? h("button", { onclick: () => go("repairs") }, "查看维修队列")
                    : h("button", { class: "gold", onclick: () => go("disinfect") }, "去消毒组批")),
              );
              refreshKpi();
            }),
          }, "提交归还检查"),
        ),
      ),
    );
    siteSel.onchange = feePreview;
    feePreview();
  }
};

/* ---------------- 视图 4：消毒组批 ---------------- */

routes.disinfect = async function (root) {
  const pendingBox = h("div");
  const batchesBox = h("div");
  const transferBox = h("div");
  root.append(
    h("div", { class: "panel" },
      h("h2", "消毒组批"),
      h("p", { class: "hint" }, "勾选待消毒设备组成灭菌批次。规则：未完成灭菌不得放行；电量不足放行后转充电；跨馆设备放行后进入调拨。"),
      pendingBox),
    h("div", { class: "panel" }, h("h2", "灭菌批次"), batchesBox),
    can(["supervisor"]) ? h("div", { class: "panel" }, h("h2", "跨馆调拨接收"), transferBox) : null,
  );

  async function load() {
    const batches = (await api(`/api/batches?site=${STATE.actor.site_id}`)).batches;
    const inOpenBatch = new Set(batches.filter(b => b.status !== "released")
      .flatMap(b => b.devices.map(d => d.id)));
    const pending = (await api(`/api/pending?site=${STATE.actor.site_id}`)).devices
      .filter(d => d.status === "pending_disinfection" && !inOpenBatch.has(d.id));
    const picked = new Set();
    pendingBox.replaceChildren(
      pending.length ? h("div", { class: "pick-list" }, ...pending.map(d => h("label", { class: "check-item" },
        h("input", { type: "checkbox", onchange: e => e.target.checked ? picked.add(d.id) : picked.delete(d.id) }),
        h("span", { class: "mono" }, d.id), verTag(d), battery(d),
        h("span", { class: "small muted" }, d.pending_transfer ? "跨馆待调拨" : ""))))
        : h("div", { class: "muted" }, "暂无待消毒设备"),
      h("div", { class: "row mt" },
        h("button", { class: "gold", onclick: () => guard(async () => {
          if (!picked.size) return toast("请先勾选设备", true);
          await api("/api/batches", { method: "POST", body: { device_ids: [...picked] } });
          toast("已创建灭菌批次"); load();
        }) }, "组成灭菌批次")),
    );

    batchesBox.replaceChildren(...batches.map(b => h("div", { class: "batch-card" },
      h("div", { class: "row", style: "justify-content:space-between" },
        h("strong", {}, `批次 #${b.id}`),
        b.status === "released" ? tag("released", "已放行")
          : b.sterilized_at ? tag("sterilizing", "灭菌完成 · 待放行")
          : tag("sterilizing", "灭菌中")),
      h("div", { class: "dev-chips" }, ...b.devices.map(dv =>
        h("span", { class: `chip ${dv.pending_transfer ? "transfer" : ""}` },
          `${dv.id} ${dv.battery}%${dv.pending_transfer ? " ↩调拨" : ""}`))),
      h("div", { class: "small muted" },
        `建批 ${fmtTime(b.created_at)}${b.sterilized_at ? " · 灭菌 " + fmtTime(b.sterilized_at) : ""}${
          b.released_at ? " · 放行 " + fmtTime(b.released_at) : ""}`),
      h("div", { class: "row mt" },
        b.status !== "released" && !b.sterilized_at ? h("button", { class: "sm",
          onclick: () => guard(async () => { await api(`/api/batches/${b.id}/sterilize`, { method: "POST", body: {} }); toast("灭菌完成"); load(); }) },
          "完成灭菌") : null,
        b.status !== "released" ? h("button", { class: "sm gold",
          onclick: () => guard(async () => {
            const r = await api(`/api/batches/${b.id}/release`, { method: "POST", body: {} });
            const txt = r.routed.map(([id, to]) => `${id}→${({ available: "可借", pending_charge: "待充电", in_transit: "调拨" })[to]}`).join("；");
            toast("放行：" + txt); load();
          }) }, "放行（未灭菌将被阻断）") : h("span", { class: "small muted" }, "已完成放行分流")),
    )));
    if (!batches.length) batchesBox.append(h("div", { class: "muted" }, "暂无批次"));

    if (can(["supervisor"])) {
      const all = (await api(`/api/devices?status=in_transit`)).devices;
      transferBox.replaceChildren(all.length ? h("div", { class: "dev-chips" }, ...all.map(d =>
        h("span", { style: "display:flex;gap:8px;align-items:center" },
          h("span", { class: "chip transfer" }, `${d.id} → ${siteName(d.site_id)}`),
          h("button", { class: "sm ghost", onclick: () => guard(async () => {
            const r = await api(`/api/transfer/${d.id}/receive`, { method: "POST", body: {} });
            toast(`${d.id} 已接收，进入「${STATUS_LABEL[r.target]}」`); load();
          }) }, "主管接收入馆"))))
        : h("div", { class: "muted" }, "暂无调拨中设备"));
    }
  }
  await load();
};

/* ---------------- 视图 5：充电槽视图（槽位阵列） ---------------- */

let slotsTimer = null;
routes.slots = async function (root) {
  const box = h("div", { class: "panel" });
  root.append(h("div", { class: "panel" },
    h("h2", "充电槽视图"),
    h("p", { class: "hint" }, `充电柜以编号槽位阵列展示；电量达到 ${STATE.cfg.ready_battery}% 自动转为「在柜可借」。故障槽不能接入设备。`),
    box));

  async function load() {
    const { slots, pending_charge } = await api(`/api/slots?site=${STATE.actor.site_id}`);
    box.replaceChildren(
      h("div", { class: "cabinet" }, h("div", { class: "dock-grid" }, ...slots.map(s => {
        const cls = !s.ok ? "fault" : s.device_status === "charging" ? "charging"
          : s.device_status === "available" ? "done" : "";
        const dock = h("div", { class: `dock ${cls}` },
          h("div", { class: "row", style: "justify-content:space-between" },
            h("span", { class: "no mono" }, s.label),
            h("label", { class: "small", style: "display:flex;gap:4px;align-items:center" },
              h("input", { type: "checkbox", checked: s.ok,
                onchange: e => guard(async () => {
                  await api(`/api/slots/${s.id}/fault`, { method: "POST",
                    body: { ok: e.target.checked, note: e.target.checked ? "" : "人工标记故障" } });
                  toast(e.target.checked ? "槽位恢复" : "槽位已停用"); load();
                }) }), "正常")),
          s.device_id
            ? h("div", {}, h("div", { class: "dev" }, s.device_id),
              s.device_status ? tag(s.device_status) : null,
              s.battery != null ? battery({ battery: s.battery }) : null)
            : h("div", { class: "muted small" }, "空槽"),
          !s.ok ? h("div", { class: "small danger-text" }, "故障：" + (s.note || "停用中")) : null,
          can(["technician", "supervisor"]) && !s.ok
            ? null : null);
        if (can(["technician", "supervisor"]) && s.ok && !s.device_id && pending_charge.length) {
          const sel = h("select", { class: "sm" }, h("option", { value: "" }, "接入待充设备…"),
            ...pending_charge.map(d => h("option", { value: d.id }, `${d.id} · ${d.battery}%`)));
          sel.onchange = () => guard(async () => {
            await api("/api/dock", { method: "POST", body: { device_id: sel.value, slot_id: s.id } });
            toast(`${sel.value} 已接入 ${s.label}`); load();
          });
          dock.append(sel);
        }
        return dock;
      }))),
      h("div", { class: "hint mt" },
        pending_charge.length ? `待充电 ${pending_charge.length} 台：${pending_charge.map(d => d.id).join("、")}` : "没有待充电设备"),
    );
  }
  await load();
  clearInterval(slotsTimer);
  slotsTimer = setInterval(() => { if (location.hash.startsWith("#/slots")) guard(load); }, 2500);
};

/* ---------------- 视图 6：维修队列 ---------------- */

routes.repairs = async function (root) {
  const listBox = h("div");
  const form = h("div", { class: "panel" });
  root.append(
    form,
    h("div", { class: "panel" }, h("h2", "维修队列"),
      h("p", { class: "hint" }, "讲解员可报故障，但只有维修员/主管能结案；配件补齐或语言包重灌后仍须重新消毒才能回柜。"),
      listBox));

  const devInput = h("input", { placeholder: "设备编号，如 AG-011", style: "font-family:monospace" });
  const catSel = h("select", {},
    ...[["shell", "外壳损伤"], ["button", "按键故障"], ["lang", "语言包损坏"],
    ["accessory", "配件缺失"], ["slot", "充电槽问题"], ["other", "其他"]].map(([v, l]) =>
      h("option", { value: v }, l)));
  const issueInput = h("input", { placeholder: "故障描述" });
  form.append(
    h("h2", "上报故障"),
    h("p", { class: "hint" }, "讲解员/柜台同样可以上报（无权批准赔付）。"),
    h("div", { class: "row" },
      h("label", { class: "fld" }, "设备", devInput),
      h("label", { class: "fld" }, "类别", catSel),
      h("label", { class: "fld", style: "flex:1" }, "描述", issueInput),
      h("button", { class: "gold", onclick: () => guard(async () => {
        await api("/api/repairs", { method: "POST",
          body: { device_id: devInput.value.trim().toUpperCase(), category: catSel.value, issue: issueInput.value || catSel.selectedOptions[0].textContent } });
        toast("已建维修单，设备转入维修"); devInput.value = issueInput.value = ""; load();
      }) }, "提交报修")),
  );

  async function load() {
    const siteQ = can(["supervisor"]) ? "" : `&site=${STATE.actor.site_id}`;
    const repairs = (await api(`/api/repairs?${siteQ.slice(1)}`)).repairs;
    listBox.replaceChildren(h("div", { class: "scroll" }, h("table", {},
      h("tbody",
        h("tr", {}, ...["#", "设备", "类别", "问题", "上报人", "状态", "操作"].map(x => h("th", {}, x))),
        ...(repairs.length ? repairs : [null]).map(t => t ? h("tr", {},
          h("td", String(t.id)),
          h("td", { class: "mono" }, t.device_id, h("div", { class: "small muted" }, siteName(t.site_id))),
          h("td", { class: "small" }, { shell: "外壳", button: "按键", lang: "语言包", accessory: "配件", slot: "槽位" }[t.category] || "其他"),
          h("td", {}, t.issue + (t.resolution ? ` → ${t.resolution}` : "")),
          h("td", { class: "small" }, t.reported_by),
          h("td", {}, tag(t.status)),
          h("td", {}, t.status !== "resolved" && can(["repair", "supervisor"])
            ? h("button", { class: "sm", onclick: () => guard(async () => {
              await api(`/api/repairs/${t.id}/resolve`, { method: "POST",
                body: { resolution: "已修复并复检", replace: { earphone: true, strap: true } } });
              toast(`${t.device_id} 修复完成，转待消毒`); load(); refreshKpi();
            }) }, "修复并转消毒")
            : t.status !== "resolved" ? h("span", { class: "small muted" }, "仅维修员可结案") : h("span", { class: "small muted" }, fmtTime(t.resolved_at))))
          : h("tr", {}, h("td", { colspan: 7, class: "muted" }, "暂无维修单")))))));
  }
  await load();
};

/* ---------------- 视图 7：押金复核 ---------------- */

routes.deposits = async function (root) {
  const box = h("div", { class: "panel" });
  root.append(box);
  const isSup = can(["supervisor"]);

  async function load() {
    const q = isSup ? "" : `?site=${STATE.actor.site_id}`;
    const deposits = (await api(`/api/deposits${q}`)).deposits;
    box.replaceChildren(
      h("h2", "押金复核"),
      h("p", { class: "hint" }, "归还时存在扣款或争议的订单在此排队。讲解员只能查看；批准扣赔 / 豁免仅值班主管可执行，且每单只退一次（回执幂等）。"),
      !isSup ? h("div", { class: "alert warn" }, "当前为讲解员视角：可查看与沟通，但赔付审批按钮不可用。") : null,
      h("div", { class: "scroll" }, h("table", {}, h("tbody",
        h("tr", {}, ...["订单", "参观者", "设备", "扣款明细", "合计", "争议", "押金", "操作"].map(x => h("th", {}, x))),
        ...(deposits.length ? deposits : [null]).map(o => o ? h("tr", {},
          h("td", { class: "mono small" }, o.id),
          h("td", {}, o.visitor_name, h("div", { class: "small muted" }, o.visitor_phone)),
          h("td", { class: "mono" }, o.device_id),
          h("td", { class: "small" },
            [["逾期", o.fee_overdue], ["耳机", o.fee_earphone], ["挂绳", o.fee_strap], ["损伤", o.fee_damage]]
              .filter(x => x[1]).map(x => `${x[0]} ${money(x[1])}`).join("、") || "—"),
          h("td", { class: "gold-text" }, money(o.total_fees)),
          h("td", {}, o.dispute ? tag("overdue", "有争议") : h("span", { class: "muted" }, "无")),
          h("td", { class: "small" }, money(o.deposit_amount)),
          h("td", {}, isSup ? h("div", { class: "row" },
            h("button", { class: "sm gold", onclick: () => settle(o, "approve") }, "批准扣赔退余额"),
            h("button", { class: "sm ghost", onclick: () => settle(o, "waive") }, "豁免全额退"))
            : h("span", { class: "small muted" }, "需主管")))
          : h("tr", {}, h("td", { colspan: 8, class: "muted" }, "没有待复核押金")))))),
    );
  }

  async function settle(o, action) {
    const r = await api(`/api/deposits/${o.id}`, { method: "POST", body: { action } });
    toast(`${action === "approve" ? "已扣赔并退余额" : "已豁免全额退"} ${money(r.refund)} · 回执 ${r.receipt}`);
    load(); refreshKpi();
  }
  await load();
};

/* ---------------- 启动 ---------------- */
initShell().then(renderRoute).catch(e => toast("初始化失败: " + e.message, true));

/* MiHome Web —— 前端。
   原生 JS，无构建步骤、无外部 CDN，内网离线可用。 */

/* 老 WebView 兜底：NodeList.forEach 要 Chrome 51+，缺失会让整份脚本
   在第一个 querySelectorAll(...).forEach 处抛错，页面直接白屏。 */
if (typeof NodeList !== "undefined" && NodeList.prototype &&
    !NodeList.prototype.forEach) {
  NodeList.prototype.forEach = Array.prototype.forEach;
}

const REFRESH_MS = 5000;

const state = {
  devices: [],
  scenes: [],
  schedules: [],
  loggedIn: false,
  currentDid: null,
  currentDetail: null,
  loginSid: null,
  loginStartedAt: 0,
};

/* ---------- 工具 ---------- */

const $ = (id) => document.getElementById(id);
/* 兼容性说明：全文避免 ?. / ?? / CSS.escape / 对象展开等较新语法，
   车机 WebView 常落后手机好几代，这些特性在老 Chromium 上直接是语法错误
   （整份脚本不执行，页面表现为一片空白）。 */
const esc = (s) => String(s == null ? "" : s).replace(/[&<>"']/g,
  (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));

function getToken() {
  const url = new URLSearchParams(location.search).get("token");
  if (url) {
    setToken(url);
    // 口令留在地址栏会进历史记录、被截图带走，存下后立刻抹掉
    history.replaceState(null, "", location.pathname);
  }
  return localStorage.getItem("mihome_token") || "";
}

function setToken(value) {
  try { localStorage.setItem("mihome_token", value); } catch (e) { /* 隐私模式禁写 */ }
}

function clearToken() {
  try { localStorage.removeItem("mihome_token"); } catch (e) { /* 同上 */ }
}

/* 口令闸门：没有口令时直接拦在门口，而不是发一堆注定 401 的请求 */
function showGate(wrong) {
  $("gate").hidden = false;
  $("gate-err").hidden = !wrong;
  setTimeout(() => {
    const input = $("gate-input");
    if (input) input.focus();
  }, 60);
}

function hideGate() {
  $("gate").hidden = true;
  $("gate-err").hidden = true;
}

async function api(path, options = {}) {
  const token = getToken();
  const headers = { "Content-Type": "application/json" };
  if (token) headers["X-Token"] = token;
  if (options.headers) Object.assign(headers, options.headers);
  const res = await fetch(path, {
    method: options.method || "GET",
    body: options.body,
    headers: headers,
  });
  const text = await res.text();
  let data = null;
  try { data = text ? JSON.parse(text) : null; } catch (e) { data = null; }
  if (!res.ok) {
    const err = new Error((data && data.detail) || `请求失败 (${res.status})`);
    err.status = res.status;
    throw err;
  }
  return data;
}

let toastTimer = null;
function toast(msg, isError = false) {
  const el = $("toast");
  el.textContent = msg;
  el.className = isError ? "toast err" : "toast";
  el.hidden = false;
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => { el.hidden = true; }, 2600);
}

const isSpeaker = (d) => String(d.model || "").includes("wifispeaker");
const hasSwitch = (d) => d.power !== null && d.power !== undefined;

/* ---------- 登录 ---------- */

async function startLogin() {
  try {
    const data = await api("/api/login/begin", { method: "POST" });
    if (!data.need_qr) {
      await refreshAll();
      return;
    }
    state.loginSid = data.sid;
    state.loginStartedAt = Date.now();
    $("qr-frame").innerHTML =
      `<img src="/api/login/qr/${data.sid}.svg" alt="登录二维码">`;
    $("login-tip").textContent = "用米家 App 扫描二维码，确认后本页会自动进入。";
    pollLogin();
  } catch (err) {
    $("login-tip").textContent = `获取二维码失败：${err.message}`;
  }
}

async function pollLogin() {
  if (!state.loginSid) return;
  const expired = Date.now() - state.loginStartedAt > 110000;
  try {
    const res = await api(`/api/login/wait/${state.loginSid}`, { method: "POST" });
    if (res.status === "ok") {
      state.loginSid = null;
      toast("登录成功");
      await refreshAll();
      return;
    }
    if (res.status === "error") {
      $("login-tip").textContent = `登录失败：${res.error || "未知错误"}，正在刷新二维码…`;
      state.loginSid = null;
      setTimeout(startLogin, 1200);
      return;
    }
    if (expired) {
      state.loginSid = null;
      $("login-tip").textContent = "二维码已过期，正在重新生成…";
      setTimeout(startLogin, 500);
      return;
    }
  } catch (err) {
    if (expired) { state.loginSid = null; setTimeout(startLogin, 500); return; }
  }
  setTimeout(pollLogin, 2000);
}

async function logout() {
  if (!confirm("确定退出登录？下次需要重新扫码。")) return;
  try {
    await api("/api/logout", { method: "POST" });
    $("qr-frame").innerHTML = '<span class="muted small">已退出</span>';
    await refreshAll();
  } catch (err) {
    toast(err.message, true);
  }
}

/* ---------- 状态 ---------- */

function switchView(loggedIn) {
  state.loggedIn = loggedIn;
  $("view-login").hidden = loggedIn;
  $("view-main").hidden = !loggedIn;
  $("btn-logout").hidden = !loggedIn;
  $("status-dot").className = loggedIn ? "dot on" : "dot err";
  // 已有登录会话在轮询时不重复发起，避免二维码被反复重置
  if (!loggedIn && !state.loginSid) startLogin();
}

async function refreshDevices() {
  const data = await api("/api/devices");
  state.devices = data.devices || [];
  if (data.last_error) $("updated").textContent = `异常：${data.last_error}`;
  else if (data.updated_at) {
    $("updated").textContent =
      `更新于 ${new Date(data.updated_at * 1000).toLocaleTimeString("zh-CN")}`;
  }
  (data.events || []).forEach((e) => toast(e.message, e.kind === "schedule_failed"));
  renderDevices();
  renderVoiceDevices();
  renderScheduleDevices();
}

async function refreshScenes() {
  try {
    const data = await api("/api/scenes");
    state.scenes = data.scenes || [];
    renderScenes();
  } catch (e) { /* 场景拉取失败不阻塞主流程 */ }
}

async function refreshSchedules() {
  try {
    const data = await api("/api/schedules");
    state.schedules = data.rules || [];
    renderSchedules();
  } catch (e) { /* 同上 */ }
}

async function refreshAll() {
  try {
    const status = await api("/api/status");
    switchView(status.logged_in);
    if (!status.logged_in) return;
    await refreshDevices();
    await refreshScenes();
    await refreshSchedules();
  } catch (err) {
    // 口令问题往上抛给闸门处理；其余错误就地提示，不打断页面
    if (err && err.status === 401) throw err;
    toast(err.message, true);
  }
}

/* ---------- 渲染：设备 ---------- */

/* 墙壁开关：即使改成"无线模式"（转无线后按键不再控制实际电路，只发无线
   信号去联动别的设备），在米家云端依旧上报为 switch 型号——型号里认不出来
   是不是无线模式，所以按型号整体归类。
   多键开关派生的虚拟子设备（did 形如 947030039.s2）型号相同，一并覆盖。 */
function isSwitchDevice(d) {
  return String(d.model || "").indexOf("switch") >= 0;
}

/* 灯组：did 以 group. 开头，model 形如 mijia.light.group3。
   成员灯自己不带任何反向标记，靠后端从灯组设备的 extra.member_list
   里回填出 group_did / group_name。 */
function isGroupDevice(d) {
  return String(d.did || "").indexOf("group.") === 0;
}

/* 筛选项持久化：开关类设备是长期存在的一批，每次打开都重新勾一遍太烦。
   首次访问默认隐藏开关设备（老大主要诉求），其余默认关闭。 */
const FILTER_DEFAULTS = {
  "hide-switch": "1",
  "hide-grouped": "0",
  "hide-noctl": "0",
  "hide-offline": "0",
};

function restoreFilters() {
  Object.keys(FILTER_DEFAULTS).forEach((id) => {
    let saved = null;
    try { saved = localStorage.getItem("mihome_" + id); } catch (e) { saved = null; }
    if (saved === null) saved = FILTER_DEFAULTS[id];
    $(id).checked = saved === "1";
  });
}

function saveFilters() {
  Object.keys(FILTER_DEFAULTS).forEach((id) => {
    try { localStorage.setItem("mihome_" + id, $(id).checked ? "1" : "0"); } catch (e) { /* 隐私模式禁写 */ }
  });
}

function visibleDevices() {
  const hideSwitch = $("hide-switch").checked;
  const hideGrouped = $("hide-grouped").checked;
  const hideNoCtl = $("hide-noctl").checked;
  const hideOffline = $("hide-offline").checked;
  return state.devices.filter((d) => {
    if (hideOffline && !d.online) return false;
    if (hideSwitch && isSwitchDevice(d)) return false;
    // 组内成员让位给灯组：只藏成员，灯组自身不带 group_did，不受影响
    if (hideGrouped && d.group_did) return false;
    if (hideNoCtl && !hasSwitch(d)) return false;
    return true;
  });
}

function renderDevices() {
  const shown = visibleDevices();
  const total = state.devices.length;
  // 明确告知被筛掉了多少：否则设备"凭空少了二十多个"会让人以为掉了数据
  const hint = $("filter-hint");
  if (hint) {
    hint.textContent = shown.length < total
      ? `显示 ${shown.length} / ${total} 台，${total - shown.length} 台被筛选隐藏`
      : "";
  }
  const groups = new Map();
  shown.forEach((d) => {
    const key = `${d.home_name} · ${d.room_name}`;
    if (!groups.has(key)) groups.set(key, []);
    groups.get(key).push(d);
  });

  const box = $("device-groups");
  if (!groups.size) {
    box.innerHTML = `<div class="empty">没有可显示的设备${
      state.devices.length ? "（可能被筛选条件隐藏了）" : ""}</div>`;
    return;
  }

  box.innerHTML = Array.from(groups.entries()).map(([key, list]) => `
    <div class="group-title">${esc(key)}</div>
    <div class="grid">${list.map(deviceCard).join("")}</div>
  `).join("");

  bindGridEvents();
}

/* 事件委托，整个列表只绑一次。
   注意：device-groups 容器本身不会被重建（只替换 innerHTML），所以在
   每次 renderDevices 里 addEventListener 会不断累积监听器，点一下触发 N 次。
   用 dataset.bound 做一次性标记。

   左右两个热区，各管一件事，互不干扰：
   - .dev-main（左半）→ 设备详情
   - .dev-power（右半）→ 电源开关 */
function bindGridEvents() {
  const box = $("device-groups");
  if (box.dataset.bound) return;
  box.dataset.bound = "1";
  box.addEventListener("click", (ev) => {
    const target = ev.target;
    if (!target || !target.closest) return;

    const power = target.closest(".dev-power");
    if (power) {
      // 无开关 / 离线的右半区不给点（.na），点了也没用
      if (power.classList.contains("na")) return;
      const btn = power.querySelector(".switch");
      if (btn && !btn.disabled) togglePower(btn.dataset.did, btn);
      return;
    }

    const main = target.closest(".dev-main");
    if (main) openDetail(main.dataset.did);
  });
}

function deviceCard(d) {
  const on = d.power === true;
  const canPower = hasSwitch(d) && d.online;
  // 有开关属性但设备离线：右半区仍然画出来（保持卡片对齐），
  // 但标成 na，点了不响应——否则每次点都是"设备离线"的失败提示。
  const powerable = hasSwitch(d);

  // 灯组标识：隐藏组内单灯后，仍能一眼看出哪些是组、哪些灯挂在组下面
  let tag = "";
  if (isGroupDevice(d)) tag = '<span class="badge tag-group">灯组</span>';
  else if (d.group_name) tag = `<span class="badge tag-group">组·${esc(d.group_name)}</span>`;

  // 副标题优先显示实时读数（温度、湿度这类），没有就显示房间
  const sub = d.metric
    ? `<span class="dev-metric">${esc(d.metric)}</span>`
    : `<span>${esc(d.room_name)}</span>`;

  let right;
  if (powerable) {
    right = `<button class="switch ${on ? "on" : ""}" data-did="${esc(d.did)}"
               aria-label="开关"${canPower ? "" : " disabled"}></button>`;
  } else {
    right = `<span class="na-text">无开关</span>`;
  }

  return `
    <div class="dev-card${d.online ? "" : " offline"}${on ? " on" : ""}">
      <div class="dev-main" data-did="${esc(d.did)}">
        <div class="dev-name">${esc(d.name)}</div>
        <div class="dev-sub">${sub}</div>
        <div class="dev-tags">
          ${tag}
          <span class="badge ${d.online ? "on" : ""}">${d.online ? "在线" : "离线"}</span>
        </div>
      </div>
      <div class="dev-power${powerable && canPower ? "" : " na"}">${right}</div>
    </div>`;
}

async function togglePower(did, el) {
  el.disabled = true;
  // 乐观更新：先把按钮切过去，失败再回滚
  const before = el.classList.contains("on");
  // 卡片也带 on 状态（开着时描绿边），让车机上远距离也能一眼看出哪些是开的
  const card = el.closest ? el.closest(".dev-card") : null;
  const paint = (on) => {
    el.classList.toggle("on", on);
    if (card) card.classList.toggle("on", on);
  };
  paint(!before);
  try {
    const res = await api(`/api/devices/${encodeURIComponent(did)}/power`, {
      method: "POST", body: JSON.stringify({}),
    });
    paint(res.power === true);
    const dev = state.devices.find((d) => d.did === did);
    if (dev) dev.power = res.power;
  } catch (err) {
    paint(before);
    toast(err.message, true);
  } finally {
    el.disabled = false;
  }
}

/* ---------- 渲染：设备详情 ---------- */

async function openDetail(did) {
  state.currentDid = did;
  $("drawer").hidden = false;
  $("drawer-mask").hidden = false;
  $("drawer-title").textContent = "加载中…";
  $("drawer-sub").textContent = "";
  $("drawer-body").innerHTML = '<div class="empty">加载中…</div>';
  try {
    const detail = await api(`/api/devices/${encodeURIComponent(did)}/detail`);
    state.currentDetail = detail;
    $("drawer-title").textContent = detail.name;
    $("drawer-sub").textContent = detail.model;
    renderProps(detail);
    await loadPropValues(detail);
    renderActions(detail);
  } catch (err) {
    $("drawer-body").innerHTML = `<div class="empty">${esc(err.message)}</div>`;
  }
}

function closeDetail() {
  $("drawer").hidden = true;
  $("drawer-mask").hidden = true;
  state.currentDid = null;
}

/* 按 data-name 找属性块。
   不用 CSS.escape：老 WebView 没有这个 API，且属性名里偶有连字符，
   直接遍历比对最稳。 */
function propEl(name) {
  const nodes = document.querySelectorAll("#drawer-body .prop");
  for (let i = 0; i < nodes.length; i++) {
    if (nodes[i].getAttribute("data-name") === name) return nodes[i];
  }
  return null;
}

async function loadPropValues(detail) {
  try {
    const data = await api(`/api/devices/${encodeURIComponent(detail.did)}/props`);
    const values = data.values || {};
    detail.props.forEach((p) => {
      const node = propEl(p.name);
      if (!node) return;
      const box = node.querySelector(".prop-value");
      if (box) box.textContent = formatValue(p, values[p.name]);
      if (p.writable && p.type !== "bool" && values[p.name] != null) {
        const input = node.querySelector("input, select");
        if (input) input.value = values[p.name];
      }
    });
  } catch (e) { /* 读数失败不影响控件可用性 */ }
}

function formatValue(prop, value) {
  if (value === null || value === undefined) return "—";
  if (prop.type === "bool") return value ? "开" : "关";
  if (prop.value_list && prop.value_list.length) {
    const hit = prop.value_list.find((v) => v.value === value);
    if (hit) return hit.description || hit.value;
  }
  return String(value) + unitOf(prop.name);
}

const UNITS = {
  temperature: "°C", "relative-humidity": "%", humidity: "%",
  "battery-level": "%", "electric-power": "W", "power-consumption": "W",
  rssi: "dBm", "online-time": "min",
};
const unitOf = (name) => UNITS[name] || "";

function renderProps(detail) {
  const writable = detail.props.filter((p) => p.writable);
  const readonly = detail.props.filter((p) => p.readable && !p.writable);
  const html = [];
  if (writable.length) {
    html.push('<div class="section-title">可控制</div>');
    html.push(writable.map(propBlock).join(""));
  }
  if (readonly.length) {
    html.push('<div class="section-title">只读</div>');
    html.push(readonly.map(propBlock).join(""));
  }
  const body = $("drawer-body");
  body.innerHTML = html.join("") || '<div class="empty">该设备没有可用属性</div>';
  bindPropControls(detail);
}

function propBlock(p) {
  const ctrl = p.writable ? propControl(p) : "";
  return `
    <div class="prop" data-name="${esc(p.name)}">
      <div class="prop-head">
        <span class="prop-name">${esc(p.desc || p.name)}</span>
        <span class="prop-value">—</span>
      </div>
      ${ctrl ? `<div class="prop-ctrl">${ctrl}</div>` : ""}
    </div>`;
}

function propControl(p) {
  if (p.type === "bool") {
    return `<button class="switch" data-prop="${esc(p.name)}" aria-label="开关"></button>`;
  }
  if (p.value_list && p.value_list.length) {
    const opts = p.value_list.map((v) =>
      `<option value="${esc(v.value)}">${esc(v.description || v.value)}</option>`).join("");
    return `<select class="select-full" data-prop="${esc(p.name)}">${opts}</select>`;
  }
  if (p.type === "int" || p.type === "uint" || p.type === "float") {
    const [min, max, step] = p.range || [0, 100, 1];
    return `<input type="range" data-prop="${esc(p.name)}"
      min="${esc(min)}" max="${esc(max)}" step="${esc(step || 1)}">`;
  }
  return `<div class="form-row">
      <input type="text" data-prop="${esc(p.name)}" placeholder="输入值">
      <button class="btn" data-send="${esc(p.name)}">写入</button>
    </div>`;
}

function bindPropControls(detail) {
  const body = $("drawer-body");
  body.querySelectorAll(".switch[data-prop]").forEach((el) => {
    el.addEventListener("click", () => {
      const next = !el.classList.contains("on");
      el.classList.toggle("on", next);
      writeProp(detail.did, el.dataset.prop, next, el);
    });
  });
  body.querySelectorAll("select[data-prop]").forEach((el) => {
    el.addEventListener("change", () => writeProp(detail.did, el.dataset.prop, el.value, el));
  });
  // 滑块：松手才下发，拖动只预览（沿用桌面端的取舍）
  body.querySelectorAll("input[type=range][data-prop]").forEach((el) => {
    el.addEventListener("input", () => {
      const box = el.closest(".prop").querySelector(".prop-value");
      if (box) box.textContent = el.value + unitOf(el.dataset.prop);
    });
    el.addEventListener("change", () => {
      const num = Number(el.value);
      writeProp(detail.did, el.dataset.prop, num, el);
    });
  });
  body.querySelectorAll("button[data-send]").forEach((el) => {
    el.addEventListener("click", () => {
      const input = el.closest(".prop").querySelector("input[type=text]");
      writeProp(detail.did, el.dataset.send, input.value, el);
    });
  });
}

async function writeProp(did, name, value, el) {
  try {
    await api(`/api/devices/${encodeURIComponent(did)}/prop`, {
      method: "POST", body: JSON.stringify({ name, value }),
    });
    const propNode = el.closest(".prop");
    const box = propNode ? propNode.querySelector(".prop-value") : null;
    const detail = state.currentDetail;
    const prop = detail ? detail.props.find((p) => p.name === name) : null;
    if (box && prop) box.textContent = formatValue(prop, value);
    if (name === "on") {
      const dev = state.devices.find((d) => d.did === did);
      if (dev) dev.power = Boolean(value);
      renderDevices();
    }
  } catch (err) {
    toast(err.message, true);
    await loadPropValues(state.currentDetail);
  }
}

function renderActions(detail) {
  if (!detail.actions.length) return;
  const box = document.createElement("div");
  box.innerHTML = `<div class="section-title">动作</div>
    <div class="form-row">${detail.actions.map((a) =>
      `<button class="btn" data-action="${esc(a.name)}">${esc(a.desc || a.name)}</button>`).join("")}
    </div>`;
  $("drawer-body").appendChild(box);
  box.querySelectorAll("button[data-action]").forEach((el) => {
    el.addEventListener("click", () => {
      const name = el.dataset.action;
      const needsText = ["execute-text-directive", "play-text"].includes(name);
      let params = null;
      if (needsText) {
        const text = prompt("输入要执行的文本内容：");
        if (!text) return;
        params = [text, 0];
      }
      if (!confirm(`确认执行「${name}」？`)) return;
      runAction(detail.did, name, params);
    });
  });
}

async function runAction(did, name, params) {
  try {
    await api(`/api/devices/${encodeURIComponent(did)}/action`, {
      method: "POST", body: JSON.stringify({ name, params }),
    });
    toast("已执行");
  } catch (err) {
    toast(err.message, true);
  }
}

/* ---------- 渲染：场景 / 定时 / 语音 ---------- */

function renderScenes() {
  const box = $("scene-list");
  if (!state.scenes.length) {
    box.innerHTML = '<div class="empty">没有手动场景（可在米家 App 的「智能」里创建）</div>';
    return;
  }
  box.innerHTML = state.scenes.map((s) => `
    <div class="scene-card" data-scene="${esc(s.scene_id)}" data-home="${esc(s.home_id)}">
      <div class="dev-name">${esc(s.name)}</div>
      <div class="badge">${esc(s.home_name || "手动场景")}</div>
    </div>`).join("");
  box.querySelectorAll(".scene-card").forEach((el) => {
    el.addEventListener("click", () => runScene(el.dataset.scene, el.dataset.home));
  });
}

async function runScene(sceneId, homeId) {
  try {
    await api("/api/scenes/run", {
      method: "POST",
      body: JSON.stringify({ scene_id: sceneId, home_id: homeId, name: "" }),
    });
    toast("场景已执行");
  } catch (err) {
    toast(err.message, true);
  }
}

function renderScheduleDevices() {
  const sel = $("sch-device");
  const keep = sel.value;
  const list = state.devices.filter((d) => hasSwitch(d));
  sel.innerHTML = list.length
    ? list.map((d) => `<option value="${esc(d.did)}">${esc(d.name)}</option>`).join("")
    : '<option value="">（暂无可定时设备）</option>';
  if (keep && list.some((d) => d.did === keep)) sel.value = keep;
}

function renderSchedules() {
  const box = $("schedule-list");
  if (!state.schedules.length) {
    box.innerHTML = '<div class="empty">还没有定时任务</div>';
    return;
  }
  box.innerHTML = state.schedules.map((r) => `
    <div class="row">
      <div class="row-main">
        <div class="row-title">${esc(r.time)} · ${r.action === "on" ? "打开" : "关闭"}</div>
        <div class="row-sub">${esc(r.device_name)}
          ${r.enabled ? "" : " · 已停用"}</div>
      </div>
      <div class="row-actions">
        <button class="btn ghost" data-toggle="${esc(r.id)}">${r.enabled ? "停用" : "启用"}</button>
        <button class="btn danger ghost" data-del="${esc(r.id)}">删除</button>
      </div>
    </div>`).join("");
  box.querySelectorAll("[data-toggle]").forEach((el) => {
    el.addEventListener("click", async () => {
      const rule = state.schedules.find((r) => r.id === el.dataset.toggle);
      try {
        await api(`/api/schedules/${el.dataset.toggle}/enabled`, {
          method: "POST", body: JSON.stringify({ enabled: !rule.enabled }),
        });
        refreshSchedules();
      } catch (err) { toast(err.message, true); }
    });
  });
  box.querySelectorAll("[data-del]").forEach((el) => {
    el.addEventListener("click", async () => {
      if (!confirm("删除这条定时任务？")) return;
      try {
        await api(`/api/schedules/${el.dataset.del}`, { method: "DELETE" });
        refreshSchedules();
      } catch (err) { toast(err.message, true); }
    });
  });
}

function renderVoiceDevices() {
  const sel = $("voice-device");
  const keep = sel.value;
  const list = state.devices.filter((d) => isSpeaker(d) && d.online);
  sel.innerHTML = list.length
    ? list.map((d) => `<option value="${esc(d.did)}">${esc(d.name)}</option>`).join("")
    : '<option value="">（未发现在线小爱音箱）</option>';
  if (keep && list.some((d) => d.did === keep)) sel.value = keep;
}

async function sendVoice() {
  const did = $("voice-device").value;
  const text = $("voice-text").value.trim();
  if (!did) return toast("没有可用的小爱音箱", true);
  if (!text) return toast("请输入指令", true);
  try {
    await api("/api/voice", { method: "POST", body: JSON.stringify({ did, text }) });
    $("voice-text").value = "";
    toast("指令已发送");
  } catch (err) {
    toast(err.message, true);
  }
}

/* ---------- 事件绑定 ---------- */

document.querySelectorAll(".tab").forEach((el) => {
  el.addEventListener("click", () => {
    document.querySelectorAll(".tab").forEach((t) => t.classList.remove("active"));
    document.querySelectorAll(".tabpane").forEach((p) => p.classList.remove("active"));
    el.classList.add("active");
    $(`pane-${el.dataset.tab}`).classList.add("active");
  });
});

/* ---------- 主题 ---------- */

/* 实际生效的主题由 head 里的内联脚本首次写入（避免首屏闪配色），
   这里只负责读、切换和同步按钮文字。 */
function currentTheme() {
  return document.documentElement.getAttribute("data-theme") === "light" ? "light" : "dark";
}

function syncThemeButton() {
  $("btn-theme").textContent = currentTheme() === "light" ? "日间" : "夜间";
}

function applyTheme(theme) {
  document.documentElement.setAttribute("data-theme", theme);
  try { localStorage.setItem("mihome_theme", theme); } catch (e) { /* 隐私模式下忽略 */ }
  syncThemeButton();
}

$("btn-theme").addEventListener("click", () => {
  applyTheme(currentTheme() === "light" ? "dark" : "light");
});
syncThemeButton();

$("btn-refresh").addEventListener("click", async () => {
  try {
    await api("/api/refresh", { method: "POST" });
    setTimeout(refreshAll, 800);
  } catch (err) { toast(err.message, true); }
});
$("btn-logout").addEventListener("click", logout);
$("drawer-close").addEventListener("click", closeDetail);
$("drawer-mask").addEventListener("click", closeDetail);
$("btn-voice").addEventListener("click", sendVoice);
$("voice-text").addEventListener("keydown", (e) => { if (e.key === "Enter") sendVoice(); });
function onFilterChange() {
  saveFilters();
  renderDevices();
}
$("hide-switch").addEventListener("change", onFilterChange);
$("hide-grouped").addEventListener("change", onFilterChange);
$("hide-noctl").addEventListener("change", onFilterChange);
$("hide-offline").addEventListener("change", onFilterChange);

$("schedule-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  const did = $("sch-device").value;
  const time = $("sch-time").value;
  if (!did || !time) return toast("请选择设备与时间", true);
  try {
    await api("/api/schedules", {
      method: "POST",
      body: JSON.stringify({ did, time, action: $("sch-action").value }),
    });
    toast("已添加");
    refreshSchedules();
  } catch (err) { toast(err.message, true); }
});

/* ---------- 口令闸门 ---------- */

$("gate-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  const input = $("gate-input");
  const value = input.value.trim();
  if (!value) return;
  setToken(value);
  const btn = $("gate-submit");
  btn.disabled = true;
  // 先放行进界面再拉数据：设备列表要打云端，卡在闸门上等，
  // 按钮看着就像没反应（点了没跳转）。
  hideGate();
  $("device-groups").innerHTML = '<p class="muted small">正在加载设备…</p>';
  try {
    await refreshAll();
    input.value = "";
  } catch (err) {
    // 401 说明口令不对，退回去重输；其他错误（断网、后端挂了）只提示
    if (err && err.status === 401) {
      clearToken();
      $("device-groups").innerHTML = "";
      showGate(true);
      input.value = "";
    } else {
      $("device-groups").innerHTML = "";
      toast(err.message, true);
    }
  } finally {
    btn.disabled = false;
  }
});

/* ---------- 启动 ---------- */

// 必须在首次 renderDevices 之前恢复，否则会先按默认值渲染一遍再跳变
restoreFilters();

/* 启动时先问服务端"要不要口令"，不能只看本地有没有存过 token。
   口令被关掉后如果还弹输入框，用户随便敲一个也能进，只会以为哪里坏了。 */
async function boot() {
  let needAuth = true;              // 探测失败就按"需要"处理，选保守那一边
  try {
    const res = await fetch("/api/auth");
    const info = await res.json();
    needAuth = info.required !== false;
  } catch (err) { /* 网络异常：保持 needAuth = true */ }

  if (!needAuth) {
    clearToken();                   // 清掉残留的旧口令，别继续挂在请求头上
    hideGate();
    refreshAll().catch((err) => toast(err.message, true));
    return;
  }

  if (getToken()) {
    refreshAll().catch((err) => {
      if (err && err.status === 401) {
        clearToken();
        showGate(true);
      }
    });
  } else {
    showGate(false);
  }
}

boot();

// 轮询必须自己接住 rejected promise：口令被换掉后后端一直返 401，
// 不接就是每 5 秒一个未捕获异常，页面还停在旧数据上装没事
setInterval(() => {
  if (!state.loggedIn) return;
  refreshDevices().catch((err) => {
    if (err && err.status === 401) {
      state.loggedIn = false;
      clearToken();
      showGate(true);
    }
  });
}, REFRESH_MS);

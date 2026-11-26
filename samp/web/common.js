/* SAMP 前端公共模块：API、5 主题引擎、三语 i18n、全局异常报警闭环、统一矢量图标与模态确认框 */

const $ = (id) => document.getElementById(id);

const esc = (value) =>
  String(value ?? "").replace(/[&<>"']/g, (ch) => ({
    "&": "&amp;",
    "<": "&lt;",
    ">": "&gt;",
    '"': "&quot;",
    "'": "&#39;"
  }[ch]));

/* ===================== 统一 1.5px 线宽矢量图标集 ===================== */
const ICONS = {
  edit: '<svg class="ico ico-xs" viewBox="0 0 24 24"><path d="M12 20h9"/><path d="M16.5 3.5a2.1 2.1 0 0 1 3 3L7 19l-4 1 1-4Z"/></svg>',
  copy: '<svg class="ico ico-xs" viewBox="0 0 24 24"><rect x="9" y="9" width="13" height="13" rx="2"/><path d="M5 15H4a2 2 0 0 1-2-2V4a2 2 0 0 1 2-2h9a2 2 0 0 1 2 2v1"/></svg>',
  play: '<svg class="ico ico-xs" viewBox="0 0 24 24"><polygon points="6 4 20 12 6 20 6 4"/></svg>',
  chart: '<svg class="ico ico-xs" viewBox="0 0 24 24"><path d="M3 3v18h18"/><path d="m19 9-5 5-4-4-3 3"/></svg>',
  power: '<svg class="ico ico-xs" viewBox="0 0 24 24"><path d="M12 2v10"/><path d="M18.4 6.6a9 9 0 1 1-12.77.04"/></svg>',
  trash: '<svg class="ico ico-xs" viewBox="0 0 24 24"><path d="M3 6h18"/><path d="M19 6v14a2 2 0 0 1-2 2H7a2 2 0 0 1-2-2V6"/><path d="M8 6V4a2 2 0 0 1 2-2h4a2 2 0 0 1 2 2v2"/></svg>'
};

/* ===================== 多语言引擎（i18n） ===================== */
const LANG_STORAGE_KEY = "samp_lang";
const THEME_STORAGE_KEY = "samp_theme";
const ACK_STORAGE_KEY = "samp_acked_alarms";
const SIDEBAR_WIDTH_KEY = "samp_sidebar_width";
const SIDEBAR_COLLAPSED_KEY = "samp_sidebar_collapsed";
const SIDEBAR_DEFAULT_WIDTH = 188;
const SIDEBAR_MIN_WIDTH = 136;
const SIDEBAR_MAX_WIDTH = 320;
const SIDEBAR_SNAP_HIDE = 96;
const SUPPORTED_LANGS = ["zh-CN", "en-US", "ru-RU"];
const SUPPORTED_THEMES = [
  "minimalist-light",
  "precision-dark",
  "bento-grid",
  "glassmorphism",
  "editorial",
  "sci-fi-cyan",
  "sci-fi-purple",
  "sci-fi-matrix"
];
const _i18nListeners = [];
const _themeListeners = [];

function getLang() {
  const saved = localStorage.getItem(LANG_STORAGE_KEY);
  return SUPPORTED_LANGS.includes(saved) ? saved : "zh-CN";
}

function t(key, params = {}) {
  const dict = (window.SAMP_I18N && window.SAMP_I18N[getLang()]) || window.SAMP_I18N["zh-CN"] || {};
  const fallback = (window.SAMP_I18N && window.SAMP_I18N["zh-CN"]) || {};
  let text = dict[key] ?? fallback[key] ?? key;
  for (const [k, v] of Object.entries(params)) {
    text = text.replaceAll(`{${k}}`, String(v ?? ""));
  }
  return text;
}

function applyI18nToDOM(root = document) {
  document.documentElement.lang = getLang();
  const titleKey = document.body?.dataset?.titleKey;
  if (titleKey) {
    document.title = t(titleKey);
  }
  root.querySelectorAll("[data-i18n]").forEach((node) => {
    node.textContent = t(node.dataset.i18n);
  });
  root.querySelectorAll("[data-i18n-placeholder]").forEach((node) => {
    node.placeholder = t(node.dataset.i18nPlaceholder);
  });
  root.querySelectorAll("[data-i18n-title]").forEach((node) => {
    node.title = t(node.dataset.i18nTitle);
  });
}

function setLang(lang) {
  if (!SUPPORTED_LANGS.includes(lang)) return;
  localStorage.setItem(LANG_STORAGE_KEY, lang);
  applyI18nToDOM();
  renderAlarmUI();
  _i18nListeners.forEach((fn) => fn(lang));
}

function onLangChange(fn) {
  _i18nListeners.push(fn);
}

/* ===================== 5 主题风格预设引擎 ===================== */
function getTheme() {
  const saved = localStorage.getItem(THEME_STORAGE_KEY);
  return SUPPORTED_THEMES.includes(saved) ? saved : "minimalist-light";
}

function applyTheme(theme) {
  const target = SUPPORTED_THEMES.includes(theme) ? theme : "minimalist-light";
  document.documentElement.setAttribute("data-theme", target);
  localStorage.setItem(THEME_STORAGE_KEY, target);
  const sel = $("theme-select");
  if (sel) sel.value = target;
  _themeListeners.forEach((fn) => fn(target));
}

function onThemeChange(fn) {
  _themeListeners.push(fn);
}

/* ===================== 分级异常与确认状态闭环（确认 ≠ 恢复） ===================== */
let _activeAlarms = [];

function _getAckedSet() {
  try {
    return new Set(JSON.parse(sessionStorage.getItem(ACK_STORAGE_KEY) || "[]"));
  } catch {
    return new Set();
  }
}

function _saveAckedSet(set) {
  try {
    sessionStorage.setItem(ACK_STORAGE_KEY, JSON.stringify([...set]));
  } catch {}
}

function _ensureAlarmDrawer() {
  let backdrop = $("samp-alarm-backdrop");
  if (backdrop) return backdrop;
  backdrop = document.createElement("div");
  backdrop.id = "samp-alarm-backdrop";
  backdrop.className = "drawer-backdrop";
  backdrop.innerHTML = `
    <div class="drawer drawer-alarm" role="dialog" aria-modal="true">
      <div class="drawer-head">
        <h2 id="samp-alarm-title"></h2>
        <div class="toolbar-group">
          <button type="button" class="btn xs" id="samp-alarm-ack-all"></button>
          <button type="button" class="btn ghost xs" id="samp-alarm-close"></button>
        </div>
      </div>
      <div class="drawer-body" id="samp-alarm-list"></div>
    </div>
  `;
  document.body.appendChild(backdrop);
  $("samp-alarm-close").addEventListener("click", () => backdrop.classList.remove("open"));
  $("samp-alarm-ack-all").addEventListener("click", () => {
    const acked = _getAckedSet();
    _activeAlarms.forEach((a) => acked.add(a.sig));
    _saveAckedSet(acked);
    renderAlarmUI();
  });
  $("samp-alarm-list").addEventListener("click", (e) => {
    const btn = e.target.closest("[data-ack-sig]");
    if (!btn) return;
    const acked = _getAckedSet();
    acked.add(btn.dataset.ackSig);
    _saveAckedSet(acked);
    renderAlarmUI();
  });
  return backdrop;
}

function renderAlarmUI() {
  const pill = $("alarm-pill");
  if (!pill) return;
  const count = _activeAlarms.length;
  if (!count) {
    pill.classList.add("hidden");
    const backdrop = $("samp-alarm-backdrop");
    if (backdrop) backdrop.classList.remove("open");
    return;
  }
  const acked = _getAckedSet();
  const unacked = _activeAlarms.filter((a) => !acked.has(a.sig)).length;
  pill.classList.remove("hidden");
  pill.classList.toggle("acked", unacked === 0);
  pill.innerHTML = `<span class="alarm-dot"></span><span>${esc(t("alarm.badge", { n: count }))}</span>`;

  const backdrop = $("samp-alarm-backdrop");
  if (!backdrop) return;
  $("samp-alarm-title").textContent = `${t("alarm.title")} (${count})`;
  $("samp-alarm-ack-all").textContent = t("alarm.ack_all");
  $("samp-alarm-close").textContent = t("action.close");
  $("samp-alarm-list").innerHTML = _activeAlarms
    .map((a) => {
      const isAcked = acked.has(a.sig);
      const locHref = a.id ? `/definitions?edit=${a.id}` : "/runtime#logs";
      return `
        <div class="alarm-card">
          <div class="alarm-card-head">
            <strong>${esc(a.title)}</strong>
            <span class="mono text-muted">${esc(formatTime(a.time, true))}</span>
          </div>
          <div class="alarm-card-msg">${esc(a.message)}</div>
          <div class="alarm-card-foot">
            <a class="btn xs" href="${locHref}">${esc(t("alarm.locate"))}</a>
            ${
              isAcked
                ? `<span class="badge warn">${esc(t("alarm.acked"))}</span>`
                : `<button type="button" class="btn xs" data-ack-sig="${esc(a.sig)}">${esc(t("alarm.ack"))}</button>`
            }
          </div>
        </div>`;
    })
    .join("");
}

function syncAlarmsFromStatus(statusData) {
  const alarms = [];
  const items = statusData?.items || [];
  for (const item of items) {
    if (item.last_status === "error" && item.last_error) {
      alarms.push({
        id: item.id,
        title: item.name,
        time: item.last_run_at,
        message: item.last_error,
        sig: `def:${item.id}:${item.last_error}`
      });
    }
  }
  const sched = statusData?.scheduler || {};
  if (!alarms.length && sched.failed > 0 && sched.last_error && !sched.last_success_at) {
    alarms.push({
      id: null,
      title: t("alarm.scheduler_err"),
      time: sched.last_cycle_at,
      message: sched.last_error,
      sig: `sched:${sched.last_error}`
    });
  }
  // 故障恢复后自动从已确认集合中移除归档
  const activeSigs = new Set(alarms.map((a) => a.sig));
  const acked = _getAckedSet();
  [...acked].forEach((sig) => {
    if (!activeSigs.has(sig)) acked.delete(sig);
  });
  _saveAckedSet(acked);
  _activeAlarms = alarms;
  renderAlarmUI();
}

async function pollAlarms() {
  try {
    const data = await api("/api/status");
    syncAlarmsFromStatus(data);
  } catch {}
}

/* ===================== 左侧目录显隐与可拖拽分隔符 ===================== */
function initSidebar() {
  const shell = document.querySelector(".app-shell");
  if (!shell) return;
  const resizer = $("sidebar-resizer");
  const toggleButtons = document.querySelectorAll("[data-sidebar-toggle]");

  const clampWidth = (w) => Math.max(SIDEBAR_MIN_WIDTH, Math.min(SIDEBAR_MAX_WIDTH, Math.round(w)));

  const setWidth = (w) => {
    const val = clampWidth(w);
    shell.style.setProperty("--sidebar-width", `${val}px`);
    localStorage.setItem(SIDEBAR_WIDTH_KEY, String(val));
  };

  const setCollapsed = (collapsed) => {
    shell.classList.toggle("sidebar-collapsed", Boolean(collapsed));
    localStorage.setItem(SIDEBAR_COLLAPSED_KEY, collapsed ? "1" : "0");
    window.dispatchEvent(new Event("resize"));
  };

  const savedW = Number(localStorage.getItem(SIDEBAR_WIDTH_KEY));
  if (Number.isFinite(savedW) && savedW >= SIDEBAR_MIN_WIDTH) {
    shell.style.setProperty("--sidebar-width", `${clampWidth(savedW)}px`);
  }
  if (localStorage.getItem(SIDEBAR_COLLAPSED_KEY) === "1") {
    shell.classList.add("sidebar-collapsed");
  }

  toggleButtons.forEach((btn) => {
    btn.addEventListener("click", () => {
      setCollapsed(!shell.classList.contains("sidebar-collapsed"));
    });
  });

  if (resizer) {
    let dragging = false;
    resizer.addEventListener("mousedown", (event) => {
      if (event.button !== 0) return;
      event.preventDefault();
      dragging = true;
      document.body.classList.add("resizing-sidebar");
    });

    window.addEventListener("mousemove", (event) => {
      if (!dragging) return;
      if (event.clientX < SIDEBAR_SNAP_HIDE) {
        dragging = false;
        document.body.classList.remove("resizing-sidebar");
        setCollapsed(true);
        return;
      }
      setWidth(event.clientX);
    });

    window.addEventListener("mouseup", () => {
      if (!dragging) return;
      dragging = false;
      document.body.classList.remove("resizing-sidebar");
      window.dispatchEvent(new Event("resize"));
    });

    resizer.addEventListener("dblclick", () => {
      setCollapsed(false);
      setWidth(SIDEBAR_DEFAULT_WIDTH);
      window.dispatchEvent(new Event("resize"));
    });
  }
}

function initChrome({ pollGlobalAlarms = true } = {}) {
  applyTheme(getTheme());
  initSidebar();
  const themeSelect = $("theme-select");
  if (themeSelect) {
    themeSelect.value = getTheme();
    themeSelect.addEventListener("change", (event) => applyTheme(event.target.value));
  }
  const langSelect = $("lang-select");
  if (langSelect) {
    langSelect.value = getLang();
    langSelect.addEventListener("change", (event) => setLang(event.target.value));
  }
  const pill = $("alarm-pill");
  if (pill) {
    pill.addEventListener("click", () => {
      const backdrop = _ensureAlarmDrawer();
      renderAlarmUI();
      backdrop.classList.add("open");
    });
  }
  applyI18nToDOM();
  if (pollGlobalAlarms) {
    pollAlarms();
    setInterval(pollAlarms, 20000);
  }
}

/* ===================== HTTP 与反馈 ===================== */
async function api(url, options = {}) {
  const response = await fetch(url, options);
  let body = {};
  try {
    body = await response.json();
  } catch {
    body = {};
  }
  if (!response.ok) {
    throw new Error(body.detail || `HTTP ${response.status}`);
  }
  return body;
}

let _toastTimer = null;
function toast(message, kind = "info") {
  const node = $("toast");
  if (!node) return;
  node.textContent = message;
  node.className = `toast show ${kind === true || kind === "error" ? "error" : kind === "ok" ? "ok" : ""}`;
  clearTimeout(_toastTimer);
  _toastTimer = setTimeout(() => node.classList.remove("show"), 3200);
}

/* ===================== 高危模态确认弹窗（默认聚焦取消，禁点遮罩误关） ===================== */
function confirmModal({ title, body, confirmText }) {
  return new Promise((resolve) => {
    let backdrop = $("samp-confirm-backdrop");
    if (!backdrop) {
      backdrop = document.createElement("div");
      backdrop.id = "samp-confirm-backdrop";
      backdrop.className = "modal-backdrop";
      backdrop.innerHTML = `
        <div class="modal" role="dialog" aria-modal="true">
          <div class="modal-head" id="samp-confirm-title"></div>
          <div class="modal-body" id="samp-confirm-body"></div>
          <div class="modal-foot">
            <button type="button" class="btn" id="samp-confirm-cancel"></button>
            <button type="button" class="btn danger-solid" id="samp-confirm-ok"></button>
          </div>
        </div>
      `;
      document.body.appendChild(backdrop);
    }
    const titleEl = $("samp-confirm-title");
    const bodyEl = $("samp-confirm-body");
    const cancelBtn = $("samp-confirm-cancel");
    const okBtn = $("samp-confirm-ok");

    titleEl.textContent = title || t("action.confirm");
    bodyEl.textContent = body || "";
    cancelBtn.textContent = t("action.cancel");
    okBtn.textContent = confirmText || t("action.delete");
    backdrop.classList.add("open");

    const cleanup = (answer) => {
      backdrop.classList.remove("open");
      cancelBtn.removeEventListener("click", onCancel);
      okBtn.removeEventListener("click", onOk);
      document.removeEventListener("keydown", onKey);
      resolve(answer);
    };
    const onCancel = () => cleanup(false);
    const onOk = () => cleanup(true);
    const onKey = (event) => {
      if (event.key === "Escape") cleanup(false);
    };
    cancelBtn.addEventListener("click", onCancel);
    okBtn.addEventListener("click", onOk);
    document.addEventListener("keydown", onKey);
    cancelBtn.focus();
  });
}

/* ===================== 时间与数值格式化 ===================== */
function pad2(n) {
  return String(n).padStart(2, "0");
}

function toLocalDateTimeInput(epochSeconds) {
  const date = new Date(epochSeconds * 1000);
  return `${date.getFullYear()}-${pad2(date.getMonth() + 1)}-${pad2(date.getDate())}T${pad2(date.getHours())}:${pad2(date.getMinutes())}:${pad2(date.getSeconds())}`;
}

function parseDateTimeInput(value) {
  if (!value) return NaN;
  return Math.floor(new Date(value).getTime() / 1000);
}

function formatTime(epochSeconds, compact = false) {
  if (epochSeconds === null || epochSeconds === undefined) return t("empty.dash");
  const date = new Date(Number(epochSeconds) * 1000);
  if (Number.isNaN(date.getTime())) return t("empty.dash");
  const datePart = `${date.getFullYear()}-${pad2(date.getMonth() + 1)}-${pad2(date.getDate())}`;
  const timePart = `${pad2(date.getHours())}:${pad2(date.getMinutes())}:${pad2(date.getSeconds())}`;
  return compact ? `${pad2(date.getMonth() + 1)}-${pad2(date.getDate())} ${timePart}` : `${datePart} ${timePart}`;
}

function formatBytes(bytes) {
  if (bytes === null || bytes === undefined) return t("empty.dash");
  const n = Number(bytes);
  if (!Number.isFinite(n)) return t("empty.dash");
  if (n < 1024) return `${n} B`;
  if (n < 1024 * 1024) return `${(n / 1024).toFixed(1)} KB`;
  if (n < 1024 * 1024 * 1024) return `${(n / (1024 * 1024)).toFixed(2)} MB`;
  return `${(n / (1024 * 1024 * 1024)).toFixed(2)} GB`;
}

function formatNumber(val) {
  if (val === null || val === undefined || val === "") return t("empty.dash");
  const num = Number(val);
  if (!Number.isFinite(num)) return String(val);
  if (Number.isInteger(num)) return String(num);
  return Number(num.toFixed(4)).toString();
}

function checkedFields(container) {
  return [...container.querySelectorAll("input[type=checkbox]:checked")].map((input) => input.value);
}

function setAllFields(container, checked) {
  container.querySelectorAll("input[type=checkbox]").forEach((input) => {
    input.checked = checked;
  });
  container.dispatchEvent(new Event("change"));
}

function renderFieldChecks(container, fields, checked, fieldTypes = {}, fieldLabels = {}, filterKeyword = "") {
  const kw = (filterKeyword || "").trim().toLowerCase();
  const list = (fields || []).filter((f) => f !== "row_key" && f !== "sample_time");
  if (!list.length) {
    container.innerHTML = `<span class="text-muted">${esc(t("empty.dash"))}</span>`;
    return;
  }
  const checkedSet = new Set(checked || []);
  container.innerHTML = list
    .map((field) => {
      const chn = fieldLabels[field] || "";
      const hidden = kw && !field.toLowerCase().includes(kw) && !chn.toLowerCase().includes(kw);
      const labelHtml = chn ? `<span class="field-chn">${esc(chn)}</span>` : "";
      const fType = fieldTypes[field] ? `<span class="field-type-tag">${esc(fieldTypes[field].slice(0, 3))}</span>` : "";
      return `<label class="field-pill ${hidden ? "hidden" : ""}" title="${esc(chn ? `${field} · ${chn}` : field)}"><input type="checkbox" value="${esc(field)}" ${checkedSet.has(field) ? "checked" : ""}><span>${esc(field)}</span>${labelHtml}${fType}</label>`;
    })
    .join("");
}

window.SAMP = {
  $,
  esc,
  t,
  ICONS,
  getLang,
  setLang,
  onLangChange,
  applyI18nToDOM,
  getTheme,
  applyTheme,
  onThemeChange,
  syncAlarmsFromStatus,
  initChrome,
  api,
  toast,
  confirmModal,
  toLocalDateTimeInput,
  parseDateTimeInput,
  formatTime,
  formatBytes,
  formatNumber,
  checkedFields,
  setAllFields,
  renderFieldChecks
};

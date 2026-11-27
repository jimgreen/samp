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
  trash: '<svg class="ico ico-xs" viewBox="0 0 24 24"><path d="M3 6h18"/><path d="M19 6v14a2 2 0 0 1-2 2H7a2 2 0 0 1-2-2V6"/><path d="M8 6V4a2 2 0 0 1 2-2h4a2 2 0 0 1 2 2v2"/></svg>',
  eye: '<svg class="ico ico-xs" viewBox="0 0 24 24"><path d="M2 12s3.5-7 10-7 10 7 10 7-3.5 7-10 7-10-7-10-7Z"/><circle cx="12" cy="12" r="3"/></svg>',
  compress: '<svg class="ico ico-xs" viewBox="0 0 24 24"><path d="M4 14h6v6"/><path d="M20 10h-6V4"/><path d="M14 10l7-7"/><path d="M3 21l7-7"/></svg>',
  refresh: '<svg class="ico ico-xs" viewBox="0 0 24 24"><path d="M21 12a9 9 0 1 1-2.64-6.36"/><path d="M21 3v6h-6"/></svg>',
  download: '<svg class="ico ico-xs" viewBox="0 0 24 24"><path d="M12 3v12"/><path d="m7 10 5 5 5-5"/><path d="M4 20h16"/></svg>'
};

const THEME_SWATCHES = {
  "minimalist-light": { key: "theme.light", bg: "#F4F6F8", accent: "#2563EB", group: "light" },
  "arctic-frost": { key: "theme.arctic", bg: "#F0F6FB", accent: "#0284C7", group: "light" },
  "warm-paper": { key: "theme.warm", bg: "#F6F2E9", accent: "#15803D", group: "light" },
  "slate-daylight": { key: "theme.slate", bg: "#EDF1F5", accent: "#1D4ED8", group: "light" },
  "bento-grid": { key: "theme.bento", bg: "#F5F5F7", accent: "#0071E3", group: "light" },
  editorial: { key: "theme.editorial", bg: "#FAF9F6", accent: "#9F1239", group: "light" },
  "precision-dark": { key: "theme.dark", bg: "#0B0F19", accent: "#38BDF8", group: "dark" },
  "ocean-slate": { key: "theme.ocean", bg: "#071422", accent: "#38BDF8", group: "dark" },
  "nord-dark": { key: "theme.nord", bg: "#242933", accent: "#88C0D0", group: "dark" },
  "industrial-amber": { key: "theme.amber", bg: "#121110", accent: "#F59E0B", group: "dark" },
  glassmorphism: { key: "theme.glass", bg: "#0F172A", accent: "#60A5FA", group: "dark" },
  "sci-fi-cyan": { key: "theme.cyan", bg: "#040812", accent: "#00E5FF", group: "dark" },
  "sci-fi-purple": { key: "theme.purple", bg: "#090614", accent: "#C084FC", group: "dark" },
  "sci-fi-matrix": { key: "theme.matrix", bg: "#040D08", accent: "#00E676", group: "dark" }
};

/* ===================== 多语言引擎（i18n） ===================== */
const LANG_STORAGE_KEY = "samp_lang";
const THEME_STORAGE_KEY = "samp_theme";
const ACK_STORAGE_KEY = "samp_acked_alarms";
const SIDEBAR_WIDTH_KEY = "samp_sidebar_width";
const SIDEBAR_COLLAPSED_KEY = "samp_sidebar_collapsed";
const SIDEBAR_DEFAULT_WIDTH = 192;
const SIDEBAR_MIN_WIDTH = 148;
const SIDEBAR_MAX_WIDTH = 320;
const SIDEBAR_SNAP_HIDE = 96;
const SUPPORTED_LANGS = ["zh-CN", "en-US", "ru-RU"];
const SUPPORTED_THEMES = Object.keys(THEME_SWATCHES);
const DARK_THEMES = new Set(SUPPORTED_THEMES.filter((k) => THEME_SWATCHES[k].group === "dark"));
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
  syncThemePickerUI();
  renderAlarmUI();
  _i18nListeners.forEach((fn) => fn(lang));
}

function onLangChange(fn) {
  _i18nListeners.push(fn);
}

/* ===================== 14 主题与三语统一紧凑选择器 ===================== */
const LANG_SHORT = { "zh-CN": "中", "en-US": "EN", "ru-RU": "RU" };
const LANG_FULL = { "zh-CN": "中文", "en-US": "EN", "ru-RU": "RU" };
let _themePickerNodes = null;

function _swatchDotsHtml(theme) {
  const sw = THEME_SWATCHES[theme] || THEME_SWATCHES["minimalist-light"];
  return `<span class="theme-dot-pair"><span class="theme-dot" style="background:${sw.bg}"></span><span class="theme-dot" style="background:${sw.accent}"></span></span>`;
}

function syncThemePickerUI() {
  if (!_themePickerNodes) return;
  const cur = getTheme();
  const lang = getLang();
  const sw = THEME_SWATCHES[cur] || THEME_SWATCHES["minimalist-light"];
  const shortLang = LANG_SHORT[lang] || "中";
  _themePickerNodes.btn.innerHTML = `${_swatchDotsHtml(cur)}<span class="pref-lang-code">${esc(shortLang)}</span><span class="tree-caret">▾</span>`;
  _themePickerNodes.btn.title = `${t(sw.key)} · ${LANG_FULL[lang] || lang}`;

  const langBar = SUPPORTED_LANGS.map((code) => {
    const active = code === lang ? " active" : "";
    return `<button type="button" class="pref-lang-btn${active}" data-lang-id="${code}">${esc(LANG_FULL[code] || code)}</button>`;
  }).join("");

  const lightIds = SUPPORTED_THEMES.filter((id) => THEME_SWATCHES[id].group === "light");
  const darkIds = SUPPORTED_THEMES.filter((id) => THEME_SWATCHES[id].group === "dark");
  const renderOpts = (ids) =>
    ids
      .map((id) => {
        const meta = THEME_SWATCHES[id];
        const active = id === cur ? " active" : "";
        return `<button type="button" class="theme-opt${active}" data-theme-id="${id}">${_swatchDotsHtml(id)}<span>${esc(t(meta.key))}</span></button>`;
      })
      .join("");

  _themePickerNodes.menu.innerHTML =
    `<div class="pref-lang-row"><span class="pref-section-label">${esc(t("lang.label"))}</span><div class="pref-lang-seg">${langBar}</div></div>` +
    `<div class="theme-group-title">${esc(t("theme.group_light"))}</div><div class="theme-grid">${renderOpts(lightIds)}</div>` +
    `<div class="theme-group-title">${esc(t("theme.group_dark"))}</div><div class="theme-grid">${renderOpts(darkIds)}</div>`;

  const langSel = $("lang-select");
  if (langSel && langSel.value !== lang) langSel.value = lang;
}

function initThemePicker(themeSelect, langSelect) {
  if (!themeSelect || _themePickerNodes) return;
  const wrap = document.createElement("div");
  wrap.className = "theme-picker";
  const btn = document.createElement("button");
  btn.type = "button";
  btn.id = "theme-picker-btn";
  btn.className = "theme-swatch-btn pref-trigger";
  const menu = document.createElement("div");
  menu.id = "theme-picker-menu";
  menu.className = "theme-menu pref-menu";

  themeSelect.classList.add("hidden");
  if (langSelect) langSelect.classList.add("hidden");
  themeSelect.parentNode.insertBefore(wrap, themeSelect);
  wrap.appendChild(btn);
  wrap.appendChild(menu);
  _themePickerNodes = { wrap, btn, menu };
  syncThemePickerUI();

  btn.addEventListener("click", (event) => {
    event.stopPropagation();
    menu.classList.toggle("open");
  });
  menu.addEventListener("click", (event) => {
    const langOpt = event.target.closest("[data-lang-id]");
    if (langOpt) {
      setLang(langOpt.dataset.langId);
      return;
    }
    const opt = event.target.closest("[data-theme-id]");
    if (!opt) return;
    applyTheme(opt.dataset.themeId);
    menu.classList.remove("open");
  });
  document.addEventListener("click", (event) => {
    if (!wrap.contains(event.target)) menu.classList.remove("open");
  });
  document.addEventListener("keydown", (event) => {
    if (event.key === "Escape") menu.classList.remove("open");
  });
}

function getTheme() {
  const saved = localStorage.getItem(THEME_STORAGE_KEY);
  return SUPPORTED_THEMES.includes(saved) ? saved : "minimalist-light";
}

function applyTheme(theme) {
  const target = SUPPORTED_THEMES.includes(theme) ? theme : "minimalist-light";
  document.documentElement.setAttribute("data-theme", target);
  document.documentElement.setAttribute("data-color-scheme", DARK_THEMES.has(target) ? "dark" : "light");
  localStorage.setItem(THEME_STORAGE_KEY, target);
  const sel = $("theme-select");
  if (sel) sel.value = target;
  syncThemePickerUI();
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

/* ===================== 全站统一左栏双模式（悬浮/内嵌 + 均可隐藏）与指挥舱桥时钟 ===================== */
const SIDEBAR_MODE_KEY = "samp_sidebar_mode";
const SIDEBAR_HIDDEN_KEY = "samp_sidebar_hidden";

function initPortalSidebar() {
  const sidebar = $("portal-sidebar");
  const edge = $("sidebar-edge-zone");
  if (!sidebar || sidebar.dataset.portalBound) return;
  sidebar.dataset.portalBound = "1";

  const root = document.documentElement;
  const toggleBtn = $("btn-sidebar-toggle");
  const hideBtn = $("btn-sidebar-hide");
  const modeButtons = [...sidebar.querySelectorAll("[data-sidebar-mode-btn]")];

  const getMode = () => (localStorage.getItem(SIDEBAR_MODE_KEY) === "docked" ? "docked" : "floating");
  const getDockedHidden = () => localStorage.getItem(SIDEBAR_HIDDEN_KEY) === "1";

  let hideTimer = null;
  let suppressUntil = 0;

  const cancelHide = () => {
    if (hideTimer) {
      clearTimeout(hideTimer);
      hideTimer = null;
    }
  };

  const notifyLayoutResize = () => {
    requestAnimationFrame(() => window.dispatchEvent(new Event("resize")));
    setTimeout(() => window.dispatchEvent(new Event("resize")), 220);
  };

  const syncUI = () => {
    const mode = getMode();
    const hidden = mode === "docked" ? getDockedHidden() : !sidebar.classList.contains("open");
    root.setAttribute("data-sidebar-mode", mode);
    root.setAttribute("data-sidebar-hidden", mode === "docked" && hidden ? "1" : "0");
    modeButtons.forEach((btn) => {
      btn.classList.toggle("active", btn.dataset.sidebarModeBtn === mode);
    });
    if (toggleBtn) {
      toggleBtn.classList.toggle("active", !hidden);
    }
  };

  const openFloating = () => {
    if (getMode() !== "floating") return;
    if (Date.now() < suppressUntil) return;
    cancelHide();
    if (!sidebar.classList.contains("open")) {
      sidebar.classList.add("open");
      syncUI();
    }
  };

  const closeFloating = (withCooldown = false) => {
    cancelHide();
    if (withCooldown) suppressUntil = Date.now() + 360;
    if (sidebar.classList.contains("open")) {
      sidebar.classList.remove("open");
      syncUI();
    }
  };

  const scheduleHideFloating = (delay = 220) => {
    if (getMode() !== "floating") return;
    cancelHide();
    hideTimer = setTimeout(() => {
      sidebar.classList.remove("open");
      syncUI();
    }, delay);
  };

  const setMode = (nextMode) => {
    const mode = nextMode === "docked" ? "docked" : "floating";
    localStorage.setItem(SIDEBAR_MODE_KEY, mode);
    cancelHide();
    if (mode === "docked") {
      localStorage.setItem(SIDEBAR_HIDDEN_KEY, "0");
      sidebar.classList.remove("open");
    } else {
      sidebar.classList.add("open");
    }
    syncUI();
    notifyLayoutResize();
  };

  const setDockedHidden = (hidden) => {
    localStorage.setItem(SIDEBAR_HIDDEN_KEY, hidden ? "1" : "0");
    syncUI();
    notifyLayoutResize();
  };

  const hideSidebar = () => {
    if (getMode() === "docked") {
      setDockedHidden(true);
    } else {
      closeFloating(true);
    }
  };

  const toggleSidebar = () => {
    if (getMode() === "docked") {
      setDockedHidden(!getDockedHidden());
    } else if (sidebar.classList.contains("open")) {
      closeFloating(true);
    } else {
      suppressUntil = 0;
      openFloating();
    }
  };

  // 按钮绑定
  modeButtons.forEach((btn) => {
    btn.addEventListener("click", () => setMode(btn.dataset.sidebarModeBtn));
  });
  toggleBtn?.addEventListener("click", toggleSidebar);
  hideBtn?.addEventListener("click", hideSidebar);

  // 左边缘感应热区（悬浮模式滑出；内嵌已隐藏时点击一键恢复）
  if (edge) {
    edge.addEventListener("mouseenter", openFloating);
    edge.addEventListener("mouseleave", () => scheduleHideFloating(220));
    edge.addEventListener("click", () => {
      if (getMode() === "docked" && getDockedHidden()) {
        setDockedHidden(false);
      } else {
        suppressUntil = 0;
        openFloating();
      }
    });
  }

  sidebar.addEventListener("mouseenter", openFloating);
  sidebar.addEventListener("mouseleave", () => scheduleHideFloating(220));
  sidebar.addEventListener("focusin", openFloating);
  sidebar.addEventListener("focusout", (event) => {
    if (!sidebar.contains(event.relatedTarget)) scheduleHideFloating(160);
  });

  window.addEventListener("mousemove", (event) => {
    if (getMode() === "floating" && event.clientY >= 54 && event.clientX <= 14) {
      openFloating();
    }
  });
  document.addEventListener("mouseleave", () => scheduleHideFloating(160));
  document.addEventListener("keydown", (event) => {
    if (event.key !== "Escape") return;
    if (document.querySelector(".drawer-backdrop.open, .modal-backdrop.open")) return;
    if (getMode() === "floating" && sidebar.classList.contains("open")) {
      closeFloating(true);
    }
  });

  syncUI();
}

let _clockTimer = null;
function initBridgeClock() {
  const el = $("dash-clock");
  if (!el) return;
  const tick = () => {
    const now = new Date();
    el.textContent = `${pad2(now.getHours())}:${pad2(now.getMinutes())}:${pad2(now.getSeconds())}`;
  };
  tick();
  if (_clockTimer) clearInterval(_clockTimer);
  _clockTimer = setInterval(tick, 1000);
}

/* ===================== 顶栏 Web 连接巡检（对齐 SCADA workbench.js） ===================== */
let _connOk = null;

function renderConnStatus() {
  const node = $("web-conn-status");
  if (!node) return;
  if (_connOk === true) {
    node.className = "conn-status ok";
    node.textContent = t("conn.ok");
  } else if (_connOk === false) {
    node.className = "conn-status error";
    node.textContent = t("conn.err");
  } else {
    node.className = "conn-status";
    node.textContent = t("conn.checking");
  }
}

async function pollConnHealth() {
  const node = $("web-conn-status");
  if (!node) return;
  try {
    const res = await fetch("/api/health");
    const data = res.ok ? await res.json() : null;
    _connOk = Boolean(data && data.status === "ok");
    if (data && data.status === "ok" && !document.body.classList.contains("dash-body")) {
      const sched = data.scheduler || {};
      const sideSched = $("side-sched");
      if (sideSched) {
        if (!sched.enabled) {
          sideSched.textContent = t("state.disabled_sched");
          sideSched.className = "warn";
        } else if (sched.last_error) {
          sideSched.textContent = t("state.error");
          sideSched.className = "danger";
        } else {
          sideSched.textContent = t("state.running");
          sideSched.className = "ok";
        }
      }
      const sideEnabled = $("side-enabled");
      if (sideEnabled && data.definitions !== undefined) {
        sideEnabled.textContent = `${data.enabled ?? 0} / ${data.definitions ?? 0}`;
      }
      const sideDbs = $("side-dbs");
      const dbCount = data.result_databases ?? data.result_dbs;
      if (sideDbs && dbCount !== undefined) {
        sideDbs.textContent = String(dbCount);
      }
    }
  } catch {
    _connOk = false;
  }
  renderConnStatus();
}

function initChrome({ pollGlobalAlarms = true } = {}) {
  applyTheme(getTheme());
  initSidebar();
  initPortalSidebar();
  initBridgeClock();
  const topbar = document.querySelector(".portal-topbar, .workbench-topbar, .topbar");
  if (topbar && !$("top-progress")) {
    const bar = document.createElement("div");
    bar.id = "top-progress";
    bar.className = "top-progress";
    topbar.appendChild(bar);
  }
  const themeSelect = $("theme-select");
  const langSelect = $("lang-select");
  if (langSelect) {
    langSelect.value = getLang();
    langSelect.addEventListener("change", (event) => setLang(event.target.value));
  }
  if (themeSelect) {
    themeSelect.value = getTheme();
    themeSelect.addEventListener("change", (event) => applyTheme(event.target.value));
    initThemePicker(themeSelect, langSelect);
  }
  const pill = $("alarm-pill");
  if (pill) {
    pill.addEventListener("click", () => {
      const backdrop = _ensureAlarmDrawer();
      renderAlarmUI();
      backdrop.classList.add("open");
    });
  }
  // 全局 `/` 快捷键聚焦当前页面首个可见搜索框
  document.addEventListener("keydown", (event) => {
    if (event.key !== "/" || event.ctrlKey || event.metaKey || event.altKey) return;
    const tag = (document.activeElement?.tagName || "").toUpperCase();
    if (tag === "INPUT" || tag === "TEXTAREA" || tag === "SELECT") return;
    if (document.querySelector(".drawer-backdrop.open, .modal-backdrop.open")) return;
    const target = [...document.querySelectorAll(".ctrl-search, input[type=search]")].find(
      (el) => el.offsetParent !== null && !el.disabled
    );
    if (target) {
      event.preventDefault();
      target.focus();
      target.select?.();
    }
  });
  applyI18nToDOM();
  syncThemePickerUI();
  renderConnStatus();
  pollConnHealth();
  setInterval(() => {
    if (!document.hidden) pollConnHealth();
  }, 15000);
  onLangChange(renderConnStatus);
  if (pollGlobalAlarms) {
    pollAlarms();
    setInterval(pollAlarms, 20000);
  }
}

/* ===================== HTTP 与全局在途进度条 ===================== */
let _inflight = 0;

function _updateTopProgress() {
  const bar = $("top-progress");
  if (!bar) return;
  bar.classList.toggle("active", _inflight > 0);
}

async function api(url, options = {}) {
  _inflight += 1;
  _updateTopProgress();
  try {
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
  } finally {
    _inflight = Math.max(0, _inflight - 1);
    _updateTopProgress();
  }
}

/* ===================== 轻量过滤条件客户端预检 ===================== */
const _FILTER_CLAUSE_RE =
  /^([A-Za-z_][A-Za-z0-9_]*)\s*(=|==|!=|<>|>=|<=|>|<|contains|not\s+contains|~)\s*(.+)$/i;

function validateFilterExpr(expr) {
  const text = String(expr || "").trim();
  if (!text) return true;
  // 按顶层 and / or 切分并检查每条原子子句格式
  const parts = text.split(/\s+(?:and|or)\s+/i);
  for (const raw of parts) {
    const clause = raw.trim().replace(/^\(+/, "").replace(/\)+$/, "").trim();
    if (!clause) return false;
    if (!_FILTER_CLAUSE_RE.test(clause)) return false;
  }
  // 检查括号配对
  let depth = 0;
  for (const ch of text) {
    if (ch === "(") depth += 1;
    else if (ch === ")") depth -= 1;
    if (depth < 0) return false;
  }
  return depth === 0;
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

function formatFixedPoint(unit, pt) {
  const mm = pad2(pt?.minute ?? 0);
  const ss = pad2(pt?.second ?? 0);
  if (unit === "hour") return `${mm}:${ss}`;
  const hh = pad2(pt?.hour ?? 0);
  if (unit === "day") return `${hh}:${mm}:${ss}`;
  if (unit === "week") return `${t(`fixed.w${pt?.weekday || 1}`)} ${hh}:${mm}:${ss}`;
  return `${pt?.day || 1}${t("fixed.day_suffix")} ${hh}:${mm}:${ss}`;
}

function formatStartModeSummary(item) {
  const mode = item?.start_mode || "periodic";
  const cfg = item?.schedule_config || {};
  if (mode === "fixed") {
    const unit = cfg.unit || "day";
    const pts = cfg.points || [];
    const unitLabel = t(`fixed.${unit}`);
    const formatted = pts.map((p) => formatFixedPoint(unit, p));
    const head = formatted[0] || t("empty.dash");
    const extra = formatted.length > 1 ? ` +${formatted.length - 1}` : "";
    return {
      mode,
      shortText: `${unitLabel} ${head}${extra}`,
      title: `${unitLabel}: ${formatted.join(", ")}`
    };
  }
  if (mode === "trigger") {
    const src = cfg.source || "builtin";
    const val = cfg.trigger_value ?? "1";
    if (src === "external") {
      const shortText = `${t("mode.trigger")} ${cfg.table || ""}.${cfg.column || ""}=${val}`;
      const title = `${cfg.database || ""}:${cfg.table || ""}.${cfg.column || ""}=${val}${cfg.filter ? ` (${cfg.filter})` : ""}`;
      return { mode, shortText, title };
    }
    return {
      mode,
      shortText: `${t("mode.trigger")} flag=${val}`,
      title: t("trig.api_hint", { name: item?.samp_name || item?.name || "point" })
    };
  }
  const sec = Number(item?.interval_seconds ?? 0);
  const text = sec > 0 ? `${sec}${t("unit.sec")}` : t("state.manual");
  return { mode: "periodic", shortText: text, title: text };
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
  validateFilterExpr,
  toast,
  confirmModal,
  toLocalDateTimeInput,
  parseDateTimeInput,
  formatTime,
  formatBytes,
  formatNumber,
  formatFixedPoint,
  formatStartModeSummary,
  checkedFields,
  setAllFields,
  renderFieldChecks
};

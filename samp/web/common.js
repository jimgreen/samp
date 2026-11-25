/* SAMP 前端公共模块：API、主题与多语言引擎、模态确认框、Toast 与格式化工具 */

const $ = (id) => document.getElementById(id);

const esc = (value) =>
  String(value ?? "").replace(/[&<>"']/g, (ch) => ({
    "&": "&amp;",
    "<": "&lt;",
    ">": "&gt;",
    '"': "&quot;",
    "'": "&#39;"
  }[ch]));

/* ===================== 多语言引擎（i18n） ===================== */
const LANG_STORAGE_KEY = "samp_lang";
const THEME_STORAGE_KEY = "samp_theme";
const SUPPORTED_LANGS = ["zh-CN", "en-US", "ru-RU"];
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
  _i18nListeners.forEach((fn) => fn(lang));
}

function onLangChange(fn) {
  _i18nListeners.push(fn);
}

/* ===================== 主题引擎（minimalist-light / precision-dark） ===================== */
function getTheme() {
  const saved = localStorage.getItem(THEME_STORAGE_KEY);
  return saved === "precision-dark" ? "precision-dark" : "minimalist-light";
}

function applyTheme(theme) {
  const target = theme === "precision-dark" ? "precision-dark" : "minimalist-light";
  document.documentElement.setAttribute("data-theme", target);
  localStorage.setItem(THEME_STORAGE_KEY, target);
  const btn = $("theme-toggle");
  if (btn) {
    btn.innerHTML =
      target === "precision-dark"
        ? `<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><circle cx="12" cy="12" r="4"/><path d="M12 2v2m0 16v2M4.93 4.93l1.41 1.41m11.32 11.32 1.41 1.41M2 12h2m16 0h2M4.93 19.07l1.41-1.41m11.32-11.32 1.41-1.41"/></svg>`
        : `<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M21 12.79A9 9 0 1 1 11.21 3 7 7 0 0 0 21 12.79z"/></svg>`;
  }
  _themeListeners.forEach((fn) => fn(target));
}

function onThemeChange(fn) {
  _themeListeners.push(fn);
}

function initChrome() {
  applyTheme(getTheme());
  const langSelect = $("lang-select");
  if (langSelect) {
    langSelect.value = getLang();
    langSelect.addEventListener("change", (event) => setLang(event.target.value));
  }
  const themeBtn = $("theme-toggle");
  if (themeBtn) {
    themeBtn.addEventListener("click", () => {
      applyTheme(getTheme() === "precision-dark" ? "minimalist-light" : "precision-dark");
    });
  }
  applyI18nToDOM();
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

/* ===================== 模态高危确认弹窗（默认聚焦取消） ===================== */
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
    // 默认聚焦安全项（取消）
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

function renderFieldChecks(container, fields, checked, fieldTypes = {}) {
  const list = (fields || []).filter((f) => f !== "row_key" && f !== "sample_time");
  if (!list.length) {
    container.innerHTML = `<span class="text-muted">${esc(t("empty.dash"))}</span>`;
    return;
  }
  const checkedSet = new Set(checked || []);
  container.innerHTML = list
    .map((field) => {
      const fType = fieldTypes[field] ? `<span class="field-type-tag">${esc(fieldTypes[field].slice(0, 3))}</span>` : "";
      return `<label class="field-pill"><input type="checkbox" value="${esc(field)}" ${checkedSet.has(field) ? "checked" : ""}><span>${esc(field)}</span>${fType}</label>`;
    })
    .join("");
}

window.SAMP = {
  $,
  esc,
  t,
  getLang,
  setLang,
  onLangChange,
  applyI18nToDOM,
  getTheme,
  applyTheme,
  onThemeChange,
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

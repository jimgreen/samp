/* 采样定义工作台：极速单点级联建点 + 可视化过滤构造器 + 整库批量建点向导 + 单元格无缝内嵌编辑 */

(function () {
  const {
    $,
    esc,
    t,
    ICONS,
    api,
    validateFilterExpr,
    toast,
    confirmModal,
    formatTime,
    checkedFields,
    setAllFields,
    renderFieldChecks,
    syncAlarmsFromStatus,
    initChrome,
    onLangChange
  } = window.SAMP;

  const IDENT_RE = /^[A-Za-z_\u4e00-\u9fff][A-Za-z0-9_\u4e00-\u9fff]*$/;
  const CUSTOM_VAL = "__custom__";
  const ALL_DIRS_VAL = "__all__";

  const state = {
    config: { default_source_db: "", source_databases: [], directories: ["."], target_dirs: [], sqlite_dir: "samp" },
    definitions: [],
    runtimeById: new Map(),
    health: null,
    selectedIds: new Set(),
    editingOriginal: null, // { name, target_dir, target_name }
    nameTouched: false,
    drawerTables: [],
    sourceData: null,
    draftItems: [],
    activeItemIdx: 0,
    keyword: "",
    statusFilter: "all",
    // 整表采样双栏工作台状态
    ts: {
      tables: [],
      selected: new Set(),
      activeTable: null,
      configs: {}, // table -> { name, interval, allFields, fields, filter }
      inspect: null,
      keyword: ""
    }
  };

  /* ===================== 公共小工具 ===================== */
  function uniquePointName(baseName, ignoreId = null) {
    const clean = String(baseName || "").trim();
    if (!clean) return "";
    const used = new Set(
      state.definitions
        .filter((d) => String(d.id) !== String(ignoreId))
        .map((d) => d.samp_name)
    );
    if (!used.has(clean)) return clean;
    let idx = 2;
    while (used.has(`${clean}_${idx}`)) idx += 1;
    return `${clean}_${idx}`;
  }

  function dirOf(relPath) {
    const clean = String(relPath || "").replace(/\\/g, "/").replace(/^\/+/, "");
    const idx = clean.lastIndexOf("/");
    return idx < 0 ? "." : clean.slice(0, idx) || ".";
  }

  function baseOf(relPath) {
    const clean = String(relPath || "").replace(/\\/g, "/");
    const idx = clean.lastIndexOf("/");
    return idx < 0 ? clean : clean.slice(idx + 1);
  }

  function formatDirLabel(relDir) {
    if (!relDir || relDir === ".") return t("def.root_dir_opt");
    return `${relDir}/`;
  }

  function currentTargetDir() {
    const val = targetDirTree ? targetDirTree.getValue() : "";
    if (val === CUSTOM_VAL) return $("f-target-dir-custom").value.trim();
    return val;
  }

  function currentSourceDb() {
    const sel = $("f-source-db-select").value;
    return sel === CUSTOM_VAL ? $("f-source-db-custom").value.trim() : sel;
  }

  function currentSourceTable() {
    const sel = $("f-source-table-select").value;
    return sel === CUSTOM_VAL ? $("f-source-table-custom").value.trim() : sel;
  }

  function validateNameInput() {
    const input = $("f-name");
    const hint = $("f-name-hint");
    const val = input.value.trim();
    const editingId = $("f-id").value;
    if (!val) {
      input.classList.remove("is-invalid");
      hint.textContent = "";
      return true;
    }
    if (!IDENT_RE.test(val)) {
      input.classList.add("is-invalid");
      hint.textContent = t("def.name_invalid");
      return false;
    }
    const dup = state.definitions.some((d) => d.samp_name === val && String(d.id) !== String(editingId));
    if (dup) {
      input.classList.add("is-invalid");
      hint.textContent = t("def.name_exists");
      return false;
    }
    input.classList.remove("is-invalid");
    hint.textContent = "";
    return true;
  }

  /* ===================== 主表与指标条 ===================== */
  function updateBatchButtons() {
    const count = state.selectedIds.size;
    $("batch-enable").classList.toggle("hidden", count === 0);
    $("batch-disable").classList.toggle("hidden", count === 0);
    $("batch-sample").classList.toggle("hidden", count === 0);
    $("batch-delete").classList.toggle("hidden", count === 0);
  }

  function renderMetrics() {
    const h = state.health || {};
    const sched = h.scheduler || {};
    $("m-defs").textContent = h.definitions ?? state.definitions.length;
    $("m-enabled").textContent = h.enabled ?? state.definitions.filter((d) => d.enabled).length;
    $("m-dbs").textContent = h.result_databases ?? 0;
    const rootText = state.config.root_dir || t("empty.dash");
    $("m-root").textContent = rootText;
    $("m-root").title = rootText;
    if ($("f-root-hint")) $("f-root-hint").textContent = state.config.root_dir ? `${state.config.root_dir}/` : "";

    const dot = $("m-sched-dot");
    const val = $("m-sched");
    if (sched.enabled === false) {
      dot.className = "dot warn";
      val.className = "metric-val warn";
      val.textContent = t("state.disabled_sched");
    } else if (sched.failed > 0 && !sched.last_success_at) {
      dot.className = "dot danger";
      val.className = "metric-val danger";
      val.textContent = t("state.error");
    } else {
      dot.className = "dot ok";
      val.className = "metric-val ok";
      val.textContent = t("state.running");
    }
  }

  function visibleDefinitions() {
    const kw = state.keyword.trim().toLowerCase();
    const status = state.statusFilter;
    return state.definitions.filter((d) => {
      if (status === "enabled" && !d.enabled) return false;
      if (status === "disabled" && d.enabled) return false;
      if (!kw) return true;
      const items = d.items || [];
      const texts = [
        d.samp_name,
        d.source_table,
        d.source_database,
        d.target_preview,
        d.filter,
        ...items.flatMap((it) => [it.source_table, it.source_database, it.target_table, it.filter])
      ];
      return texts.some((v) => String(v || "").toLowerCase().includes(kw));
    });
  }

  function renderTable() {
    const rows = visibleDefinitions();
    const tbody = $("def-tbody");
    const checkAll = $("check-all");

    if (!rows.length) {
      tbody.innerHTML = `<tr><td colspan="11" class="empty-cell">${esc(t("empty.dash"))}</td></tr>`;
      checkAll.checked = false;
      return;
    }

    checkAll.checked = rows.every((d) => state.selectedIds.has(d.id));

    tbody.innerHTML = rows
      .map((d) => {
        const checked = state.selectedIds.has(d.id);
        const badge = d.enabled
          ? `<span class="badge ok">${esc(t("state.enabled"))}</span>`
          : `<span class="badge muted">${esc(t("state.disabled"))}</span>`;
        const rt = state.runtimeById.get(d.id);
        let lastHtml = `<span class="text-muted">${esc(t("empty.dash"))}</span>`;
        if (rt && rt.last_status === "error") {
          lastHtml = `<a href="/runtime#logs" class="badge danger" style="text-decoration:none" title="${esc(rt.last_error || "")}">${esc(t("state.error"))}</a>`;
        } else if (rt && (rt.last_status === "ok" || rt.last_result_at)) {
          const tsText = rt.last_result_at ? formatTime(rt.last_result_at, true) : formatTime(rt.last_run_at, true);
          const rowsTag =
            rt.last_rows_written !== null && rt.last_rows_written !== undefined
              ? ` <span class="badge ok">+${esc(rt.last_rows_written)}</span>`
              : "";
          lastHtml = `<span class="mono" style="font-size:11px" title="${esc(tsText)}">${esc(tsText)}</span>${rowsTag}`;
        }
        const items = d.items && d.items.length ? d.items : [{ source_table: d.source_table, fields: d.fields || [] }];
        const itemBadge =
          items.length > 1
            ? ` <span class="badge ok" title="${esc(items.map((it) => `${it.source_table}→${it.target_table || t("def.item_default_table")}`).join("; "))}">${esc(t("def.item_count", { n: items.length }))}</span>`
            : "";
        const tablesStr = items.map((it) => it.source_table).join(", ");
        const isSingleWild = items.length === 1 && (items[0].fields || []).includes("*");
        const fieldsStr =
          items.length > 1
            ? items
                .map((it) => `${it.source_table}(${(it.fields || []).includes("*") ? "*" : (it.fields || []).length})`)
                .join(", ")
            : isSingleWild
            ? t("def.all_fields_wild")
            : (d.fields || []).join(", ");
        const fieldsCell = isSingleWild
          ? `<span class="badge ok">${esc(t("def.all_fields_wild"))}</span>`
          : esc(fieldsStr);
        const targetStr = d.target_preview || `samp/samp_${d.samp_name}_{year}.db`;
        const toggleTitle = d.enabled ? t("action.disable") : t("action.enable");
        return `
          <tr class="${checked ? "selected" : ""}" data-row-id="${d.id}">
            <td><input type="checkbox" class="row-check" data-id="${d.id}" ${checked ? "checked" : ""}></td>
            <td><strong>${esc(d.samp_name)}</strong>${itemBadge}</td>
            <td class="mono cell-ellipsis-sm" title="${esc(tablesStr)}">${esc(tablesStr)}</td>
            <td class="mono cell-ellipsis" title="${esc(d.source_database)}">${esc(d.source_database)}</td>
            <td class="mono cell-ellipsis" title="${esc(targetStr)}">${esc(targetStr)}</td>
            <td class="mono cell-ellipsis-sm" title="${esc(fieldsStr)}">${fieldsCell}</td>
            <td class="cell-input">
              <input type="number" min="0" step="1" class="num-in inline-period" data-id="${d.id}" value="${esc(d.interval_seconds)}" title="${esc(t("def.label_interval"))}">
            </td>
            <td class="cell-input">
              <input type="text" class="filter-in inline-filter" data-id="${d.id}" value="${esc(d.filter || "")}" placeholder="${esc(t("empty.dash"))}" title="${esc(d.filter || t("def.label_filter"))}">
            </td>
            <td>${badge}</td>
            <td>${lastHtml}</td>
            <td class="sticky-actions">
              <div class="cell-actions">
                <button type="button" class="btn xs icon-only" data-act="edit" data-id="${d.id}" title="${esc(t("action.edit"))}">${ICONS.edit}</button>
                <button type="button" class="btn xs icon-only" data-act="copy" data-id="${d.id}" title="${esc(t("action.copy"))}">${ICONS.copy}</button>
                <button type="button" class="btn xs icon-only" data-act="sample" data-id="${d.id}" title="${esc(t("action.sample_now"))}">${ICONS.play}</button>
                <a class="btn xs icon-only" href="/query?id=${d.id}&range=hour" title="${esc(t("action.view_data"))}">${ICONS.chart}</a>
                <button type="button" class="btn xs icon-only" data-act="toggle" data-id="${d.id}" title="${esc(toggleTitle)}">${ICONS.power}</button>
                <button type="button" class="btn xs icon-only danger" data-act="delete" data-id="${d.id}" title="${esc(t("action.delete"))}">${ICONS.trash}</button>
              </div>
            </td>
          </tr>`;
      })
      .join("");
    updateBatchButtons();
  }

  async function commitInlineUpdate(inputEl) {
    const id = Number(inputEl.dataset.id);
    const def = state.definitions.find((d) => d.id === id);
    if (!def) return;
    const isPeriod = inputEl.classList.contains("inline-period");
    const nextInterval = isPeriod ? Number(inputEl.value) : def.interval_seconds;
    const nextFilter = isPeriod ? def.filter || "" : inputEl.value.trim();

    if (isPeriod && (!Number.isInteger(nextInterval) || nextInterval < 0)) {
      inputEl.classList.add("is-invalid");
      return;
    }
    if (nextInterval === def.interval_seconds && nextFilter === (def.filter || "")) {
      inputEl.classList.remove("is-invalid");
      return;
    }

    const nextItems = (def.items || []).map((it, idx) => ({
      ...it,
      fields: [...(it.fields || [])],
      filter: idx === 0 ? nextFilter : it.filter || ""
    }));

    try {
      const saved = await api(`/api/definitions/${id}`, {
        method: "PUT",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          name: def.samp_name,
          source_database: def.source_database,
          source_table: def.source_table,
          target_dir: def.target_dir || "",
          target_name: def.target_name || "",
          fields: def.fields,
          items: nextItems.length ? nextItems : undefined,
          interval_seconds: nextInterval,
          filter: nextFilter,
          enabled: Boolean(def.enabled)
        })
      });
      def.interval_seconds = saved.interval_seconds;
      def.filter = saved.filter;
      def.items = saved.items;
      inputEl.classList.remove("is-invalid");
      toast(t("def.toast_updated"), "ok");
    } catch (err) {
      inputEl.classList.add("is-invalid");
      toast(err.message, "error");
    }
  }

  /* ===================== 可折叠多级目录树下拉选择器 ===================== */
  const FOLDER_SVG =
    '<svg class="tree-folder-ico" viewBox="0 0 24 24"><path d="M3 6a2 2 0 0 1 2-2h5l2 2h7a2 2 0 0 1 2 2v10a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2z"/></svg>';
  const openDirTrees = new Set();
  let targetDirTree = null;
  let sourceDirTree = null;
  let tsSourceDirTree = null;
  let tsTargetDirTree = null;

  async function refreshDirectoriesFromServer() {
    try {
      state.config = await api("/api/config?refresh=1");
      renderMetrics();
      const allDbs = state.config.source_databases || [];
      const dirs = collectedDirs();
      const countByDir = new Map();
      allDbs.forEach((p) => {
        if (/^[a-zA-Z]:/.test(p) || p.startsWith("/")) return;
        const d = dirOf(p);
        countByDir.set(d, (countByDir.get(d) || 0) + 1);
      });
      const defDir = state.config.sqlite_dir || "samp";
      targetDirTree?.setData(dirs, new Map(), 0, defDir);
      tsTargetDirTree?.setData(dirs, new Map(), 0, defDir);
      sourceDirTree?.setData(dirs, countByDir, allDbs.length, defDir);
      tsSourceDirTree?.setData(dirs, countByDir, allDbs.length, defDir);
      if ($("drawer-backdrop").classList.contains("open")) {
        renderSourceDbForDir(sourceDirTree.getValue(), currentSourceDb());
      }
      if ($("ts-backdrop").classList.contains("open")) {
        tsRenderSourceDbForDir(tsSourceDirTree.getValue(), $("ts-source-db").value);
      }
    } catch (err) {
      toast(err.message, "error");
    }
  }

  function closeAllDirTrees(except = null) {
    [...openDirTrees].forEach((picker) => {
      if (picker !== except) picker.close();
    });
  }

  function buildDirTreeNodes(dirList, directCounts = new Map()) {
    const rootChildren = new Map();
    const ensureNode = (map, name, fullPath) => {
      let node = map.get(name);
      if (!node) {
        node = { name, path: fullPath, childrenMap: new Map(), children: [], directCount: 0, totalCount: 0 };
        map.set(name, node);
      }
      return node;
    };
    dirList.forEach((raw) => {
      const clean = String(raw || "").trim().replace(/\\/g, "/").replace(/^\/+|\/+$/g, "");
      if (!clean || clean === ".") return;
      const parts = clean.split("/").filter(Boolean);
      let map = rootChildren;
      let acc = "";
      parts.forEach((seg) => {
        acc = acc ? `${acc}/${seg}` : seg;
        const node = ensureNode(map, seg, acc);
        map = node.childrenMap;
      });
    });

    const finalize = (map) => {
      const arr = [...map.values()].sort((a, b) => a.name.localeCompare(b.name, undefined, { sensitivity: "base" }));
      arr.forEach((node) => {
        node.children = finalize(node.childrenMap);
        node.directCount = directCounts.get(node.path) || 0;
        node.totalCount = node.directCount + node.children.reduce((s, c) => s + c.totalCount, 0);
      });
      return arr;
    };
    return finalize(rootChildren);
  }

  function ancestorPaths(relPath) {
    const clean = String(relPath || "").trim().replace(/\\/g, "/").replace(/^\/+|\/+$/g, "");
    if (!clean || clean === "." || clean === ALL_DIRS_VAL || clean === CUSTOM_VAL) return [];
    const parts = clean.split("/").filter(Boolean);
    const out = [];
    let acc = "";
    for (let i = 0; i < parts.length - 1; i += 1) {
      acc = acc ? `${acc}/${parts[i]}` : parts[i];
      out.push(acc);
    }
    return out;
  }

  function createDirTreePicker(triggerEl, options = {}) {
    const opts = {
      includeAll: Boolean(options.includeAll),
      allowCustom: Boolean(options.allowCustom),
      showCounts: Boolean(options.showCounts),
      defaultDirMode: Boolean(options.defaultDirMode), // true 时 "" 代表默认结果目录
      onChange: options.onChange || (() => {})
    };

    const popover = document.createElement("div");
    popover.className = "tree-popover";
    popover.innerHTML = `
      <div class="tree-head">
        <input type="text" class="ctrl mono tree-search" autocomplete="off">
        <button type="button" class="btn xs tree-expand-all"></button>
        <button type="button" class="btn xs tree-collapse-all"></button>
        <button type="button" class="btn xs icon-only tree-refresh-dirs">${ICONS.refresh}</button>
      </div>
      <div class="tree-list" role="tree"></div>
      <div class="tree-foot hidden"></div>
    `;
    document.body.appendChild(popover);

    const searchInput = popover.querySelector(".tree-search");
    const expandBtn = popover.querySelector(".tree-expand-all");
    const collapseBtn = popover.querySelector(".tree-collapse-all");
    const refreshBtn = popover.querySelector(".tree-refresh-dirs");
    const listEl = popover.querySelector(".tree-list");
    const footEl = popover.querySelector(".tree-foot");

    let value = opts.includeAll ? ALL_DIRS_VAL : "";
    let treeNodes = [];
    let allDirs = [];
    let directCounts = new Map();
    let totalDbCount = 0;
    let defaultDir = "samp";
    let keyword = "";
    const expanded = new Set();

    function syncTriggerText() {
      const textEl = triggerEl.querySelector(".tree-trigger-text");
      let display = "";
      if (value === ALL_DIRS_VAL) {
        display = `${t("def.all_dirs")} (${totalDbCount})`;
      } else if (value === CUSTOM_VAL) {
        display = t("def.custom_dir");
      } else if (opts.defaultDirMode && !value) {
        display = `${defaultDir}/`;
      } else if (value === ".") {
        display = t("def.root_dir_opt");
      } else {
        display = `${value}/`;
      }
      textEl.textContent = display;
      triggerEl.title = display;
    }

    function positionPopover() {
      const rect = triggerEl.getBoundingClientRect();
      const vw = window.innerWidth;
      const vh = window.innerHeight;
      const width = Math.min(Math.max(rect.width, 280), vw - 16);
      let left = rect.left;
      if (left + width > vw - 8) left = Math.max(8, vw - width - 8);

      const spaceBelow = vh - rect.bottom - 10;
      const spaceAbove = rect.top - 10;
      const openUp = spaceBelow < 220 && spaceAbove > spaceBelow;
      const maxH = Math.max(160, Math.min(320, openUp ? spaceAbove : spaceBelow));
      listEl.style.maxHeight = `${Math.max(110, maxH - 76)}px`;

      popover.style.width = `${width}px`;
      popover.style.left = `${left}px`;
      if (openUp) {
        popover.style.top = "auto";
        popover.style.bottom = `${vh - rect.top + 4}px`;
      } else {
        popover.style.bottom = "auto";
        popover.style.top = `${rect.bottom + 4}px`;
      }
    }

    function renderTreeList() {
      searchInput.placeholder = t("def.ph_search_dir");
      expandBtn.textContent = t("def.tree_expand");
      collapseBtn.textContent = t("def.tree_collapse");
      refreshBtn.title = t("action.refresh_dirs");

      const kw = keyword.trim().toLowerCase();
      const rows = [];

      const activeEffective = opts.defaultDirMode && !value ? defaultDir : value;

      if (!kw && opts.includeAll) {
        const sel = value === ALL_DIRS_VAL;
        rows.push(
          `<div class="tree-row special ${sel ? "selected" : ""}" data-val="${esc(ALL_DIRS_VAL)}" style="padding-left:6px">` +
            `<span class="tree-toggle leaf"></span>` +
            `<span class="tree-name">${esc(t("def.all_dirs"))}</span>` +
            (opts.showCounts ? `<span class="tree-badge ${totalDbCount ? "has-db" : ""}">${totalDbCount}</span>` : "") +
            `</div>`
        );
      }

      const rootLabel = t("def.root_dir_opt");
      if (!kw || ".".includes(kw) || rootLabel.toLowerCase().includes(kw)) {
        const rootCount = directCounts.get(".") || 0;
        const sel = value === ".";
        rows.push(
          `<div class="tree-row special ${sel ? "selected" : ""}" data-val="." title="./" style="padding-left:6px">` +
            `<span class="tree-toggle leaf"></span>` +
            `${FOLDER_SVG}` +
            `<span class="tree-name">${esc(rootLabel)}</span>` +
            (opts.showCounts && rootCount ? `<span class="tree-badge has-db">${rootCount}</span>` : "") +
            `</div>`
        );
      }

      const nodeMatches = (node) => {
        if (!kw) return true;
        if (node.path.toLowerCase().includes(kw) || node.name.toLowerCase().includes(kw)) return true;
        return node.children.some(nodeMatches);
      };

      const renderNode = (node, depth) => {
        if (!nodeMatches(node)) return;
        const hasChildren = node.children.length > 0;
        const isExpanded = Boolean(kw) || expanded.has(node.path);
        const isSelected = activeEffective === node.path;
        const valAttr = opts.defaultDirMode && node.path === defaultDir ? "" : node.path;
        const padLeft = 6 + depth * 14;
        const badgeCount = isExpanded ? node.directCount : node.totalCount;
        const badgeHtml =
          opts.showCounts && badgeCount > 0
            ? `<span class="tree-badge has-db" title="${esc(`${node.directCount} / ${node.totalCount}`)}">${badgeCount}</span>`
            : "";
        const toggleHtml = hasChildren
          ? `<button type="button" class="tree-toggle" data-toggle="${esc(node.path)}" tabindex="-1">${isExpanded ? "▾" : "▸"}</button>`
          : `<span class="tree-toggle leaf"></span>`;
        rows.push(
          `<div class="tree-row ${isSelected ? "selected" : ""}" data-val="${esc(valAttr)}" title="${esc(`${node.path}/`)}" style="padding-left:${padLeft}px">` +
            `${toggleHtml}${FOLDER_SVG}` +
            `<span class="tree-name">${esc(node.name)}</span>` +
            `${badgeHtml}` +
            `</div>`
        );
        if (hasChildren && isExpanded) {
          node.children.forEach((child) => renderNode(child, depth + 1));
        }
      };

      treeNodes.forEach((node) => renderNode(node, 0));

      if (!rows.length) {
        listEl.innerHTML = `<div class="text-muted" style="padding:10px;text-align:center;font-size:12px">${esc(t("empty.dash"))}</div>`;
      } else {
        listEl.innerHTML = rows.join("");
      }

      if (opts.allowCustom) {
        const sel = value === CUSTOM_VAL;
        footEl.classList.remove("hidden");
        footEl.innerHTML = `<div class="tree-row special ${sel ? "selected" : ""}" data-val="${esc(CUSTOM_VAL)}" style="padding-left:6px"><span class="tree-toggle leaf"></span><span class="tree-name">${esc(t("def.custom_dir"))}</span></div>`;
      } else {
        footEl.classList.add("hidden");
        footEl.innerHTML = "";
      }
    }

    function open() {
      closeAllDirTrees(apiObj);
      keyword = "";
      searchInput.value = "";
      const activePath = opts.defaultDirMode && !value ? defaultDir : value;
      ancestorPaths(activePath).forEach((p) => expanded.add(p));
      renderTreeList();
      popover.classList.add("open");
      triggerEl.classList.add("open");
      triggerEl.setAttribute("aria-expanded", "true");
      openDirTrees.add(apiObj);
      positionPopover();
      const selRow = listEl.querySelector(".tree-row.selected");
      if (selRow) selRow.scrollIntoView({ block: "nearest" });
      searchInput.focus();
    }

    function close() {
      popover.classList.remove("open");
      triggerEl.classList.remove("open");
      triggerEl.setAttribute("aria-expanded", "false");
      openDirTrees.delete(apiObj);
    }

    function setData(dirs, countsMap = new Map(), totalDbs = 0, defDir = "samp") {
      allDirs = [...dirs];
      directCounts = countsMap;
      totalDbCount = totalDbs;
      defaultDir = defDir || "samp";
      treeNodes = buildDirTreeNodes(allDirs, directCounts);
      syncTriggerText();
      if (popover.classList.contains("open")) renderTreeList();
    }

    function setValue(nextVal, silent = true) {
      value = nextVal;
      const activePath = opts.defaultDirMode && !value ? defaultDir : value;
      ancestorPaths(activePath).forEach((p) => expanded.add(p));
      syncTriggerText();
      if (popover.classList.contains("open")) renderTreeList();
      if (!silent) opts.onChange(value);
    }

    triggerEl.addEventListener("click", (e) => {
      e.stopPropagation();
      if (popover.classList.contains("open")) close();
      else open();
    });

    popover.addEventListener("click", (e) => {
      e.stopPropagation();
      const toggle = e.target.closest("[data-toggle]");
      if (toggle) {
        const path = toggle.dataset.toggle;
        if (expanded.has(path)) expanded.delete(path);
        else expanded.add(path);
        renderTreeList();
        return;
      }
      const row = e.target.closest(".tree-row[data-val]");
      if (row) {
        const nextVal = row.dataset.val;
        close();
        setValue(nextVal, false);
      }
    });

    searchInput.addEventListener("input", (e) => {
      keyword = e.target.value;
      renderTreeList();
    });

    expandBtn.addEventListener("click", () => {
      const addAll = (nodes) => {
        nodes.forEach((n) => {
          if (n.children.length) {
            expanded.add(n.path);
            addAll(n.children);
          }
        });
      };
      addAll(treeNodes);
      renderTreeList();
    });

    collapseBtn.addEventListener("click", () => {
      expanded.clear();
      renderTreeList();
    });

    refreshBtn.addEventListener("click", () => {
      refreshDirectoriesFromServer();
    });

    const apiObj = {
      open,
      close,
      isOpen: () => popover.classList.contains("open"),
      setData,
      setValue,
      getValue: () => value,
      refreshLabels: () => {
        syncTriggerText();
        if (popover.classList.contains("open")) renderTreeList();
      }
    };
    return apiObj;
  }

  /* ===================== 单点弹窗多级目录 + 源库自动级联 ===================== */
  function collectedDirs() {
    const list = [];
    const seen = new Set();
    const push = (d) => {
      const val = String(d || "").trim().replace(/\\/g, "/").replace(/\/+$/, "") || ".";
      if (!seen.has(val)) {
        seen.add(val);
        list.push(val);
      }
    };
    (state.config.directories || ["."]).forEach(push);
    (state.config.target_dirs || []).forEach(push);
    (state.config.source_databases || []).forEach((p) => {
      if (!/^[a-zA-Z]:/.test(p) && !p.startsWith("/")) push(dirOf(p));
    });
    return list;
  }

  function renderTargetDirTree(picker, targetDir = "", allowCustom = true) {
    const defaultDir = state.config.sqlite_dir || "samp";
    const allDirs = collectedDirs();
    const validSet = new Set(["", ".", defaultDir, ...allDirs]);
    picker.setData(allDirs, new Map(), 0, defaultDir);

    const norm = String(targetDir || "").trim().replace(/\\/g, "/").replace(/\/+$/, "");
    const customInput = allowCustom ? $("f-target-dir-custom") : null;
    if (!norm || norm === defaultDir) {
      picker.setValue("", true);
      if (customInput) {
        customInput.value = "";
        customInput.classList.add("hidden");
      }
    } else if (validSet.has(norm)) {
      picker.setValue(norm, true);
      if (customInput) {
        customInput.value = "";
        customInput.classList.add("hidden");
      }
    } else if (allowCustom && customInput) {
      picker.setValue(CUSTOM_VAL, true);
      customInput.value = norm;
      customInput.classList.remove("hidden");
    } else {
      picker.setData([...allDirs, norm], new Map(), 0, defaultDir);
      picker.setValue(norm, true);
    }
  }

  function renderSourceDbSelect(selectEl, targetDb = null, allowCustom = true) {
    const list = state.config.source_databases || [];
    const opts = list.map((p) => `<option value="${esc(p)}">${esc(p)}</option>`);
    if (allowCustom) {
      opts.push(`<option value="${CUSTOM_VAL}">${esc(t("def.custom_path"))}</option>`);
    }
    selectEl.innerHTML = opts.join("");
    const desired = targetDb || list[0] || state.config.default_source_db || "";
    if (list.includes(desired)) {
      selectEl.value = desired;
      if (allowCustom) {
        $("f-source-db-custom").classList.add("hidden");
        $("f-source-db-custom").required = false;
      }
    } else if (allowCustom) {
      selectEl.value = CUSTOM_VAL;
      $("f-source-db-custom").value = desired;
      $("f-source-db-custom").classList.remove("hidden");
      $("f-source-db-custom").required = true;
    }
  }

  function renderSourceDbForDir(selectedDir, targetDb = null) {
    const dbSel = $("f-source-db-select");
    const customIn = $("f-source-db-custom");
    const allDbs = state.config.source_databases || [];
    const filtered =
      selectedDir === ALL_DIRS_VAL
        ? allDbs
        : allDbs.filter((p) => !/^[a-zA-Z]:/.test(p) && !p.startsWith("/") && dirOf(p) === selectedDir);

    const opts = [];
    if (!filtered.length) {
      opts.push(`<option value="" disabled>${esc(t("def.no_db_in_dir"))}</option>`);
    } else {
      filtered.forEach((p) => {
        const label = selectedDir === ALL_DIRS_VAL ? p : baseOf(p);
        opts.push(`<option value="${esc(p)}" title="${esc(p)}">${esc(label)}</option>`);
      });
    }
    opts.push(`<option value="${CUSTOM_VAL}">${esc(t("def.custom_path"))}</option>`);
    dbSel.innerHTML = opts.join("");

    if (targetDb && filtered.includes(targetDb)) {
      dbSel.value = targetDb;
      customIn.classList.add("hidden");
      customIn.required = false;
    } else if (targetDb && !allDbs.includes(targetDb)) {
      dbSel.value = CUSTOM_VAL;
      customIn.value = targetDb;
      customIn.classList.remove("hidden");
      customIn.required = true;
    } else if (filtered.length) {
      dbSel.value = filtered[0];
      customIn.classList.add("hidden");
      customIn.required = false;
    } else {
      dbSel.value = "";
      customIn.classList.add("hidden");
      customIn.required = false;
    }
  }

  function renderSourceDirAndDb(targetDb = null) {
    const allDbs = state.config.source_databases || [];
    const dirs = collectedDirs();
    const countByDir = new Map();
    allDbs.forEach((p) => {
      if (/^[a-zA-Z]:/.test(p) || p.startsWith("/")) return;
      const d = dirOf(p);
      countByDir.set(d, (countByDir.get(d) || 0) + 1);
    });

    sourceDirTree.setData(dirs, countByDir, allDbs.length, state.config.sqlite_dir || "samp");

    const desired = targetDb || allDbs[0] || state.config.default_source_db || "";
    let chosenDir = ALL_DIRS_VAL;
    if (desired && allDbs.includes(desired) && !/^[a-zA-Z]:/.test(desired) && !desired.startsWith("/")) {
      const d = dirOf(desired);
      chosenDir = dirs.includes(d) ? d : ALL_DIRS_VAL;
    }
    sourceDirTree.setValue(chosenDir, true);
    renderSourceDbForDir(chosenDir, desired);
  }

  function renderDrawerTableSelect(tables, targetTable = null) {
    const sel = $("f-source-table-select");
    const customInput = $("f-source-table-custom");
    const opts = [];
    if (!tables.length) {
      opts.push(`<option value="">${esc(t("def.select_table"))}</option>`);
    }
    tables.forEach((tb) => {
      const chn = tb.chn_name ? ` · ${tb.chn_name}` : "";
      const tag = tb.existing_definitions?.length ? ` [${t("def.batch_linked")}]` : "";
      opts.push(`<option value="${esc(tb.table)}">${esc(`${tb.table}${chn} (${tb.row_count})${tag}`)}</option>`);
    });
    opts.push(`<option value="${CUSTOM_VAL}">${esc(t("def.custom_path"))}</option>`);
    sel.innerHTML = opts.join("");

    const names = tables.map((tb) => tb.table);
    if (targetTable && names.includes(targetTable)) {
      sel.value = targetTable;
      customInput.classList.add("hidden");
    } else if (targetTable) {
      sel.value = CUSTOM_VAL;
      customInput.value = targetTable;
      customInput.classList.remove("hidden");
    } else if (tables.length) {
      // 新建时优先默认选中第一张尚未建点的表
      const firstNew = tables.find((tb) => !tb.existing_definitions?.length) || tables[0];
      sel.value = firstNew.table;
      customInput.classList.add("hidden");
    } else {
      sel.value = "";
      customInput.classList.add("hidden");
    }
  }

  function highlightPeriodChip() {
    const val = $("f-interval").value;
    $("period-chips").querySelectorAll("[data-period]").forEach((btn) => {
      btn.classList.toggle("active-chip", btn.dataset.period === String(val));
    });
  }

  function previewTargetPath(name, targetDir, targetName) {
    const dirRaw = String(targetDir || "").trim().replace(/\\/g, "/").replace(/\/+$/, "");
    const dir = dirRaw || state.config.sqlite_dir || "samp";
    const nm = String(targetName || "").trim();
    const pt = String(name || "").trim() || "{name}";
    let file;
    if (!nm) {
      file = `samp_${pt}_{year}.db`;
    } else if (/\{year\}/i.test(nm)) {
      const norm = nm.replace(/\{year\}/gi, "{year}");
      file = norm.toLowerCase().endsWith(".db") ? norm : `${norm}.db`;
    } else if (nm.toLowerCase().endsWith(".db")) {
      file = nm;
    } else {
      file = `${nm}_{year}.db`;
    }
    return dir === "." ? file : `${dir}/${file}`;
  }

  function updateTargetPreview() {
    const el = $("f-target-preview");
    if (!el) return;
    const text = previewTargetPath($("f-name").value, currentTargetDir(), $("f-target-name").value);
    el.textContent = text;
    el.title = text;
  }

  function updateRenameWarning() {
    const editing = Boolean($("f-id").value);
    const orig = state.editingOriginal;
    const curDir = currentTargetDir();
    const origDir = orig ? (orig.target_dir === (state.config.sqlite_dir || "samp") ? "" : orig.target_dir) : "";
    const changed =
      editing &&
      orig &&
      ($("f-name").value.trim() !== orig.name ||
        curDir !== origDir ||
        $("f-target-name").value.trim() !== orig.target_name);
    $("rename-warn").classList.toggle("show", Boolean(changed));
  }

  function syncActiveItemFromForm() {
    const item = state.draftItems[state.activeItemIdx];
    if (!item) return;
    item.source_database = currentSourceDb() || item.source_database || state.config.default_source_db || "";
    item.source_table = currentSourceTable() || "";
    item.target_table = $("f-target-table").value.trim();
    if ($("f-all-fields")?.checked) {
      item.fields = ["*"];
    } else {
      const checked = checkedFields($("f-fields"));
      if (state.sourceData || checked.length) {
        item.fields = checked;
      }
    }
    item.filter = $("f-filter").value.trim();
  }

  function renderItemsStrip() {
    const strip = $("items-strip");
    const badge = $("items-count-badge");
    if (!strip) return;
    badge.textContent = `(${state.draftItems.length})`;
    const canDel = state.draftItems.length > 1;
    strip.innerHTML = state.draftItems
      .map((it, idx) => {
        const active = idx === state.activeItemIdx;
        const src = it.source_table || t("def.select_table");
        const tgt = it.target_table || (idx === 0 ? t("def.item_default_table") : src);
        const fTag = (it.fields || []).includes("*") ? "*" : (it.fields || []).length;
        const label = `#${idx + 1} ${src} → ${tgt} (${fTag})`;
        const delBtn = canDel
          ? `<button type="button" class="item-tab-del" data-item-del="${idx}" title="${esc(t("def.item_remove"))}">&times;</button>`
          : "";
        return `<div class="item-tab ${active ? "active" : ""}" data-item-idx="${idx}" title="${esc(`${it.source_database} : ${src} → ${tgt}`)}"><span class="item-tab-label">${esc(label)}</span>${delBtn}</div>`;
      })
      .join("");
  }

  async function loadItemIntoForm(idx, autoSelectNumeric = false) {
    state.activeItemIdx = Math.max(0, Math.min(idx, state.draftItems.length - 1));
    const item = state.draftItems[state.activeItemIdx];
    if (!item) return;
    state.sourceData = null;
    const isWild = (item.fields || []).includes("*");
    $("f-all-fields").checked = isWild;
    $("f-fields").classList.toggle("ts-fields-locked", isWild);
    $("f-field-search").value = "";
    $("fb-val").value = "";
    $("f-filter").value = item.filter || "";
    $("f-target-table").value = item.target_table || "";
    renderSourceDirAndDb(item.source_database || (state.config.source_databases || [])[0] || state.config.default_source_db);
    renderItemsStrip();
    const presetFields = isWild ? null : autoSelectNumeric ? null : item.fields && item.fields.length ? item.fields : null;
    await loadDrawerTables(item.source_table || null, presetFields, isWild ? false : autoSelectNumeric || !item.fields?.length);
    syncActiveItemFromForm();
    renderItemsStrip();
  }

  async function switchDraftItem(idx) {
    if (idx === state.activeItemIdx || idx < 0 || idx >= state.draftItems.length) return;
    syncActiveItemFromForm();
    await loadItemIntoForm(idx, false);
  }

  async function addDraftItem() {
    syncActiveItemFromForm();
    const baseDb = currentSourceDb() || state.config.default_source_db || "";
    const usedTables = new Set(state.draftItems.map((it) => (it.source_table || "").toLowerCase()).filter(Boolean));
    // 优先挑选同源库内尚未挂载的下一张业务表
    const candidate = (state.drawerTables || []).find((tb) => !usedTables.has(tb.table.toLowerCase()));
    const nextTable = candidate ? candidate.table : "";
    let seq = state.draftItems.length + 1;
    const usedIds = new Set(state.draftItems.map((it) => it.item_id));
    while (usedIds.has(`i${seq}`)) seq += 1;
    state.draftItems.push({
      item_id: `i${seq}`,
      source_database: baseDb,
      source_table: nextTable,
      target_table: nextTable,
      fields: [],
      filter: ""
    });
    await loadItemIntoForm(state.draftItems.length - 1, true);
  }

  async function removeDraftItem(idx) {
    if (state.draftItems.length <= 1) {
      toast(t("def.toast_item_min"), "error");
      return;
    }
    syncActiveItemFromForm();
    state.draftItems.splice(idx, 1);
    const nextIdx = Math.min(idx, state.draftItems.length - 1);
    await loadItemIntoForm(nextIdx, false);
  }

  function renderFilterBuilderOptions(data) {
    const fieldSel = $("fb-field");
    const cols = data?.fields || [];
    const labels = data?.field_labels || {};
    fieldSel.innerHTML = cols
      .map((c) => `<option value="${esc(c)}">${esc(labels[c] ? `${c} · ${labels[c]}` : c)}</option>`)
      .join("");
    updateFilterSampleDatalist();
  }

  function updateFilterSampleDatalist() {
    const field = $("fb-field").value;
    const vals = state.sourceData?.sample_values?.[field] || [];
    $("fb-val-list").innerHTML = vals.map((v) => `<option value="${esc(v)}"></option>`).join("");
  }

  function formatConditionLiteral(field, rawVal, op) {
    const fType = state.sourceData?.field_types?.[field] || "REAL";
    if (op === "in") {
      const cleaned = rawVal.trim().replace(/^\(|\)$/g, "");
      return `${field} in (${cleaned})`;
    }
    const text = String(rawVal ?? "").trim();
    if (fType === "TEXT" && !(text.startsWith("'") && text.endsWith("'"))) {
      return `${field}${op}'${text}'`;
    }
    return `${field}${op}${text}`;
  }

  function appendFilterCondition(clause) {
    const input = $("f-filter");
    const curr = input.value.trim();
    if (!clause) return;
    if (curr && curr.toLowerCase().includes(clause.toLowerCase())) {
      inspectSource(checkedFields($("f-fields")));
      return;
    }
    input.value = curr ? `${curr} and ${clause}` : clause;
    inspectSource(checkedFields($("f-fields")));
  }

  function renderPreview(data) {
    $("preview-meta").textContent = t("def.preview_meta", {
      matched: data.matched_rows ?? data.rows.length,
      total: data.total_rows ?? data.rows.length,
      cols: (data.fields || []).length
    });
    if (!data.rows || !data.rows.length) {
      $("preview-wrap").innerHTML = `<div class="empty-cell">${esc(t("empty.dash"))}</div>`;
      return;
    }
    const cols = data.fields || [];
    const labels = data.field_labels || {};
    $("preview-wrap").innerHTML = `
      <table class="data-table">
        <thead><tr>${cols.map((c) => `<th title="${esc(labels[c] || c)}">${esc(c)}</th>`).join("")}</tr></thead>
        <tbody>
          ${data.rows
            .map(
              (row) =>
                `<tr>${cols
                  .map((c) => {
                    const val = row[c];
                    const canClick = val !== null && val !== undefined && val !== "";
                    return `<td class="mono ${canClick ? "clickable-cell" : ""}" data-pcol="${esc(c)}" data-pval="${esc(val ?? "")}" title="${esc(t("def.click_cell_filter"))}">${esc(val)}</td>`;
                  })
                  .join("")}</tr>`
            )
            .join("")}
        </tbody>
      </table>`;
  }

  function renderDrawerFields(presetFields = null) {
    if (!state.sourceData) return;
    const kw = $("f-field-search").value;
    const chosen = presetFields !== null ? presetFields : checkedFields($("f-fields"));
    renderFieldChecks(
      $("f-fields"),
      state.sourceData.fields || [],
      chosen,
      state.sourceData.field_types || {},
      state.sourceData.field_labels || {},
      kw
    );
  }

  async function loadDrawerTables(targetTable = null, presetFields = null, autoSelectNumeric = false) {
    const db = currentSourceDb();
    if (!db) return;
    try {
      const summary = await api(`/api/source/tables/summary?source_database=${encodeURIComponent(db)}`);
      state.drawerTables = summary.tables || [];
      renderDrawerTableSelect(state.drawerTables, targetTable);
      const chosenTable = currentSourceTable();
      if (chosenTable) {
        await inspectSource(presetFields, autoSelectNumeric);
      } else {
        state.sourceData = null;
        $("f-fields").innerHTML = `<span class="text-muted">${esc(t("empty.dash"))}</span>`;
        $("preview-meta").textContent = t("empty.dash");
        $("preview-wrap").innerHTML = "";
      }
    } catch (err) {
      state.drawerTables = [];
      renderDrawerTableSelect([], targetTable);
      $("preview-meta").textContent = err.message;
      $("preview-wrap").innerHTML = "";
    }
  }

  async function inspectSource(presetFields = null, autoSelectNumeric = false) {
    const db = currentSourceDb();
    const table = currentSourceTable();
    const filter = $("f-filter").value.trim();
    if (!db || !table) return;
    try {
      const url = `/api/source/inspect?source_database=${encodeURIComponent(db)}&source_table=${encodeURIComponent(table)}&filter=${encodeURIComponent(filter)}`;
      const data = await api(url);
      state.sourceData = data;
      let targetChecked;
      if ($("f-all-fields")?.checked) {
        targetChecked = (data.fields || []).filter((f) => f !== "row_key" && f !== "sample_time");
      } else if (presetFields !== null) {
        targetChecked = presetFields;
      } else if (autoSelectNumeric) {
        targetChecked = data.numeric_fields?.length
          ? data.numeric_fields
          : (data.fields || []).filter((f) => f !== "row_key" && f !== "sample_time");
      } else {
        targetChecked = checkedFields($("f-fields"));
      }
      renderDrawerFields(targetChecked);
      $("f-fields").classList.toggle("ts-fields-locked", Boolean($("f-all-fields")?.checked));
      renderFilterBuilderOptions(data);
      renderPreview(data);

      // 新建且编辑首条采集项、用户未手动改名时，自动预填不冲突采样点名
      if (!$("f-id").value && state.activeItemIdx === 0 && !state.nameTouched) {
        $("f-name").value = uniquePointName(table);
        validateNameInput();
        updateTargetPreview();
      }
      // 新增第 2 条及以上采集项时，目标表名留空则默认同步源表名
      if (state.activeItemIdx > 0 && !$("f-target-table").value.trim()) {
        $("f-target-table").value = table;
      }
      syncActiveItemFromForm();
      renderItemsStrip();
    } catch (err) {
      $("preview-meta").textContent = err.message;
      $("preview-wrap").innerHTML = "";
      toast(err.message, "error");
    }
  }

  async function openDrawer(mode, def = null) {
    try {
      state.config = await api("/api/config");
      renderMetrics();
    } catch {
      // 忽略网络瞬断，沿用当前缓存目录
    }
    $("f-id").value = mode === "edit" && def ? String(def.id) : "";
    state.editingOriginal =
      mode === "edit" && def
        ? {
            name: def.samp_name,
            target_dir: def.target_dir || "",
            target_name: def.target_name || ""
          }
        : null;
    state.nameTouched = mode !== "new";
    state.sourceData = null;
    $("f-field-search").value = "";
    $("fb-val").value = "";
    $("f-name-hint").textContent = "";
    $("f-name").classList.remove("is-invalid");

    const defaultSrcDb = (state.config.source_databases || [])[0] || state.config.default_source_db || "";
    const buildDraftItems = (srcDef) => {
      if (srcDef?.items?.length) {
        return srcDef.items.map((it, idx) => ({
          item_id: it.item_id || `i${idx + 1}`,
          source_database: it.source_database || srcDef.source_database || defaultSrcDb,
          source_table: it.source_table || "",
          target_table: it.target_table || "",
          fields: [...(it.fields || [])],
          filter: it.filter || ""
        }));
      }
      if (srcDef) {
        return [
          {
            item_id: "i1",
            source_database: srcDef.source_database || defaultSrcDb,
            source_table: srcDef.source_table || "",
            target_table: "",
            fields: [...(srcDef.fields || [])],
            filter: srcDef.filter || ""
          }
        ];
      }
      return [
        {
          item_id: "i1",
          source_database: defaultSrcDb,
          source_table: "",
          target_table: "",
          fields: [],
          filter: ""
        }
      ];
    };

    if (mode === "edit" && def) {
      $("drawer-title").textContent = t("def.drawer_edit", { name: def.samp_name });
      $("f-name").value = def.samp_name;
      $("f-interval").value = def.interval_seconds;
      renderTargetDirTree(targetDirTree, def.target_dir || "", true);
      $("f-target-name").value = def.target_name || "";
      $("f-enabled").checked = Boolean(def.enabled);
      state.draftItems = buildDraftItems(def);
    } else if (mode === "copy" && def) {
      $("drawer-title").textContent = t("def.drawer_copy");
      $("f-name").value = uniquePointName(def.samp_name);
      $("f-interval").value = def.interval_seconds;
      renderTargetDirTree(targetDirTree, def.target_dir || "", true);
      $("f-target-name").value = "";
      $("f-enabled").checked = false;
      state.draftItems = buildDraftItems(def);
    } else {
      $("drawer-title").textContent = t("def.drawer_new");
      $("f-name").value = "";
      $("f-interval").value = "60";
      renderTargetDirTree(targetDirTree, "", true);
      $("f-target-name").value = "";
      $("f-enabled").checked = true;
      state.draftItems = buildDraftItems(null);
    }

    $("preview-meta").textContent = t("empty.dash");
    $("preview-wrap").innerHTML = "";
    $("f-rename-migrate").checked = true;
    highlightPeriodChip();
    updateTargetPreview();
    updateRenameWarning();
    $("drawer-backdrop").classList.add("open");

    await loadItemIntoForm(0, mode === "new");
    if (mode === "copy") {
      $("f-name").focus();
      $("f-name").select();
    }
  }

  function closeDrawer() {
    closeAllDirTrees();
    $("drawer-backdrop").classList.remove("open");
  }

  async function saveDefinition(runSampleAfter = false) {
    if (!validateNameInput()) {
      $("f-name").focus();
      return;
    }
    syncActiveItemFromForm();
    if (!state.draftItems.length) {
      toast(t("def.toast_need_source"), "error");
      return;
    }
    for (let i = 0; i < state.draftItems.length; i += 1) {
      const it = state.draftItems[i];
      if (!it.source_database || !it.source_table) {
        await loadItemIntoForm(i, false);
        toast(t("def.toast_need_source"), "error");
        return;
      }
      if (!it.fields || !it.fields.length) {
        await loadItemIntoForm(i, false);
        toast(t("def.toast_need_fields"), "error");
        return;
      }
    }
    const id = $("f-id").value;
    const orig = state.editingOriginal;
    const curTargetDir = currentTargetDir();
    const origDir = orig ? (orig.target_dir === (state.config.sqlite_dir || "samp") ? "" : orig.target_dir) : "";
    const targetChanged = Boolean(
      id &&
        orig &&
        ($("f-name").value.trim() !== orig.name ||
          curTargetDir !== origDir ||
          $("f-target-name").value.trim() !== orig.target_name)
    );
    const primary = state.draftItems[0];
    const payload = {
      name: $("f-name").value.trim(),
      source_database: primary.source_database,
      source_table: primary.source_table,
      target_dir: curTargetDir,
      target_name: $("f-target-name").value.trim(),
      fields: primary.fields,
      items: state.draftItems,
      interval_seconds: Number($("f-interval").value),
      filter: primary.filter || "",
      enabled: $("f-enabled").checked,
      rename_results: targetChanged ? $("f-rename-migrate").checked : false
    };
    try {
      const saved = await api(id ? `/api/definitions/${id}` : "/api/definitions", {
        method: id ? "PUT" : "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(payload)
      });
      if (runSampleAfter) {
        const res = await api(`/api/sample/${saved.id}`, { method: "POST" });
        toast(t("def.toast_sampled", { name: saved.samp_name, rows: res.rows_written, ms: res.duration_ms ?? 0 }), "ok");
      } else {
        const msg = saved.migrated_results?.length
          ? `${t("def.toast_updated")} · ${t("def.toast_migrated", { n: saved.migrated_results.length })}`
          : id
          ? t("def.toast_updated")
          : t("def.toast_created");
        toast(msg, "ok");
      }
      closeDrawer();
      await loadAll();
    } catch (err) {
      toast(err.message, "error");
    }
  }

  /* ===================== 整表采样居中双栏工作台 ===================== */
  function tsEnsureConfig(tb) {
    const existing = state.ts.configs[tb.table];
    if (existing) return existing;
    const cfg = {
      name: tb.table,
      interval: Math.max(0, Number($("ts-default-period").value || 60)),
      allFields: true,
      fields: [...(tb.fields || [])],
      filter: ""
    };
    state.ts.configs[tb.table] = cfg;
    return cfg;
  }

  function tsSyncActiveConfigFromForm() {
    const tbl = state.ts.activeTable;
    if (!tbl || !state.ts.configs[tbl]) return;
    const cfg = state.ts.configs[tbl];
    cfg.name = $("ts-point-name").value.trim() || tbl;
    cfg.interval = Math.max(0, Number($("ts-period").value) || 0);
    cfg.allFields = $("ts-all-fields").checked;
    if (!cfg.allFields) {
      const checked = checkedFields($("ts-fields"));
      if (state.ts.inspect || checked.length) cfg.fields = checked;
    }
    cfg.filter = $("ts-filter").value.trim();
  }

  function tsUpdateFooter() {
    const cnt = state.ts.selected.size;
    $("ts-foot-meta").textContent = t("def.ts_selected_cnt", { n: cnt });
    $("ts-save").disabled = cnt === 0;
    $("ts-save-run").disabled = cnt === 0;
    $("ts-save").textContent = t("def.ts_save", { n: cnt });
    $("ts-save-run").textContent = t("def.ts_save_run", { n: cnt });
  }

  function tsRenderTableList() {
    const listEl = $("ts-table-list");
    const kw = state.ts.keyword.trim().toLowerCase();
    const visible = state.ts.tables.filter(
      (tb) => !kw || tb.table.toLowerCase().includes(kw) || String(tb.chn_name || "").toLowerCase().includes(kw)
    );
    if (!visible.length) {
      listEl.innerHTML = `<div class="empty-cell">${esc(t("empty.dash"))}</div>`;
      $("ts-check-all").checked = false;
      tsUpdateFooter();
      return;
    }
    $("ts-check-all").checked = visible.every((tb) => state.ts.selected.has(tb.table));
    listEl.innerHTML = visible
      .map((tb) => {
        const cfg = tsEnsureConfig(tb);
        const checked = state.ts.selected.has(tb.table);
        const active = state.ts.activeTable === tb.table;
        const colBadge = cfg.allFields
          ? `<span class="badge ok">${esc(t("def.all_fields_wild"))}</span>`
          : `<span class="badge muted">${esc(t("def.ts_cols_n", { n: (cfg.fields || []).length }))}</span>`;
        const filterDot = cfg.filter ? `<span class="dot ok" title="${esc(cfg.filter)}"></span>` : "";
        const sub = tb.chn_name ? `${tb.chn_name} · ${tb.row_count}` : `${tb.row_count}`;
        return `
          <div class="ts-table-row ${active ? "active" : ""}" data-ts-table="${esc(tb.table)}">
            <input type="checkbox" class="ts-check" data-ts-check="${esc(tb.table)}" ${checked ? "checked" : ""}>
            <div class="ts-table-main">
              <div class="ts-table-name" title="${esc(tb.table)}">${esc(tb.table)}</div>
              <div class="ts-table-sub" title="${esc(sub)}">${esc(sub)}</div>
            </div>
            <div class="ts-table-meta">${filterDot}${colBadge}</div>
          </div>`;
      })
      .join("");
    tsUpdateFooter();
  }

  function tsRenderFields(presetFields = null) {
    const data = state.ts.inspect;
    if (!data) {
      $("ts-fields").innerHTML = `<span class="text-muted">${esc(t("empty.dash"))}</span>`;
      return;
    }
    const isWild = $("ts-all-fields").checked;
    const allBiz = (data.fields || []).filter((f) => f !== "row_key" && f !== "sample_time");
    const chosen = isWild ? allBiz : presetFields !== null ? presetFields : checkedFields($("ts-fields"));
    renderFieldChecks(
      $("ts-fields"),
      data.fields || [],
      chosen,
      data.field_types || {},
      data.field_labels || {},
      $("ts-field-search").value
    );
    $("ts-fields").classList.toggle("ts-fields-locked", isWild);
  }

  function tsRenderPreview(data) {
    $("ts-preview-meta").textContent = t("def.preview_meta", {
      matched: data.matched_rows ?? data.rows.length,
      total: data.total_rows ?? data.rows.length,
      cols: (data.fields || []).length
    });
    if (!data.rows || !data.rows.length) {
      $("ts-preview-wrap").innerHTML = `<div class="empty-cell">${esc(t("empty.dash"))}</div>`;
      return;
    }
    const cols = data.fields || [];
    const labels = data.field_labels || {};
    $("ts-preview-wrap").innerHTML = `
      <table class="data-table">
        <thead><tr>${cols.map((c) => `<th title="${esc(labels[c] || c)}">${esc(c)}</th>`).join("")}</tr></thead>
        <tbody>
          ${data.rows
            .map(
              (row) =>
                `<tr>${cols
                  .map((c) => {
                    const val = row[c];
                    const canClick = val !== null && val !== undefined && val !== "";
                    return `<td class="mono ${canClick ? "clickable-cell" : ""}" data-pcol="${esc(c)}" data-pval="${esc(val ?? "")}" title="${esc(t("def.click_cell_filter"))}">${esc(val)}</td>`;
                  })
                  .join("")}</tr>`
            )
            .join("")}
        </tbody>
      </table>`;
  }

  function tsFormatCondition(field, rawVal, op) {
    const fType = state.ts.inspect?.field_types?.[field] || "REAL";
    if (op === "in") {
      const cleaned = rawVal.trim().replace(/^\(|\)$/g, "");
      return `${field} in (${cleaned})`;
    }
    const text = String(rawVal ?? "").trim();
    if (fType === "TEXT" && !(text.startsWith("'") && text.endsWith("'"))) {
      return `${field}${op}'${text}'`;
    }
    return `${field}${op}${text}`;
  }

  function tsAppendFilter(clause) {
    const input = $("ts-filter");
    const curr = input.value.trim();
    if (!clause) return;
    if (!curr || !curr.toLowerCase().includes(clause.toLowerCase())) {
      input.value = curr ? `${curr} and ${clause}` : clause;
    }
    tsSyncActiveConfigFromForm();
    tsRenderTableList();
    tsInspectActive(false);
  }

  async function tsInspectActive(resetFieldsFromConfig = true) {
    const db = $("ts-source-db").value;
    const tbl = state.ts.activeTable;
    if (!db || !tbl) return;
    const filter = $("ts-filter").value.trim();
    try {
      const url = `/api/source/inspect?source_database=${encodeURIComponent(db)}&source_table=${encodeURIComponent(tbl)}&filter=${encodeURIComponent(filter)}`;
      const data = await api(url);
      state.ts.inspect = data;
      const cfg = state.ts.configs[tbl];
      const preset = resetFieldsFromConfig && cfg ? cfg.fields : null;
      tsRenderFields(preset);
      const fbSel = $("ts-fb-field");
      const labels = data.field_labels || {};
      fbSel.innerHTML = (data.fields || [])
        .map((c) => `<option value="${esc(c)}">${esc(labels[c] ? `${c} · ${labels[c]}` : c)}</option>`)
        .join("");
      const vals = data.sample_values?.[fbSel.value] || [];
      $("ts-fb-val-list").innerHTML = vals.map((v) => `<option value="${esc(v)}"></option>`).join("");
      tsRenderPreview(data);
    } catch (err) {
      $("ts-preview-meta").textContent = err.message;
      $("ts-preview-wrap").innerHTML = "";
    }
  }

  async function tsSelectTable(tableName) {
    tsSyncActiveConfigFromForm();
    const tb = state.ts.tables.find((x) => x.table === tableName);
    if (!tb) return;
    state.ts.activeTable = tableName;
    const cfg = tsEnsureConfig(tb);
    $("ts-point-name").value = cfg.name;
    $("ts-period").value = cfg.interval;
    $("ts-all-fields").checked = cfg.allFields;
    $("ts-field-search").value = "";
    $("ts-fb-val").value = "";
    $("ts-filter").value = cfg.filter || "";
    tsRenderTableList();
    await tsInspectActive(true);
  }

  function tsRenderSourceDbForDir(selectedDir, targetDb = null) {
    const sel = $("ts-source-db");
    const allDbs = state.config.source_databases || [];
    const filtered =
      selectedDir === ALL_DIRS_VAL
        ? allDbs
        : allDbs.filter((p) => !/^[a-zA-Z]:/.test(p) && !p.startsWith("/") && dirOf(p) === selectedDir);
    if (!filtered.length) {
      sel.innerHTML = `<option value="" disabled>${esc(t("def.no_db_in_dir"))}</option>`;
      sel.value = "";
      return;
    }
    sel.innerHTML = filtered
      .map((p) => `<option value="${esc(p)}" title="${esc(p)}">${esc(selectedDir === ALL_DIRS_VAL ? p : baseOf(p))}</option>`)
      .join("");
    sel.value = targetDb && filtered.includes(targetDb) ? targetDb : filtered[0];
  }

  async function tsLoadTables() {
    const db = $("ts-source-db").value;
    state.ts.tables = [];
    state.ts.selected.clear();
    state.ts.configs = {};
    state.ts.activeTable = null;
    state.ts.inspect = null;
    if (!db) {
      tsRenderTableList();
      $("ts-fields").innerHTML = `<span class="text-muted">${esc(t("empty.dash"))}</span>`;
      $("ts-preview-meta").textContent = t("empty.dash");
      $("ts-preview-wrap").innerHTML = "";
      return;
    }
    try {
      const summary = await api(`/api/source/tables/summary?source_database=${encodeURIComponent(db)}`);
      state.ts.tables = summary.tables || [];
      state.ts.tables.forEach((tb) => tsEnsureConfig(tb));
      if (state.ts.tables.length) {
        // 默认选中第一张表方便立刻单表整表采样，也可在左栏再多选
        const first = state.ts.tables[0];
        state.ts.selected.add(first.table);
        await tsSelectTable(first.table);
      } else {
        tsRenderTableList();
        $("ts-fields").innerHTML = `<span class="text-muted">${esc(t("empty.dash"))}</span>`;
        $("ts-preview-meta").textContent = t("empty.dash");
        $("ts-preview-wrap").innerHTML = "";
      }
    } catch (err) {
      tsRenderTableList();
      toast(err.message, "error");
    }
  }

  async function openTableSample() {
    try {
      state.config = await api("/api/config");
      renderMetrics();
    } catch {}
    const allDbs = state.config.source_databases || [];
    const dirs = collectedDirs();
    const countByDir = new Map();
    allDbs.forEach((p) => {
      if (/^[a-zA-Z]:/.test(p) || p.startsWith("/")) return;
      const d = dirOf(p);
      countByDir.set(d, (countByDir.get(d) || 0) + 1);
    });
    const firstDb = allDbs[0] || state.config.default_source_db || "";
    const initDir = firstDb && !/^[a-zA-Z]:/.test(firstDb) && !firstDb.startsWith("/") && dirs.includes(dirOf(firstDb)) ? dirOf(firstDb) : ALL_DIRS_VAL;
    tsSourceDirTree.setData(dirs, countByDir, allDbs.length, state.config.sqlite_dir || "samp");
    tsSourceDirTree.setValue(initDir, true);
    tsRenderSourceDbForDir(initDir, firstDb);
    renderTargetDirTree(tsTargetDirTree, "", false);

    $("ts-mode").value = "per_table";
    $("ts-bundle-name").value = "";
    $("ts-bundle-name").classList.add("hidden");
    $("ts-default-period").value = "60";
    $("ts-conflict").value = "overwrite";
    $("ts-enabled").checked = true;
    $("ts-table-search").value = "";
    state.ts.keyword = "";
    $("ts-backdrop").classList.add("open");
    await tsLoadTables();
  }

  function closeTableSample() {
    closeAllDirTrees();
    $("ts-backdrop").classList.remove("open");
  }

  async function submitTableSample(sampleNowFlag) {
    tsSyncActiveConfigFromForm();
    const db = $("ts-source-db").value;
    if (!db) {
      toast(t("def.toast_need_source"), "error");
      return;
    }
    const mode = $("ts-mode").value;
    const bundleName = $("ts-bundle-name").value.trim();
    if (mode === "single_db" && !bundleName) {
      toast(t("def.ts_need_bundle"), "error");
      $("ts-bundle-name").focus();
      return;
    }
    const selectedTables = state.ts.tables.filter((tb) => state.ts.selected.has(tb.table));
    if (!selectedTables.length) {
      toast(t("def.toast_need_tables"), "error");
      return;
    }
    const enabled = $("ts-enabled").checked;
    const tablesPayload = [];
    for (const tb of selectedTables) {
      const cfg = tsEnsureConfig(tb);
      const fields = cfg.allFields ? ["*"] : cfg.fields || [];
      if (!fields.length) {
        await tsSelectTable(tb.table);
        toast(t("def.toast_need_fields"), "error");
        return;
      }
      tablesPayload.push({
        source_table: tb.table,
        name: cfg.name || tb.table,
        target_table: mode === "single_db" ? tb.table : "",
        fields,
        filter: cfg.filter || "",
        interval_seconds: Math.max(0, Number(cfg.interval) || 0),
        enabled
      });
    }

    try {
      const res = await api("/api/table-sample", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          source_database: db,
          target_dir: (tsTargetDirTree ? tsTargetDirTree.getValue() : "").trim(),
          target_mode: mode,
          bundle_name: bundleName,
          conflict_mode: $("ts-conflict").value,
          sample_now: Boolean(sampleNowFlag),
          tables: tablesPayload
        })
      });
      const errCount = (res.errors || []).length + (res.sample_errors || []).length;
      toast(
        t("def.ts_toast_done", {
          c: res.created || 0,
          u: res.updated || 0,
          s: res.skipped || 0,
          rows: res.rows_written || 0
        }),
        errCount ? "error" : "ok"
      );
      closeTableSample();
      await loadAll();
    } catch (err) {
      toast(err.message, "error");
    }
  }

  /* ===================== 启停、删除、采样、导入导出 ===================== */
  async function toggleDefinition(id) {
    const def = state.definitions.find((d) => d.id === id);
    if (!def) return;
    try {
      await api(`/api/definitions/${id}/enable?enabled=${!def.enabled}`, { method: "POST" });
      toast(def.enabled ? t("def.toast_toggled_off") : t("def.toast_toggled_on"), "ok");
      await loadAll();
    } catch (err) {
      toast(err.message, "error");
    }
  }

  async function batchToggle(enabled) {
    const ids = [...state.selectedIds];
    if (!ids.length) return;
    try {
      const res = await api("/api/definitions/batch-enable", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ ids, enabled })
      });
      toast(t("def.toast_batch", { n: res.updated }), "ok");
      state.selectedIds.clear();
      await loadAll();
    } catch (err) {
      toast(err.message, "error");
    }
  }

  async function batchSample() {
    const ids = [...state.selectedIds];
    if (!ids.length) return;
    const btn = $("batch-sample");
    btn.disabled = true;
    try {
      const res = await api("/api/sample/batch", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ ids })
      });
      toast(
        t("def.toast_batch_sample", { ok: (res.completed || []).length, fail: (res.failed || []).length }),
        (res.failed || []).length ? "error" : "ok"
      );
      await loadAll();
    } catch (err) {
      toast(err.message, "error");
    } finally {
      btn.disabled = false;
    }
  }

  async function batchDelete() {
    const ids = [...state.selectedIds];
    if (!ids.length) return;
    const ok = await confirmModal({
      title: t("def.confirm_batch_del_title"),
      body: t("def.confirm_batch_del_body", { n: ids.length }),
      confirmText: t("action.delete")
    });
    if (!ok) return;
    try {
      const res = await api("/api/definitions/batch-delete", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ ids })
      });
      state.selectedIds.clear();
      toast(t("def.toast_batch_del", { n: res.deleted }), "ok");
      await loadAll();
    } catch (err) {
      toast(err.message, "error");
    }
  }

  async function deleteDefinition(id) {
    const def = state.definitions.find((d) => d.id === id);
    if (!def) return;
    const ok = await confirmModal({
      title: t("def.confirm_delete_title"),
      body: t("def.confirm_delete_body", { name: def.samp_name }),
      confirmText: t("action.delete")
    });
    if (!ok) return;
    try {
      await api(`/api/definitions/${id}`, { method: "DELETE" });
      state.selectedIds.delete(id);
      toast(t("def.toast_deleted"), "ok");
      await loadAll();
    } catch (err) {
      toast(err.message, "error");
    }
  }

  async function sampleNow(id) {
    try {
      const res = await api(`/api/sample/${id}`, { method: "POST" });
      toast(t("def.toast_sampled", { name: res.samp_name || `#${id}`, rows: res.rows_written, ms: res.duration_ms ?? 0 }), "ok");
      await loadAll();
    } catch (err) {
      toast(err.message, "error");
    }
  }

  async function loadAll() {
    try {
      const [cfg, defs, statusData] = await Promise.all([
        api("/api/config"),
        api("/api/definitions"),
        api("/api/status")
      ]);
      state.config = cfg;
      state.definitions = defs;
      state.health = statusData;
      state.runtimeById = new Map((statusData.items || []).map((it) => [it.id, it]));
      syncAlarmsFromStatus(statusData);
      const validIds = new Set(defs.map((d) => d.id));
      [...state.selectedIds].forEach((id) => {
        if (!validIds.has(id)) state.selectedIds.delete(id);
      });
      renderMetrics();
      renderTable();
    } catch (err) {
      toast(err.message, "error");
    }
  }

  async function exportJson() {
    try {
      const data = await api("/api/definitions/export");
      const blob = new Blob([JSON.stringify(data, null, 2)], { type: "application/json;charset=utf-8" });
      const link = document.createElement("a");
      link.href = URL.createObjectURL(blob);
      link.download = "samp_definitions.json";
      link.click();
      URL.revokeObjectURL(link.href);
    } catch (err) {
      toast(err.message, "error");
    }
  }

  async function submitImport(event) {
    event.preventDefault();
    const file = $("import-file").files?.[0];
    if (!file) return;
    let parsed;
    try {
      parsed = JSON.parse(await file.text());
    } catch {
      toast(t("def.toast_bad_json"), "error");
      return;
    }
    const items = Array.isArray(parsed) ? parsed : Array.isArray(parsed?.items) ? parsed.items : null;
    if (!items) {
      toast(t("def.toast_bad_json"), "error");
      return;
    }
    try {
      const res = await api("/api/definitions/import", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ items, mode: $("import-mode").value })
      });
      toast(
        t("def.toast_imported", { c: res.created, u: res.updated, s: res.skipped, e: (res.errors || []).length }),
        (res.errors || []).length ? "error" : "ok"
      );
      $("import-backdrop").classList.remove("open");
      $("import-file").value = "";
      await loadAll();
    } catch (err) {
      toast(err.message, "error");
    }
  }

  /* ===================== 事件绑定 ===================== */
  function bindEvents() {
    $("btn-export-json").addEventListener("click", exportJson);
    $("btn-import-json").addEventListener("click", () => $("import-backdrop").classList.add("open"));
    $("import-cancel").addEventListener("click", () => $("import-backdrop").classList.remove("open"));
    $("import-form").addEventListener("submit", submitImport);
    $("btn-table-sample").addEventListener("click", openTableSample);
    $("ts-close").addEventListener("click", closeTableSample);
    $("ts-cancel").addEventListener("click", closeTableSample);
    $("ts-backdrop").addEventListener("click", (e) => {
      if (e.target === $("ts-backdrop")) closeTableSample();
    });
    document.addEventListener("click", () => closeAllDirTrees());
    window.addEventListener("resize", () => closeAllDirTrees());
    document.addEventListener("keydown", (e) => {
      if ((e.ctrlKey || e.metaKey) && e.key === "Enter") {
        if ($("drawer-backdrop").classList.contains("open")) {
          e.preventDefault();
          saveDefinition(false);
          return;
        }
        if ($("ts-backdrop").classList.contains("open") && !$("ts-save").disabled) {
          e.preventDefault();
          submitTableSample(false);
          return;
        }
      }
      if (e.key !== "Escape") return;
      if (openDirTrees.size > 0) {
        closeAllDirTrees();
        return;
      }
      if ($("drawer-backdrop").classList.contains("open")) closeDrawer();
      if ($("ts-backdrop").classList.contains("open")) closeTableSample();
      if ($("import-backdrop").classList.contains("open")) $("import-backdrop").classList.remove("open");
    });

    // 初始化 4 个可折叠多级目录树选择器
    targetDirTree = createDirTreePicker($("f-target-dir-trigger"), {
      allowCustom: true,
      defaultDirMode: true,
      onChange: (val) => {
        const isCustom = val === CUSTOM_VAL;
        $("f-target-dir-custom").classList.toggle("hidden", !isCustom);
        if (isCustom) $("f-target-dir-custom").focus();
        updateTargetPreview();
        updateRenameWarning();
      }
    });
    sourceDirTree = createDirTreePicker($("f-source-dir-trigger"), {
      includeAll: true,
      showCounts: true,
      onChange: (val) => {
        renderSourceDbForDir(val, null);
        const db = currentSourceDb();
        if (db) {
          loadDrawerTables(null, null, true);
        } else {
          state.drawerTables = [];
          state.sourceData = null;
          renderDrawerTableSelect([], null);
          $("f-fields").innerHTML = `<span class="text-muted">${esc(t("empty.dash"))}</span>`;
          $("preview-meta").textContent = t("empty.dash");
          $("preview-wrap").innerHTML = "";
        }
      }
    });
    tsSourceDirTree = createDirTreePicker($("ts-source-dir-trigger"), {
      includeAll: true,
      showCounts: true,
      onChange: (val) => {
        tsRenderSourceDbForDir(val, null);
        tsLoadTables();
      }
    });
    tsTargetDirTree = createDirTreePicker($("ts-target-dir-trigger"), {
      defaultDirMode: true
    });
    $("def-form").querySelector(".drawer-body").addEventListener("scroll", () => closeAllDirTrees());
    $("ts-right").addEventListener("scroll", () => closeAllDirTrees());

    // 整表采样弹窗交互绑定
    $("ts-source-db").addEventListener("change", tsLoadTables);
    $("ts-mode").addEventListener("change", (e) => {
      const single = e.target.value === "single_db";
      $("ts-bundle-name").classList.toggle("hidden", !single);
      if (single && !$("ts-bundle-name").value.trim()) {
        const dbName = baseOf($("ts-source-db").value || "").replace(/\.(db|sqlite3|sqlite)$/i, "");
        $("ts-bundle-name").value = dbName ? uniquePointName(dbName) : "";
      }
    });
    $("ts-default-period").addEventListener("change", (e) => {
      const val = Math.max(0, Number(e.target.value) || 0);
      Object.values(state.ts.configs).forEach((cfg) => {
        cfg.interval = val;
      });
      if (state.ts.activeTable) $("ts-period").value = val;
    });
    $("ts-table-search").addEventListener("input", (e) => {
      state.ts.keyword = e.target.value;
      tsRenderTableList();
    });
    $("ts-check-all").addEventListener("change", (e) => {
      const checked = e.target.checked;
      const kw = state.ts.keyword.trim().toLowerCase();
      state.ts.tables.forEach((tb) => {
        if (kw && !tb.table.toLowerCase().includes(kw) && !String(tb.chn_name || "").toLowerCase().includes(kw)) return;
        if (checked) state.ts.selected.add(tb.table);
        else state.ts.selected.delete(tb.table);
      });
      tsRenderTableList();
    });
    $("ts-table-list").addEventListener("click", (e) => {
      const cb = e.target.closest(".ts-check");
      if (cb) {
        const tbl = cb.dataset.tsCheck;
        if (cb.checked) state.ts.selected.add(tbl);
        else state.ts.selected.delete(tbl);
        tsUpdateFooter();
        if (cb.checked && state.ts.activeTable !== tbl) tsSelectTable(tbl);
        return;
      }
      const row = e.target.closest(".ts-table-row[data-ts-table]");
      if (row) tsSelectTable(row.dataset.tsTable);
    });
    $("ts-point-name").addEventListener("input", () => tsSyncActiveConfigFromForm());
    $("ts-period").addEventListener("input", () => tsSyncActiveConfigFromForm());
    $("ts-all-fields").addEventListener("change", () => {
      tsSyncActiveConfigFromForm();
      tsRenderFields(state.ts.configs[state.ts.activeTable]?.fields || []);
      tsRenderTableList();
    });
    $("ts-field-search").addEventListener("input", () => tsRenderFields(null));
    $("ts-fields").addEventListener("change", () => {
      $("ts-all-fields").checked = false;
      tsSyncActiveConfigFromForm();
      tsRenderTableList();
    });
    $("ts-fields-all").addEventListener("click", () => {
      $("ts-all-fields").checked = false;
      $("ts-fields").classList.remove("ts-fields-locked");
      setAllFields($("ts-fields"), true);
      tsSyncActiveConfigFromForm();
      tsRenderTableList();
    });
    $("ts-fields-none").addEventListener("click", () => {
      $("ts-all-fields").checked = false;
      $("ts-fields").classList.remove("ts-fields-locked");
      setAllFields($("ts-fields"), false);
      tsSyncActiveConfigFromForm();
      tsRenderTableList();
    });
    $("ts-fields-num").addEventListener("click", () => {
      if (!state.ts.inspect) return;
      $("ts-all-fields").checked = false;
      tsRenderFields(state.ts.inspect.numeric_fields || []);
      tsSyncActiveConfigFromForm();
      tsRenderTableList();
    });
    $("ts-fb-field").addEventListener("change", () => {
      const vals = state.ts.inspect?.sample_values?.[$("ts-fb-field").value] || [];
      $("ts-fb-val-list").innerHTML = vals.map((v) => `<option value="${esc(v)}"></option>`).join("");
    });
    $("ts-add-cond").addEventListener("click", () => {
      const field = $("ts-fb-field").value;
      const op = $("ts-fb-op").value;
      const val = $("ts-fb-val").value.trim();
      if (!field || !val) return;
      tsAppendFilter(tsFormatCondition(field, val, op));
      $("ts-fb-val").value = "";
    });
    let _tsFilterTimer = null;
    $("ts-filter").addEventListener("input", () => {
      tsSyncActiveConfigFromForm();
      tsRenderTableList();
      clearTimeout(_tsFilterTimer);
      _tsFilterTimer = setTimeout(() => {
        const el = $("ts-filter");
        const ok = validateFilterExpr(el.value);
        el.classList.toggle("is-invalid", !ok);
        el.title = ok ? "" : t("def.filter_syntax_err");
      }, 350);
    });
    $("ts-test-filter").addEventListener("click", () => {
      tsSyncActiveConfigFromForm();
      tsInspectActive(false);
    });
    $("ts-preview-wrap").addEventListener("click", (e) => {
      const td = e.target.closest("td.clickable-cell");
      if (!td) return;
      const col = td.dataset.pcol;
      const val = td.dataset.pval;
      if (!col || val === "") return;
      tsAppendFilter(tsFormatCondition(col, val, "="));
    });
    $("ts-save").addEventListener("click", () => submitTableSample(false));
    $("ts-save-run").addEventListener("click", () => submitTableSample(true));

    // 主表筛选与操作
    $("def-search").addEventListener("input", (e) => {
      state.keyword = e.target.value;
      renderTable();
    });
    $("def-status").addEventListener("change", (e) => {
      state.statusFilter = e.target.value;
      renderTable();
    });
    $("btn-refresh").addEventListener("click", loadAll);
    $("btn-new").addEventListener("click", () => openDrawer("new"));
    $("drawer-close").addEventListener("click", closeDrawer);
    $("btn-cancel").addEventListener("click", closeDrawer);
    $("drawer-backdrop").addEventListener("click", (e) => {
      if (e.target === $("drawer-backdrop")) closeDrawer();
    });

    // 单点弹窗：多采集项切换/新增/删除
    $("btn-add-item").addEventListener("click", addDraftItem);
    $("items-strip").addEventListener("click", (e) => {
      const delBtn = e.target.closest("[data-item-del]");
      if (delBtn) {
        e.stopPropagation();
        removeDraftItem(Number(delBtn.dataset.itemDel));
        return;
      }
      const tab = e.target.closest("[data-item-idx]");
      if (tab) switchDraftItem(Number(tab.dataset.itemIdx));
    });

    // 单点弹窗：源库 / 源表自动级联
    $("f-source-db-select").addEventListener("change", (e) => {
      const isCustom = e.target.value === CUSTOM_VAL;
      $("f-source-db-custom").classList.toggle("hidden", !isCustom);
      $("f-source-db-custom").required = isCustom;
      if (!isCustom && e.target.value) loadDrawerTables(null, null, true);
      else if (isCustom) $("f-source-db-custom").focus();
    });
    $("f-source-table-select").addEventListener("change", (e) => {
      const isCustom = e.target.value === CUSTOM_VAL;
      $("f-source-table-custom").classList.toggle("hidden", !isCustom);
      if (!isCustom && e.target.value) {
        if (state.activeItemIdx > 0) {
          $("f-target-table").value = e.target.value;
        }
        inspectSource(null, true);
      } else if (isCustom) {
        $("f-source-table-custom").focus();
      }
    });
    $("f-target-table").addEventListener("input", () => {
      syncActiveItemFromForm();
      renderItemsStrip();
    });
    let _fFilterTimer = null;
    $("f-filter").addEventListener("input", () => {
      syncActiveItemFromForm();
      clearTimeout(_fFilterTimer);
      _fFilterTimer = setTimeout(() => {
        const el = $("f-filter");
        const ok = validateFilterExpr(el.value);
        el.classList.toggle("is-invalid", !ok);
        el.title = ok ? "" : t("def.filter_syntax_err");
      }, 350);
    });
    $("f-fields").addEventListener("change", () => {
      syncActiveItemFromForm();
      renderItemsStrip();
    });
    $("btn-load-tables").addEventListener("click", () => loadDrawerTables(null, null, true));
    $("btn-inspect").addEventListener("click", () => inspectSource(null, false));
    $("btn-test-filter").addEventListener("click", () => inspectSource(null, false));

    // 字段快捷选择与搜索
    $("f-all-fields").addEventListener("change", (e) => {
      const isWild = e.target.checked;
      $("f-fields").classList.toggle("ts-fields-locked", isWild);
      if (isWild && state.sourceData) {
        renderDrawerFields((state.sourceData.fields || []).filter((f) => f !== "row_key" && f !== "sample_time"));
      }
      syncActiveItemFromForm();
      renderItemsStrip();
    });
    $("f-field-search").addEventListener("input", () => renderDrawerFields(checkedFields($("f-fields"))));
    $("btn-fields-all").addEventListener("click", () => {
      $("f-all-fields").checked = false;
      $("f-fields").classList.remove("ts-fields-locked");
      setAllFields($("f-fields"), true);
      syncActiveItemFromForm();
      renderItemsStrip();
    });
    $("btn-fields-none").addEventListener("click", () => {
      $("f-all-fields").checked = false;
      $("f-fields").classList.remove("ts-fields-locked");
      setAllFields($("f-fields"), false);
      syncActiveItemFromForm();
      renderItemsStrip();
    });
    $("btn-fields-num").addEventListener("click", () => {
      if (!state.sourceData) return;
      $("f-all-fields").checked = false;
      $("f-fields").classList.remove("ts-fields-locked");
      renderDrawerFields(state.sourceData.numeric_fields || []);
      syncActiveItemFromForm();
      renderItemsStrip();
    });

    // 可视化过滤构造器与预览表点选
    $("fb-field").addEventListener("change", updateFilterSampleDatalist);
    $("btn-add-cond").addEventListener("click", () => {
      const field = $("fb-field").value;
      const op = $("fb-op").value;
      const val = $("fb-val").value.trim();
      if (!field || !val) return;
      appendFilterCondition(formatConditionLiteral(field, val, op));
      $("fb-val").value = "";
    });
    $("preview-wrap").addEventListener("click", (e) => {
      const td = e.target.closest("td.clickable-cell");
      if (!td) return;
      const col = td.dataset.pcol;
      const val = td.dataset.pval;
      if (!col || val === "") return;
      appendFilterCondition(formatConditionLiteral(col, val, "="));
    });

    // 保存 / 保存并采样 / 校验
    $("def-form").addEventListener("submit", (e) => {
      e.preventDefault();
      saveDefinition(false);
    });
    $("btn-save-sample").addEventListener("click", () => saveDefinition(true));
    $("f-interval").addEventListener("input", highlightPeriodChip);
    $("f-name").addEventListener("input", () => {
      state.nameTouched = true;
      updateTargetPreview();
      updateRenameWarning();
      validateNameInput();
    });
    $("f-target-dir-custom").addEventListener("input", () => {
      updateTargetPreview();
      updateRenameWarning();
    });
    $("f-target-name").addEventListener("input", () => {
      updateTargetPreview();
      updateRenameWarning();
    });
    $("period-chips").addEventListener("click", (e) => {
      const btn = e.target.closest("[data-period]");
      if (!btn) return;
      $("f-interval").value = btn.dataset.period;
      highlightPeriodChip();
    });

    $("batch-enable").addEventListener("click", () => batchToggle(true));
    $("batch-disable").addEventListener("click", () => batchToggle(false));
    $("batch-sample").addEventListener("click", batchSample);
    $("batch-delete").addEventListener("click", batchDelete);

    $("check-all").addEventListener("change", (e) => {
      const checked = e.target.checked;
      visibleDefinitions().forEach((d) => {
        if (checked) state.selectedIds.add(d.id);
        else state.selectedIds.delete(d.id);
      });
      renderTable();
    });

    $("def-tbody").addEventListener("change", (e) => {
      const cb = e.target.closest(".row-check");
      if (cb) {
        const id = Number(cb.dataset.id);
        if (cb.checked) state.selectedIds.add(id);
        else state.selectedIds.delete(id);
        renderTable();
        return;
      }
      const inline = e.target.closest(".inline-period, .inline-filter");
      if (inline) commitInlineUpdate(inline);
    });

    $("def-tbody").addEventListener("keydown", (e) => {
      const inline = e.target.closest(".inline-period, .inline-filter");
      if (inline && e.key === "Enter") {
        e.preventDefault();
        inline.blur();
      }
    });

    $("def-tbody").addEventListener("click", (e) => {
      const btn = e.target.closest("[data-act]");
      if (!btn) return;
      const id = Number(btn.dataset.id);
      const def = state.definitions.find((d) => d.id === id);
      if (btn.dataset.act === "edit" && def) openDrawer("edit", def);
      else if (btn.dataset.act === "copy" && def) openDrawer("copy", def);
      else if (btn.dataset.act === "sample") sampleNow(id);
      else if (btn.dataset.act === "toggle") toggleDefinition(id);
      else if (btn.dataset.act === "delete") deleteDefinition(id);
    });

    onLangChange(() => {
      renderMetrics();
      renderTable();
      targetDirTree?.refreshLabels();
      sourceDirTree?.refreshLabels();
      tsSourceDirTree?.refreshLabels();
      tsTargetDirTree?.refreshLabels();
      if ($("drawer-backdrop").classList.contains("open")) {
        renderItemsStrip();
      }
      if ($("ts-backdrop").classList.contains("open")) {
        tsRenderTableList();
      }
    });
  }

  initChrome({ pollGlobalAlarms: false });
  bindEvents();

  loadAll().then(() => {
    const editParam = new URLSearchParams(location.search).get("edit");
    if (editParam) {
      const def = state.definitions.find((d) => String(d.id) === editParam);
      if (def) openDrawer("edit", def);
    }
  });
  setInterval(loadAll, 15000);
})();

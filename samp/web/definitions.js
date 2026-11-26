/* 采样定义工作台：极速单点级联建点 + 可视化过滤构造器 + 整库批量建点向导 + 单元格无缝内嵌编辑 */

(function () {
  const { $, esc, t, ICONS, api, toast, confirmModal, checkedFields, setAllFields, renderFieldChecks, initChrome, onLangChange } = window.SAMP;

  const IDENT_RE = /^[A-Za-z_\u4e00-\u9fff][A-Za-z0-9_\u4e00-\u9fff]*$/;
  const CUSTOM_VAL = "__custom__";

  const state = {
    config: { default_source_db: "", source_databases: [] },
    definitions: [],
    health: null,
    selectedIds: new Set(),
    editingOriginalName: null,
    nameTouched: false,
    drawerTables: [],
    sourceData: null,
    keyword: "",
    statusFilter: "all",
    // 批量建点向导状态
    bwTables: [],
    bwSelected: new Set(),
    bwOverrides: {} // table -> { name, interval }
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
  }

  function renderMetrics() {
    const h = state.health || {};
    const sched = h.scheduler || {};
    $("m-defs").textContent = h.definitions ?? state.definitions.length;
    $("m-enabled").textContent = h.enabled ?? state.definitions.filter((d) => d.enabled).length;
    $("m-dbs").textContent = h.result_databases ?? 0;

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
      return [d.samp_name, d.source_table, d.source_database, d.filter].some((v) =>
        String(v || "").toLowerCase().includes(kw)
      );
    });
  }

  function renderTable() {
    const rows = visibleDefinitions();
    const tbody = $("def-tbody");
    const checkAll = $("check-all");

    if (!rows.length) {
      tbody.innerHTML = `<tr><td colspan="9" class="empty-cell">${esc(t("empty.dash"))}</td></tr>`;
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
        const fieldsStr = (d.fields || []).join(", ");
        const toggleTitle = d.enabled ? t("action.disable") : t("action.enable");
        return `
          <tr class="${checked ? "selected" : ""}" data-row-id="${d.id}">
            <td><input type="checkbox" class="row-check" data-id="${d.id}" ${checked ? "checked" : ""}></td>
            <td><strong>${esc(d.samp_name)}</strong></td>
            <td class="mono">${esc(d.source_table)}</td>
            <td class="mono cell-ellipsis" title="${esc(d.source_database)}">${esc(d.source_database)}</td>
            <td class="mono cell-ellipsis-sm" title="${esc(fieldsStr)}">${esc(fieldsStr)}</td>
            <td class="cell-input">
              <input type="number" min="0" step="1" class="num-in inline-period" data-id="${d.id}" value="${esc(d.interval_seconds)}" title="${esc(t("def.label_interval"))}">
            </td>
            <td class="cell-input">
              <input type="text" class="filter-in inline-filter" data-id="${d.id}" value="${esc(d.filter || "")}" placeholder="${esc(t("empty.dash"))}" title="${esc(d.filter || t("def.label_filter"))}">
            </td>
            <td>${badge}</td>
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

    try {
      const saved = await api(`/api/definitions/${id}`, {
        method: "PUT",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          name: def.samp_name,
          source_database: def.source_database,
          source_table: def.source_table,
          fields: def.fields,
          interval_seconds: nextInterval,
          filter: nextFilter,
          enabled: Boolean(def.enabled)
        })
      });
      def.interval_seconds = saved.interval_seconds;
      def.filter = saved.filter;
      inputEl.classList.remove("is-invalid");
      toast(t("def.toast_updated"), "ok");
    } catch (err) {
      inputEl.classList.add("is-invalid");
      toast(err.message, "error");
    }
  }

  /* ===================== 单点极速抽屉（自动级联 + 可视化过滤） ===================== */
  function renderSourceDbSelect(selectEl, targetDb = null, allowCustom = true) {
    const list = state.config.source_databases || [];
    const opts = list.map((p) => `<option value="${esc(p)}">${esc(p)}</option>`);
    if (allowCustom) {
      opts.push(`<option value="${CUSTOM_VAL}">${esc(t("def.custom_path"))}</option>`);
    }
    selectEl.innerHTML = opts.join("");
    const desired = targetDb || state.config.default_source_db || list[0] || "";
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

  function updateRenameWarning() {
    const editing = Boolean($("f-id").value);
    const changed = editing && state.editingOriginalName && $("f-name").value.trim() !== state.editingOriginalName;
    $("rename-warn").classList.toggle("show", Boolean(changed));
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
      if (presetFields !== null) {
        targetChecked = presetFields;
      } else if (autoSelectNumeric) {
        targetChecked = data.numeric_fields?.length
          ? data.numeric_fields
          : (data.fields || []).filter((f) => f !== "row_key" && f !== "sample_time");
      } else {
        targetChecked = checkedFields($("f-fields"));
      }
      renderDrawerFields(targetChecked);
      renderFilterBuilderOptions(data);
      renderPreview(data);

      // 新建且用户未手动改名时，自动预填不冲突采样点名
      if (!$("f-id").value && !state.nameTouched) {
        $("f-name").value = uniquePointName(table);
        validateNameInput();
      }
    } catch (err) {
      $("preview-meta").textContent = err.message;
      $("preview-wrap").innerHTML = "";
      toast(err.message, "error");
    }
  }

  async function openDrawer(mode, def = null) {
    $("f-id").value = mode === "edit" && def ? String(def.id) : "";
    state.editingOriginalName = mode === "edit" && def ? def.samp_name : null;
    state.nameTouched = mode !== "new";
    state.sourceData = null;
    $("f-field-search").value = "";
    $("fb-val").value = "";
    $("f-name-hint").textContent = "";
    $("f-name").classList.remove("is-invalid");

    if (mode === "edit" && def) {
      $("drawer-title").textContent = t("def.drawer_edit", { name: def.samp_name });
      $("f-name").value = def.samp_name;
      $("f-interval").value = def.interval_seconds;
      $("f-filter").value = def.filter || "";
      $("f-enabled").checked = Boolean(def.enabled);
      renderSourceDbSelect($("f-source-db-select"), def.source_database, true);
    } else if (mode === "copy" && def) {
      $("drawer-title").textContent = t("def.drawer_copy");
      $("f-name").value = uniquePointName(def.samp_name);
      $("f-interval").value = def.interval_seconds;
      $("f-filter").value = def.filter || "";
      $("f-enabled").checked = false;
      renderSourceDbSelect($("f-source-db-select"), def.source_database, true);
    } else {
      $("drawer-title").textContent = t("def.drawer_new");
      $("f-name").value = "";
      $("f-interval").value = "60";
      $("f-filter").value = "";
      $("f-enabled").checked = true;
      renderSourceDbSelect($("f-source-db-select"), state.config.default_source_db, true);
    }

    $("preview-meta").textContent = t("empty.dash");
    $("preview-wrap").innerHTML = "";
    $("f-rename-migrate").checked = true;
    highlightPeriodChip();
    updateRenameWarning();
    $("drawer-backdrop").classList.add("open");

    if (def) {
      await loadDrawerTables(def.source_table, def.fields || [], false);
      if (mode === "copy") {
        $("f-name").focus();
        $("f-name").select();
      }
    } else {
      await loadDrawerTables(null, null, true);
    }
  }

  function closeDrawer() {
    $("drawer-backdrop").classList.remove("open");
  }

  async function saveDefinition(runSampleAfter = false) {
    if (!validateNameInput()) {
      $("f-name").focus();
      return;
    }
    const db = currentSourceDb();
    const table = currentSourceTable();
    if (!db || !table) {
      toast(t("def.toast_need_source"), "error");
      return;
    }
    const fields = checkedFields($("f-fields"));
    if (!fields.length) {
      toast(t("def.toast_need_fields"), "error");
      return;
    }
    const id = $("f-id").value;
    const renamed = Boolean(id && state.editingOriginalName && $("f-name").value.trim() !== state.editingOriginalName);
    const payload = {
      name: $("f-name").value.trim(),
      source_database: db,
      source_table: table,
      fields,
      interval_seconds: Number($("f-interval").value),
      filter: $("f-filter").value.trim(),
      enabled: $("f-enabled").checked,
      rename_results: renamed ? $("f-rename-migrate").checked : false
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

  /* ===================== 整库批量建点向导 ===================== */
  function bwPointNameFor(table) {
    const ov = state.bwOverrides[table]?.name;
    if (ov !== undefined) return ov;
    const prefix = $("bw-prefix").value.trim();
    return `${prefix}${table}`;
  }

  function bwPeriodFor(table) {
    const ov = state.bwOverrides[table]?.interval;
    if (ov !== undefined) return ov;
    return Number($("bw-period").value || 60);
  }

  function bwFieldsFor(tb) {
    const mode = $("bw-field-mode").value;
    if (mode === "numeric" && tb.numeric_fields?.length) return tb.numeric_fields;
    return tb.fields || [];
  }

  function renderBatchWizardTable() {
    const tbody = $("bw-tbody");
    const submitBtn = $("batch-submit");
    const tables = state.bwTables || [];
    if (!tables.length) {
      tbody.innerHTML = `<tr><td colspan="7" class="empty-cell">${esc(t("empty.dash"))}</td></tr>`;
      $("bw-check-all").checked = false;
      submitBtn.disabled = true;
      submitBtn.textContent = t("action.batch_create", { n: 0 });
      return;
    }

    const selectable = tables.filter((tb) => bwFieldsFor(tb).length > 0);
    $("bw-check-all").checked = selectable.length > 0 && selectable.every((tb) => state.bwSelected.has(tb.table));

    tbody.innerHTML = tables
      .map((tb) => {
        const fields = bwFieldsFor(tb);
        const disabled = fields.length === 0;
        const checked = !disabled && state.bwSelected.has(tb.table);
        const linked = tb.existing_definitions?.length > 0;
        const badge = linked
          ? `<span class="badge warn" title="${esc(tb.existing_definitions.join(", "))}">${esc(t("def.batch_linked"))}</span>`
          : `<span class="badge ok">${esc(t("def.batch_new"))}</span>`;
        const tableLabel = tb.chn_name ? `${tb.table} · ${tb.chn_name}` : tb.table;
        return `
          <tr class="${checked ? "selected" : ""}">
            <td><input type="checkbox" class="bw-check" data-table="${esc(tb.table)}" ${checked ? "checked" : ""} ${disabled ? "disabled" : ""}></td>
            <td class="mono" title="${esc(tableLabel)}"><strong>${esc(tb.table)}</strong>${tb.chn_name ? ` <span class="text-muted">${esc(tb.chn_name)}</span>` : ""}</td>
            <td class="num">${esc(tb.row_count)}</td>
            <td class="num" title="${esc(fields.join(", "))}">${esc(fields.length)}</td>
            <td class="cell-input">
              <input type="text" class="bw-name" data-table="${esc(tb.table)}" value="${esc(bwPointNameFor(tb.table))}">
            </td>
            <td class="cell-input">
              <input type="number" min="0" step="1" class="num-in bw-period" data-table="${esc(tb.table)}" value="${esc(bwPeriodFor(tb.table))}">
            </td>
            <td>${badge}</td>
          </tr>`;
      })
      .join("");

    const count = state.bwSelected.size;
    submitBtn.disabled = count === 0;
    submitBtn.textContent = t("action.batch_create", { n: count });
  }

  async function loadBatchWizardTables() {
    const db = $("bw-db").value;
    if (!db) return;
    try {
      const summary = await api(`/api/source/tables/summary?source_database=${encodeURIComponent(db)}`);
      state.bwTables = summary.tables || [];
      state.bwOverrides = {};
      state.bwSelected = new Set(
        state.bwTables
          .filter((tb) => !tb.existing_definitions?.length && bwFieldsFor(tb).length > 0)
          .map((tb) => tb.table)
      );
      renderBatchWizardTable();
    } catch (err) {
      state.bwTables = [];
      state.bwSelected.clear();
      renderBatchWizardTable();
      toast(err.message, "error");
    }
  }

  async function openBatchWizard() {
    renderSourceDbSelect($("bw-db"), state.config.default_source_db, false);
    $("bw-prefix").value = "";
    $("bw-period").value = "60";
    $("bw-field-mode").value = "numeric";
    $("bw-conflict").value = "skip";
    $("bw-enabled").checked = true;
    $("batch-backdrop").classList.add("open");
    await loadBatchWizardTables();
  }

  async function submitBatchWizard() {
    const db = $("bw-db").value;
    const enabled = $("bw-enabled").checked;
    const items = [];
    for (const tb of state.bwTables) {
      if (!state.bwSelected.has(tb.table)) continue;
      const fields = bwFieldsFor(tb);
      if (!fields.length) continue;
      items.push({
        name: bwPointNameFor(tb.table).trim(),
        source_database: db,
        source_table: tb.table,
        fields,
        interval_seconds: Math.max(0, Number(bwPeriodFor(tb.table)) || 0),
        filter: "",
        enabled
      });
    }
    if (!items.length) {
      toast(t("def.toast_need_tables"), "error");
      return;
    }
    try {
      const res = await api("/api/definitions/import", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ items, mode: $("bw-conflict").value })
      });
      toast(
        t("def.toast_imported", { c: res.created, u: res.updated, s: res.skipped, e: (res.errors || []).length }),
        (res.errors || []).length ? "error" : "ok"
      );
      $("batch-backdrop").classList.remove("open");
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
      const [cfg, defs, health] = await Promise.all([
        api("/api/config"),
        api("/api/definitions"),
        api("/api/health")
      ]);
      state.config = cfg;
      state.definitions = defs;
      state.health = health;
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
    $("btn-batch-wizard").addEventListener("click", openBatchWizard);
    $("batch-close").addEventListener("click", () => $("batch-backdrop").classList.remove("open"));
    $("batch-cancel").addEventListener("click", () => $("batch-backdrop").classList.remove("open"));
    $("batch-submit").addEventListener("click", submitBatchWizard);
    $("bw-db").addEventListener("change", loadBatchWizardTables);
    $("bw-field-mode").addEventListener("change", renderBatchWizardTable);
    $("bw-prefix").addEventListener("input", renderBatchWizardTable);
    $("bw-period").addEventListener("input", renderBatchWizardTable);
    $("bw-check-all").addEventListener("change", (e) => {
      const checked = e.target.checked;
      state.bwTables.forEach((tb) => {
        if (bwFieldsFor(tb).length === 0) return;
        if (checked) state.bwSelected.add(tb.table);
        else state.bwSelected.delete(tb.table);
      });
      renderBatchWizardTable();
    });
    $("bw-tbody").addEventListener("change", (e) => {
      const cb = e.target.closest(".bw-check");
      if (cb) {
        if (cb.checked) state.bwSelected.add(cb.dataset.table);
        else state.bwSelected.delete(cb.dataset.table);
        renderBatchWizardTable();
        return;
      }
      const nameIn = e.target.closest(".bw-name");
      if (nameIn) {
        state.bwOverrides[nameIn.dataset.table] = {
          ...(state.bwOverrides[nameIn.dataset.table] || {}),
          name: nameIn.value
        };
        return;
      }
      const perIn = e.target.closest(".bw-period");
      if (perIn) {
        state.bwOverrides[perIn.dataset.table] = {
          ...(state.bwOverrides[perIn.dataset.table] || {}),
          interval: Number(perIn.value)
        };
      }
    });

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

    // 单点抽屉自动级联
    $("f-source-db-select").addEventListener("change", (e) => {
      const isCustom = e.target.value === CUSTOM_VAL;
      $("f-source-db-custom").classList.toggle("hidden", !isCustom);
      $("f-source-db-custom").required = isCustom;
      if (!isCustom) loadDrawerTables(null, null, true);
      else $("f-source-db-custom").focus();
    });
    $("f-source-table-select").addEventListener("change", (e) => {
      const isCustom = e.target.value === CUSTOM_VAL;
      $("f-source-table-custom").classList.toggle("hidden", !isCustom);
      if (!isCustom && e.target.value) inspectSource(null, !$("f-id").value);
      else if (isCustom) $("f-source-table-custom").focus();
    });
    $("btn-load-tables").addEventListener("click", () => loadDrawerTables(null, null, !$("f-id").value));
    $("btn-inspect").addEventListener("click", () => inspectSource(null, false));
    $("btn-test-filter").addEventListener("click", () => inspectSource(null, false));

    // 字段快捷选择与搜索
    $("f-field-search").addEventListener("input", () => renderDrawerFields(checkedFields($("f-fields"))));
    $("btn-fields-all").addEventListener("click", () => setAllFields($("f-fields"), true));
    $("btn-fields-none").addEventListener("click", () => setAllFields($("f-fields"), false));
    $("btn-fields-num").addEventListener("click", () => {
      if (!state.sourceData) return;
      renderDrawerFields(state.sourceData.numeric_fields || []);
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
      updateRenameWarning();
      validateNameInput();
    });
    $("period-chips").addEventListener("click", (e) => {
      const btn = e.target.closest("[data-period]");
      if (!btn) return;
      $("f-interval").value = btn.dataset.period;
      highlightPeriodChip();
    });

    $("batch-enable").addEventListener("click", () => batchToggle(true));
    $("batch-disable").addEventListener("click", () => batchToggle(false));

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
      renderBatchWizardTable();
    });
  }

  initChrome();
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

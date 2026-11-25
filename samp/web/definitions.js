/* 采样定义工作台逻辑 */

(function () {
  const { $, esc, t, api, toast, confirmModal, checkedFields, setAllFields, renderFieldChecks, initChrome, onLangChange } = window.SAMP;

  const state = {
    config: { default_source_db: "", source_databases: [] },
    definitions: [],
    health: null,
    selectedIds: new Set(),
    editingOriginalName: null,
    sourceData: null,
    keyword: "",
    statusFilter: "all"
  };

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

    const allChecked = rows.every((d) => state.selectedIds.has(d.id));
    checkAll.checked = allChecked;

    tbody.innerHTML = rows
      .map((d) => {
        const checked = state.selectedIds.has(d.id);
        const periodText = d.interval_seconds > 0 ? `${d.interval_seconds}${t("unit.sec")}` : t("state.manual");
        const badge = d.enabled
          ? `<span class="badge ok">${esc(t("state.enabled"))}</span>`
          : `<span class="badge muted">${esc(t("state.disabled"))}</span>`;
        const fieldsStr = (d.fields || []).join(", ");
        return `
          <tr class="${checked ? "selected" : ""}" data-row-id="${d.id}">
            <td><input type="checkbox" class="row-check" data-id="${d.id}" ${checked ? "checked" : ""}></td>
            <td><strong>${esc(d.samp_name)}</strong></td>
            <td class="mono">${esc(d.source_table)}</td>
            <td class="mono cell-ellipsis" title="${esc(d.source_database)}">${esc(d.source_database)}</td>
            <td class="mono cell-ellipsis-sm" title="${esc(fieldsStr)}">${esc(fieldsStr)}</td>
            <td class="num">${esc(periodText)}</td>
            <td class="mono cell-ellipsis-sm" title="${esc(d.filter || "")}">${esc(d.filter || t("empty.dash"))}</td>
            <td>${badge}</td>
            <td>
              <div class="cell-actions">
                <button type="button" class="btn xs" data-act="edit" data-id="${d.id}">${esc(t("action.edit"))}</button>
                <button type="button" class="btn xs" data-act="copy" data-id="${d.id}">${esc(t("action.copy"))}</button>
                <button type="button" class="btn xs" data-act="sample" data-id="${d.id}">${esc(t("action.sample_now"))}</button>
                <a class="btn xs" href="/query?id=${d.id}&range=hour" style="text-decoration:none">${esc(t("action.view_data"))}</a>
                <button type="button" class="btn xs" data-act="toggle" data-id="${d.id}">${esc(d.enabled ? t("action.disable") : t("action.enable"))}</button>
                <button type="button" class="btn xs danger" data-act="delete" data-id="${d.id}">${esc(t("action.delete"))}</button>
              </div>
            </td>
          </tr>`;
      })
      .join("");
    updateBatchButtons();
  }

  function renderSourceDbOptions() {
    const list = state.config.source_databases || [];
    $("source-db-list").innerHTML = list.map((p) => `<option value="${esc(p)}"></option>`).join("");
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

  function openDrawer(mode, def = null) {
    $("f-id").value = mode === "edit" && def ? String(def.id) : "";
    state.editingOriginalName = mode === "edit" && def ? def.samp_name : null;
    state.sourceData = null;

    if (mode === "edit" && def) {
      $("drawer-title").textContent = t("def.drawer_edit", { name: def.samp_name });
      $("f-name").value = def.samp_name;
      $("f-source-db").value = def.source_database;
      $("f-source-table").value = def.source_table;
      $("f-interval").value = def.interval_seconds;
      $("f-filter").value = def.filter || "";
      $("f-enabled").checked = Boolean(def.enabled);
      renderFieldChecks($("f-fields"), def.fields || [], def.fields || []);
    } else if (mode === "copy" && def) {
      $("drawer-title").textContent = t("def.drawer_copy");
      $("f-name").value = `${def.samp_name}_copy`;
      $("f-source-db").value = def.source_database;
      $("f-source-table").value = def.source_table;
      $("f-interval").value = def.interval_seconds;
      $("f-filter").value = def.filter || "";
      $("f-enabled").checked = false;
      renderFieldChecks($("f-fields"), def.fields || [], def.fields || []);
    } else {
      $("drawer-title").textContent = t("def.drawer_new");
      $("f-name").value = "";
      $("f-source-db").value = state.config.default_source_db || "";
      $("f-source-table").value = "";
      $("f-interval").value = "60";
      $("f-filter").value = "";
      $("f-enabled").checked = true;
      $("f-fields").innerHTML = `<span class="text-muted">${esc(t("empty.dash"))}</span>`;
    }

    $("preview-meta").textContent = t("empty.dash");
    $("preview-wrap").innerHTML = "";
    $("f-rename-migrate").checked = true;
    highlightPeriodChip();
    updateRenameWarning();
    $("drawer-backdrop").classList.add("open");

    if (def) {
      inspectSource(def.fields || []);
    }
  }

  function closeDrawer() {
    $("drawer-backdrop").classList.remove("open");
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
    $("preview-wrap").innerHTML = `
      <table class="data-table">
        <thead><tr>${cols.map((c) => `<th>${esc(c)}</th>`).join("")}</tr></thead>
        <tbody>
          ${data.rows.map((row) => `<tr>${cols.map((c) => `<td class="mono">${esc(row[c])}</td>`).join("")}</tr>`).join("")}
        </tbody>
      </table>`;
  }

  async function loadTables() {
    const db = $("f-source-db").value.trim();
    if (!db) {
      toast(t("def.toast_need_source"), "error");
      return;
    }
    try {
      const data = await api(`/api/source/tables?source_database=${encodeURIComponent(db)}`);
      $("source-table-list").innerHTML = (data.tables || []).map((tb) => `<option value="${esc(tb)}"></option>`).join("");
      if (data.tables && data.tables.length && !$("f-source-table").value.trim()) {
        $("f-source-table").value = data.tables[0];
        await inspectSource();
      }
      toast(t("def.toast_tables", { n: (data.tables || []).length }), "ok");
    } catch (err) {
      toast(err.message, "error");
    }
  }

  async function inspectSource(presetFields = null) {
    const db = $("f-source-db").value.trim();
    const table = $("f-source-table").value.trim();
    const filter = $("f-filter").value.trim();
    if (!db || !table) {
      toast(t("def.toast_need_source"), "error");
      return;
    }
    const keepChecked = presetFields !== null ? presetFields : checkedFields($("f-fields"));
    try {
      const url = `/api/source/inspect?source_database=${encodeURIComponent(db)}&source_table=${encodeURIComponent(table)}&filter=${encodeURIComponent(filter)}`;
      const data = await api(url);
      state.sourceData = data;
      renderFieldChecks($("f-fields"), data.fields || [], keepChecked, data.field_types || {});
      renderPreview(data);
    } catch (err) {
      $("preview-meta").textContent = err.message;
      $("preview-wrap").innerHTML = "";
      toast(err.message, "error");
    }
  }

  async function saveDefinition(event) {
    event.preventDefault();
    const fields = checkedFields($("f-fields"));
    if (!fields.length) {
      toast(t("def.toast_need_fields"), "error");
      return;
    }
    const id = $("f-id").value;
    const renamed = Boolean(id && state.editingOriginalName && $("f-name").value.trim() !== state.editingOriginalName);
    const payload = {
      name: $("f-name").value.trim(),
      source_database: $("f-source-db").value.trim(),
      source_table: $("f-source-table").value.trim(),
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
      const msg = saved.migrated_results?.length
        ? `${t("def.toast_updated")} · ${t("def.toast_migrated", { n: saved.migrated_results.length })}`
        : id
        ? t("def.toast_updated")
        : t("def.toast_created");
      toast(msg, "ok");
      closeDrawer();
      await loadAll();
    } catch (err) {
      toast(err.message, "error");
    }
  }

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
      renderSourceDbOptions();
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

  function bindEvents() {
    $("btn-export-json").addEventListener("click", exportJson);
    $("btn-import-json").addEventListener("click", () => $("import-backdrop").classList.add("open"));
    $("import-cancel").addEventListener("click", () => $("import-backdrop").classList.remove("open"));
    $("import-form").addEventListener("submit", submitImport);
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
    $("def-form").addEventListener("submit", saveDefinition);
    $("btn-load-tables").addEventListener("click", loadTables);
    $("btn-inspect").addEventListener("click", () => inspectSource());
    $("btn-test-filter").addEventListener("click", () => inspectSource());
    $("btn-fields-all").addEventListener("click", () => setAllFields($("f-fields"), true));
    $("btn-fields-none").addEventListener("click", () => setAllFields($("f-fields"), false));
    $("f-interval").addEventListener("input", highlightPeriodChip);
    $("f-name").addEventListener("input", updateRenameWarning);
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
      if (!cb) return;
      const id = Number(cb.dataset.id);
      if (cb.checked) state.selectedIds.add(id);
      else state.selectedIds.delete(id);
      renderTable();
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
      const periodZero = $("period-chips").querySelector('[data-period="0"]');
      if (periodZero) periodZero.textContent = `0 (${t("state.manual")})`;
    });
  }

  initChrome();
  bindEvents();
  const periodZero = $("period-chips").querySelector('[data-period="0"]');
  if (periodZero) periodZero.textContent = `0 (${t("state.manual")})`;

  loadAll().then(() => {
    const editParam = new URLSearchParams(location.search).get("edit");
    if (editParam) {
      const def = state.definitions.find((d) => String(d.id) === editParam);
      if (def) openDrawer("edit", def);
    }
  });
  setInterval(loadAll, 15000);
})();

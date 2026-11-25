/* 运行监控、执行日志与结果库管理逻辑（单次 /api/status 消除 N+1） */

(function () {
  const { $, esc, t, ICONS, api, toast, confirmModal, formatTime, formatBytes, syncAlarmsFromStatus, initChrome, onLangChange } = window.SAMP;

  const state = {
    activeTab: location.hash === "#storage" ? "storage" : location.hash === "#logs" ? "logs" : "status",
    statusData: null,
    logs: [],
    storage: null,
    logFilter: "all"
  };

  function setTab(tab) {
    state.activeTab = tab;
    $("rt-tabs").querySelectorAll(".tab-btn").forEach((btn) => {
      btn.classList.toggle("active", btn.dataset.tab === tab);
    });
    $("pane-status").classList.toggle("hidden", tab !== "status");
    $("pane-logs").classList.toggle("hidden", tab !== "logs");
    $("pane-storage").classList.toggle("hidden", tab !== "storage");
    $("log-filter").classList.toggle("hidden", tab !== "logs");
    if (tab === "logs") loadLogs();
    if (tab === "storage") loadStorage();
  }

  function formatCountdown(nextTs) {
    if (!nextTs) return t("empty.dash");
    const diff = Math.max(0, Math.round(nextTs - Date.now() / 1000));
    if (diff < 60) return `${diff}${t("unit.sec")}`;
    const m = Math.floor(diff / 60);
    const s = diff % 60;
    return `${m}m${s}s`;
  }

  function renderMetrics() {
    const data = state.statusData || {};
    const sched = data.scheduler || {};
    const items = data.items || [];
    const autoCount = items.filter((i) => i.enabled && i.interval_seconds > 0).length;
    const totalSize = items.reduce((acc, i) => acc + (i.result_size_bytes || 0), 0);

    const dot = $("rt-dot");
    const schedEl = $("rt-sched");
    if (sched.enabled === false) {
      dot.className = "dot warn";
      schedEl.className = "metric-val warn";
      schedEl.textContent = t("state.disabled_sched");
    } else if ((sched.failed || 0) > 0 && !sched.last_success_at) {
      dot.className = "dot danger";
      schedEl.className = "metric-val danger";
      schedEl.textContent = t("state.error");
    } else {
      dot.className = "dot ok";
      schedEl.className = "metric-val ok";
      schedEl.textContent = t("state.running");
    }

    $("rt-enabled").textContent = `${data.enabled ?? 0} / ${data.definitions ?? 0}`;
    $("rt-auto").textContent = String(autoCount);
    $("rt-ok").textContent = String(sched.sampled ?? 0);
    const failEl = $("rt-fail");
    failEl.textContent = String(sched.failed ?? 0);
    failEl.className = `metric-val ${(sched.failed || 0) > 0 ? "danger" : ""}`;
    $("rt-cycle").textContent = sched.last_cycle_at ? formatTime(sched.last_cycle_at, true) : t("empty.dash");
    $("rt-size").textContent = formatBytes(totalSize);
  }

  function renderStatusTable() {
    const tbody = $("status-tbody");
    const items = state.statusData?.items || [];
    if (!items.length) {
      tbody.innerHTML = `<tr><td colspan="12" class="empty-cell">${esc(t("empty.dash"))}</td></tr>`;
      return;
    }

    tbody.innerHTML = items
      .map((item) => {
        const enabledBadge = item.enabled
          ? `<span class="badge ok">${esc(t("state.enabled"))}</span>`
          : `<span class="badge muted">${esc(t("state.disabled"))}</span>`;
        const period = item.interval_seconds > 0 ? `${item.interval_seconds}${t("unit.sec")}` : t("state.manual");
        const years = item.result_years?.length ? item.result_years.join(", ") : t("empty.dash");

        let msgHtml = `<span class="text-muted">${esc(t("state.idle"))}</span>`;
        if (item.last_status === "error") {
          msgHtml = `<span class="badge danger" title="${esc(item.last_error || "")}">${esc(t("state.error"))}</span> <span class="text-danger">${esc(item.last_error || "")}</span>`;
        } else if (item.last_status === "ok") {
          msgHtml = `<span class="badge ok">${esc(t("state.ok"))}</span> <span class="mono">+${esc(item.last_rows_written ?? 0)}</span>`;
        }

        return `
          <tr>
            <td><strong>${esc(item.name)}</strong></td>
            <td>${enabledBadge}</td>
            <td class="num">${esc(period)}</td>
            <td class="mono">${esc(item.source_table)}</td>
            <td class="mono">${esc(years)}</td>
            <td class="num">${esc(item.total_rows ?? 0)}</td>
            <td class="num">${esc(formatBytes(item.result_size_bytes))}</td>
            <td class="num">${esc(formatTime(item.last_result_at, true))}</td>
            <td class="num">${esc(formatCountdown(item.next_run_at))}</td>
            <td class="num">${item.last_duration_ms !== null && item.last_duration_ms !== undefined ? esc(item.last_duration_ms) : esc(t("empty.dash"))}</td>
            <td class="cell-ellipsis" title="${esc(item.last_error || "")}">${msgHtml}</td>
            <td class="sticky-actions">
              <div class="cell-actions">
                <button type="button" class="btn xs icon-only" data-act="sample" data-id="${item.id}" title="${esc(t("action.sample_now"))}">${ICONS.play}</button>
                <a class="btn xs icon-only" href="/query?id=${item.id}&range=hour" title="${esc(t("action.view_data"))}">${ICONS.chart}</a>
                <a class="btn xs icon-only" href="/definitions?edit=${item.id}" title="${esc(t("action.edit"))}">${ICONS.edit}</a>
                <button type="button" class="btn xs icon-only" data-act="toggle" data-id="${item.id}" data-enabled="${item.enabled ? "1" : "0"}" title="${esc(item.enabled ? t("action.disable") : t("action.enable"))}">${ICONS.power}</button>
              </div>
            </td>
          </tr>`;
      })
      .join("");
  }

  function renderLogsTable() {
    const tbody = $("logs-tbody");
    const logs = state.logs || [];
    if (!logs.length) {
      tbody.innerHTML = `<tr><td colspan="7" class="empty-cell">${esc(t("empty.dash"))}</td></tr>`;
      return;
    }
    tbody.innerHTML = logs
      .map((row) => {
        const triggerKey = row.trigger === "auto" ? "rt.trigger_auto" : row.trigger === "batch" ? "rt.trigger_batch" : "rt.trigger_manual";
        const badge =
          row.status === "error"
            ? `<span class="badge danger">${esc(t("state.error"))}</span>`
            : `<span class="badge ok">${esc(t("state.ok"))}</span>`;
        return `
          <tr>
            <td class="num">${esc(formatTime(row.time, true))}</td>
            <td><strong>${esc(row.samp_name)}</strong></td>
            <td>${esc(t(triggerKey))}</td>
            <td>${badge}</td>
            <td class="num">${esc(row.rows_written ?? 0)}</td>
            <td class="num">${esc(row.duration_ms ?? 0)}</td>
            <td class="mono cell-ellipsis ${row.status === "error" ? "text-danger" : ""}" title="${esc(row.message || "")}">${esc(row.message || t("empty.dash"))}</td>
          </tr>`;
      })
      .join("");
  }

  function renderStorageTable() {
    const tbody = $("storage-tbody");
    const items = state.storage?.items || [];
    if (!items.length) {
      tbody.innerHTML = `<tr><td colspan="9" class="empty-cell">${esc(t("empty.dash"))}</td></tr>`;
      return;
    }
    tbody.innerHTML = items
      .map((db) => {
        const range =
          db.min_sample_time !== null && db.max_sample_time !== null
            ? `${formatTime(db.min_sample_time, true)} ~ ${formatTime(db.max_sample_time, true)}`
            : t("empty.dash");
        const rtdbBadge = db.rtdb_registered
          ? `<span class="badge ok">${esc(t("state.registered"))}</span>`
          : `<span class="badge warn">${esc(t("state.unregistered"))}</span>`;
        const linkBadge = db.is_orphan
          ? `<span class="badge warn">${esc(t("state.orphan"))}</span>`
          : `<span class="badge ok">${esc(t("state.active"))}</span>`;
        return `
          <tr>
            <td class="mono">${esc(db.filename)}</td>
            <td><strong>${esc(db.samp_name)}</strong></td>
            <td class="num">${esc(db.year)}</td>
            <td class="num">${esc(db.row_count)}</td>
            <td class="num">${esc(formatBytes(db.size_bytes))}</td>
            <td class="mono">${esc(range)}</td>
            <td>${rtdbBadge}</td>
            <td>${linkBadge}</td>
            <td class="sticky-actions">
              <button type="button" class="btn xs icon-only danger" data-del-db="${esc(db.filename)}" data-rows="${db.row_count}" data-size="${esc(formatBytes(db.size_bytes))}" title="${esc(t("action.delete"))}">${ICONS.trash}</button>
            </td>
          </tr>`;
      })
      .join("");
  }

  async function loadStatus() {
    try {
      const data = await api("/api/status");
      state.statusData = data;
      syncAlarmsFromStatus(data);
      renderMetrics();
      renderStatusTable();
      if (state.activeTab === "logs" && state.logFilter === "all") {
        state.logs = data.recent_logs || [];
        renderLogsTable();
      }
    } catch (err) {
      toast(err.message, "error");
    }
  }

  async function loadLogs() {
    try {
      const data = await api(`/api/runtime/logs?limit=200&status=${encodeURIComponent(state.logFilter)}`);
      state.logs = data.logs || [];
      renderLogsTable();
    } catch (err) {
      toast(err.message, "error");
    }
  }

  async function loadStorage() {
    try {
      state.storage = await api("/api/storage/databases");
      renderStorageTable();
    } catch (err) {
      toast(err.message, "error");
    }
  }

  async function sampleAll() {
    const btn = $("btn-sample-all");
    btn.disabled = true;
    try {
      const res = await api("/api/sample/all", { method: "POST" });
      toast(t("rt.batch_done", { ok: res.completed.length, fail: res.failed.length }), res.failed.length ? "error" : "ok");
      await loadStatus();
      if (state.activeTab === "logs") await loadLogs();
      if (state.activeTab === "storage") await loadStorage();
    } catch (err) {
      toast(err.message, "error");
    } finally {
      btn.disabled = false;
    }
  }

  async function sampleOne(id) {
    try {
      const res = await api(`/api/sample/${id}`, { method: "POST" });
      toast(t("def.toast_sampled", { name: res.samp_name || `#${id}`, rows: res.rows_written, ms: res.duration_ms ?? 0 }), "ok");
      await loadStatus();
    } catch (err) {
      toast(err.message, "error");
    }
  }

  async function toggleOne(id, currentlyEnabled) {
    try {
      await api(`/api/definitions/${id}/enable?enabled=${!currentlyEnabled}`, { method: "POST" });
      toast(currentlyEnabled ? t("def.toast_toggled_off") : t("def.toast_toggled_on"), "ok");
      await loadStatus();
    } catch (err) {
      toast(err.message, "error");
    }
  }

  async function deleteStorageDb(filename, rows, sizeText) {
    const ok = await confirmModal({
      title: t("rt.confirm_del_db_title"),
      body: t("rt.confirm_del_db_body", { file: filename, rows, size: sizeText }),
      confirmText: t("action.delete")
    });
    if (!ok) return;
    try {
      await api(`/api/storage/databases/${encodeURIComponent(filename)}`, { method: "DELETE" });
      toast(t("rt.toast_db_deleted", { file: filename }), "ok");
      await Promise.all([loadStorage(), loadStatus()]);
    } catch (err) {
      toast(err.message, "error");
    }
  }

  function bindEvents() {
    $("rt-tabs").addEventListener("click", (e) => {
      const btn = e.target.closest("[data-tab]");
      if (!btn) return;
      location.hash = btn.dataset.tab === "status" ? "" : `#${btn.dataset.tab}`;
      setTab(btn.dataset.tab);
    });
    $("log-filter").addEventListener("change", (e) => {
      state.logFilter = e.target.value;
      loadLogs();
    });
    $("btn-sample-all").addEventListener("click", sampleAll);
    $("btn-refresh").addEventListener("click", () => {
      loadStatus();
      if (state.activeTab === "logs") loadLogs();
      if (state.activeTab === "storage") loadStorage();
    });
    $("status-tbody").addEventListener("click", (e) => {
      const btn = e.target.closest("[data-act]");
      if (!btn) return;
      const id = Number(btn.dataset.id);
      if (btn.dataset.act === "sample") sampleOne(id);
      else if (btn.dataset.act === "toggle") toggleOne(id, btn.dataset.enabled === "1");
    });
    $("storage-tbody").addEventListener("click", (e) => {
      const btn = e.target.closest("[data-del-db]");
      if (!btn) return;
      deleteStorageDb(btn.dataset.delDb, btn.dataset.rows, btn.dataset.size);
    });
    onLangChange(() => {
      renderMetrics();
      renderStatusTable();
      renderLogsTable();
      renderStorageTable();
    });
  }

  initChrome({ pollGlobalAlarms: false });
  bindEvents();
  setTab(state.activeTab);
  loadStatus();
  setInterval(() => {
    loadStatus();
    if (state.activeTab === "logs") loadLogs();
    if (state.activeTab === "storage") loadStorage();
  }, 15000);
})();

/* 运行监控、执行日志与结果库管理逻辑（含 Sparkline、异常置顶、Data Bar、在线透视与 VACUUM） */

(function () {
  const {
    $,
    esc,
    t,
    ICONS,
    api,
    toast,
    confirmModal,
    formatTime,
    formatBytes,
    formatStartModeSummary,
    syncAlarmsFromStatus,
    initChrome,
    onLangChange,
    onThemeChange
  } = window.SAMP;

  const state = {
    activeTab: location.hash === "#storage" ? "storage" : location.hash === "#logs" ? "logs" : "status",
    statusData: null,
    logs: [],
    storage: null,
    statusFilter: "all",
    logFilter: "all",
    inspectPath: ""
  };

  function setTab(tab) {
    state.activeTab = tab;
    $("rt-tabs").querySelectorAll(".tab-btn").forEach((btn) => {
      btn.classList.toggle("active", btn.dataset.tab === tab);
    });
    $("pane-status").classList.toggle("hidden", tab !== "status");
    $("spark-strip").classList.toggle("hidden", tab !== "status");
    $("status-filter").classList.toggle("hidden", tab !== "status");
    $("pane-logs").classList.toggle("hidden", tab !== "logs");
    $("pane-storage").classList.toggle("hidden", tab !== "storage");
    $("log-filter").classList.toggle("hidden", tab !== "logs");
    if (tab === "status") renderSparkline();
    if (tab === "logs") loadLogs();
    if (tab === "storage") loadStorage();
  }

  function formatCountdown(nextTs, item = null) {
    if (item && item.enabled && item.start_mode === "trigger") {
      return `<span class="badge ok">${esc(t("trig.waiting"))}</span>`;
    }
    if (!nextTs) return esc(t("empty.dash"));
    const diff = Math.max(0, Math.round(nextTs - Date.now() / 1000));
    if (diff < 60) return esc(`${diff}${t("unit.sec")}`);
    if (diff < 3600) {
      const m = Math.floor(diff / 60);
      const s = diff % 60;
      return esc(`${m}m${s}s`);
    }
    if (diff < 86400) {
      const h = Math.floor(diff / 3600);
      const m = Math.floor((diff % 3600) / 60);
      return esc(`${h}h${m}m`);
    }
    return esc(formatTime(nextTs, true));
  }

  function renderMetrics() {
    const data = state.statusData || {};
    const sched = data.scheduler || {};
    const items = data.items || [];
    const autoCount = items.filter(
      (i) => i.enabled && (i.start_mode === "fixed" || i.start_mode === "trigger" || i.interval_seconds > 0)
    ).length;
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

  /* 近 50 次采样执行耗时与成败 Sparkline（跟随 8 套主题色） */
  function renderSparkline() {
    const canvas = $("spark-canvas");
    const metaEl = $("spark-meta");
    if (!canvas || !metaEl) return;
    const logs = [...(state.statusData?.recent_logs || [])].slice(0, 50).reverse();
    const dpr = Math.max(1, Math.min(2, window.devicePixelRatio || 1));
    const width = Math.max(120, Math.floor(canvas.clientWidth || 300));
    const height = Math.max(20, Math.floor(canvas.clientHeight || 24));
    canvas.width = Math.round(width * dpr);
    canvas.height = Math.round(height * dpr);
    const ctx = canvas.getContext("2d");
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    ctx.clearRect(0, 0, width, height);

    const cs = getComputedStyle(document.documentElement);
    const okColor = cs.getPropertyValue("--ok").trim() || "#16A34A";
    const dangerColor = cs.getPropertyValue("--danger").trim() || "#DC2626";
    const lineColor = cs.getPropertyValue("--border-color").trim() || "rgba(148,163,184,0.35)";

    ctx.strokeStyle = lineColor;
    ctx.lineWidth = 1;
    ctx.beginPath();
    ctx.moveTo(0, height - 1.5);
    ctx.lineTo(width, height - 1.5);
    ctx.stroke();

    if (!logs.length) {
      metaEl.textContent = t("empty.dash");
      return;
    }

    const okCount = logs.filter((r) => r.status !== "error").length;
    const avgMs = Math.round(logs.reduce((s, r) => s + Number(r.duration_ms || 0), 0) / logs.length);
    const rate = Math.round((okCount / logs.length) * 100);
    metaEl.textContent = t("rt.spark_rate", { rate, avg: avgMs });

    const maxMs = Math.max(10, ...logs.map((r) => Number(r.duration_ms || 0)));
    const slotW = width / Math.max(logs.length, 20);
    const barW = Math.max(2, Math.min(10, slotW - 2));
    const offsetX = width - logs.length * slotW;

    logs.forEach((row, idx) => {
      const ms = Math.max(1, Number(row.duration_ms || 0));
      const h = Math.max(3, Math.round((ms / maxMs) * (height - 4)));
      const x = offsetX + idx * slotW + (slotW - barW) / 2;
      const y = height - 2 - h;
      ctx.fillStyle = row.status === "error" ? dangerColor : okColor;
      ctx.fillRect(x, y, barW, h);
    });
  }

  function dataBarTd(value, maxVal, text, isWarn = false) {
    const num = Number(value || 0);
    const pct = maxVal > 0 && num > 0 ? Math.max(4, Math.min(100, Math.round((num / maxVal) * 100))) : 0;
    const fillHtml = pct > 0 ? `<span class="data-bar-fill${isWarn ? " warn" : ""}" style="width:${pct}%"></span>` : "";
    return `<td class="num data-bar-cell">${fillHtml}<span class="data-bar-text">${esc(text)}</span></td>`;
  }

  function renderStatusTable() {
    const tbody = $("status-tbody");
    const rawItems = state.statusData?.items || [];
    const filtered = rawItems.filter((item) => {
      const mode = item.start_mode || "periodic";
      if (state.statusFilter === "error") return item.last_status === "error";
      if (state.statusFilter === "periodic") return mode === "periodic" && item.interval_seconds > 0;
      if (state.statusFilter === "fixed") return mode === "fixed";
      if (state.statusFilter === "trigger") return mode === "trigger";
      if (state.statusFilter === "manual") return mode === "periodic" && item.interval_seconds === 0;
      return true;
    });
    // 异常项自动置顶，同级保持原有顺序
    const items = [...filtered].sort((a, b) => {
      const ae = a.last_status === "error" ? 0 : 1;
      const be = b.last_status === "error" ? 0 : 1;
      return ae - be;
    });

    if (!items.length) {
      tbody.innerHTML = `<tr><td colspan="12" class="empty-cell">${esc(t("empty.dash"))}</td></tr>`;
      return;
    }

    const maxRows = Math.max(0, ...items.map((i) => Number(i.total_rows || 0)));
    const maxSize = Math.max(0, ...items.map((i) => Number(i.result_size_bytes || 0)));
    const maxMs = Math.max(0, ...items.map((i) => Number(i.last_duration_ms || 0)));

    tbody.innerHTML = items
      .map((item) => {
        const enabledBadge = item.enabled
          ? `<span class="badge ok">${esc(t("state.enabled"))}</span>`
          : `<span class="badge muted">${esc(t("state.disabled"))}</span>`;
        const modeInfo = formatStartModeSummary(item);
        const modeBadgeCls = modeInfo.mode === "fixed" ? "ok" : modeInfo.mode === "trigger" ? "warn" : "muted";
        const modeCell =
          modeInfo.mode === "periodic"
            ? `<span class="mono">${esc(modeInfo.shortText)}</span>`
            : `<span class="badge ${modeBadgeCls}" title="${esc(modeInfo.title)}">${esc(modeInfo.shortText)}</span>`;
        const years = item.result_years?.length ? item.result_years.join(", ") : t("empty.dash");
        const itemBadge =
          item.item_count > 1 ? ` <span class="badge ok">${esc(t("def.item_count", { n: item.item_count }))}</span>` : "";

        let msgHtml = `<span class="text-muted">${esc(t("state.idle"))}</span>`;
        if (item.last_status === "error") {
          msgHtml = `<span class="badge danger" title="${esc(item.last_error || "")}">${esc(t("state.error"))}</span> <span class="text-danger">${esc(item.last_error || "")}</span>`;
        } else if (item.last_status === "ok") {
          msgHtml = `<span class="badge ok">${esc(t("state.ok"))}</span> <span class="mono">+${esc(item.last_rows_written ?? 0)}</span>`;
        }

        const durText =
          item.last_duration_ms !== null && item.last_duration_ms !== undefined
            ? String(item.last_duration_ms)
            : t("empty.dash");

        return `
          <tr>
            <td><strong>${esc(item.name)}</strong>${itemBadge}</td>
            <td>${enabledBadge}</td>
            <td class="cell-ellipsis-sm" title="${esc(modeInfo.title)}">${modeCell}</td>
            <td class="mono">${esc(item.source_table)}</td>
            <td class="mono" title="${esc(item.target_preview || "")}">${esc(years)}</td>
            ${dataBarTd(item.total_rows, maxRows, String(item.total_rows ?? 0))}
            ${dataBarTd(item.result_size_bytes, maxSize, formatBytes(item.result_size_bytes))}
            <td class="num">${esc(formatTime(item.last_result_at, true))}</td>
            <td class="num">${formatCountdown(item.next_run_at, item)}</td>
            ${dataBarTd(item.last_duration_ms, maxMs, durText, Number(item.last_duration_ms || 0) >= 500)}
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
        const triggerMap = {
          auto: "rt.trigger_auto",
          fixed: "rt.trigger_fixed",
          flag: "rt.trigger_flag",
          api: "rt.trigger_api",
          batch: "rt.trigger_batch",
          manual: "rt.trigger_manual"
        };
        const triggerKey = triggerMap[row.trigger] || "rt.trigger_manual";
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

    const defByName = new Map((state.statusData?.items || []).map((it) => [it.name, it.id]));
    const maxRows = Math.max(0, ...items.map((d) => Number(d.row_count || 0)));
    const maxSize = Math.max(0, ...items.map((d) => Number(d.size_bytes || 0)));

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
        const dbPath = db.path || db.filename;
        const tblBadge =
          db.table_count > 1
            ? ` <span class="badge ok" title="${esc((db.tables || []).join(", "))}">${esc(db.table_count)}T</span>`
            : "";
        const linkedId = !db.is_orphan ? defByName.get(db.samp_name) : null;
        const queryLink = linkedId
          ? `<a class="btn xs icon-only" href="/query?id=${linkedId}&range=day" title="${esc(t("action.view_data"))}">${ICONS.chart}</a>`
          : "";
        return `
          <tr>
            <td class="mono cell-ellipsis" title="${esc(dbPath)}">${esc(dbPath)}${tblBadge}</td>
            <td><strong>${esc(db.samp_name)}</strong></td>
            <td class="num">${esc(db.year ?? t("empty.dash"))}</td>
            ${dataBarTd(db.row_count, maxRows, String(db.row_count))}
            ${dataBarTd(db.size_bytes, maxSize, formatBytes(db.size_bytes))}
            <td class="mono">${esc(range)}</td>
            <td>${rtdbBadge}</td>
            <td>${linkBadge}</td>
            <td class="sticky-actions">
              <div class="cell-actions">
                <button type="button" class="btn xs icon-only" data-inspect-db="${esc(dbPath)}" title="${esc(t("action.inspect_db"))}">${ICONS.eye}</button>
                <button type="button" class="btn xs icon-only" data-vacuum-db="${esc(dbPath)}" title="${esc(t("action.vacuum_db"))}">${ICONS.compress}</button>
                ${queryLink}
                <button type="button" class="btn xs icon-only danger" data-del-db="${esc(dbPath)}" data-rows="${db.row_count}" data-size="${esc(formatBytes(db.size_bytes))}" title="${esc(t("action.delete"))}">${ICONS.trash}</button>
              </div>
            </td>
          </tr>`;
      })
      .join("");
  }

  async function openInspectDb(dbPath, tableName = "") {
    state.inspectPath = dbPath;
    try {
      const params = new URLSearchParams({ path: dbPath, limit: "20" });
      if (tableName) params.set("table", tableName);
      const data = await api(`/api/storage/inspect?${params.toString()}`);

      $("inspect-db-title").textContent = t("rt.inspect_title", { file: data.path || dbPath });
      const totalRows = (data.tables || []).reduce((s, tb) => s + Number(tb.row_count || 0), 0);
      $("inspect-db-meta").textContent = t("rt.inspect_meta", {
        tables: (data.tables || []).length,
        rows: totalRows,
        size: formatBytes(data.size_bytes)
      });

      const tablesEl = $("inspect-db-tables");
      tablesEl.innerHTML = (data.tables || [])
        .map((tb) => {
          const active = tb.name === data.active_table ? " active-chip" : "";
          return `<button type="button" class="btn xs mono${active}" data-insp-table="${esc(tb.name)}">${esc(tb.name)} (${tb.row_count})</button>`;
        })
        .join("");

      const activeMeta = (data.tables || []).find((tb) => tb.name === data.active_table);
      $("inspect-db-cols").innerHTML = (activeMeta?.columns || [])
        .map(
          (col) =>
            `<span class="field-pill mono" style="height:20px;font-size:11px;cursor:default">${esc(col.name)} <span class="field-type-tag">${esc(col.type || "TEXT")}</span></span>`
        )
        .join("");

      const cols = data.columns || [];
      const rows = data.rows || [];
      if (!cols.length || !rows.length) {
        $("inspect-db-wrap").innerHTML = `<div class="empty-cell">${esc(t("empty.dash"))}</div>`;
      } else {
        const thead = `<tr>${cols.map((c) => `<th class="mono">${esc(c)}</th>`).join("")}</tr>`;
        const tbody = rows
          .map(
            (r) =>
              `<tr>${cols
                .map((c) => {
                  const val = c === "sample_time" ? formatTime(r[c], true) : r[c] ?? "";
                  return `<td class="mono">${esc(val)}</td>`;
                })
                .join("")}</tr>`
          )
          .join("");
        $("inspect-db-wrap").innerHTML = `<table class="data-table"><thead>${thead}</thead><tbody>${tbody}</tbody></table>`;
      }
      $("inspect-db-backdrop").classList.add("open");
    } catch (err) {
      toast(err.message, "error");
    }
  }

  async function vacuumStorageDb(dbPath) {
    try {
      const res = await api("/api/storage/vacuum", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ path: dbPath })
      });
      toast(t("rt.toast_vacuum", { file: res.path || dbPath, saved: formatBytes(res.saved_bytes || 0) }), "ok");
      await Promise.all([loadStorage(), loadStatus()]);
    } catch (err) {
      toast(err.message, "error");
    }
  }

  async function loadStatus() {
    try {
      const data = await api("/api/status");
      state.statusData = data;
      syncAlarmsFromStatus(data);
      renderMetrics();
      renderSparkline();
      renderStatusTable();
      if (state.activeTab === "logs" && state.logFilter === "all") {
        state.logs = data.recent_logs || [];
        renderLogsTable();
      }
      if (state.activeTab === "storage" && state.storage) {
        renderStorageTable();
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
      const encodedPath = String(filename)
        .split("/")
        .map((seg) => encodeURIComponent(seg))
        .join("/");
      await api(`/api/storage/databases/${encodedPath}`, { method: "DELETE" });
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
    $("status-filter").addEventListener("change", (e) => {
      state.statusFilter = e.target.value;
      renderStatusTable();
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
      const inspectBtn = e.target.closest("[data-inspect-db]");
      if (inspectBtn) {
        openInspectDb(inspectBtn.dataset.inspectDb);
        return;
      }
      const vacuumBtn = e.target.closest("[data-vacuum-db]");
      if (vacuumBtn) {
        vacuumStorageDb(vacuumBtn.dataset.vacuumDb);
        return;
      }
      const delBtn = e.target.closest("[data-del-db]");
      if (delBtn) {
        deleteStorageDb(delBtn.dataset.delDb, delBtn.dataset.rows, delBtn.dataset.size);
      }
    });
    $("inspect-db-tables").addEventListener("click", (e) => {
      const btn = e.target.closest("[data-insp-table]");
      if (!btn || !state.inspectPath) return;
      openInspectDb(state.inspectPath, btn.dataset.inspTable);
    });
    $("inspect-db-close").addEventListener("click", () => {
      $("inspect-db-backdrop").classList.remove("open");
    });
    $("inspect-db-backdrop").addEventListener("click", (e) => {
      if (e.target === $("inspect-db-backdrop")) $("inspect-db-backdrop").classList.remove("open");
    });
    document.addEventListener("keydown", (e) => {
      if (e.key === "Escape" && $("inspect-db-backdrop").classList.contains("open")) {
        $("inspect-db-backdrop").classList.remove("open");
      }
    });
    window.addEventListener("resize", () => {
      if (state.activeTab === "status") renderSparkline();
    });
    onThemeChange(() => {
      if (state.activeTab === "status") renderSparkline();
    });
    onLangChange(() => {
      renderMetrics();
      renderSparkline();
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

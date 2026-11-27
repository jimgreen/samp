/* SAMP 大屏幕主画面控制器：驱动 5 联 KPI、动态 SVG 采样流拓扑、全息成功率圆环与 Canvas 趋势图 */

(function () {
  const { $, esc, t, api, toast, formatBytes, initChrome, onLangChange, onThemeChange, syncAlarmsFromStatus } = window.SAMP;

  const CIRCUMFERENCE = 2 * Math.PI * 44; // r=44 => 276.46
  const state = {
    status: null,
    storage: null,
    config: null,
    activeTab: "logs",
    refreshMs: 5000,
    timer: null,
    loading: false
  };

  /* ===================== 数据拉取与刷新控制 ===================== */
  async function loadAll(showSpin = false) {
    if (state.loading) return;
    state.loading = true;
    const btn = $("btn-dash-refresh");
    if (showSpin && btn) btn.classList.add("spinning");
    try {
      const [statusData, storageData, configData] = await Promise.all([
        api("/api/status"),
        api("/api/storage/databases"),
        state.config ? Promise.resolve(state.config) : api("/api/config")
      ]);
      state.status = statusData;
      state.storage = storageData;
      state.config = configData;
      syncAlarmsFromStatus(statusData);
      renderAll();
    } catch (err) {
      toast(err.message || String(err), "error");
    } finally {
      state.loading = false;
      if (btn) btn.classList.remove("spinning");
    }
  }

  function scheduleRefresh() {
    clearInterval(state.timer);
    state.timer = null;
    if (state.refreshMs > 0) {
      state.timer = setInterval(() => {
        if (!document.hidden) loadAll(false);
      }, state.refreshMs);
    }
  }

  /* ===================== 核心数据统计与渲染 ===================== */
  function computeMetrics() {
    const st = state.status || {};
    const db = state.storage || {};
    const cfg = state.config || {};
    const items = st.items || [];
    const logs = st.recent_logs || [];
    const sched = st.scheduler || {};

    const totalDefs = items.length;
    const enabledDefs = items.filter((it) => it.enabled).length;
    const totalRows = items.reduce((acc, it) => acc + Number(it.total_rows || 0), 0);

    const periodic = items.filter((it) => (it.start_mode || "periodic") === "periodic");
    const fixed = items.filter((it) => it.start_mode === "fixed");
    const trigger = items.filter((it) => it.start_mode === "trigger");

    const nowSec = Math.floor(Date.now() / 1000);
    const dueTimes = items
      .filter((it) => it.enabled && it.next_due_at)
      .map((it) => Number(it.next_due_at))
      .filter((ts) => Number.isFinite(ts) && ts > 0)
      .sort((a, b) => a - b);
    const nextDue = dueTimes.length ? dueTimes[0] : null;
    const nextDelta = nextDue !== null ? Math.max(0, nextDue - nowSec) : null;

    const fixedDueTimes = fixed
      .filter((it) => it.enabled && it.next_due_at)
      .map((it) => Number(it.next_due_at))
      .sort((a, b) => a - b);
    const fixedNextDelta = fixedDueTimes.length ? Math.max(0, fixedDueTimes[0] - nowSec) : null;

    const faultItems = items.filter((it) => it.last_status === "error" || it.last_error);
    const faultCount = faultItems.length + (sched.last_error ? 1 : 0);

    const logTotal = logs.length;
    const logOk = logs.filter((lg) => lg.status === "ok").length;
    const avgMs = logTotal
      ? Math.round(logs.reduce((acc, lg) => acc + Number(lg.duration_ms || 0), 0) / logTotal)
      : 0;
    const successRate = logTotal ? Math.round((logOk / logTotal) * 100) : null;

    const dbList = db.items || [];
    const rtdbCount = dbList.filter((d) => d.rtdb_registered).length;
    const orphanCount = Number(db.orphans || 0);
    const totalSize = Number(db.total_size_bytes || 0);
    const srcDbs = (cfg.source_databases || []).length;

    return {
      st,
      db,
      cfg,
      items,
      logs,
      sched,
      totalDefs,
      enabledDefs,
      totalRows,
      periodic,
      fixed,
      trigger,
      nextDelta,
      fixedNextDelta,
      faultCount,
      logTotal,
      logOk,
      avgMs,
      successRate,
      dbList,
      rtdbCount,
      orphanCount,
      totalSize,
      srcDbs
    };
  }

  function formatCountdown(sec) {
    if (sec === null || sec === undefined) return t("empty.dash");
    if (sec <= 1) return t("dash.next_soon");
    if (sec < 120) return t("dash.next_in", { sec });
    const m = Math.floor(sec / 60);
    const s = sec % 60;
    return `${m}m ${s}s`;
  }

  function renderAll() {
    const m = computeMetrics();

    // 顶栏路径与健康微标
    const rootEl = $("dash-root-tag");
    if (rootEl && m.cfg.root_dir) {
      rootEl.textContent = m.cfg.root_dir;
      rootEl.title = m.cfg.root_dir;
    }

    const chipEl = $("dash-health-chip");
    if (chipEl) {
      if (m.successRate === null) {
        chipEl.textContent = `${t("dash.health_rate")} --`;
        chipEl.className = "health-chip";
      } else {
        chipEl.textContent = `${t("dash.health_rate")} ${m.successRate}%`;
        chipEl.className = `health-chip ${m.successRate < 80 ? "danger" : m.successRate < 98 ? "warn" : ""}`;
      }
    }

    // 左侧底部运行健康卡
    const sideSched = $("side-sched");
    if (sideSched) {
      if (!m.sched.enabled) {
        sideSched.textContent = t("state.disabled_sched");
        sideSched.className = "warn";
      } else if (m.sched.last_error) {
        sideSched.textContent = t("state.error");
        sideSched.className = "danger";
      } else {
        sideSched.textContent = t("state.running");
        sideSched.className = "ok";
      }
    }
    $("side-enabled").textContent = `${m.enabledDefs} / ${m.totalDefs}`;
    $("side-dbs").textContent = String(m.dbList.length);
    $("side-size").textContent = formatBytes(m.totalSize);

    // 顶部 5 联 KPI
    $("kpi-defs-val").textContent = `${m.enabledDefs} / ${m.totalDefs}`;
    $("kpi-rows-val").textContent = m.totalRows.toLocaleString();
    $("kpi-dbs-val").textContent = String(m.dbList.length);
    $("kpi-dbs-sub").textContent = t("dash.kpi_dbs_sub", { rtdb: m.rtdbCount, orphan: m.orphanCount });
    $("kpi-size-val").textContent = formatBytes(m.totalSize);
    const okRuns = Number(m.sched.total_sampled || 0);
    const failRuns = Number(m.sched.total_failed || 0);
    const runsEl = $("kpi-runs-val");
    runsEl.textContent = `${okRuns} / ${failRuns}`;
    runsEl.className = failRuns > 0 ? "danger" : "ok";

    // 舞台顶部环境芯片
    $("chip-workers").textContent = String(m.cfg.workers ?? "—");
    $("chip-align").textContent = m.cfg.align ? t("dash.chip_align_on") : t("dash.chip_align_off");
    $("chip-next").textContent = formatCountdown(m.nextDelta);

    // 左 3 启动通道节点
    const periodicOn = m.periodic.filter((x) => x.enabled).length;
    $("val-periodic").textContent = String(m.periodic.length);
    $("sub-periodic").textContent = `${periodicOn} ${t("state.enabled")}`;

    const fixedOn = m.fixed.filter((x) => x.enabled).length;
    $("val-fixed").textContent = String(m.fixed.length);
    $("sub-fixed").textContent = m.fixedNextDelta !== null ? formatCountdown(m.fixedNextDelta) : `${fixedOn} ${t("state.enabled")}`;

    const triggerOn = m.trigger.filter((x) => x.enabled).length;
    $("val-trigger").textContent = String(m.trigger.length);
    $("sub-trigger").textContent = triggerOn > 0 ? `${triggerOn} · ${t("dash.trig_api")}` : t("dash.trig_api");

    // 右 3 数据链路节点
    $("val-source").textContent = String(m.srcDbs);
    $("sub-source").textContent = m.cfg.default_source_db || "SQLite";

    $("val-result").textContent = String(m.dbList.length);
    $("sub-result").textContent = `RTDB ${m.rtdbCount} · ${formatBytes(m.totalSize)}`;

    $("val-fault").textContent = String(m.faultCount);
    const faultNode = $("node-fault");
    const faultIcon = $("icon-fault");
    const faultVal = $("val-fault");
    const faultSub = $("sub-fault");
    if (m.faultCount > 0) {
      faultNode.className = "topo-node danger-state";
      faultIcon.className = "node-icon danger";
      faultVal.className = "node-val danger";
      faultSub.textContent = t("state.error");
    } else {
      faultNode.className = "topo-node ok-state";
      faultIcon.className = "node-icon ok";
      faultVal.className = "node-val ok";
      faultSub.textContent = t("dash.fault_zero");
    }

    // 中央全息八角成功率圆环
    const arc = $("hud-gauge-arc");
    const rateVal = $("hud-rate-val");
    const rateSub = $("hud-rate-sub");
    if (m.successRate === null) {
      rateVal.textContent = "--";
      rateSub.textContent = t("dash.center_empty");
      if (arc) arc.style.strokeDashoffset = String(CIRCUMFERENCE);
    } else {
      rateVal.textContent = String(m.successRate);
      rateSub.textContent = t("dash.center_sub", { n: m.logTotal, ms: m.avgMs });
      if (arc) {
        const offset = CIRCUMFERENCE * (1 - Math.max(0, Math.min(100, m.successRate)) / 100);
        arc.style.strokeDashoffset = offset.toFixed(2);
      }
    }

    renderTopologyFlows(m);
    renderTrendChart(m.logs);
    renderFeed(m);
  }

  /* ===================== 动态 SVG 发光贝塞尔脉冲连线 ===================== */
  function renderTopologyFlows(m) {
    const stage = $("hero-stage");
    const svg = $("hero-flow-svg");
    const center = $("hero-center-hud");
    if (!stage || !svg || !center) return;

    const sRect = stage.getBoundingClientRect();
    const cRect = center.getBoundingClientRect();
    if (!sRect.width || !sRect.height) return;

    const cxLeft = cRect.left - sRect.left;
    const cxRight = cRect.right - sRect.left;
    const cy = cRect.top - sRect.top + cRect.height / 2;

    const leftLinks = [
      { id: "node-periodic", active: m.periodic.some((x) => x.enabled), kind: "accent", dy: -22 },
      { id: "node-fixed", active: m.fixed.some((x) => x.enabled), kind: "ok", dy: 0 },
      { id: "node-trigger", active: m.trigger.some((x) => x.enabled), kind: "accent", dy: 22 }
    ];
    const rightLinks = [
      { id: "node-source", active: m.srcDbs > 0, kind: "accent", dy: -22 },
      { id: "node-result", active: m.dbList.length > 0, kind: "ok", dy: 0 },
      { id: "node-fault", active: m.faultCount > 0, kind: m.faultCount > 0 ? "danger" : "idle", dy: 22 }
    ];

    const paths = [];
    const buildCurve = (x1, y1, x2, y2, active, kind) => {
      const dx = Math.abs(x2 - x1) * 0.48;
      const d = `M ${x1.toFixed(1)} ${y1.toFixed(1)} C ${(x1 + dx).toFixed(1)} ${y1.toFixed(1)}, ${(x2 - dx).toFixed(1)} ${y2.toFixed(1)}, ${x2.toFixed(1)} ${y2.toFixed(1)}`;
      const cls = active ? (kind === "ok" ? "" : kind) : "idle";
      return `<path class="flow-track ${cls}" d="${d}"/><path class="flow-path ${cls}" d="${d}"/>`;
    };

    leftLinks.forEach((lk) => {
      const el = $(lk.id);
      if (!el) return;
      const r = el.getBoundingClientRect();
      const x1 = r.right - sRect.left;
      const y1 = r.top - sRect.top + r.height / 2;
      paths.push(buildCurve(x1, y1, cxLeft, cy + lk.dy, lk.active, lk.kind));
    });

    rightLinks.forEach((lk) => {
      const el = $(lk.id);
      if (!el) return;
      const r = el.getBoundingClientRect();
      const x2 = r.left - sRect.left;
      const y2 = r.top - sRect.top + r.height / 2;
      paths.push(buildCurve(cxRight, cy + lk.dy, x2, y2, lk.active, lk.kind));
    });

    svg.setAttribute("viewBox", `0 0 ${sRect.width} ${sRect.height}`);
    const defs = `<defs><filter id="silk-glow" x="-25%" y="-25%" width="150%" height="150%"><feGaussianBlur stdDeviation="2.4" result="blur"/><feMerge><feMergeNode in="blur"/><feMergeNode in="SourceGraphic"/></feMerge></filter></defs>`;
    svg.innerHTML = defs + paths.join("");
  }

  /* ===================== 近 50 次耗时与写入吞吐 Canvas 趋势图 ===================== */
  function renderTrendChart(logs) {
    const canvas = $("dash-trend-canvas");
    if (!canvas) return;
    const rect = canvas.getBoundingClientRect();
    if (!rect.width || !rect.height) return;

    const dpr = window.devicePixelRatio || 1;
    canvas.width = Math.round(rect.width * dpr);
    canvas.height = Math.round(rect.height * dpr);
    const ctx = canvas.getContext("2d");
    ctx.scale(dpr, dpr);

    const style = getComputedStyle(document.documentElement);
    const accent = style.getPropertyValue("--accent").trim() || "#2563EB";
    const okColor = style.getPropertyValue("--ok").trim() || "#0D9488";
    const danger = style.getPropertyValue("--danger").trim() || "#DC2626";
    const textSec = style.getPropertyValue("--text-secondary").trim() || "#64748B";
    const border = style.getPropertyValue("--border-color").trim() || "#E2E8F0";
    const fontSans = style.getPropertyValue("--font-sans").trim() || "Inter, Segoe UI, Microsoft YaHei, sans-serif";
    const fontMono = style.getPropertyValue("--font-mono").trim() || "JetBrains Mono, Consolas, monospace";

    const w = rect.width;
    const h = rect.height;
    ctx.clearRect(0, 0, w, h);

    const data = [...(logs || [])].reverse().slice(-32);
    if (!data.length) {
      ctx.fillStyle = textSec;
      ctx.font = `12px ${fontSans}`;
      ctx.textAlign = "center";
      ctx.textBaseline = "middle";
      ctx.fillText(t("dash.empty_logs"), w / 2, h / 2);
      return;
    }

    const pad = { top: 14, right: 44, bottom: 24, left: 44 };
    const cw = Math.max(40, w - pad.left - pad.right);
    const ch = Math.max(40, h - pad.top - pad.bottom);

    const maxMs = Math.max(10, ...data.map((d) => Number(d.duration_ms || 0)));
    const maxRows = Math.max(1, ...data.map((d) => Number(d.rows_written || 0)));

    // 水平参考网格
    ctx.strokeStyle = border;
    ctx.globalAlpha = 0.45;
    ctx.lineWidth = 1;
    ctx.font = `11px ${fontMono}`;
    for (let i = 0; i <= 3; i += 1) {
      const y = pad.top + (ch * i) / 3;
      ctx.beginPath();
      ctx.moveTo(pad.left, y);
      ctx.lineTo(pad.left + cw, y);
      ctx.stroke();

      const msVal = Math.round(maxMs * (1 - i / 3));
      const rowVal = Math.round(maxRows * (1 - i / 3));
      ctx.globalAlpha = 0.9;
      ctx.fillStyle = textSec;
      ctx.textAlign = "right";
      ctx.textBaseline = "middle";
      ctx.fillText(`${msVal}`, pad.left - 6, y);
      ctx.textAlign = "left";
      ctx.fillText(`${rowVal}`, pad.left + cw + 6, y);
      ctx.globalAlpha = 0.45;
    }
    ctx.globalAlpha = 1;

    const step = cw / Math.max(1, data.length);
    const barW = Math.max(4, Math.min(14, step * 0.48));

    // 写入行数柱图
    data.forEach((d, idx) => {
      const rows = Number(d.rows_written || 0);
      const bh = (rows / maxRows) * ch;
      const x = pad.left + idx * step + (step - barW) / 2;
      const y = pad.top + ch - bh;
      ctx.fillStyle = d.status === "error" ? danger : okColor;
      ctx.globalAlpha = d.status === "error" ? 0.55 : 0.34;
      ctx.fillRect(x, y, barW, Math.max(2, bh));
    });
    ctx.globalAlpha = 1;

    // 耗时折线与柔和面积
    const pts = data.map((d, idx) => ({
      x: pad.left + idx * step + step / 2,
      y: pad.top + ch - (Number(d.duration_ms || 0) / maxMs) * ch,
      err: d.status === "error"
    }));

    ctx.beginPath();
    pts.forEach((p, idx) => (idx === 0 ? ctx.moveTo(p.x, p.y) : ctx.lineTo(p.x, p.y)));
    ctx.strokeStyle = accent;
    ctx.lineWidth = 2;
    ctx.stroke();

    // 面积渐变
    ctx.lineTo(pts[pts.length - 1].x, pad.top + ch);
    ctx.lineTo(pts[0].x, pad.top + ch);
    ctx.closePath();
    const grad = ctx.createLinearGradient(0, pad.top, 0, pad.top + ch);
    grad.addColorStop(0, accent);
    grad.addColorStop(1, "transparent");
    ctx.globalAlpha = 0.16;
    ctx.fillStyle = grad;
    ctx.fill();
    ctx.globalAlpha = 1;

    // 节点圆点
    pts.forEach((p) => {
      ctx.beginPath();
      ctx.arc(p.x, p.y, p.err ? 3.5 : 2.3, 0, Math.PI * 2);
      ctx.fillStyle = p.err ? danger : accent;
      ctx.fill();
    });

    // X 轴首尾时间标签
    const fmtShort = (ts) => {
      if (!ts) return "";
      const dt = new Date(Number(ts) * 1000);
      const p2 = (n) => String(n).padStart(2, "0");
      return `${p2(dt.getHours())}:${p2(dt.getMinutes())}:${p2(dt.getSeconds())}`;
    };
    ctx.fillStyle = textSec;
    ctx.font = `11px ${fontMono}`;
    ctx.textBaseline = "top";
    ctx.textAlign = "left";
    ctx.fillText(fmtShort(data[0].finished_at), pad.left, pad.top + ch + 6);
    ctx.textAlign = "right";
    ctx.fillText(fmtShort(data[data.length - 1].finished_at), pad.left + cw, pad.top + ch + 6);
  }

  /* ===================== 右下实时流水 / 热点采样点 / 存储 Top ===================== */
  function renderFeed(m) {
    const listEl = $("dash-feed-list");
    const countEl = $("dash-feed-count");
    if (!listEl) return;

    if (state.activeTab === "logs") {
      const logs = m.logs.slice(0, 24);
      if (countEl) countEl.textContent = String(logs.length);
      if (!logs.length) {
        listEl.innerHTML = `<div class="feed-empty">${esc(t("dash.empty_logs"))}</div>`;
        return;
      }
      listEl.innerHTML = logs
        .map((lg) => {
          const dt = new Date(Number(lg.finished_at || 0) * 1000);
          const p2 = (n) => String(n).padStart(2, "0");
          const tm = Number.isNaN(dt.getTime()) ? "—" : `${p2(dt.getHours())}:${p2(dt.getMinutes())}:${p2(dt.getSeconds())}`;
          const isErr = lg.status === "error";
          const href = lg.samp_id ? `/query?id=${encodeURIComponent(lg.samp_id)}` : "/runtime";
          return `<a class="feed-row" href="${href}" title="${esc(lg.error || lg.samp_name || "")}">
            <span class="feed-time">${esc(tm)}</span>
            <span class="feed-name">${esc(lg.samp_name || `#${lg.samp_id}`)}</span>
            <span class="feed-meta">${Number(lg.rows_written || 0)} · ${Number(lg.duration_ms || 0)}ms</span>
            <span class="feed-status ${isErr ? "error" : "ok"}">${esc(isErr ? t("state.error") : t("state.ok"))}</span>
          </a>`;
        })
        .join("");
      return;
    }

    if (state.activeTab === "hot") {
      const sorted = [...m.items].sort((a, b) => Number(b.total_rows || 0) - Number(a.total_rows || 0)).slice(0, 12);
      if (countEl) countEl.textContent = String(sorted.length);
      if (!sorted.length) {
        listEl.innerHTML = `<div class="feed-empty">${esc(t("dash.empty_points"))}</div>`;
        return;
      }
      const maxRows = Math.max(1, ...sorted.map((x) => Number(x.total_rows || 0)));
      listEl.innerHTML = sorted
        .map((it, idx) => {
          const rows = Number(it.total_rows || 0);
          const pct = Math.max(4, Math.round((rows / maxRows) * 100));
          return `<a class="feed-row rank-row" href="/query?id=${encodeURIComponent(it.id)}" title="${esc(it.samp_name)}">
            <span class="rank-idx ${idx < 3 ? "top" : ""}">${idx + 1}</span>
            <span class="feed-name">${esc(it.samp_name)}</span>
            <span class="rank-bar-track"><span class="rank-bar-fill" style="width:${pct}%"></span></span>
            <span class="feed-meta">${rows.toLocaleString()}</span>
          </a>`;
        })
        .join("");
      return;
    }

    // storage top
    const dbs = [...m.dbList].sort((a, b) => Number(b.size_bytes || 0) - Number(a.size_bytes || 0)).slice(0, 12);
    if (countEl) countEl.textContent = String(dbs.length);
    if (!dbs.length) {
      listEl.innerHTML = `<div class="feed-empty">${esc(t("dash.empty_dbs"))}</div>`;
      return;
    }
    const maxBytes = Math.max(1, ...dbs.map((x) => Number(x.size_bytes || 0)));
    listEl.innerHTML = dbs
      .map((d, idx) => {
        const size = Number(d.size_bytes || 0);
        const pct = Math.max(4, Math.round((size / maxBytes) * 100));
        return `<a class="feed-row rank-row" href="/runtime#storage" title="${esc(d.relative_path || d.name)}">
          <span class="rank-idx ${idx < 3 ? "top" : ""}">${idx + 1}</span>
          <span class="feed-name">${esc(d.name)}</span>
          <span class="rank-bar-track"><span class="rank-bar-fill" style="width:${pct}%"></span></span>
          <span class="feed-meta">${esc(formatBytes(size))}</span>
        </a>`;
      })
      .join("");
  }

  /* ===================== 一键全部采样 ===================== */
  async function triggerSampleAll() {
    const enabledIds = (state.status?.items || []).filter((it) => it.enabled).map((it) => it.id);
    if (!enabledIds.length) {
      toast(t("dash.toast_no_enabled"), "error");
      return;
    }
    const btn = $("btn-dash-sample-all");
    if (btn) btn.disabled = true;
    try {
      const res = await api("/api/definitions/batch/sample", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ ids: enabledIds })
      });
      toast(t("dash.toast_all_ok", { ok: res.ok_count || 0, fail: res.error_count || 0 }), res.error_count ? "error" : "ok");
      await loadAll(false);
    } catch (err) {
      toast(err.message || String(err), "error");
    } finally {
      if (btn) btn.disabled = false;
    }
  }

  /* ===================== 事件绑定与初始化 ===================== */
  function bindEvents() {
    $("btn-dash-refresh")?.addEventListener("click", () => loadAll(true));
    $("btn-dash-sample-all")?.addEventListener("click", triggerSampleAll);

    $("dash-refresh-rate")?.addEventListener("change", (event) => {
      state.refreshMs = Number(event.target.value || 0);
      scheduleRefresh();
    });

    document.querySelectorAll("[data-feed-tab]").forEach((btn) => {
      btn.addEventListener("click", () => {
        state.activeTab = btn.dataset.feedTab;
        document.querySelectorAll("[data-feed-tab]").forEach((b) => {
          b.classList.toggle("active", b.dataset.feedTab === state.activeTab);
        });
        if (state.status) renderFeed(computeMetrics());
      });
    });

    let resizeTimer = null;
    window.addEventListener("resize", () => {
      clearTimeout(resizeTimer);
      resizeTimer = setTimeout(() => {
        if (state.status) {
          const m = computeMetrics();
          renderTopologyFlows(m);
          renderTrendChart(m.logs);
        }
      }, 60);
    });

    onLangChange(() => {
      if (state.status) renderAll();
    });

    onThemeChange(() => {
      if (state.status) {
        const m = computeMetrics();
        renderTopologyFlows(m);
        renderTrendChart(m.logs);
      }
    });
  }

  initChrome({ pollGlobalAlarms: false });
  bindEvents();
  loadAll(false);
  scheduleRefresh();
})();

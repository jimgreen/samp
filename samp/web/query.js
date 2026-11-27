/* 采样查询与多量纲交互式趋势曲线逻辑（含宽表冻结列、表头排序、快照 Diff、参考线、导出 PNG 与 AbortController） */

(function () {
  const {
    $,
    esc,
    t,
    api,
    toast,
    toLocalDateTimeInput,
    parseDateTimeInput,
    formatTime,
    formatNumber,
    checkedFields,
    setAllFields,
    renderFieldChecks,
    initChrome,
    onLangChange,
    onThemeChange
  } = window.SAMP;

  const state = {
    definitions: [],
    itemSchema: { fields: [], field_types: {}, numeric_fields: [], is_wildcard: false },
    availableKeys: [],
    selectedKeys: new Set(),
    availableTimes: [],
    snapTime: "",
    diffEnabled: false,
    prevSnapRows: null, // Map<row_key, row>
    viewMode: "split",
    autoSwitchedTable: false,
    keyKeyword: "",
    rowKeyword: "",
    sortCol: null,
    sortDir: "asc",
    lastTable: null,
    lastSeries: null,
    pinnedIdx: null,
    page: 0,
    limit: 500,
    request: null,
    abortCtrl: null,
    autoTimer: null,
    activeRange: "hour",
    chart: null
  };

  function setTimeRange(kind) {
    state.activeRange = kind;
    const now = Math.floor(Date.now() / 1000);
    let start = now - 3600;
    if (kind === "15m") start = now - 900;
    else if (kind === "hour") start = now - 3600;
    else if (kind === "day") start = now - 86400;
    else if (kind === "7d") start = now - 86400 * 7;
    else if (kind === "today") {
      const d = new Date();
      d.setHours(0, 0, 0, 0);
      start = Math.floor(d.getTime() / 1000);
    }
    $("q-start").value = toLocalDateTimeInput(start);
    $("q-end").value = toLocalDateTimeInput(now);
    $("q-ranges").querySelectorAll("[data-range]").forEach((btn) => {
      btn.classList.toggle("active-chip", btn.dataset.range === kind);
    });
  }

  function currentDefinition() {
    return state.definitions.find((d) => String(d.id) === String($("q-def").value)) || null;
  }

  function currentItem(def = currentDefinition()) {
    if (!def) return null;
    const items =
      def.items && def.items.length
        ? def.items
        : [{ item_id: "i1", source_table: def.source_table, target_table: "", fields: def.fields || [] }];
    const val = $("q-item").value;
    return items.find((it) => it.item_id === val) || items[0];
  }

  function renderQueryFields(presetFields = null) {
    const schema = state.itemSchema || { fields: [], field_types: {} };
    const allFields = schema.fields || [];
    if (!allFields.length) {
      $("q-fields").innerHTML = `<span class="text-muted">${esc(t("empty.dash"))}</span>`;
      return;
    }
    const chosen = presetFields !== null ? presetFields : checkedFields($("q-fields"));
    renderFieldChecks(
      $("q-fields"),
      allFields,
      chosen,
      schema.field_types || {},
      {},
      $("q-field-search") ? $("q-field-search").value : ""
    );
  }

  function applySmartDefaultView() {
    const hasNumeric = (state.itemSchema.numeric_fields || []).length > 0;
    if (!hasNumeric && (state.itemSchema.fields || []).length > 0) {
      if (state.viewMode !== "table") {
        state.autoSwitchedTable = true;
        setViewMode("table");
        toast(t("q.auto_table_hint"), "info");
      }
    } else if (hasNumeric && state.autoSwitchedTable) {
      state.autoSwitchedTable = false;
      setViewMode("split");
    }
  }

  async function loadItemSchema(def, item, preserveChecked = false) {
    if (!def || !item) {
      state.itemSchema = { fields: [], field_types: {}, numeric_fields: [], is_wildcard: false };
      renderQueryFields([]);
      return;
    }
    const prevChecked = preserveChecked ? checkedFields($("q-fields")) : null;
    try {
      const qs = item.item_id ? `?item_id=${encodeURIComponent(item.item_id)}` : "";
      const data = await api(`/api/definitions/${def.id}/fields${qs}`);
      state.itemSchema = {
        fields: data.fields || [],
        field_types: data.field_types || {},
        numeric_fields: data.numeric_fields || [],
        is_wildcard: Boolean(data.is_wildcard)
      };
    } catch {
      const fallback = (item.fields || []).filter((f) => f !== "*");
      state.itemSchema = { fields: fallback, field_types: {}, numeric_fields: fallback, is_wildcard: false };
    }
    const preset = prevChecked && prevChecked.length ? prevChecked : state.itemSchema.fields;
    renderQueryFields(preset);
    if (!preserveChecked) applySmartDefaultView();
  }

  function getPrevSnapTime() {
    if (!state.snapTime || !state.availableTimes.length) return null;
    const idx = state.availableTimes.indexOf(Number(state.snapTime));
    if (idx < 0 || idx + 1 >= state.availableTimes.length) return null;
    return state.availableTimes[idx + 1];
  }

  function syncDiffControls() {
    const prevTs = getPrevSnapTime();
    const cb = $("q-diff-prev");
    const wrap = $("q-diff-wrap");
    const badge = $("q-diff-badge");
    const available = prevTs !== null;
    cb.disabled = !available;
    wrap.classList.toggle("disabled", !available);
    if (!available) {
      cb.checked = false;
      state.diffEnabled = false;
      state.prevSnapRows = null;
      badge.classList.add("hidden");
    }
  }

  function renderSnapTimes() {
    const sel = $("q-snap-time");
    if (!sel) return;
    const prev = state.snapTime;
    const opts = [`<option value="">${esc(t("q.all_times"))}</option>`];
    state.availableTimes.forEach((ts) => {
      opts.push(`<option value="${ts}">${esc(formatTime(ts, true))}</option>`);
    });
    sel.innerHTML = opts.join("");
    if (prev && state.availableTimes.includes(Number(prev))) {
      sel.value = String(prev);
    } else {
      state.snapTime = "";
      sel.value = "";
    }
    syncDiffControls();
  }

  function setViewMode(mode) {
    state.viewMode = mode;
    const split = $("query-split");
    if (split) {
      split.classList.toggle("view-table", mode === "table");
      split.classList.toggle("view-chart", mode === "chart");
    }
    $("q-view-modes").querySelectorAll(".tab-btn").forEach((b) => {
      b.classList.toggle("active", b.dataset.view === mode);
    });
    if (mode !== "table") {
      requestAnimationFrame(() => state.chart?.draw());
    }
  }

  async function renderItemSelect(def, targetItemId = null, preserveChecked = false) {
    const itemSel = $("q-item");
    if (!def) {
      itemSel.classList.add("hidden");
      itemSel.innerHTML = "";
      await loadItemSchema(null, null, false);
      return;
    }
    const items =
      def.items && def.items.length
        ? def.items
        : [{ item_id: "i1", source_table: def.source_table, target_table: "", fields: def.fields || [] }];
    const prev = targetItemId ?? itemSel.value;
    itemSel.innerHTML = items
      .map((it) => {
        const tbl = it.target_table || t("def.item_default_table");
        const label = it.target_table && it.target_table === it.source_table ? tbl : `${tbl} · ${it.source_table}`;
        return `<option value="${esc(it.item_id)}">${esc(label)}</option>`;
      })
      .join("");
    const chosenItem = items.find((it) => it.item_id === prev) || items[0];
    itemSel.value = chosenItem.item_id;
    itemSel.classList.toggle("hidden", items.length <= 1);
    await loadItemSchema(def, chosenItem, preserveChecked);
  }

  async function renderDefinitionSelect(targetId = null, targetItemId = null) {
    const select = $("q-def");
    const prev = targetId ?? select.value;
    select.innerHTML = state.definitions.map((d) => `<option value="${d.id}">${esc(d.samp_name)}</option>`).join("");
    const chosen = state.definitions.find((d) => String(d.id) === String(prev)) || state.definitions[0];
    if (chosen) {
      select.value = String(chosen.id);
      await renderItemSelect(chosen, targetItemId, false);
    } else {
      await renderItemSelect(null, null, false);
    }
  }

  function updateKeyTriggerLabel() {
    const count = state.selectedKeys.size;
    const el = $("key-trigger-text");
    if (count === 0) {
      el.textContent = t("filter.all_keys");
    } else if (count === 1) {
      el.textContent = [...state.selectedKeys][0];
    } else {
      el.textContent = t("q.keys_selected", { n: count });
    }
  }

  function renderKeyMenu() {
    const list = $("key-list");
    const kw = state.keyKeyword.trim().toLowerCase();
    const visible = state.availableKeys.filter((k) => !kw || k.toLowerCase().includes(kw));
    if (!visible.length) {
      list.innerHTML = `<div class="text-muted" style="padding:8px;text-align:center">${esc(t("empty.dash"))}</div>`;
    } else {
      list.innerHTML = visible
        .map(
          (k) =>
            `<label class="multi-opt"><input type="checkbox" value="${esc(k)}" ${state.selectedKeys.has(k) ? "checked" : ""}><span>${esc(k)}</span></label>`
        )
        .join("");
    }
    updateKeyTriggerLabel();
  }

  function buildRequest() {
    const id = $("q-def").value;
    if (!id) {
      toast(t("q.toast_no_def"), "error");
      return null;
    }
    const start = parseDateTimeInput($("q-start").value);
    const end = parseDateTimeInput($("q-end").value);
    if (!Number.isFinite(start) || !Number.isFinite(end) || end < start) {
      toast(t("q.toast_bad_range"), "error");
      return null;
    }
    const fields = checkedFields($("q-fields"));
    if (!fields.length) {
      toast(t("q.toast_need_fields"), "error");
      return null;
    }
    const item = currentItem();
    return {
      id,
      itemId: item ? item.item_id : "",
      start,
      end,
      snapTime: state.snapTime ? Number(state.snapTime) : null,
      fields,
      rowKeys: [...state.selectedKeys],
      agg: $("q-agg").value,
      order: $("q-order").value
    };
  }

  function renderStatsBar(seriesPayload) {
    const bar = $("chart-stats");
    const series = seriesPayload?.series || [];
    const fields = seriesPayload?.fields || [];
    if (!series.length) {
      bar.textContent = t("empty.dash");
      return;
    }
    const chips = [];
    const multiKey = series.length > 1;
    for (const s of series) {
      for (const f of fields) {
        const st = s.stats?.[f];
        if (!st || !st.count) continue;
        const name = multiKey ? `${s.row_key}·${f}` : f;
        if (st.max !== null && st.min !== null) {
          chips.push(
            `<span class="stat-chip"><strong>${esc(name)}</strong> ` +
              `<span class="text-muted">${esc(t("q.stat_max"))}</span> ${esc(formatNumber(st.max))} ` +
              `<span class="text-muted">${esc(t("q.stat_min"))}</span> ${esc(formatNumber(st.min))} ` +
              `<span class="text-muted">${esc(t("q.stat_avg"))}</span> ${esc(formatNumber(st.avg))} ` +
              `<span class="text-muted">${esc(t("q.stat_last"))}</span> ${esc(formatNumber(st.latest))}</span>`
          );
        } else {
          chips.push(
            `<span class="stat-chip"><strong>${esc(name)}</strong> <span class="text-muted">${esc(t("q.stat_last"))}</span> ${esc(st.latest ?? t("empty.dash"))}</span>`
          );
        }
      }
    }
    bar.innerHTML = chips.length ? chips.join("") : esc(t("empty.dash"));
  }

  function renderChartMeta(seriesRes) {
    const metaEl = $("chart-meta");
    if (!seriesRes) {
      metaEl.textContent = "";
      metaEl.className = "mono text-muted";
      return;
    }
    if (seriesRes.truncated) {
      metaEl.textContent = t("q.truncated_info", { max: seriesRes.max_raw_points });
      metaEl.className = "mono text-warn";
    } else if (seriesRes.bucket_seconds > 0) {
      metaEl.textContent = t("q.bucket_info", { sec: seriesRes.bucket_seconds });
      metaEl.className = "mono text-muted";
    } else {
      metaEl.textContent = t("q.raw_info", { n: seriesRes.total_raw_rows });
      metaEl.className = "mono text-muted";
    }
  }

  function valuesDiffer(a, b) {
    if (a === b) return false;
    if ((a === null || a === undefined || a === "") && (b === null || b === undefined || b === "")) return false;
    const na = Number(a);
    const nb = Number(b);
    if (a !== "" && b !== "" && Number.isFinite(na) && Number.isFinite(nb)) {
      return Math.abs(na - nb) > 1e-9;
    }
    return String(a ?? "") !== String(b ?? "");
  }

  function renderTable(result) {
    const cols = result?.columns || [];
    const rawRows = result?.rows || [];
    const kw = state.rowKeyword.trim().toLowerCase();

    // 表内关键词过滤
    let displayRows = kw
      ? rawRows.filter((r) => cols.some((c) => String(r[c] ?? "").toLowerCase().includes(kw)))
      : [...rawRows];

    // 表头列排序
    if (state.sortCol && cols.includes(state.sortCol)) {
      const col = state.sortCol;
      const dir = state.sortDir === "desc" ? -1 : 1;
      displayRows.sort((a, b) => {
        const va = a[col];
        const vb = b[col];
        if (va === vb) return 0;
        if (va === null || va === undefined || va === "") return 1;
        if (vb === null || vb === undefined || vb === "") return -1;
        const na = Number(va);
        const nb = Number(vb);
        if (Number.isFinite(na) && Number.isFinite(nb)) return (na - nb) * dir;
        return String(va).localeCompare(String(vb), undefined, { numeric: true, sensitivity: "base" }) * dir;
      });
    }

    $("q-thead").innerHTML = cols.length
      ? `<tr>${cols
          .map((c) => {
            const label = c === "sample_time" ? t("col.sample_time") : c === "row_key" ? t("col.row_key") : c;
            const classes = ["sortable-th"];
            if (c === "sample_time") classes.push("num", "freeze-1");
            else if (c === "row_key") classes.push("freeze-2");
            else classes.push("num");
            const ind = state.sortCol === c ? `<span class="sort-ind">${state.sortDir === "asc" ? "▲" : "▼"}</span>` : "";
            return `<th class="${classes.join(" ")}" data-sort-col="${esc(c)}">${esc(label)}${ind}</th>`;
          })
          .join("")}</tr>`
      : "";

    const prevMap = state.diffEnabled && state.prevSnapRows ? state.prevSnapRows : null;
    let diffCount = 0;

    if (!displayRows.length) {
      $("q-tbody").innerHTML = `<tr><td colspan="${Math.max(1, cols.length)}" class="empty-cell">${esc(t("empty.dash"))}</td></tr>`;
    } else {
      $("q-tbody").innerHTML = displayRows
        .map((row, idx) => {
          const prevRow = prevMap ? prevMap.get(String(row.row_key ?? "")) : null;
          return `<tr data-idx="${idx}" data-ts="${row.sample_time}" class="${state.pinnedIdx === idx ? "selected" : ""}">${cols
            .map((c) => {
              if (c === "sample_time") return `<td class="num freeze-1">${esc(formatTime(row[c]))}</td>`;
              if (c === "row_key") return `<td class="mono freeze-2" title="${esc(row[c])}">${esc(row[c])}</td>`;
              const val = row[c];
              const isNum = typeof val === "number" || (val !== null && val !== "" && Number.isFinite(Number(val)));
              const text = isNum ? formatNumber(val) : val ?? t("empty.dash");
              let diffCls = "";
              let titleAttr = "";
              if (prevMap) {
                const prevVal = prevRow ? prevRow[c] : undefined;
                if (!prevRow || valuesDiffer(val, prevVal)) {
                  diffCls = " cell-diff";
                  diffCount += 1;
                  const prevDisp = prevRow ? (prevVal ?? t("empty.dash")) : "NEW";
                  titleAttr = ` title="${esc(`${prevDisp} → ${text}`)}"`;
                }
              }
              return `<td class="${isNum ? "num" : ""}${diffCls}"${titleAttr}>${esc(text)}</td>`;
            })
            .join("")}</tr>`;
        })
        .join("");
    }

    const badge = $("q-diff-badge");
    if (prevMap) {
      badge.textContent = t("q.diff_changed", { n: diffCount });
      badge.classList.remove("hidden");
    } else {
      badge.classList.add("hidden");
    }

    const total = Number(result?.total_rows || 0);
    const offset = Number(result?.offset || 0);
    const limit = Number(result?.limit || state.limit);
    const page = total ? Math.floor(offset / limit) + 1 : 0;
    const pages = total ? Math.ceil(total / limit) : 0;
    $("q-page-info").textContent = total
      ? t("q.page_info", { page, pages, from: offset + 1, to: Math.min(offset + rawRows.length, total), total })
      : t("empty.dash");
    $("q-prev").disabled = page <= 1;
    $("q-next").disabled = !total || page >= pages;
  }

  async function runQuery(page = 0, useSaved = false, refreshSeries = true) {
    const req = useSaved ? state.request : buildRequest();
    if (!req) return;
    if (state.abortCtrl) state.abortCtrl.abort();
    const ctrl = new AbortController();
    state.abortCtrl = ctrl;
    const fetchOpts = { signal: ctrl.signal };

    state.page = Math.max(0, page);
    state.pinnedIdx = null;
    if (!useSaved) state.request = req;

    const tableStart = req.snapTime !== null ? req.snapTime : req.start;
    const tableEnd = req.snapTime !== null ? req.snapTime : req.end;
    const params = new URLSearchParams({
      start: String(tableStart),
      end: String(tableEnd),
      fields: req.fields.join(","),
      order: req.order,
      limit: String(state.limit),
      offset: String(state.page * state.limit)
    });
    if (req.itemId) params.set("item_id", req.itemId);
    if (req.rowKeys.length) params.set("row_keys", req.rowKeys.join(","));

    const prevSnapTs = state.diffEnabled && req.snapTime !== null ? getPrevSnapTime() : null;

    try {
      const tableJob = api(`/api/query/${req.id}?${params.toString()}`, fetchOpts);
      let prevSnapJob = Promise.resolve(null);
      if (prevSnapTs !== null) {
        const pParams = new URLSearchParams({
          start: String(prevSnapTs),
          end: String(prevSnapTs),
          fields: req.fields.join(","),
          order: req.order,
          limit: String(state.limit),
          offset: "0"
        });
        if (req.itemId) pParams.set("item_id", req.itemId);
        if (req.rowKeys.length) pParams.set("row_keys", req.rowKeys.join(","));
        prevSnapJob = api(`/api/query/${req.id}?${pParams.toString()}`, fetchOpts);
      }

      let seriesJob = Promise.resolve(null);
      let keysJob = Promise.resolve(null);
      let timesJob = Promise.resolve(null);
      if (refreshSeries) {
        const sParams = new URLSearchParams({
          start: String(req.start),
          end: String(req.end),
          fields: req.fields.join(","),
          agg: req.agg
        });
        if (req.itemId) sParams.set("item_id", req.itemId);
        if (req.rowKeys.length) sParams.set("row_keys", req.rowKeys.join(","));
        const kParams = new URLSearchParams({ start: String(req.start), end: String(req.end) });
        if (req.itemId) kParams.set("item_id", req.itemId);
        seriesJob = api(`/api/query/${req.id}/series?${sParams.toString()}`, fetchOpts);
        keysJob = api(`/api/query/${req.id}/keys?${kParams.toString()}`, fetchOpts);
        timesJob = api(`/api/query/${req.id}/times?${kParams.toString()}`, fetchOpts);
      }

      const [tableRes, prevRes, seriesRes, keysRes, timesRes] = await Promise.all([
        tableJob,
        prevSnapJob,
        seriesJob,
        keysJob,
        timesJob
      ]);

      if (ctrl.signal.aborted) return;

      state.prevSnapRows = prevRes ? new Map((prevRes.rows || []).map((r) => [String(r.row_key ?? ""), r])) : null;
      state.lastTable = tableRes;
      renderTable(tableRes);
      const wildTag = state.itemSchema.is_wildcard ? ` · ${t("def.all_fields_wild")}` : "";
      $("q-meta").textContent = `${tableRes.tables.join(", ") || t("empty.dash")}${wildTag}`;

      if (refreshSeries && seriesRes) {
        state.lastSeries = seriesRes;
        state.chart.setData(seriesRes);
        state.chart.setPinnedTime(req.snapTime !== null ? req.snapTime : null);
        renderStatsBar(seriesRes);
        renderChartMeta(seriesRes);
      } else if (req.snapTime !== null) {
        state.chart?.setPinnedTime(req.snapTime);
      } else {
        state.chart?.setPinnedTime(null);
      }
      if (refreshSeries && keysRes) {
        state.availableKeys = keysRes.keys || [];
        const validSet = new Set(state.availableKeys);
        [...state.selectedKeys].forEach((k) => {
          if (!validSet.has(k)) state.selectedKeys.delete(k);
        });
        renderKeyMenu();
      }
      if (refreshSeries && timesRes) {
        state.availableTimes = timesRes.times || [];
        renderSnapTimes();
      }
    } catch (err) {
      if (err?.name === "AbortError") return;
      toast(err.message, "error");
    }
  }

  function exportCsv() {
    const req = buildRequest();
    if (!req) return;
    const params = new URLSearchParams({
      start: String(req.snapTime !== null ? req.snapTime : req.start),
      end: String(req.snapTime !== null ? req.snapTime : req.end),
      fields: req.fields.join(","),
      order: req.order
    });
    if (req.itemId) params.set("item_id", req.itemId);
    if (req.rowKeys.length) params.set("row_keys", req.rowKeys.join(","));
    window.location.href = `/api/query/${req.id}/export?${params.toString()}`;
  }

  function updateAutoRefresh() {
    clearInterval(state.autoTimer);
    state.autoTimer = null;
    const sec = Number($("q-auto").value || 0);
    if (sec > 0) {
      state.autoTimer = setInterval(() => {
        if (state.activeRange) setTimeRange(state.activeRange);
        runQuery(0, false, true);
      }, sec * 1000);
    }
  }

  async function loadDefinitions(targetId = null) {
    try {
      state.definitions = await api("/api/definitions");
      await renderDefinitionSelect(targetId);
    } catch (err) {
      toast(err.message, "error");
    }
  }

  function bindEvents() {
    $("btn-query").addEventListener("click", () => runQuery(0, false, true));
    $("btn-export").addEventListener("click", exportCsv);
    $("btn-reset-zoom").addEventListener("click", () => state.chart?.resetZoom());
    $("btn-export-png").addEventListener("click", () => {
      const def = currentDefinition();
      const fname = `${def ? def.samp_name : "samp"}_trend.png`;
      state.chart?.exportPng(fname);
    });

    const syncThresholds = () => {
      state.chart?.setThresholds({ low: $("q-ref-low").value, high: $("q-ref-high").value });
    };
    $("q-ref-low").addEventListener("input", syncThresholds);
    $("q-ref-high").addEventListener("input", syncThresholds);

    // 曲线视图模式切换（同轴 / 归一化 / 分轨）
    $("chart-modes").addEventListener("click", (e) => {
      const btn = e.target.closest("[data-mode]");
      if (!btn) return;
      $("chart-modes").querySelectorAll(".tab-btn").forEach((b) => b.classList.toggle("active", b === btn));
      state.chart?.setMode(btn.dataset.mode);
    });

    // 多 row_key 下拉多选交互
    $("key-trigger").addEventListener("click", (e) => {
      e.stopPropagation();
      $("key-menu").classList.toggle("open");
    });
    $("key-menu").addEventListener("click", (e) => e.stopPropagation());
    document.addEventListener("click", () => $("key-menu").classList.remove("open"));
    $("key-search").addEventListener("input", (e) => {
      state.keyKeyword = e.target.value;
      renderKeyMenu();
    });
    $("key-list").addEventListener("change", (e) => {
      const cb = e.target.closest("input[type=checkbox]");
      if (!cb) return;
      if (cb.checked) state.selectedKeys.add(cb.value);
      else state.selectedKeys.delete(cb.value);
      updateKeyTriggerLabel();
      runQuery(0, false, true);
    });
    $("key-all").addEventListener("click", () => {
      state.availableKeys.forEach((k) => state.selectedKeys.add(k));
      renderKeyMenu();
      runQuery(0, false, true);
    });
    $("key-none").addEventListener("click", () => {
      state.selectedKeys.clear();
      renderKeyMenu();
      runQuery(0, false, true);
    });

    // 表头点击排序
    $("q-thead").addEventListener("click", (e) => {
      const th = e.target.closest("th[data-sort-col]");
      if (!th) return;
      const col = th.dataset.sortCol;
      if (state.sortCol !== col) {
        state.sortCol = col;
        state.sortDir = "asc";
      } else if (state.sortDir === "asc") {
        state.sortDir = "desc";
      } else {
        state.sortCol = null;
        state.sortDir = "asc";
      }
      if (state.lastTable) renderTable(state.lastTable);
    });

    // 表内行搜索
    $("q-row-search").addEventListener("input", (e) => {
      state.rowKeyword = e.target.value;
      if (state.lastTable) renderTable(state.lastTable);
    });

    // 表格点击行 -> 图表高亮对应时刻竖线
    $("q-tbody").addEventListener("click", (e) => {
      const tr = e.target.closest("tr[data-ts]");
      if (!tr) return;
      const idx = Number(tr.dataset.idx);
      const ts = Number(tr.dataset.ts);
      if (state.pinnedIdx === idx) {
        state.pinnedIdx = null;
        state.chart?.setPinnedTime(null);
      } else {
        state.pinnedIdx = idx;
        state.chart?.setPinnedTime(ts);
      }
      $("q-tbody").querySelectorAll("tr[data-idx]").forEach((row) => {
        row.classList.toggle("selected", Number(row.dataset.idx) === state.pinnedIdx);
      });
    });

    $("q-ranges").addEventListener("click", (e) => {
      const btn = e.target.closest("[data-range]");
      if (!btn) return;
      setTimeRange(btn.dataset.range);
      runQuery(0, false, true);
    });
    $("q-start").addEventListener("change", () => {
      state.activeRange = null;
    });
    $("q-end").addEventListener("change", () => {
      state.activeRange = null;
    });

    $("q-def").addEventListener("change", async () => {
      state.snapTime = "";
      state.diffEnabled = false;
      state.prevSnapRows = null;
      state.sortCol = null;
      state.availableTimes = [];
      renderSnapTimes();
      await renderItemSelect(currentDefinition(), null, false);
      state.selectedKeys.clear();
      state.availableKeys = [];
      renderKeyMenu();
      state.page = 0;
      runQuery(0, false, true);
    });
    $("q-item").addEventListener("change", async () => {
      state.snapTime = "";
      state.diffEnabled = false;
      state.prevSnapRows = null;
      state.sortCol = null;
      state.availableTimes = [];
      renderSnapTimes();
      await loadItemSchema(currentDefinition(), currentItem(), false);
      state.selectedKeys.clear();
      state.availableKeys = [];
      renderKeyMenu();
      state.page = 0;
      runQuery(0, false, true);
    });
    $("q-snap-time").addEventListener("change", (e) => {
      state.snapTime = e.target.value;
      syncDiffControls();
      state.page = 0;
      runQuery(0, false, false);
    });
    $("q-diff-prev").addEventListener("change", (e) => {
      state.diffEnabled = e.target.checked;
      if (!state.diffEnabled) state.prevSnapRows = null;
      runQuery(0, false, false);
    });
    $("q-view-modes").addEventListener("click", (e) => {
      const btn = e.target.closest("[data-view]");
      if (!btn) return;
      state.autoSwitchedTable = false;
      setViewMode(btn.dataset.view);
    });
    $("q-agg").addEventListener("change", () => runQuery(0, false, true));
    $("q-order").addEventListener("change", () => runQuery(0, false, false));
    $("q-auto").addEventListener("change", updateAutoRefresh);

    $("q-limit").addEventListener("change", () => {
      state.limit = Number($("q-limit").value) || 500;
      if (state.request) runQuery(0, true, false);
    });
    $("q-prev").addEventListener("click", () => {
      if (state.page > 0) runQuery(state.page - 1, true, false);
    });
    $("q-next").addEventListener("click", () => {
      if (state.lastTable && state.lastTable.offset + state.lastTable.rows.length < state.lastTable.total_rows) {
        runQuery(state.page + 1, true, false);
      }
    });
    $("q-field-search").addEventListener("input", () => renderQueryFields(null));
    $("q-fields-num").addEventListener("click", () => {
      const nums = state.itemSchema.numeric_fields || [];
      renderQueryFields(nums.length ? nums : state.itemSchema.fields || []);
    });
    $("q-fields-all").addEventListener("click", () => setAllFields($("q-fields"), true));
    $("q-fields-none").addEventListener("click", () => setAllFields($("q-fields"), false));

    onThemeChange(() => state.chart?.refreshTheme());
    onLangChange(() => {
      const curDef = currentDefinition();
      if (curDef) {
        renderItemSelect(curDef, $("q-item").value, true);
      }
      renderSnapTimes();
      if (state.lastTable) renderTable(state.lastTable);
      if (state.lastSeries) {
        renderStatsBar(state.lastSeries);
        renderChartMeta(state.lastSeries);
        state.chart?.draw();
      }
      renderKeyMenu();
    });
  }

  initChrome();
  state.chart = new window.SAMPChart.TimeSeriesChart($("trend-canvas"), $("chart-legend"));
  bindEvents();

  const urlParams = new URLSearchParams(location.search);
  const paramId = urlParams.get("id");
  const paramRange = urlParams.get("range") || "hour";
  setTimeRange(["15m", "hour", "today", "day", "7d"].includes(paramRange) ? paramRange : "hour");

  loadDefinitions(paramId).then(() => {
    if (state.definitions.length) {
      runQuery(0, false, true);
    }
  });
})();

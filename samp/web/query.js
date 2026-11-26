/* 采样查询与多量纲交互式趋势曲线逻辑 */

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
    viewMode: "split",
    keyKeyword: "",
    lastTable: null,
    lastSeries: null,
    pinnedIdx: null,
    page: 0,
    limit: 500,
    request: null,
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
    const items = def.items && def.items.length ? def.items : [{ item_id: "i1", source_table: def.source_table, target_table: "", fields: def.fields || [] }];
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
    const items = def.items && def.items.length ? def.items : [{ item_id: "i1", source_table: def.source_table, target_table: "", fields: def.fields || [] }];
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
    select.innerHTML = state.definitions
      .map((d) => `<option value="${d.id}">${esc(d.samp_name)}</option>`)
      .join("");
    const chosen =
      state.definitions.find((d) => String(d.id) === String(prev)) || state.definitions[0];
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

  function renderTable(result) {
    const cols = result?.columns || [];
    const rows = result?.rows || [];
    $("q-thead").innerHTML = cols.length
      ? `<tr>${cols
          .map((c) => {
            const label = c === "sample_time" ? t("col.sample_time") : c === "row_key" ? t("col.row_key") : c;
            const cls = c === "row_key" ? "" : "num";
            return `<th class="${cls}">${esc(label)}</th>`;
          })
          .join("")}</tr>`
      : "";

    if (!rows.length) {
      $("q-tbody").innerHTML = `<tr><td colspan="${Math.max(1, cols.length)}" class="empty-cell">${esc(t("empty.dash"))}</td></tr>`;
    } else {
      $("q-tbody").innerHTML = rows
        .map(
          (row, idx) =>
            `<tr data-idx="${idx}" data-ts="${row.sample_time}" class="${state.pinnedIdx === idx ? "selected" : ""}">${cols
              .map((c) => {
                if (c === "sample_time") return `<td class="num">${esc(formatTime(row[c]))}</td>`;
                if (c === "row_key") return `<td class="mono">${esc(row[c])}</td>`;
                const val = row[c];
                const isNum = typeof val === "number" || (val !== null && val !== "" && Number.isFinite(Number(val)));
                return `<td class="${isNum ? "num" : ""}">${esc(isNum ? formatNumber(val) : val ?? t("empty.dash"))}</td>`;
              })
              .join("")}</tr>`
        )
        .join("");
    }

    const total = Number(result?.total_rows || 0);
    const offset = Number(result?.offset || 0);
    const limit = Number(result?.limit || state.limit);
    const page = total ? Math.floor(offset / limit) + 1 : 0;
    const pages = total ? Math.ceil(total / limit) : 0;
    $("q-page-info").textContent = total
      ? t("q.page_info", { page, pages, from: offset + 1, to: Math.min(offset + rows.length, total), total })
      : t("empty.dash");
    $("q-prev").disabled = page <= 1;
    $("q-next").disabled = !total || page >= pages;
  }

  async function runQuery(page = 0, useSaved = false, refreshSeries = true) {
    const req = useSaved ? state.request : buildRequest();
    if (!req) return;
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

    try {
      const jobs = [api(`/api/query/${req.id}?${params.toString()}`)];
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
        jobs.push(api(`/api/query/${req.id}/series?${sParams.toString()}`));
        jobs.push(api(`/api/query/${req.id}/keys?${kParams.toString()}`));
        jobs.push(api(`/api/query/${req.id}/times?${kParams.toString()}`));
      }
      const [tableRes, seriesRes, keysRes, timesRes] = await Promise.all(jobs);
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
    $("q-start").addEventListener("change", () => { state.activeRange = null; });
    $("q-end").addEventListener("change", () => { state.activeRange = null; });

    $("q-def").addEventListener("change", async () => {
      state.snapTime = "";
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
      state.page = 0;
      runQuery(0, false, false);
    });
    $("q-view-modes").addEventListener("click", (e) => {
      const btn = e.target.closest("[data-view]");
      if (!btn) return;
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

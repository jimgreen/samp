/* 零外部依赖 Canvas 2D 交互式多量纲时序趋势图组件
   - 视图模式：shared（同轴） / normalized（0-100% 归一化，Tooltip 仍显示真实物理值） / stacked（分轨堆叠）
   - 交互能力：滚轮缩放、拖拽平移、Shift+拖拽框选缩放、双击重置、十字准星 Hover 读数、图例开关、NULL 断点、表格联动定位线
*/

(function () {
  const PALETTES = {
    light: {
      bg: "#FFFFFF",
      grid: "#E2E8F0",
      axis: "#CBD5E1",
      text: "#64748B",
      crosshair: "#94A3B8",
      pinned: "#2563EB",
      selectionBg: "rgba(37, 99, 235, 0.14)",
      selectionBorder: "#2563EB",
      labelBg: "rgba(255, 255, 255, 0.95)",
      labelBorder: "#CBD5E1",
      labelText: "#0F172A",
      laneBorder: "#E2E8F0",
      series: ["#2563EB", "#D97706", "#0D9488", "#DC2626", "#7C3AED", "#64748B", "#0284C7", "#EA580C"]
    },
    dark: {
      bg: "#111827",
      grid: "rgba(255, 255, 255, 0.07)",
      axis: "rgba(255, 255, 255, 0.16)",
      text: "#94A3B8",
      crosshair: "rgba(148, 163, 184, 0.72)",
      pinned: "#38BDF8",
      selectionBg: "rgba(56, 189, 248, 0.16)",
      selectionBorder: "#38BDF8",
      labelBg: "rgba(17, 24, 39, 0.95)",
      labelBorder: "rgba(255, 255, 255, 0.18)",
      labelText: "#F8FAFC",
      laneBorder: "rgba(255, 255, 255, 0.10)",
      series: ["#38BDF8", "#FBBF24", "#34D399", "#FB7185", "#A78BFA", "#94A3B8", "#22D3EE", "#FB923C"]
    }
  };

  function chartPalette() {
    const theme = document.documentElement.getAttribute("data-theme");
    return theme === "precision-dark" ? PALETTES.dark : PALETTES.light;
  }

  function formatAxisTime(sec, spanSec) {
    const d = new Date(sec * 1000);
    const p = (n) => String(n).padStart(2, "0");
    if (spanSec <= 3600 * 6) {
      return `${p(d.getHours())}:${p(d.getMinutes())}:${p(d.getSeconds())}`;
    }
    if (spanSec <= 86400 * 2) {
      return `${p(d.getMonth() + 1)}-${p(d.getDate())} ${p(d.getHours())}:${p(d.getMinutes())}`;
    }
    return `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())}`;
  }

  function formatAxisVal(v) {
    if (!Number.isFinite(v)) return "";
    const abs = Math.abs(v);
    if (abs >= 1e6 || (abs > 0 && abs < 1e-3)) return v.toExponential(2);
    if (Number.isInteger(v)) return String(v);
    return Number(v.toFixed(3)).toString();
  }

  function rangeInWindow(line, xMin, xMax) {
    let lo = Infinity;
    let hi = -Infinity;
    const { times, values } = line;
    for (let i = 0; i < times.length; i += 1) {
      const ts = times[i];
      if (ts < xMin || ts > xMax) continue;
      const v = values[i];
      if (v !== null && Number.isFinite(v)) {
        if (v < lo) lo = v;
        if (v > hi) hi = v;
      }
    }
    if (!Number.isFinite(lo) || !Number.isFinite(hi)) return [0, 1];
    if (lo === hi) {
      const d = Math.abs(lo) * 0.1 || 1;
      return [lo - d, hi + d];
    }
    const margin = (hi - lo) * 0.08;
    return [lo - margin, hi + margin];
  }

  class TimeSeriesChart {
    constructor(canvas, legendEl) {
      this.canvas = canvas;
      this.legendEl = legendEl;
      this.ctx = canvas.getContext("2d");
      this.lines = [];
      this.mode = "shared"; // "shared" | "normalized" | "stacked"
      this.pinnedTime = null;
      this.fullX = [0, 1];
      this.viewX = [0, 1];
      this.hoverPx = null;
      this.dragMode = null;
      this.dragStartPx = 0;
      this.dragCurrPx = 0;
      this.dragStartView = null;
      this.pad = { top: 10, right: 14, bottom: 24, left: 58 };

      this._bindEvents();
      if (window.ResizeObserver) {
        this._ro = new ResizeObserver(() => this.draw());
        this._ro.observe(canvas.parentElement);
      }
      window.addEventListener("resize", () => this.draw());
    }

    setMode(mode) {
      if (!["shared", "normalized", "stacked"].includes(mode)) return;
      this.mode = mode;
      this.draw();
    }

    setPinnedTime(ts) {
      this.pinnedTime = ts === null || ts === undefined ? null : Number(ts);
      this.draw();
    }

    setData(seriesPayload) {
      const palette = chartPalette();
      const prevHidden = new Set(this.lines.filter((l) => l.hidden).map((l) => l.id));
      const lines = [];
      let minT = Infinity;
      let maxT = -Infinity;
      const fields = seriesPayload?.fields || [];
      const seriesList = seriesPayload?.series || [];
      const multiKey = seriesList.length > 1;

      let idx = 0;
      for (const s of seriesList) {
        const times = s.times || [];
        if (times.length) {
          minT = Math.min(minT, times[0]);
          maxT = Math.max(maxT, times[times.length - 1]);
        }
        for (const field of fields) {
          const rawVals = s.values?.[field] || [];
          const nums = rawVals.map((v) => {
            if (v === null || v === undefined || v === "") return null;
            const n = Number(v);
            return Number.isFinite(n) ? n : null;
          });
          if (!nums.some((v) => v !== null)) continue;
          const id = `${s.row_key}::${field}`;
          const label = multiKey ? `${s.row_key} · ${field}` : field;
          lines.push({
            id,
            label,
            times,
            values: nums,
            colorIndex: idx,
            color: palette.series[idx % palette.series.length],
            hidden: prevHidden.has(id)
          });
          idx += 1;
        }
      }

      this.lines = lines;
      this.pinnedTime = null;
      if (!Number.isFinite(minT) || !Number.isFinite(maxT)) {
        this.fullX = [0, 1];
      } else if (minT === maxT) {
        this.fullX = [minT - 10, maxT + 10];
      } else {
        this.fullX = [minT, maxT];
      }
      this.viewX = [...this.fullX];
      this.renderLegend();
      this.draw();
    }

    refreshTheme() {
      const palette = chartPalette();
      this.lines.forEach((line) => {
        line.color = palette.series[line.colorIndex % palette.series.length];
      });
      this.renderLegend();
      this.draw();
    }

    resetZoom() {
      this.viewX = [...this.fullX];
      this.draw();
    }

    renderLegend() {
      if (!this.legendEl) return;
      if (!this.lines.length) {
        this.legendEl.innerHTML = `<span class="text-muted">${window.SAMP.esc(window.SAMP.t("empty.dash"))}</span>`;
        return;
      }
      this.legendEl.innerHTML = this.lines
        .map(
          (line) =>
            `<button type="button" class="legend-item ${line.hidden ? "hidden" : ""}" data-line-id="${window.SAMP.esc(line.id)}">` +
            `<span class="legend-swatch" style="background:${line.color}"></span>` +
            `<span>${window.SAMP.esc(line.label)}</span></button>`
        )
        .join("");
      this.legendEl.querySelectorAll("[data-line-id]").forEach((btn) => {
        btn.addEventListener("click", () => {
          const target = this.lines.find((l) => l.id === btn.dataset.lineId);
          if (!target) return;
          target.hidden = !target.hidden;
          this.renderLegend();
          this.draw();
        });
      });
    }

    _plotRect(width, height) {
      return {
        x: this.pad.left,
        y: this.pad.top,
        w: Math.max(10, width - this.pad.left - this.pad.right),
        h: Math.max(10, height - this.pad.top - this.pad.bottom)
      };
    }

    _bindEvents() {
      const c = this.canvas;
      const getLocalX = (event) => event.clientX - c.getBoundingClientRect().left;

      c.addEventListener("mousemove", (event) => {
        const rect = c.getBoundingClientRect();
        const x = event.clientX - rect.left;
        const y = event.clientY - rect.top;
        this.hoverPx = { x, y };
        if (this.dragMode === "pan" && this.dragStartView) {
          const plot = this._plotRect(rect.width, rect.height);
          const span = this.dragStartView[1] - this.dragStartView[0];
          const dt = -((x - this.dragStartPx) / plot.w) * span;
          let nextStart = this.dragStartView[0] + dt;
          let nextEnd = this.dragStartView[1] + dt;
          if (nextStart < this.fullX[0]) {
            nextEnd += this.fullX[0] - nextStart;
            nextStart = this.fullX[0];
          }
          if (nextEnd > this.fullX[1]) {
            nextStart -= nextEnd - this.fullX[1];
            nextEnd = this.fullX[1];
          }
          this.viewX = [Math.max(this.fullX[0], nextStart), Math.min(this.fullX[1], nextEnd)];
        } else if (this.dragMode === "zoom") {
          this.dragCurrPx = x;
        }
        this.draw();
      });

      c.addEventListener("mouseleave", () => {
        this.hoverPx = null;
        if (!this.dragMode) this.draw();
      });

      c.addEventListener("mousedown", (event) => {
        if (event.button !== 0 || !this.lines.length) return;
        const x = getLocalX(event);
        this.dragStartPx = x;
        this.dragCurrPx = x;
        this.dragStartView = [...this.viewX];
        this.dragMode = event.shiftKey ? "zoom" : "pan";
      });

      window.addEventListener("mouseup", () => {
        if (!this.dragMode) return;
        const rect = c.getBoundingClientRect();
        const plot = this._plotRect(rect.width, rect.height);
        if (this.dragMode === "zoom" && Math.abs(this.dragCurrPx - this.dragStartPx) > 8) {
          const x1 = Math.max(plot.x, Math.min(this.dragStartPx, this.dragCurrPx));
          const x2 = Math.min(plot.x + plot.w, Math.max(this.dragStartPx, this.dragCurrPx));
          const span = this.viewX[1] - this.viewX[0];
          const t1 = this.viewX[0] + ((x1 - plot.x) / plot.w) * span;
          const t2 = this.viewX[0] + ((x2 - plot.x) / plot.w) * span;
          if (t2 - t1 >= 1) this.viewX = [t1, t2];
        }
        this.dragMode = null;
        this.dragStartView = null;
        this.draw();
      });

      c.addEventListener(
        "wheel",
        (event) => {
          if (!this.lines.length) return;
          event.preventDefault();
          const rect = c.getBoundingClientRect();
          const plot = this._plotRect(rect.width, rect.height);
          const ratio = Math.max(0, Math.min(1, (event.clientX - rect.left - plot.x) / plot.w));
          const span = this.viewX[1] - this.viewX[0];
          const anchor = this.viewX[0] + ratio * span;
          const factor = event.deltaY > 0 ? 1.25 : 0.8;
          const fullSpan = this.fullX[1] - this.fullX[0];
          const nextSpan = Math.max(2, Math.min(fullSpan, span * factor));
          let s = anchor - ratio * nextSpan;
          let e = s + nextSpan;
          if (s < this.fullX[0]) {
            s = this.fullX[0];
            e = Math.min(this.fullX[1], s + nextSpan);
          }
          if (e > this.fullX[1]) {
            e = this.fullX[1];
            s = Math.max(this.fullX[0], e - nextSpan);
          }
          this.viewX = [s, e];
          this.draw();
        },
        { passive: false }
      );

      c.addEventListener("dblclick", () => this.resetZoom());
    }

    _nearestIndex(times, target) {
      let lo = 0;
      let hi = times.length - 1;
      while (lo < hi) {
        const mid = (lo + hi) >> 1;
        if (times[mid] < target) lo = mid + 1;
        else hi = mid;
      }
      if (lo > 0 && Math.abs(times[lo - 1] - target) < Math.abs(times[lo] - target)) {
        return lo - 1;
      }
      return lo;
    }

    _drawLinePath(ctx, line, xMin, xMax, xToPx, yMapFn) {
      const { times, values, color } = line;
      const xSpan = Math.max(1e-6, xMax - xMin);
      ctx.beginPath();
      ctx.strokeStyle = color;
      ctx.lineWidth = 1.5;
      let drawing = false;
      let drawnPoints = 0;
      let lastSingle = null;
      for (let i = 0; i < times.length; i += 1) {
        const ts = times[i];
        if (ts < xMin - xSpan * 0.2 || ts > xMax + xSpan * 0.2) continue;
        const v = values[i];
        if (v === null) {
          drawing = false;
          continue;
        }
        const px = xToPx(ts);
        const py = yMapFn(v);
        if (!drawing) {
          ctx.moveTo(px, py);
          drawing = true;
        } else {
          ctx.lineTo(px, py);
        }
        drawnPoints += 1;
        lastSingle = { px, py };
      }
      ctx.stroke();
      if (drawnPoints === 1 && lastSingle) {
        ctx.beginPath();
        ctx.fillStyle = color;
        ctx.arc(lastSingle.px, lastSingle.py, 3, 0, Math.PI * 2);
        ctx.fill();
      }
    }

    draw() {
      const parent = this.canvas.parentElement;
      if (!parent) return;
      const width = parent.clientWidth || 600;
      const height = parent.clientHeight || 200;
      const dpr = window.devicePixelRatio || 1;
      if (this.canvas.width !== Math.round(width * dpr) || this.canvas.height !== Math.round(height * dpr)) {
        this.canvas.width = Math.round(width * dpr);
        this.canvas.height = Math.round(height * dpr);
      }

      const ctx = this.ctx;
      ctx.save();
      ctx.scale(dpr, dpr);
      const pal = chartPalette();
      ctx.fillStyle = pal.bg;
      ctx.fillRect(0, 0, width, height);

      const visibleLines = this.lines.filter((l) => !l.hidden);
      if (!visibleLines.length) {
        ctx.fillStyle = pal.text;
        ctx.font = "12px Inter, Segoe UI, Microsoft YaHei, sans-serif";
        ctx.textAlign = "center";
        ctx.textBaseline = "middle";
        ctx.fillText(window.SAMP.t("empty.dash"), width / 2, height / 2);
        ctx.restore();
        return;
      }

      const plot = this._plotRect(width, height);
      const [xMin, xMax] = this.viewX;
      const xSpan = Math.max(1e-6, xMax - xMin);
      const xToPx = (t) => plot.x + ((t - xMin) / xSpan) * plot.w;

      // 计算每条曲线的 Y 映射函数
      const lineYMap = new Map();
      const stacked = this.mode === "stacked" && visibleLines.length > 1;
      const normalized = this.mode === "normalized";

      if (stacked) {
        const laneGap = 4;
        const count = visibleLines.length;
        const laneH = Math.max(18, (plot.h - laneGap * (count - 1)) / count);
        ctx.font = "10px JetBrains Mono, Consolas, monospace";

        visibleLines.forEach((line, idx) => {
          const ly = plot.y + idx * (laneH + laneGap);
          const [yMin, yMax] = rangeInWindow(line, xMin, xMax);
          const ySpan = Math.max(1e-9, yMax - yMin);
          const yFn = (v) => ly + laneH - ((v - yMin) / ySpan) * laneH;
          lineYMap.set(line.id, yFn);

          // 泳道底线与上下极值刻度
          ctx.beginPath();
          ctx.strokeStyle = pal.laneBorder;
          ctx.moveTo(plot.x, Math.round(ly + laneH) + 0.5);
          ctx.lineTo(plot.x + plot.w, Math.round(ly + laneH) + 0.5);
          ctx.stroke();

          ctx.fillStyle = pal.text;
          ctx.textAlign = "right";
          ctx.textBaseline = "top";
          ctx.fillText(formatAxisVal(yMax), plot.x - 5, ly + 1);
          ctx.textBaseline = "bottom";
          ctx.fillText(formatAxisVal(yMin), plot.x - 5, ly + laneH - 1);

          // 泳道左上角曲线胶囊标签（半透明胶囊，非白描边）
          const tag = line.label;
          const tw = ctx.measureText(tag).width + 10;
          ctx.fillStyle = pal.labelBg;
          ctx.strokeStyle = pal.labelBorder;
          ctx.lineWidth = 1;
          ctx.beginPath();
          ctx.roundRect(plot.x + 4, ly + 2, tw, 14, 3);
          ctx.fill();
          ctx.stroke();
          ctx.fillStyle = line.color;
          ctx.textAlign = "left";
          ctx.textBaseline = "top";
          ctx.fillText(tag, plot.x + 9, ly + 4);
        });
      } else {
        let sharedMin = Infinity;
        let sharedMax = -Infinity;
        if (!normalized) {
          for (const line of visibleLines) {
            const [lo, hi] = rangeInWindow(line, xMin, xMax);
            if (lo < sharedMin) sharedMin = lo;
            if (hi > sharedMax) sharedMax = hi;
          }
          if (!Number.isFinite(sharedMin) || !Number.isFinite(sharedMax)) {
            sharedMin = 0;
            sharedMax = 1;
          }
        } else {
          sharedMin = 0;
          sharedMax = 100;
        }
        const sharedSpan = Math.max(1e-9, sharedMax - sharedMin);

        // 水平虚线网格 + Y 轴刻度
        const yTicks = 4;
        ctx.font = "11px JetBrains Mono, Consolas, monospace";
        ctx.textAlign = "right";
        ctx.textBaseline = "middle";
        for (let i = 0; i <= yTicks; i += 1) {
          const ratio = i / yTicks;
          const val = sharedMin + ratio * sharedSpan;
          const py = Math.round(plot.y + plot.h - ratio * plot.h) + 0.5;
          ctx.beginPath();
          ctx.setLineDash([3, 3]);
          ctx.strokeStyle = pal.grid;
          ctx.moveTo(plot.x, py);
          ctx.lineTo(plot.x + plot.w, py);
          ctx.stroke();
          ctx.setLineDash([]);
          ctx.fillStyle = pal.text;
          ctx.fillText(normalized ? `${Math.round(val)}%` : formatAxisVal(val), plot.x - 6, py);
        }

        for (const line of visibleLines) {
          if (normalized) {
            const [lo, hi] = rangeInWindow(line, xMin, xMax);
            const sp = Math.max(1e-9, hi - lo);
            lineYMap.set(line.id, (v) => plot.y + plot.h - ((v - lo) / sp) * plot.h);
          } else {
            lineYMap.set(line.id, (v) => plot.y + plot.h - ((v - sharedMin) / sharedSpan) * plot.h);
          }
        }
      }

      // X 轴底线与时间刻度
      ctx.beginPath();
      ctx.strokeStyle = pal.axis;
      ctx.moveTo(plot.x, plot.y + plot.h + 0.5);
      ctx.lineTo(plot.x + plot.w, plot.y + plot.h + 0.5);
      ctx.stroke();

      const xTicks = Math.max(2, Math.min(7, Math.floor(plot.w / 120)));
      ctx.font = "11px JetBrains Mono, Consolas, monospace";
      ctx.textAlign = "center";
      ctx.textBaseline = "top";
      ctx.fillStyle = pal.text;
      for (let i = 0; i <= xTicks; i += 1) {
        const ratio = i / xTicks;
        const ts = xMin + ratio * xSpan;
        ctx.fillText(formatAxisTime(ts, xSpan), plot.x + ratio * plot.w, plot.y + plot.h + 5);
      }

      // 绘制所有曲线
      ctx.save();
      ctx.beginPath();
      ctx.rect(plot.x, plot.y, plot.w, plot.h);
      ctx.clip();
      for (const line of visibleLines) {
        this._drawLinePath(ctx, line, xMin, xMax, xToPx, lineYMap.get(line.id));
      }

      // 表格点击联动定位竖线
      if (this.pinnedTime !== null && this.pinnedTime >= xMin && this.pinnedTime <= xMax) {
        const px = Math.round(xToPx(this.pinnedTime)) + 0.5;
        ctx.beginPath();
        ctx.strokeStyle = pal.pinned;
        ctx.lineWidth = 1.5;
        ctx.moveTo(px, plot.y);
        ctx.lineTo(px, plot.y + plot.h);
        ctx.stroke();
      }

      // Shift 框选高亮
      if (this.dragMode === "zoom") {
        const x1 = Math.min(this.dragStartPx, this.dragCurrPx);
        const w = Math.abs(this.dragCurrPx - this.dragStartPx);
        ctx.fillStyle = pal.selectionBg;
        ctx.fillRect(x1, plot.y, w, plot.h);
        ctx.strokeStyle = pal.selectionBorder;
        ctx.lineWidth = 1;
        ctx.strokeRect(x1 + 0.5, plot.y + 0.5, w, plot.h - 1);
      }
      ctx.restore();

      // 定位线底部时间胶囊
      if (this.pinnedTime !== null && this.pinnedTime >= xMin && this.pinnedTime <= xMax) {
        const px = xToPx(this.pinnedTime);
        const text = window.SAMP.formatTime(this.pinnedTime, true);
        ctx.font = "10px JetBrains Mono, Consolas, monospace";
        const tw = ctx.measureText(text).width + 10;
        const bx = Math.max(plot.x, Math.min(plot.x + plot.w - tw, px - tw / 2));
        const by = plot.y + plot.h - 17;
        ctx.fillStyle = pal.labelBg;
        ctx.strokeStyle = pal.pinned;
        ctx.lineWidth = 1;
        ctx.beginPath();
        ctx.roundRect(bx, by, tw, 15, 3);
        ctx.fill();
        ctx.stroke();
        ctx.fillStyle = pal.pinned;
        ctx.textAlign = "left";
        ctx.textBaseline = "top";
        ctx.fillText(text, bx + 5, by + 2);
      }

      // Hover 垂直十字准星 + 真实物理值胶囊 Tooltip
      if (
        this.hoverPx &&
        !this.dragMode &&
        this.hoverPx.x >= plot.x &&
        this.hoverPx.x <= plot.x + plot.w &&
        this.hoverPx.y >= plot.y &&
        this.hoverPx.y <= plot.y + plot.h
      ) {
        const hoverTime = xMin + ((this.hoverPx.x - plot.x) / plot.w) * xSpan;
        const rows = [];
        let snapTs = null;
        for (const line of visibleLines) {
          if (!line.times.length) continue;
          const idx = this._nearestIndex(line.times, hoverTime);
          const ts = line.times[idx];
          const val = line.values[idx];
          if (val === null) continue;
          if (snapTs === null || Math.abs(ts - hoverTime) < Math.abs(snapTs - hoverTime)) {
            snapTs = ts;
          }
          const yFn = lineYMap.get(line.id);
          rows.push({ label: line.label, color: line.color, ts, val, px: xToPx(ts), py: yFn(val) });
        }

        if (rows.length && snapTs !== null) {
          const cx = Math.round(xToPx(snapTs)) + 0.5;
          ctx.beginPath();
          ctx.setLineDash([4, 3]);
          ctx.strokeStyle = pal.crosshair;
          ctx.moveTo(cx, plot.y);
          ctx.lineTo(cx, plot.y + plot.h);
          ctx.stroke();
          ctx.setLineDash([]);

          for (const r of rows) {
            if (r.px >= plot.x && r.px <= plot.x + plot.w) {
              ctx.beginPath();
              ctx.fillStyle = r.color;
              ctx.arc(r.px, r.py, 3.2, 0, Math.PI * 2);
              ctx.fill();
            }
          }

          const header = window.SAMP.formatTime(snapTs);
          const displayRows = rows.slice(0, 8);
          ctx.font = "11px JetBrains Mono, Consolas, monospace";
          const lineHeight = 16;
          let boxW = ctx.measureText(header).width + 20;
          for (const r of displayRows) {
            const w = ctx.measureText(`${r.label}  ${formatAxisVal(r.val)}`).width + 28;
            if (w > boxW) boxW = w;
          }
          const boxH = 14 + (displayRows.length + 1) * lineHeight;
          let bx = cx + 10;
          if (bx + boxW > plot.x + plot.w) bx = cx - boxW - 10;
          if (bx < plot.x) bx = plot.x + 4;
          const by = plot.y + 6;

          ctx.fillStyle = pal.labelBg;
          ctx.strokeStyle = pal.labelBorder;
          ctx.lineWidth = 1;
          ctx.beginPath();
          ctx.roundRect(bx, by, boxW, boxH, 6);
          ctx.fill();
          ctx.stroke();

          ctx.fillStyle = pal.text;
          ctx.textAlign = "left";
          ctx.textBaseline = "top";
          ctx.fillText(header, bx + 8, by + 6);

          displayRows.forEach((r, idx) => {
            const ry = by + 6 + (idx + 1) * lineHeight;
            ctx.fillStyle = r.color;
            ctx.fillRect(bx + 8, ry + 3, 7, 7);
            ctx.fillStyle = pal.labelText;
            ctx.textAlign = "left";
            ctx.fillText(r.label, bx + 19, ry);
            ctx.textAlign = "right";
            ctx.fillText(formatAxisVal(r.val), bx + boxW - 8, ry);
          });
        }
      }

      ctx.restore();
    }
  }

  window.SAMPChart = { TimeSeriesChart };
})();

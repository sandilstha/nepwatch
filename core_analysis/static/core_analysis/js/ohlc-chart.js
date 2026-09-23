/* ============================================================================
   Index OHLC — TradingView Lightweight Charts candlestick + volume chart.

   - Pulls daily OHLCV from the UDF datafeed (/insights/udf/history).
   - Symbol dropdown (NEPSE index + sub-indices), session count, last-price line.
   - Theme-aware (follows the dashboard's data-theme), recolours on toggle.
   - A lightweight drawing layer (Cursor / Line / Horizontal / Vertical / Clear)
     painted on an overlay canvas, anchored to chart time/price coordinates.

   Exposes window.MIOHLC = { refresh, setTheme, destroy } so the dashboard's
   theme toggle and the TradingView Advanced Charts upgrade can coordinate.
   ========================================================================== */
(function () {
  "use strict";

  if (typeof LightweightCharts === "undefined") return;

  var cfg = window.MI_CONFIG || {};
  var udfBase = cfg.udfBase || "/insights/udf";
  var DASHBOARD_SESSION_COUNT = 320;

  var SYMBOLS = [
    { t: "NEPSE", l: "NEPSE Index" },
    { t: "SENSITIVE", l: "Sensitive" },
    { t: "FLOAT", l: "Float" },
    { t: "SENFLOAT", l: "Sensitive Float" },
    { t: "BANKING", l: "Banking" },
    { t: "DEVBANK", l: "Development Bank" },
    { t: "FINANCE", l: "Finance" },
    { t: "HOTEL", l: "Hotels & Tourism" },
    { t: "HYDRO", l: "Hydropower" },
    { t: "INVEST", l: "Investment" },
    { t: "LIFEINSU", l: "Life Insurance" },
    { t: "MANUFAC", l: "Manufacturing" },
    { t: "MICROFIN", l: "Microfinance" },
    { t: "MUTUAL", l: "Mutual Fund" },
    { t: "NONLIFE", l: "Non-Life Insurance" },
    { t: "OTHERS", l: "Others" },
    { t: "TRADING", l: "Trading" }
  ];

  var state = {
    chart: null,
    candle: null,
    volume: null,
    bars: null,        // raw {t,o,h,l,c,v} arrays
    symbol: "NEPSE",
    destroyed: false
  };

  function $(id) { return document.getElementById(id); }
  function cssVar(name) { return getComputedStyle(document.documentElement).getPropertyValue(name).trim(); }
  function themeName() { return document.documentElement.getAttribute("data-theme") === "light" ? "light" : "dark"; }
  function pad(n) { return (n < 10 ? "0" : "") + n; }
  function tsToDate(ts) { var d = new Date(ts * 1000); return d.getUTCFullYear() + "-" + pad(d.getUTCMonth() + 1) + "-" + pad(d.getUTCDate()); }
  function deferInitialFetch(fn) {
    if (typeof window.requestIdleCallback === "function") {
      window.requestIdleCallback(fn, { timeout: 1800 });
    } else {
      window.setTimeout(fn, 1200);
    }
  }

  function hexToRgba(hex, a) {
    var h = (hex || "").replace("#", "");
    if (h.length === 3) h = h[0] + h[0] + h[1] + h[1] + h[2] + h[2];
    var n = parseInt(h, 16);
    if (isNaN(n)) return "rgba(120,120,120," + a + ")";
    return "rgba(" + ((n >> 16) & 255) + "," + ((n >> 8) & 255) + "," + (n & 255) + "," + a + ")";
  }

  // ── theme palette for the chart ────────────────────────────────────────
  function palette() {
    return {
      up: cssVar("--up") || "#14b88a",
      down: cssVar("--down") || "#e0414b",
      text: cssVar("--ink-2") || "#888",
      grid: cssVar("--line-soft") || "rgba(120,120,120,0.1)",
      border: cssVar("--line") || "rgba(120,120,120,0.2)"
    };
  }

  function chartOptions() {
    var p = palette();
    return {
      autoSize: true,
      layout: { background: { type: "solid", color: "transparent" }, textColor: p.text, fontFamily: "Manrope, sans-serif" },
      grid: { vertLines: { color: p.grid }, horzLines: { color: p.grid } },
      rightPriceScale: { borderColor: p.border },
      timeScale: { borderColor: p.border, timeVisible: false, secondsVisible: false, rightOffset: 4 },
      crosshair: { mode: LightweightCharts.CrosshairMode.Normal },
      handleScale: true,
      handleScroll: true
    };
  }

  // ── build / render ─────────────────────────────────────────────────────
  function buildSeries() {
    if (state.destroyed || !state.bars || !state.candle || !state.chart) return;
    var b = state.bars, p = palette();
    var candles = [], vols = [];
    for (var i = 0; i < b.t.length; i++) {
      var time = tsToDate(b.t[i]);
      candles.push({ time: time, open: b.o[i], high: b.h[i], low: b.l[i], close: b.c[i] });
      var up = b.c[i] >= b.o[i];
      vols.push({ time: time, value: b.v[i], color: hexToRgba(up ? p.up : p.down, 0.5) });
    }
    state.candle.setData(candles);
    state.volume.setData(vols);
    var el = $("ohlc-sessions");
    if (el) el.textContent = candles.length.toLocaleString("en-US") + " sessions";
  }

  function applyColors() {
    var p = palette();
    state.chart.applyOptions(chartOptions());
    state.candle.applyOptions({
      upColor: p.up, downColor: p.down,
      borderUpColor: p.up, borderDownColor: p.down,
      wickUpColor: p.up, wickDownColor: p.down
    });
    buildSeries(); // recolour volume bars
  }

  function fitDashboardRange() {
    if (!state.chart || !state.bars || !state.bars.t || !state.bars.t.length) return;
    var host = $("ohlc-chart");
    var width = host ? host.clientWidth : 800;
    var visible = width < 560 ? 120 : (width < 800 ? 200 : 280);
    var count = state.bars.t.length;
    if (count <= visible) {
      state.chart.timeScale().fitContent();
      return;
    }
    state.chart.timeScale().setVisibleLogicalRange({
      from: count - visible,
      to: count + 4
    });
  }

  function fetchBars(symbol) {
    if (state.destroyed) return;   // TradingView took over while the deferred fetch was pending
    var now = Math.floor(Date.now() / 1000);
    // This is a dashboard overview, not the full charting terminal. Loading the
    // entire index history (1997+) inflated the response and compressed every
    // candle into an unreadable strip. A bounded countback keeps it fast and
    // leaves roughly one market cycle visible; the Technical Analysis desk is
    // still available for deeper history.
    var url = udfBase + "/history?symbol=" + encodeURIComponent(symbol) +
      "&resolution=1D&countback=" + DASHBOARD_SESSION_COUNT + "&to=" + now;
    var sess = $("ohlc-sessions");
    if (sess) sess.textContent = "loading…";
    return fetch(url, { credentials: "same-origin" })
      .then(function (r) { return r.json(); })
      .then(function (d) {
        if (d.s !== "ok" || !d.t || !d.t.length) { if (sess) sess.textContent = "no data"; return; }
        state.bars = d;
        buildSeries();
        fitDashboardRange();
        if (window.MIDraw) window.MIDraw.redraw();
      })
      .catch(function () { if (sess) sess.textContent = "load failed"; });
  }

  // ── drawing layer ──────────────────────────────────────────────────────
  function DrawingLayer(chart, series, canvas, host) {
    var ctx = canvas.getContext("2d");
    var tool = "cursor";
    var items = [];
    var selected = null;
    var pending = null;     // first point of a 2-click line
    var hover = null;       // current mouse {x,y} for rubber-band preview
    var raf = null;

    function dpr() { return window.devicePixelRatio || 1; }
    function paneW() { return chart.timeScale().width(); }
    function paneH() { return host.clientHeight - chart.timeScale().height(); }
    function xToTime(x) { return chart.timeScale().coordinateToTime(x); }
    function timeToX(t) { return chart.timeScale().timeToCoordinate(t); }
    function yToPrice(y) { return series.coordinateToPrice(y); }
    function priceToY(pr) { return series.priceToCoordinate(pr); }

    function resize() {
      var w = host.clientWidth, h = host.clientHeight, r = dpr();
      canvas.style.width = w + "px"; canvas.style.height = h + "px";
      canvas.width = Math.round(w * r); canvas.height = Math.round(h * r);
      schedule();
    }

    function schedule() {
      if (raf) return;
      raf = requestAnimationFrame(function () { raf = null; redraw(); });
    }

    function stroke(color, width, dash) {
      ctx.strokeStyle = color; ctx.lineWidth = width || 1.4;
      ctx.setLineDash(dash || []);
    }

    function redraw() {
      var r = dpr();
      ctx.setTransform(r, 0, 0, r, 0, 0);
      ctx.clearRect(0, 0, canvas.width, canvas.height);
      var pw = paneW(), ph = paneH();
      var accent = cssVar("--accent") || "#12a";
      var sel = cssVar("--caution") || "#e0a000";

      items.forEach(function (it) {
        var color = (it === selected) ? sel : accent;
        stroke(color, it === selected ? 2 : 1.4);
        if (it.type === "h") {
          var y = priceToY(it.price); if (y == null) return;
          line(0, y, pw, y);
          tag(pw, y, fmt(it.price), color);
        } else if (it.type === "v") {
          var x = timeToX(it.time); if (x == null) return;
          line(x, 0, x, ph);
        } else if (it.type === "line") {
          var x1 = timeToX(it.a.time), y1 = priceToY(it.a.price);
          var x2 = timeToX(it.b.time), y2 = priceToY(it.b.price);
          if (x1 == null || x2 == null || y1 == null || y2 == null) return;
          line(x1, y1, x2, y2);
          handle(x1, y1, color); handle(x2, y2, color);
        }
      });

      // rubber-band preview while placing a line
      if (pending && hover) {
        var px = timeToX(pending.time), py = priceToY(pending.price);
        if (px != null && py != null) { stroke(accent, 1.2, [4, 4]); line(px, py, hover.x, hover.y); }
      }
    }

    function line(x1, y1, x2, y2) { ctx.beginPath(); ctx.moveTo(x1, y1); ctx.lineTo(x2, y2); ctx.stroke(); }
    function handle(x, y, color) { ctx.setLineDash([]); ctx.fillStyle = color; ctx.beginPath(); ctx.arc(x, y, 3.5, 0, 2 * Math.PI); ctx.fill(); }
    function fmt(v) { return (Math.round(v * 100) / 100).toLocaleString("en-US", { minimumFractionDigits: 2, maximumFractionDigits: 2 }); }
    function tag(x, y, text, color) {
      ctx.setLineDash([]); ctx.font = "11px Manrope, sans-serif";
      var w = ctx.measureText(text).width + 10;
      ctx.fillStyle = color; ctx.fillRect(x - w, y - 9, w, 18);
      ctx.fillStyle = "#fff"; ctx.textBaseline = "middle"; ctx.fillText(text, x - w + 5, y);
    }

    // hit testing for selection
    function distToSeg(px, py, x1, y1, x2, y2) {
      var dx = x2 - x1, dy = y2 - y1, len2 = dx * dx + dy * dy;
      var t = len2 ? ((px - x1) * dx + (py - y1) * dy) / len2 : 0;
      t = Math.max(0, Math.min(1, t));
      var qx = x1 + t * dx, qy = y1 + t * dy;
      return Math.hypot(px - qx, py - qy);
    }
    function hit(x, y) {
      for (var i = items.length - 1; i >= 0; i--) {
        var it = items[i], d = 999;
        if (it.type === "h") { var hy = priceToY(it.price); if (hy != null) d = Math.abs(y - hy); }
        else if (it.type === "v") { var vx = timeToX(it.time); if (vx != null) d = Math.abs(x - vx); }
        else if (it.type === "line") {
          var x1 = timeToX(it.a.time), y1 = priceToY(it.a.price), x2 = timeToX(it.b.time), y2 = priceToY(it.b.price);
          if (x1 != null && x2 != null && y1 != null && y2 != null) d = distToSeg(x, y, x1, y1, x2, y2);
        }
        if (d <= 6) return it;
      }
      return null;
    }

    function onClick(e) {
      if (tool === "cursor") return;
      var rect = canvas.getBoundingClientRect();
      var x = e.clientX - rect.left, y = e.clientY - rect.top;

      // clicking an existing drawing selects it instead of creating a new one
      var h = hit(x, y);
      if (h && !pending) { selected = h; schedule(); return; }

      if (tool === "hline") {
        var pr = yToPrice(y); if (pr != null) { items.push({ type: "h", price: pr }); }
      } else if (tool === "vline") {
        var t = xToTime(x); if (t != null) { items.push({ type: "v", time: t }); }
      } else if (tool === "line") {
        var time = xToTime(x), price = yToPrice(y);
        if (time == null || price == null) return;
        if (!pending) { pending = { time: time, price: price }; }
        else { items.push({ type: "line", a: pending, b: { time: time, price: price } }); pending = null; }
      }
      schedule();
    }

    function onMove(e) {
      if (tool !== "line" || !pending) { hover = null; return; }
      var rect = canvas.getBoundingClientRect();
      hover = { x: e.clientX - rect.left, y: e.clientY - rect.top };
      schedule();
    }

    function onKey(e) {
      if ((e.key === "Delete" || e.key === "Backspace") && selected) {
        var i = items.indexOf(selected); if (i >= 0) items.splice(i, 1);
        selected = null; schedule();
      } else if (e.key === "Escape") { pending = null; selected = null; schedule(); }
    }

    function setTool(t) {
      tool = t; pending = null;
      canvas.style.pointerEvents = (t === "cursor") ? "none" : "auto";
      canvas.style.cursor = (t === "cursor") ? "default" : "crosshair";
      schedule();
    }

    function clear() { items = []; selected = null; pending = null; schedule(); }

    canvas.addEventListener("mousedown", onClick);
    canvas.addEventListener("mousemove", onMove);
    document.addEventListener("keydown", onKey);
    chart.timeScale().subscribeVisibleLogicalRangeChange(schedule);
    var ro = new ResizeObserver(resize); ro.observe(host);
    resize();

    return {
      setTool: setTool, clear: clear, redraw: schedule,
      destroy: function () {
        document.removeEventListener("keydown", onKey);
        try { ro.disconnect(); } catch (e) {}
        ctx.clearRect(0, 0, canvas.width, canvas.height);
      }
    };
  }

  // ── toolbar wiring ─────────────────────────────────────────────────────
  function initToolbar() {
    var bar = $("ohlc-toolbar");
    if (!bar) return;
    bar.querySelectorAll(".mi-draw-btn[data-tool]").forEach(function (btn) {
      btn.addEventListener("click", function () {
        bar.querySelectorAll(".mi-draw-btn[data-tool]").forEach(function (b) { b.classList.remove("is-active"); });
        btn.classList.add("is-active");
        if (window.MIDraw) window.MIDraw.setTool(btn.getAttribute("data-tool"));
      });
    });
    var clearBtn = $("ohlc-clear");
    if (clearBtn) clearBtn.addEventListener("click", function () { if (window.MIDraw) window.MIDraw.clear(); });
  }

  function initSymbolSelect() {
    var sel = $("ohlc-symbol");
    if (!sel) return;
    sel.innerHTML = SYMBOLS.map(function (s) { return '<option value="' + s.t + '">' + s.l + "</option>"; }).join("");
    sel.value = state.symbol;
    sel.addEventListener("change", function () { state.symbol = sel.value; fetchBars(state.symbol); });
  }

  // ── public hooks ───────────────────────────────────────────────────────
  window.MIOHLC = {
    refresh: function () { if (!state.destroyed) fetchBars(state.symbol); },
    setTheme: function () { if (!state.destroyed && state.chart) applyColors(); },
    destroy: function () {
      state.destroyed = true;
      if (window.MIDraw && window.MIDraw.destroy) { try { window.MIDraw.destroy(); } catch (e) {} }
      if (state.chart) { try { state.chart.remove(); } catch (e) {} state.chart = null; }
      var wrap = $("ohlc-chart"); if (wrap) wrap.style.display = "none";
      var cv = $("ohlc-draw"); if (cv) cv.style.display = "none";
      var tb = $("ohlc-toolbar"); if (tb) tb.style.display = "none";
    }
  };

  // ── boot ───────────────────────────────────────────────────────────────
  function init() {
    var host = $("ohlc-chart");
    if (!host) return;
    state.chart = LightweightCharts.createChart(host, chartOptions());
    state.candle = state.chart.addCandlestickSeries({ priceFormat: { type: "price", precision: 2, minMove: 0.01 } });
    state.volume = state.chart.addHistogramSeries({ priceFormat: { type: "volume" }, priceScaleId: "" });
    state.volume.priceScale().applyOptions({ scaleMargins: { top: 0.8, bottom: 0 } });
    state.candle.priceScale().applyOptions({ scaleMargins: { top: 0.08, bottom: 0.28 } });
    applyColors();

    var canvas = $("ohlc-draw");
    if (canvas) window.MIDraw = DrawingLayer(state.chart, state.candle, canvas, host);

    initSymbolSelect();
    initToolbar();
    deferInitialFetch(function () { fetchBars(state.symbol); });
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", init);
  } else {
    init();
  }
})();

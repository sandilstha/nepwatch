/* ============================================================================
   Market Insights dashboard front-end.
   - Renders all widgets from a single payload (single source of truth).
   - Auto-refreshes by polling the JSON API at a user-configurable interval.
   - Degrades gracefully: failed fetches keep the last good data and flag "stale".
   - Dark / light theme toggle persisted in localStorage.
   ========================================================================== */
(function () {
  "use strict";

  var CONFIG = window.MI_CONFIG || { apiUrl: "/insights/api/", refreshSeconds: 30 };
  var LS_THEME = "mi-theme";
  // Key bumped to v2 when the default became Manual: browsers holding the old
  // saved "30" would otherwise keep auto-refreshing forever and the new
  // default would appear not to work. Bumping discards that value once.
  var LS_INTERVAL = "mi-refresh-interval-v2";
  var LS_HEATMAP_SECTOR = "mi-heatmap-sector";
  var LS_HEATMAP_ZOOM = "mi-heatmap-zoom";
  var LS_COMPARE_DAYS = "mi-compare-days";

  var HEATMAP_ALL_LIMIT = 60;     // tiles shown for "All sectors"
  var HEATMAP_SECTOR_LIMIT = 80;  // tiles shown when a single sector is picked
  // Manual size control for the heatmap. Zoom scales tile height + label font so
  // dense sectors (e.g. Hydropower's ~40+ scrips) can be enlarged until the
  // symbol/point/% labels fit inside their boxes instead of spilling out.
  var HEATMAP_ZOOM_MIN = 0.7;
  var HEATMAP_ZOOM_MAX = 2.0;
  var HEATMAP_ZOOM_STEP = 0.15;

  // Endpoint for the sub-index comparison chart (independent of the polled
  // dashboard payload so its heavy historical series isn't re-fetched every tick).
  var COMPARE_URL = "/insights/subindices/";
  // Windowed sector turnover (weekly / monthly / quarterly / custom range).
  var SECTOR_URL = "/insights/sector-turnover/";
  var LS_SECTOR_PERIOD = "mi.sectorPeriod";
  var COMPARE_DEFAULT_DAYS = 250;
  // One distinct colour per series, in the backend's order: NEPSE first (drawn
  // emphasised below), then the aggregate indices, then the 13 sector sub-indices.
  var COMPARE_COLORS = [
    "#e5e7eb", "#94a3b8", "#64748b", "#475569",
    "#3b82f6", "#ef4444", "#22c55e", "#f59e0b", "#a855f7", "#06b6d4",
    "#ec4899", "#84cc16", "#f97316", "#14b8a6", "#6366f1", "#eab308", "#d946ef"
  ];

  var state = {
    data: null,
    timer: null,
    intervalSec: CONFIG.refreshSeconds,
    inFlight: false,
    charts: {},
    heatmapSector: "ALL",
    heatmapZoom: 1,
    compare: { days: COMPARE_DEFAULT_DAYS, data: null, inFlight: false },
    // Sector turnover card: "1D" paints straight from the polled payload; every
    // other period is served by SECTOR_URL and cached here.
    sector: { period: "1D", today: [], data: null, inFlight: false }
  };

  // ── Formatting helpers ─────────────────────────────────────────────────
  function isNum(v) { return typeof v === "number" && isFinite(v); }

  function clamp(v, lo, hi) { return Math.min(hi, Math.max(lo, v)); }

  function clampZoom(z) { return clamp(isNum(z) ? z : 1, HEATMAP_ZOOM_MIN, HEATMAP_ZOOM_MAX); }

  function fmtNum(v, dp) {
    if (!isNum(v)) return "—";
    return v.toLocaleString("en-US", { minimumFractionDigits: dp || 0, maximumFractionDigits: dp || 0 });
  }

  function fmtCompact(v) {
    if (!isNum(v)) return "—";
    var abs = Math.abs(v);
    if (abs >= 1e11) return (v / 1e11).toFixed(2) + " Kh";  // Kharba (hundred billion)
    if (abs >= 1e9) return (v / 1e9).toFixed(2) + " Ar";   // Arba (billion)
    if (abs >= 1e7) return (v / 1e7).toFixed(2) + " Cr";   // Crore (10 million)
    if (abs >= 1e5) return (v / 1e5).toFixed(2) + " L";    // Lakh (hundred thousand)
    return fmtNum(v, 0);
  }

  function fmtMoney(v) {
    if (!isNum(v)) return "—";
    return "Rs " + fmtCompact(v);
  }

  function fmtPct(v) {
    if (!isNum(v)) return "—";
    return (v > 0 ? "+" : "") + v.toFixed(2) + "%";
  }

  function fmtSigned(v) {
    if (!isNum(v)) return "—";
    return (v > 0 ? "+" : "") + v.toFixed(2);
  }

  function dirClass(v) {
    if (!isNum(v) || v === 0) return "flat";
    return v > 0 ? "up" : "down";
  }

  function cssVar(name) {
    return getComputedStyle(document.documentElement).getPropertyValue(name).trim();
  }

  function el(id) { return document.getElementById(id); }

  // ── Theme ──────────────────────────────────────────────────────────────
  function applyTheme(theme) {
    document.documentElement.setAttribute("data-theme", theme);
    var btn = el("mi-theme-btn");
    if (btn) {
      var nextTheme = theme === "light" ? "dark" : "light";
      btn.textContent = nextTheme === "light" ? "Light" : "Dark";
      btn.title = "Switch to " + nextTheme + " theme";
      btn.setAttribute("aria-label", btn.title);
    }
    try { localStorage.setItem(LS_THEME, theme); } catch (e) {}
  }

  function initTheme() {
    var saved;
    try { saved = localStorage.getItem(LS_THEME); } catch (e) {}
    if (!saved) {
      saved = window.matchMedia && window.matchMedia("(prefers-color-scheme: light)").matches ? "light" : "dark";
    }
    applyTheme(saved);
    var themeBtn = el("mi-theme-btn");
    if (themeBtn) themeBtn.addEventListener("click", function () {
      var next = document.documentElement.getAttribute("data-theme") === "light" ? "dark" : "light";
      applyTheme(next);
      // Rebuild charts so their baked-in colours match the new palette.
      destroyCharts();
      if (state.data) renderCharts(state.data);
      // Recolour the OHLC chart and the TradingView terminal (if active) too.
      if (window.MIOHLC && window.MIOHLC.setTheme) window.MIOHLC.setTheme(next);
      if (typeof window.MI_setTVTheme === "function") window.MI_setTVTheme(next);
    });
  }

  // ── Status indicator ───────────────────────────────────────────────────
  function setStatus(kind, text) {
    var box = el("mi-status");
    box.classList.remove("is-live", "is-stale");
    if (kind === "live") box.classList.add("is-live");
    if (kind === "stale") box.classList.add("is-stale");
    el("mi-status-text").textContent = text;
  }

  function stamp() {
    var d = new Date();
    el("mi-last-updated").textContent = "Updated " + d.toLocaleTimeString("en-US");
  }

  function setPayloadStatus(d) {
    if (d && d.live) {
      setStatus("live", "Live");
    } else if (d && d.has_data) {
      setStatus("live", "EOD ready");
    } else {
      setStatus("stale", "No data");
    }
  }

  function deferNonCritical(fn, timeout) {
    if (typeof window.requestIdleCallback === "function") {
      window.requestIdleCallback(fn, { timeout: timeout || 2000 });
    } else {
      window.setTimeout(fn, timeout || 1200);
    }
  }

  // ── Renderers ──────────────────────────────────────────────────────────
  function renderOverview(d) {
    var ov = d.overview || {};
    var deltaEl = el("ov-index-delta");
    deltaEl.innerHTML = '<span class="mi-stat-value">' + fmtNum(ov.nepse_index, 2) + '</span> <span class="mi-stat-delta ' + dirClass(ov.nepse_pct) + '">' + fmtSigned(ov.nepse_change) + ' <small>(' + fmtPct(ov.nepse_pct) + ')</small></span>';
    el("ov-index").innerHTML = ""; // Clear the old index-only element
    el("ov-prevclose").textContent = fmtNum(ov.nepse_prev_close, 2);

    el("ov-52w-high").textContent = fmtNum(ov.nepse_52w_high, 2);
    el("ov-52w-low").textContent = fmtNum(ov.nepse_52w_low, 2);
    el("ov-day-high").textContent = fmtNum(ov.nepse_high, 2);
    el("ov-day-low").textContent = fmtNum(ov.nepse_low, 2);

    el("ov-turnover").textContent = fmtMoney(ov.turnover);
    var turnoverDelta = el("ov-turnover-delta");
    turnoverDelta.textContent = "(" + fmtPct(ov.turnover_pct) + ")";
    turnoverDelta.className = "mi-stat-delta " + dirClass(ov.turnover_pct);
    el("ov-mcap").textContent = fmtMoney(ov.market_cap);
    var mcapDelta = el("ov-mcap-delta");
    mcapDelta.textContent = "(" + fmtPct(ov.market_cap_pct) + ")";
    mcapDelta.className = "mi-sub-delta " + dirClass(ov.market_cap_pct);

    el("ov-trades").textContent = fmtNum(ov.trades, 0);
    el("ov-kitta").textContent = fmtCompact(ov.volume) + " kitta";
    el("ov-scrips").textContent = " · " + fmtNum(ov.scrips_traded, 0) + " scrips";

    el("mi-asof-date").textContent = d.as_of || "—";

    // Live vs end-of-day indicator.
    var badge = el("mi-live-badge");
    var label = el("mi-asof-label");
    if (badge) badge.hidden = !d.live;
    if (label) label.textContent = d.live ? "Live" : "As of";

  }

  function symCell(row) {
    var name = row.name ? "<small>" + escapeHtml(row.name) + "</small>" : "";
    return '<span class="mi-sym">' + escapeHtml(row.symbol) + name + "</span>";
  }

  function escapeHtml(s) {
    return String(s == null ? "" : s)
      .replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;").replace(/'/g, "&#39;");
  }

  function renderRankedTable(tbodyId, rows, kind) {
    var tbody = el(tbodyId);
    // The host card may be hidden on the page — skip instead of throwing, so a
    // removed table can't break the renders that follow (contributors, charts).
    if (!tbody) return;
    if (!rows || !rows.length) {
      tbody.innerHTML = '<tr><td colspan="3" class="mi-empty">No data available</td></tr>';
      return;
    }
    var html = rows.map(function (r) {
      if (kind === "active") {
        return "<tr><td>" + symCell(r) + "</td><td class='num'>" + fmtCompact(r.volume) +
          "</td><td class='num'>" + fmtMoney(r.turnover) + "</td></tr>";
      }
      var cls = dirClass(r.pct);
      return "<tr><td>" + symCell(r) + "</td><td class='num'>" + fmtNum(r.ltp, 2) +
        "</td><td class='num'><span class='mi-chg-badge " + cls + "'>" + fmtPct(r.pct) + "</span></td></tr>";
    }).join("");
    tbody.innerHTML = html;
  }

  // ── Charts (ApexCharts) ────────────────────────────────────────────────
  function baseChartOpts() {
    var grid = cssVar("--line-soft");
    var ink = cssVar("--ink-2");
    return {
      foreColor: ink,
      gridColor: grid,
      up: cssVar("--up"),
      down: cssVar("--down"),
      flat: cssVar("--flat"),
      accent: cssVar("--accent")
    };
  }

  function destroyCharts() {
    Object.keys(state.charts).forEach(function (k) {
      try { state.charts[k].destroy(); } catch (e) {}
    });
    state.charts = {};
  }

  function renderCharts(d) {
    // The NEPSE Index card is now the Lightweight Charts OHLC chart (ohlc-chart.js).
    renderBreadthChart(d.breadth || {}, d.live, d.source);
    renderGreedMeter(d.breadth || {}, d.overview || {}, d.live, d.source, d.greed_history || []);
    renderSectorChart(d.sectors || []);
    populateHeatmapSectors(d.heatmap || []);
    renderHeatmap();
    // Comparison chart is fed by its own endpoint; re-paint from cached data so a
    // theme toggle (which destroys all charts) or a refresh restores it.
    if (state.compare.data) renderSubindexCompare();
  }

  // Build the sector <select> from the tiles present, preserving the current
  // choice when it still exists after a refresh.
  function populateHeatmapSectors(tiles) {
    var sel = el("heatmap-sector");
    if (!sel) return;
    var counts = {};
    tiles.forEach(function (t) {
      var s = t.sector || "Other";
      counts[s] = (counts[s] || 0) + 1;
    });
    var current = state.heatmapSector || "ALL";
    if (current !== "ALL" && counts[current] === undefined) current = "ALL";

    var opts = ['<option value="ALL">All sectors (' + tiles.length + ")</option>"];
    Object.keys(counts).sort().forEach(function (s) {
      opts.push('<option value="' + escapeHtml(s) + '">' + escapeHtml(s) + " (" + counts[s] + ")</option>");
    });
    sel.innerHTML = opts.join("");
    sel.value = current;
    state.heatmapSector = current;
  }

  function filteredHeatmap() {
    var all = (state.data && state.data.heatmap) || [];
    var sel = state.heatmapSector || "ALL";
    if (sel === "ALL") return all.slice(0, HEATMAP_ALL_LIMIT);
    return all.filter(function (t) { return (t.sector || "Other") === sel; }).slice(0, HEATMAP_SECTOR_LIMIT);
  }

  function renderBreadthChart(b, live, source) {
    // Compact participation donut. Exact values and shares are shown in the
    // table below, so the chart remains useful without relying on hover.
    var node = el("chart-breadth");
    if (node) {
      var t = baseChartOpts();
      var opts = {
        chart: { type: "donut", height: 190, parentHeightOffset: 0, fontFamily: "Manrope, sans-serif" },
        series: [b.advancing || 0, b.declining || 0, b.unchanged || 0],
        labels: ["Advancing", "Declining", "Unchanged"],
        colors: [t.up, t.down, t.flat],
        stroke: { width: 0 },
        legend: { show: false },
        dataLabels: { enabled: true, formatter: function (val, o) { return o.w.config.series[o.seriesIndex]; }, style: { fontSize: "11px" } },
        plotOptions: { pie: { customScale: 1, donut: { size: "64%", labels: { show: true, total: { show: true, label: "Scrips", color: t.foreColor, formatter: function (w) { return w.globals.seriesTotals.reduce(function (a, c) { return a + c; }, 0); } } } } } },
        tooltip: {
          theme: themeName(),
          y: { formatter: function (val, o) {
            var tot = o.w.globals.seriesTotals.reduce(function (a, c) { return a + c; }, 0) || 1;
            return fmtNum(val, 0) + " · " + (val / tot * 100).toFixed(1) + "%";
          } }
        }
      };
      mountChart("chart-breadth", "breadth", opts);
    }
    renderBreadthDetail(b, live, source);
  }

  // Sets the headline breadth signal and reports whether it is intraday or a
  // valid end-of-day close. EOD data is not stale data and must not be faded.
  function renderBreadthDetail(b, live, source) {
    var adv = b.advancing || 0, dec = b.declining || 0, unch = b.unchanged || 0;
    var total = adv + dec + unch;
    var chip = el("breadth-sentiment");
    var table = el("breadth-table");

    if (!total) {
      if (chip) { chip.textContent = "—"; chip.className = "mi-pill"; }
      if (table) table.innerHTML = "";
      return;
    }

    var ratio = dec > 0 ? adv / dec : (adv > 0 ? Infinity : 0);
    var label, cls;
    if (Math.abs(adv - dec) <= total * 0.03) { label = "Neutral"; cls = "flat"; }
    else if (ratio >= 1.5) { label = "Strong Bullish"; cls = "up"; }
    else if (ratio > 1) { label = "Bullish"; cls = "up"; }
    else if (ratio > 0.67) { label = "Bearish"; cls = "down"; }
    else { label = "Strong Bearish"; cls = "down"; }

    if (chip) {
      var prefix = live ? "Live" : (source === "eod" ? "EOD" : "Close");
      chip.textContent = prefix + " · " + label;
      chip.className = "mi-pill " + (cls === "up" ? "mi-pill-up" : cls === "down" ? "mi-pill-down" : "");
    }
    if (table) {
      var ratioText = dec > 0 ? ratio.toFixed(2) : (adv > 0 ? "∞" : "0.00");
      function pct(value) { return (value / total * 100).toFixed(1) + "%"; }
      function row(label, value, cls, dot) {
        return '<tr><th scope="row"><span class="mi-breadth-label">' +
          (dot ? '<i class="mi-breadth-dot ' + dot + '"></i>' : "") + label +
          '</span></th><td class="' + (cls || "") + '">' + value + '</td></tr>';
      }
      table.innerHTML =
        row("A/D Ratio", ratioText, ratio >= 1 ? "up" : "down", "") +
        row("Advancing", fmtNum(adv, 0) + " · " + pct(adv), "up", "up") +
        row("Declining", fmtNum(dec, 0) + " · " + pct(dec), "down", "down") +
        row("Unchanged", fmtNum(unch, 0) + " · " + pct(unch), "flat", "flat") +
        row("Total scrips", fmtNum(total, 0), "", "");
    }
  }

  // ── NEPSE Greed / Fear meter ─────────────────────────────────────────
  // A 0–100 market-mood score tailored to NEPSE: it blends only the signals the
  // platform already computes (no CNN-style put/call, junk-bond or options
  // inputs, which NEPSE doesn't publish). Each component yields 0..1 and is
  // combined by weight, normalised over whichever components are available — so
  // dropping in a future signal (e.g. turnover-vs-average) is a one-line add.
  //
  // GREED_CONFIG is the single tuning surface; adjust weights / momentum band
  // here to change sensitivity without touching the render or component code.
  var GREED_CONFIG = {
    momentumBandPct: 3,            // NEPSE %-change mapped across ±this → 0..1
    weights: {
      breadth: 0.65,              // share of advancing scrips (dominant signal)
      momentum: 0.35              // NEPSE index daily % change
      // Future NEPSE inputs (need a small backend add) plug in here, e.g.:
      // priceStrength: 0.0,      // % of scrips near 52w highs vs lows
      // volatility: 0.0,         // inverted index ATR / daily range
      // safeHaven: 0.0,          // defensive sectors vs broad market
      // turnover: 0.0            // turnover vs recent average
    }
  };
  var GREED_BANDS = [
    { max: 24, label: "Extreme Fear", color: "#d83d44" },
    { max: 44, label: "Fear", color: "#e07f2a" },
    { max: 55, label: "Neutral", color: "#e0a82e" },
    { max: 75, label: "Greed", color: "#5cb83f" },
    { max: 100, label: "Extreme Greed", color: "#0fb383" }
  ];
  function greedBand(s) {
    for (var i = 0; i < GREED_BANDS.length; i++) if (s <= GREED_BANDS[i].max) return GREED_BANDS[i];
    return GREED_BANDS[GREED_BANDS.length - 1];
  }
  // Build the available component readings (each value 0..1) with their weights.
  function greedComponents(b, ov) {
    var adv = b.advancing || 0, dec = b.declining || 0, unch = b.unchanged || 0;
    var total = adv + dec + unch;
    if (!total) return null;
    var W = GREED_CONFIG.weights, comps = [];
    comps.push({ key: "breadth", label: "Market Breadth", weight: W.breadth,
                 value: total ? (adv + unch * 0.5) / total : 0.5 });
    var pct = ov && isNum(ov.nepse_pct) ? ov.nepse_pct : null;
    if (pct != null) {
      var band = GREED_CONFIG.momentumBandPct || 3;
      comps.push({ key: "momentum", label: "Market Momentum", weight: W.momentum,
                   value: Math.max(0, Math.min(1, (pct + band) / (2 * band))) });
    }
    return comps;
  }
  function computeGreed(b, ov) {
    var comps = greedComponents(b, ov);
    if (!comps || !comps.length) return null;
    var wsum = 0, acc = 0;
    comps.forEach(function (c) { wsum += c.weight; acc += c.weight * c.value; });
    if (wsum <= 0) return null;
    return Math.max(0, Math.min(100, Math.round(100 * acc / wsum)));
  }

  // Gauge geometry (math-angle: 180 = left/0-score, 0 = right/100-score).
  var GG = { cx: 140, cy: 132, R: 96, w: 22 };
  function gaugePt(deg, r) {
    var a = deg * Math.PI / 180;
    return [GG.cx + r * Math.cos(a), GG.cy - r * Math.sin(a)];
  }
  function gaugeArc(a0, a1, r) {
    var s = gaugePt(a0, r), e = gaugePt(a1, r);
    return "M" + s[0].toFixed(1) + " " + s[1].toFixed(1) +
      " A" + r + " " + r + " 0 0 1 " + e[0].toFixed(1) + " " + e[1].toFixed(1);
  }
  function scoreAngle(s) { return 180 - (s / 100) * 180; }
  function gaugeSVG(score, band, foreColor, muted) {
    var R = GG.R, w = GG.w, cx = GG.cx, cy = GG.cy;
    var rOut = R + w / 2 + 11, rTick = R - w / 2 - 11, rNeedle = R - w / 2 - 3;
    var s = "";
    for (var i = 0; i < 5; i++) {
      var seg = GREED_BANDS[i];
      var active = score <= seg.max && (i === 0 || score > GREED_BANDS[i - 1].max);
      var lo = i === 0 ? 0 : GREED_BANDS[i - 1].max;
      var hi = seg.max;
      s += '<path d="' + gaugeArc(scoreAngle(lo), scoreAngle(hi), R) +
        '" stroke="' + seg.color + '" stroke-width="' + w +
        '" fill="none" opacity="' + (active ? 1 : 0.3) + '"/>';
      // Labels use the same non-uniform thresholds as the scoring function, so
      // a needle can never visually land in a different band than its caption.
      var mid = scoreAngle((lo + hi) / 2), lp = gaugePt(mid, rOut), rot = (90 - mid).toFixed(1);
      s += '<text class="mi-greed-seg" x="' + lp[0].toFixed(1) + '" y="' + lp[1].toFixed(1) +
        '" text-anchor="middle" dominant-baseline="middle" transform="rotate(' + rot + ' ' +
        lp[0].toFixed(1) + ' ' + lp[1].toFixed(1) + ')" fill="' + (active ? seg.color : muted) +
        '" opacity="' + (active ? 1 : 0.75) + '">' + seg.label.toUpperCase() + '</text>';
    }
    // Scale ticks 0 / 25 / 50 / 75 / 100 just inside the arc.
    [0, 25, 50, 75, 100].forEach(function (tv) {
      var tp = gaugePt(scoreAngle(tv), rTick);
      s += '<text class="mi-greed-tick" x="' + tp[0].toFixed(1) + '" y="' + tp[1].toFixed(1) +
        '" text-anchor="middle" dominant-baseline="middle" fill="' + muted + '">' + tv + '</text>';
    });
    // Needle + hub.
    var tip = gaugePt(scoreAngle(score), rNeedle);
    s += '<line x1="' + cx + '" y1="' + cy + '" x2="' + tip[0].toFixed(1) + '" y2="' + tip[1].toFixed(1) +
      '" stroke="' + foreColor + '" stroke-width="3.5" stroke-linecap="round"/>' +
      '<circle cx="' + cx + '" cy="' + cy + '" r="7" fill="' + foreColor + '"/>';
    // Score + current band name below the dial.
    s += '<text class="mi-greed-num" x="' + cx + '" y="' + (cy + 30) + '" text-anchor="middle" fill="' + foreColor + '">' + score + '</text>' +
      '<text class="mi-greed-band" x="' + cx + '" y="' + (cy + 48) + '" text-anchor="middle" fill="' + band.color + '">' + band.label + '</text>';
    return '<svg viewBox="0 0 ' + (cx * 2) + ' ' + (cy + 56) + '" role="img" aria-label="NEPSE greed gauge ' +
      score + ' of 100, ' + band.label + '">' + s + '</svg>';
  }
  // Past gauge readings (previous close / 1w / 1m / 1y ago). Each backend entry
  // carries raw breadth + index momentum; we re-run computeGreed() here so the
  // historical numbers always match the live gauge's formula exactly.
  function renderGreedHistory(history) {
    var hist = el("greed-history");
    if (!hist) return;
    var rows = (history || []).map(function (h) {
      var hs = computeGreed(h.breadth || {}, { nepse_pct: h.nepse_pct });
      if (hs == null) return "";
      var hb = greedBand(hs);
      return '<div class="mi-greed-hist-row" title="' + h.label + ' · ' + (h.date || "") + '">' +
        '<span class="mi-greed-hist-label">' + h.label + '</span>' +
        '<span class="mi-greed-hist-score"><b style="color:' + hb.color + '">' + hs + '</b>' +
        '<span class="mi-greed-hist-band">' + hb.label + '</span></span>' +
        '</div>';
    }).join("");
    hist.innerHTML = rows;
  }
  function renderGreedMeter(b, ov, live, source, history) {
    var node = el("greed-meter");
    if (!node) return;
    var score = computeGreed(b, ov);
    var detail = el("greed-components");
    if (score == null) {
      node.innerHTML = '<div class="mi-greed-empty">No data</div>';
      if (detail) detail.innerHTML = "";
      renderGreedHistory([]);
      return;
    }
    renderGreedHistory(history);
    var band = greedBand(score), t = baseChartOpts(), muted = cssVar("--ink-3");
    node.innerHTML = gaugeSVG(score, band, t.foreColor, muted);
    var components = greedComponents(b, ov);
    var parts = components.map(function (c) {
      return c.label + " " + Math.round(c.value * 100) + " (" + Math.round(c.weight * 100) + "%)";
    }).join(" · ");
    if (detail) {
      detail.innerHTML = components.map(function (c) {
        var label = c.key === "breadth" ? "Breadth" : "Momentum";
        return '<span class="mi-greed-component"><b>' + Math.round(c.value * 100) + '</b> ' + label + '</span>';
      }).join("");
    }
    node.setAttribute("title", "NEPSE mood " + score + "/100 · " + band.label +
      (live ? " (live)" : source === "eod" ? " (end of day)" : "") + " — " + parts);
  }

  var SECTOR_COLORS = [
    "#12d39a", "#5cb3ff", "#ffc166", "#ff6e72", "#a78bfa", "#34d399", "#f472b6",
    "#60a5fa", "#fbbf24", "#fb7185", "#2dd4bf", "#c084fc", "#4ade80", "#f59e0b"
  ];

  // Sector turnover share — where the money flowed today (complements the
  // Sector Performance list, which shows daily % change).
  function renderSectorChart(sectors) {
    if (sectors) state.sector.today = sectors;
    // Anything but "Daily" is painted from the windowed feed once it lands.
    var windowed = state.sector.period !== "1D" ? state.sector.data : null;
    var source = windowed ? windowed.sectors : state.sector.today;
    updateSectorHint(windowed);

    var t = baseChartOpts();
    var rows = (source || []).filter(function (s) { return isNum(s.turnover) && s.turnover > 0; });
    rows.sort(function (a, b) { return b.turnover - a.turnover; });
    var labels = rows.map(function (s) { return s.sector; });
    var data = rows.map(function (s) { return Math.round(s.turnover); });
    var total = data.reduce(function (sum, v) { return sum + v; }, 0);

    var opts = {
      chart: {
        type: "bar", height: "100%", toolbar: { show: false }, fontFamily: "Manrope, sans-serif",
        events: {
          mounted: shrinkSectorAmountLabels,
          updated: shrinkSectorAmountLabels,
          animationEnd: shrinkSectorAmountLabels
        }
      },
      series: [{ name: "Turnover", data: data }],
      colors: SECTOR_COLORS.slice(0, Math.max(data.length, 1)),
      legend: { show: false },
      plotOptions: {
        bar: {
          horizontal: false,
          distributed: true,
          borderRadius: 4,
          borderRadiusApplication: "end",
          columnWidth: "58%",
          dataLabels: { position: "top" }
        }
      },
      dataLabels: {
        enabled: true,
        // An array makes Apex render one tspan per line: the raw turnover on
        // top (shrunk by shrinkSectorAmountLabels once painted), share below.
        formatter: function (v) {
          return [fmtCompact(v), total ? (v / total * 100).toFixed(1) + "%" : ""];
        },
        offsetY: -22,
        style: { fontSize: "11px", fontWeight: 700, colors: [t.foreColor] },
        background: { enabled: false },
        dropShadow: { enabled: false }
      },
      xaxis: {
        categories: labels,
        labels: {
          rotate: -45,
          rotateAlways: true,
          trim: true,
          hideOverlappingLabels: false,
          maxHeight: 92,
          style: { colors: t.foreColor, fontSize: "11px" }
        },
        axisBorder: { color: t.grid },
        axisTicks: { color: t.grid }
      },
      yaxis: {
        labels: {
          formatter: function (v) { return fmtCompact(Number(v)); },
          style: { colors: t.foreColor, fontSize: "11px", fontWeight: 600 }
        }
      },
      grid: { borderColor: t.grid, strokeDashArray: 3, xaxis: { lines: { show: false } }, yaxis: { lines: { show: true } } },
      tooltip: {
        theme: themeName(),
        custom: function (op) {
          var r = rows[op.dataPointIndex] || {};
          var html = '<div style="padding:6px 10px;font-family:Manrope">' +
            "<strong>" + escapeHtml(r.sector || "") + "</strong><br/>" +
            "Turnover: " + fmtMoney(r.turnover) + "<br/>" +
            "Share: " + (total ? (r.turnover / total * 100).toFixed(1) + "%" : "—");
          if (isNum(r.avg_per_session)) html += "<br/>Avg / session: " + fmtMoney(r.avg_per_session);
          if (isNum(r.change_pct)) html += "<br/>vs prev period: " + fmtPct(r.change_pct);
          return html + "</div>";
        }
      },
      noData: { text: "No sector turnover available", style: { color: t.foreColor } }
    };
    mountChart("chart-sectors", "sectors", opts);
  }

  // Apex applies one style to a whole data label, so the second line (the
  // turnover amount) is shrunk here after paint. Purely cosmetic and fully
  // guarded — if Apex ever changes its label markup the labels just stay
  // uniform rather than throwing mid-render.
  function shrinkSectorAmountLabels() {
    var host = el("chart-sectors");
    if (!host) return;
    try {
      var labels = host.querySelectorAll(".apexcharts-datalabels text");
      Array.prototype.forEach.call(labels, function (text) {
        var lines = text.querySelectorAll("tspan");
        if (lines.length < 2) return;
        var amount = lines[0];
        amount.style.fontSize = "9px";
        amount.style.fontWeight = "600";
        amount.style.opacity = "0.72";
      });
    } catch (e) { /* cosmetic only */ }
  }

  // Card subtitle doubles as the window read-out: which dates the columns cover.
  function updateSectorHint(windowed) {
    var node = el("sector-turnover-hint");
    if (!node) return;
    if (!windowed) { node.textContent = "column = turnover share · today"; return; }
    node.textContent = "column = turnover share · " + windowed.from + " → " + windowed.to +
      " (" + windowed.sessions + " session" + (windowed.sessions === 1 ? "" : "s") + ")";
  }

  function fetchSectorTurnover(period, from, to) {
    state.sector.period = period;
    if (period === "1D") { state.sector.data = null; renderSectorChart(null); return; }
    if (state.sector.inFlight) return;
    state.sector.inFlight = true;
    var url = SECTOR_URL + "?period=" + encodeURIComponent(period);
    if (period === "CUSTOM") {
      url += "&from=" + encodeURIComponent(from || "") + "&to=" + encodeURIComponent(to || "");
    }
    fetch(url, { headers: { "X-Requested-With": "XMLHttpRequest" }, credentials: "same-origin" })
      .then(function (r) { if (!r.ok) throw new Error("HTTP " + r.status); return r.json(); })
      .then(function (d) {
        if (d.ok === false) throw new Error(d.error || "Service error");
        state.sector.data = d;
        renderSectorChart(null);
      })
      .catch(function (err) {
        if (window.console) console.warn("Sector turnover fetch failed:", err.message);
      })
      .finally(function () { state.sector.inFlight = false; });
  }

  function initSectorRange() {
    var box = el("sector-range");
    if (!box) return;
    var customBox = el("sector-range-custom");
    var btns = box.querySelectorAll(".mi-range-btn");
    var saved;
    try { saved = localStorage.getItem(LS_SECTOR_PERIOD); } catch (e) {}
    // A saved CUSTOM range isn't restorable without its dates — fall back to Daily.
    if (saved && saved !== "CUSTOM") state.sector.period = saved;

    function paintActive() {
      btns.forEach(function (b) {
        b.classList.toggle("is-active", b.getAttribute("data-period") === state.sector.period);
      });
      if (customBox) customBox.hidden = state.sector.period !== "CUSTOM";
    }

    btns.forEach(function (b) {
      b.addEventListener("click", function () {
        var period = b.getAttribute("data-period");
        state.sector.period = period;
        paintActive();
        try { localStorage.setItem(LS_SECTOR_PERIOD, period); } catch (e) {}
        // CUSTOM waits for Apply — there are no dates to query yet.
        if (period !== "CUSTOM") fetchSectorTurnover(period);
      });
    });

    var apply = el("sector-apply");
    if (apply) {
      apply.addEventListener("click", function () {
        var from = (el("sector-from") || {}).value;
        var to = (el("sector-to") || {}).value;
        if (!from || !to) return;
        fetchSectorTurnover("CUSTOM", from, to);
      });
    }

    paintActive();
    if (state.sector.period !== "1D") fetchSectorTurnover(state.sector.period);
  }

  function pctColor(pct) {
    var t = baseChartOpts();
    if (!isNum(pct) || pct === 0) return t.flat;
    if (pct <= -3) return "#c0392b";
    if (pct <= -1) return t.down;
    if (pct < 0) return "#e98a8c";
    if (pct < 1) return "#7fd8b4";
    if (pct < 3) return t.up;
    return "#0f9e6e";
  }

  function renderHeatmap() {
    // Tile SIZE = turnover (liquidity), tile COLOUR = daily change % via a
    // per-point fillColor. Grouped by sector so related scrips sit together.
    // Honours the sector filter dropdown (state.heatmapSector).
    // The heatmap is always end-of-day (settled close-vs-prev-close change %),
    // so it carries its own "As of <date>" label rather than the live badge.
    var asofEl = el("heatmap-asof");
    if (asofEl) {
      var hAsOf = state.data && state.data.heatmap_as_of;
      asofEl.textContent = hAsOf ? "EOD · as of " + hAsOf : "";
    }
    var tiles = filteredHeatmap();
    var bySector = {};
    tiles.forEach(function (tile) {
      var key = tile.sector || "Other";
      (bySector[key] = bySector[key] || []).push({
        x: tile.symbol,
        y: Math.round(tile.turnover || 0),
        fillColor: pctColor(tile.pct),
        pct: tile.pct,
        change: tile.change,
        ltp: tile.ltp
      });
    });
    var series = Object.keys(bySector).map(function (k) { return { name: k, data: bySector[k] }; });

    // Dense sectors (e.g. Hydro Power's ~110 scrips) shrink each tile until a
    // multi-line label can't fit. Rather than hide the point/% everywhere, decide
    // PER TILE from its actual pixel area: a big, liquid tile shows symbol + point
    // + %, a tiny one shows just the symbol (full detail is always on hover). The
    // +/- control grows the board so more tiles clear the area threshold.
    var zoom = clampZoom(state.heatmapZoom);
    var n = tiles.length;
    var height = Math.round(clamp((360 + n * 6) * zoom, 400, 1600));
    var fontPx = Math.round(clamp(n > 90 ? 9 : n > 50 ? 10 : 11, 8, 14));
    // Tile area (share of turnover × board pixels) needed to fit the second line.
    var totalTurnover = tiles.reduce(function (a, t) { return a + Math.max(0, t.turnover || 0); }, 0);
    var boardW = (el("chart-heatmap") || {}).clientWidth || 1000;
    var boardArea = boardW * height;
    var detailMinArea = fontPx * fontPx * 22;  // ~room for two lines at this font

    var opts = {
      chart: { type: "treemap", height: height, toolbar: { show: false }, fontFamily: "Manrope, sans-serif" },
      series: series.length ? series : [{ name: "Market", data: [] }],
      legend: { show: false },
      dataLabels: {
        enabled: true,
        style: { fontSize: fontPx + "px", fontFamily: "Manrope, sans-serif", colors: ["#ffffff"] },
        formatter: function (text, op) {
          var d = op.w.config.series[op.seriesIndex].data[op.dataPointIndex] || {};
          if (!isNum(d.change) && !isNum(d.pct)) return text;
          var tileArea = totalTurnover > 0 ? (d.y / totalTurnover) * boardArea : 0;
          if (tileArea < detailMinArea) return text;  // too small — symbol only
          var chg = isNum(d.change) ? fmtSigned(d.change) : "";
          var pct = isNum(d.pct) ? fmtPct(d.pct) : "";
          return [text, chg && pct ? chg + " (" + pct + ")" : chg || pct];
        },
        offsetY: -4
      },
      plotOptions: {
        treemap: { distributed: true, enableShades: false, useFillColorAsStroke: false }
      },
      tooltip: {
        theme: themeName(),
        custom: function (op) {
          var pt = op.ctx.w.config.series[op.seriesIndex].data[op.dataPointIndex] || {};
          return '<div style="padding:6px 10px;font-family:Manrope">' +
            "<strong>" + escapeHtml(pt.x) + "</strong><br/>" +
            "LTP: " + fmtNum(pt.ltp, 2) + "<br/>" +
            "Change: " + fmtSigned(pt.change) + " (" + fmtPct(pt.pct) + ")<br/>" +
            "Turnover: " + fmtMoney(pt.y) + "</div>";
        }
      }
    };
    mountChart("chart-heatmap", "heatmap", opts);
  }

  // ── Sub-index comparison chart ─────────────────────────────────────────
  function fetchCompare(days) {
    if (state.compare.inFlight) return;
    state.compare.inFlight = true;
    state.compare.days = days;
    var url = COMPARE_URL + "?days=" + encodeURIComponent(days);
    fetch(url, { headers: { "X-Requested-With": "XMLHttpRequest" }, credentials: "same-origin" })
      .then(function (r) { if (!r.ok) throw new Error("HTTP " + r.status); return r.json(); })
      .then(function (d) {
        if (d.ok === false) throw new Error(d.error || "Service error");
        state.compare.data = d;
        renderSubindexCompare();
      })
      .catch(function (err) {
        if (window.console) console.warn("Sub-index comparison fetch failed:", err.message);
      })
      .finally(function () { state.compare.inFlight = false; });
  }

  function renderSubindexCompare() {
    var d = state.compare.data;
    if (!d || !d.series) return;
    var t = baseChartOpts();
    // Normalise each line to % change from its first point so all 17 indices,
    // whatever their absolute level, share a single 0%-baseline scale.
    var series = d.series.map(function (s) {
      var pts = s.points || [];
      var base = pts.length ? pts[0][1] : 0;
      var data = pts.map(function (p) {
        var pct = base ? (p[1] / base - 1) * 100 : 0;
        return { x: new Date(p[0] + "T00:00:00").getTime(), y: Math.round(pct * 100) / 100 };
      });
      return { name: s.label, data: data };
    });

    var opts = {
      chart: {
        type: "line", height: 460, fontFamily: "Manrope, sans-serif",
        toolbar: { show: true, tools: { download: false, selection: true, zoom: true, zoomin: true, zoomout: true, pan: true, reset: true } },
        animations: { enabled: false }
      },
      series: series,
      colors: COMPARE_COLORS,
      // NEPSE (series 0) drawn thicker so the headline stands out from sectors.
      stroke: { curve: "straight", width: series.map(function (_s, i) { return i === 0 ? 3 : 1.5; }) },
      legend: { show: true, position: "bottom", labels: { colors: t.foreColor }, fontSize: "12px", itemMargin: { horizontal: 8, vertical: 2 } },
      xaxis: { type: "datetime", labels: { style: { colors: t.foreColor } }, axisBorder: { color: t.gridColor }, axisTicks: { color: t.gridColor } },
      yaxis: { labels: { style: { colors: t.foreColor }, formatter: function (v) { return (v > 0 ? "+" : "") + v.toFixed(0) + "%"; } } },
      grid: { borderColor: t.gridColor, strokeDashArray: 3 },
      dataLabels: { enabled: false },
      tooltip: { theme: themeName(), shared: true, x: { format: "dd MMM yyyy" }, y: { formatter: function (v) { return fmtPct(v); } } },
      markers: { size: 0, hover: { size: 4 } },
      noData: { text: "No sub-index data available", style: { color: t.foreColor } }
    };
    mountChart("chart-subindex-compare", "compare", opts);
  }

  function themeName() {
    return document.documentElement.getAttribute("data-theme") === "light" ? "light" : "dark";
  }

  function mountChart(elemId, key, opts) {
    if (state.charts[key]) {
      state.charts[key].updateOptions(opts, true, true);
      return;
    }
    var node = el(elemId);
    if (!node || typeof ApexCharts === "undefined") return;
    state.charts[key] = new ApexCharts(node, opts);
    state.charts[key].render();
  }

  function renderContributors(d) {
    var c = d.contributors || {};
    // `pending` means the feed is fetched off-thread and has not landed yet
    // (normal for the first build after a restart) — as opposed to the feed
    // genuinely returning nothing. The two must not read the same on screen.
    var contribPending = !!c.pending;
    renderContribList("contrib-positive", c.positive, "up", contribPending);
    renderContribList("contrib-negative", c.negative, "down", contribPending);
    renderSectorMoverList("sector-positive", (c.sectors || {}).positive, "up", contribPending);
    renderSectorMoverList("sector-negative", (c.sectors || {}).negative, "down", contribPending);
    scheduleContribRecheck(contribPending);
  }

  // While contributors are pending, fetch again shortly so the panels fill in
  // on their own. Bounded to a few attempts: this exists to cover the ~7s
  // upstream fetch after a restart, not to poll forever if the feed is dead.
  var contribRechecks = 0;
  var contribTimer = null;
  function scheduleContribRecheck(pending) {
    if (!pending) { contribRechecks = 0; return; }
    if (contribTimer || contribRechecks >= 4) return;
    contribRechecks += 1;
    contribTimer = setTimeout(function () {
      contribTimer = null;
      // refresh() is the module's payload fetch; it self-guards on
      // state.inFlight, so this can never stack onto an in-progress poll.
      refresh(false);
    }, 4000);
  }

  function renderContribList(id, rows, cls, pending) {
    var box = el(id);
    if (!box) return;
    if (!rows || !rows.length) {
      box.innerHTML = pending
        ? '<div class="mi-empty mi-loading">Loading contributors…</div>'
        : '<div class="mi-empty">No data available</div>';
      return;
    }
    var maxAbs = rows.reduce(function (m, r) {
      return Math.max(m, Math.abs(isNum(r.points) ? r.points : 0));
    }, 0.01);
    box.innerHTML = rows.map(function (r) {
      var pts = isNum(r.points) ? r.points : 0;
      var width = Math.min(100, Math.abs(pts) / maxAbs * 100);
      return '<div class="mi-contrib-row">' +
        '<span class="mi-contrib-sym">' + escapeHtml(r.symbol) + "</span>" +
        '<span class="mi-contrib-track"><i class="' + cls + '" style="width:' + width + '%"></i></span>' +
        '<span class="mi-contrib-pts ' + cls + '">' + (pts > 0 ? "+" : "") + pts.toFixed(2) + "</span>" +
        '<span class="mi-contrib-pct ' + cls + '">' + fmtPct(r.pct) + "</span>" +
        "</div>";
    }).join("");
  }

  // ── Render everything ──────────────────────────────────────────────────
  function renderSectorMoverList(id, rows, cls, pending) {
    var box = el(id);
    if (!box) return;
    if (!rows || !rows.length) {
      box.innerHTML = pending
        ? '<div class="mi-empty mi-loading">Loading sector movers…</div>'
        : '<div class="mi-empty">No sector data available</div>';
      return;
    }
    box.innerHTML = rows.map(function (r) {
      var points = isNum(r.points) ? r.points : 0;
      var label = r.sector || r.name || "";
      return '<div class="mi-sector-mover-row ' + cls + '">' +
        '<span class="mi-sector-mover-name" title="' + escapeHtml(label) + '">' + escapeHtml(label) + "</span>" +
        '<span class="mi-sector-mover-pts">' + (points > 0 ? "+" : "") + points.toFixed(2) + "</span>" +
        "</div>";
    }).join("");
  }

  function renderAll(d) {
    state.data = d;
    // Stock-level widgets sourced from the per-scrip live feed (heatmap, breadth)
    // are a prior session's data when the feed lags. Flag the page so those are
    // visibly marked delayed rather than read as current.
    document.body.classList.toggle("mi-feed-delayed", d.live === false && !!d.has_data);
    renderOverview(d);
    renderRankedTable("tbl-gainers", d.gainers, "ranked");
    renderRankedTable("tbl-losers", d.losers, "ranked");
    renderRankedTable("tbl-active", d.most_active, "active");
    renderContributors(d);
    renderCharts(d);
  }

  // ── Polling ────────────────────────────────────────────────────────────
  function refresh(manual, options) {
    options = options || {};
    if (state.inFlight) return;
    state.inFlight = true;
    var btn = el("mi-refresh-btn");
    if (manual && btn) btn.classList.add("is-spinning");

    // A manual refresh forces a fresh server build (bypasses the short payload cache).
    var params = [];
    if (manual) params.push("force=1");
    if (options.fast && !manual) params.push("fast=1");
    var url = CONFIG.apiUrl + (params.length ? (CONFIG.apiUrl.indexOf("?") >= 0 ? "&" : "?") + params.join("&") : "");
    // Abort a hung request so it can't pin inFlight=true and stall auto-refresh.
    var ctrl = (typeof AbortController !== "undefined") ? new AbortController() : null;
    var timeoutId = ctrl ? setTimeout(function () { ctrl.abort(); }, 12000) : null;
    fetch(url, {
      headers: { "X-Requested-With": "XMLHttpRequest" },
      credentials: "same-origin",
      signal: ctrl ? ctrl.signal : undefined
    })
      .then(function (r) {
        if (!r.ok) throw new Error("HTTP " + r.status);
        return r.json();
      })
      .then(function (d) {
        if (d.ok === false) throw new Error(d.error || "Service error");
        var banner = el("mi-load-banner");
        if (banner && d.has_data) banner.style.display = "none";
        renderAll(d);
        setPayloadStatus(d);
        state.lastFetchMs = Date.now();
        stamp();
        if (options.fast && !manual && !d.live) {
          deferNonCritical(function () { refresh(false); }, 2500);
        }
      })
      .catch(function (err) {
        setStatus("stale", "Stale — retrying");
        // Keep last good data on screen; just flag the staleness.
        if (window.console) console.warn("Market Insights refresh failed:", err.message);
      })
      .finally(function () {
        if (timeoutId) clearTimeout(timeoutId);
        state.inFlight = false;
        if (btn) btn.classList.remove("is-spinning");
      });
  }

  function scheduleNext() {
    if (state.timer) { clearTimeout(state.timer); state.timer = null; }
    if (state.intervalSec > 0) {
      state.timer = setTimeout(function () {
        if (!document.hidden) refresh(false);
        scheduleNext();
      }, state.intervalSec * 1000);
    }
  }

  function initControls() {
    var sel = el("mi-refresh-select");
    var saved;
    try { saved = localStorage.getItem(LS_INTERVAL); } catch (e) {}
    if (saved !== null && saved !== undefined) state.intervalSec = parseInt(saved, 10) || 0;
    if (sel) sel.value = String(state.intervalSec);
    if (sel) sel.addEventListener("change", function () {
      state.intervalSec = parseInt(sel.value, 10) || 0;
      try { localStorage.setItem(LS_INTERVAL, String(state.intervalSec)); } catch (e) {}
      scheduleNext();
    });

    var refreshBtn = el("mi-refresh-btn");
    if (refreshBtn) refreshBtn.addEventListener("click", function () { refresh(true); });

    // Heatmap sector filter (restore last choice, re-render on change).
    var hsel = el("heatmap-sector");
    if (hsel) {
      var savedSector;
      try { savedSector = localStorage.getItem(LS_HEATMAP_SECTOR); } catch (e) {}
      if (savedSector) state.heatmapSector = savedSector;
      hsel.addEventListener("change", function () {
        state.heatmapSector = hsel.value || "ALL";
        try { localStorage.setItem(LS_HEATMAP_SECTOR, state.heatmapSector); } catch (e) {}
        renderHeatmap();
      });
    }

    // Heatmap tile-size control: enlarge/shrink tiles + labels so dense sectors
    // stay legible. Restore the last choice and re-render on each step.
    var savedZoom;
    try { savedZoom = parseFloat(localStorage.getItem(LS_HEATMAP_ZOOM)); } catch (e) {}
    if (isNum(savedZoom)) state.heatmapZoom = clampZoom(savedZoom);
    function stepHeatmapZoom(delta) {
      state.heatmapZoom = clampZoom(clampZoom(state.heatmapZoom) + delta);
      try { localStorage.setItem(LS_HEATMAP_ZOOM, String(state.heatmapZoom)); } catch (e) {}
      renderHeatmap();
    }
    var zoomIn = el("heatmap-zoom-in");
    var zoomOut = el("heatmap-zoom-out");
    if (zoomIn) zoomIn.addEventListener("click", function () { stepHeatmapZoom(HEATMAP_ZOOM_STEP); });
    if (zoomOut) zoomOut.addEventListener("click", function () { stepHeatmapZoom(-HEATMAP_ZOOM_STEP); });

    // Sub-index comparison range buttons (restore last choice, re-fetch on click).
    var rangeBox = el("compare-range");
    if (rangeBox) {
      var savedDays;
      try { savedDays = parseInt(localStorage.getItem(LS_COMPARE_DAYS), 10); } catch (e) {}
      if (savedDays) state.compare.days = savedDays;
      var btns = rangeBox.querySelectorAll(".mi-range-btn");
      btns.forEach(function (b) {
        b.classList.toggle("is-active", parseInt(b.getAttribute("data-days"), 10) === state.compare.days);
        b.addEventListener("click", function () {
          var days = parseInt(b.getAttribute("data-days"), 10) || COMPARE_DEFAULT_DAYS;
          btns.forEach(function (x) { x.classList.remove("is-active"); });
          b.classList.add("is-active");
          try { localStorage.setItem(LS_COMPARE_DAYS, String(days)); } catch (e) {}
          fetchCompare(days);
        });
      });
    }

    // Sector turnover period buttons (Daily … Yearly + custom date range).
    initSectorRange();

    // Resume promptly when the tab regains focus. This must ALSO fire in
    // Manual mode: with Manual now the default, a tab left open across the
    // market close otherwise freezes at its load-time snapshot forever — which
    // reads as "the heatmap stopped working" when it is simply never asked
    // again. Manual still means no polling; a stale tab regaining focus gets
    // exactly one catch-up request, and only when the data is >10 min old.
    var STALE_TAB_MS = 10 * 60 * 1000;
    document.addEventListener("visibilitychange", function () {
      if (document.hidden) return;
      if (state.intervalSec > 0) { refresh(false); return; }
      var age = Date.now() - (state.lastFetchMs || 0);
      if (age > STALE_TAB_MS) refresh(false);
    });
  }

  // ── Boot ───────────────────────────────────────────────────────────────
  function init() {
    initTheme();
    initControls();

    var bootstrap = el("mi-bootstrap");
    var initial = null;
    if (bootstrap) {
      try { initial = JSON.parse(bootstrap.textContent); } catch (e) {}
    }
    if (initial && initial.has_data) {
      // Warm cache: server embedded a ready payload — paint it, no fetch needed.
      renderAll(initial);
      setPayloadStatus(initial);
      stamp();
    } else {
      // Cold cache: the server shipped the shell instantly without touching the
      // external feeds. Paint the empty structure, then fetch the live payload
      // on demand so the page is interactive immediately and data fills in.
      if (initial) renderAll(initial);
      setStatus("stale", "Loading…");
      refresh(false, { fast: true });
    }
    // The comparison chart loads its own historical series, independent of the
    // dashboard payload, so kick it off once here.
    deferNonCritical(function () { fetchCompare(state.compare.days); }, 1600);
    scheduleNext();
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", init);
  } else {
    init();
  }
})();

/* morningstar.js — Morning Star tab on the Fundamental Analysis desk.
 *
 * Renders the sector scan from /fundamentals/morningstar/ (services/
 * morningstar.py): star ratings, Growth/Value percentiles, style box, size
 * tier, confidence tag, and quality gates/flags. Clicking a row expands the
 * per-factor breakdown (raw value + within-sector percentile + weight) so
 * every score is auditable against the methodology.
 *
 * Namespaced ms-* / MS_CONFIG — must never collide with fundamentals-desk.js
 * (ia-*) or canslim.js (cs-*) which share this page.
 */
(function () {
  "use strict";

  var CFG = window.MS_CONFIG || { scanUrl: "/fundamentals/morningstar/" };
  var state = { loaded: false, data: null };

  function el(id) { return document.getElementById(id); }
  function esc(s) {
    return String(s == null ? "" : s).replace(/[&<>"']/g, function (c) {
      return { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c];
    });
  }

  function stars(n) {
    if (n == null) return "<span class='ms-dim'>—</span>";
    var out = "";
    for (var i = 1; i <= 5; i++) {
      out += "<span class='" + (i <= n ? "ms-star on" : "ms-star") + "'>★</span>";
    }
    return "<span class='ms-stars' title='" + n + " of 5'>" + out + "</span>";
  }

  function pct(v) {
    if (v == null) return "<span class='ms-dim'>—</span>";
    var cls = v >= 60 ? "num-pos" : v < 40 ? "num-neg" : "";
    return "<span class='" + cls + "'>" + v.toFixed(1) + "</span>";
  }

  function styleChip(s) {
    if (!s || s === "—") return "<span class='ms-dim'>—</span>";
    return "<span class='ms-chip ms-style-" + s.toLowerCase() + "'>" + esc(s) + "</span>";
  }

  /* Growth/Value quadrant, split at the sector median. A gated company is held
     out of both High halves, so this reads "Low / Low" for anything whose score
     the quality screen says cannot be trusted. */
  function quadChip(q) {
    if (!q || q === "—") return "<span class='ms-dim'>—</span>";
    var hg = q.indexOf("High Growth") === 0;
    var hv = q.indexOf("High Value") > 0;
    var cls = hg && hv ? "ms-quad-hh" : hg ? "ms-quad-hl" : hv ? "ms-quad-lh" : "ms-quad-ll";
    var short = (hg ? "HG" : "LG") + " / " + (hv ? "HV" : "LV");
    return "<span class='ms-chip " + cls + "' title='" + esc(q) + "'>" + short + "</span>";
  }

  function fmtRaw(v, asPct) {
    if (v == null) return "—";
    if (typeof v !== "number") return esc(v);
    // Growth-pillar raws are YoY fractions — show as %. Value-pillar raws are
    // the metric itself (a P/B of 0.85 must NOT render as 85%).
    if (asPct) return (v * 100).toFixed(2) + "%";
    if (Math.abs(v) >= 1e9) return (v / 1e9).toFixed(2) + "B";
    if (Math.abs(v) >= 1e6) return (v / 1e6).toFixed(2) + "M";
    return v.toLocaleString(undefined, { maximumFractionDigits: Math.abs(v) < 10 ? 3 : 2 });
  }

  function factorRows(dd) {
    var html = "";
    ["growth", "value"].forEach(function (pillar) {
      var d = dd[pillar];
      if (!d) return;
      html += "<div class='ms-detail-block'><div class='ms-detail-head'>" +
        (pillar === "growth" ? "Growth factors" : "Value factors") +
        " <span class='ms-dim'>(" + d.present + " of " + d.total + " present)</span></div>";
      html += "<table class='ms-detail-table'><thead><tr><th>Factor</th><th>Weight</th><th>Raw</th><th>Sector percentile</th></tr></thead><tbody>";
      (d.factors || []).forEach(function (f) {
        html += "<tr><td>" + esc(f.label) + "</td><td>" + f.weight + "%</td><td>" +
          fmtRaw(f.raw, pillar === "growth") + "</td><td>" + (f.pct == null ? "<span class='ms-dim'>missing — weight redistributed</span>" : f.pct.toFixed(1)) + "</td></tr>";
      });
      html += "</tbody></table></div>";
    });
    return html;
  }

  function drawBarometer(data) {
    var host = el("ms-barometer");
    if (!host) return;
    var b = data.barometer;
    if (!b) { host.innerHTML = ""; host.hidden = true; return; }
    host.hidden = false;
    var horizon = state.horizon || "1d";
    var grid = b[horizon] || {};
    var styles = ["Value", "Blend", "Growth"], sizes = ["Large", "Mid", "Small"];

    function cell(c) {
      if (!c || c.avg == null) return "<div class='ms-baro-cell ms-baro-empty'>—</div>";
      var v = c.avg;
      var cls = v > 0 ? "pos" : v < 0 ? "neg" : "flat";
      var mag = Math.min(Math.abs(v) / (horizon === "1y" ? 40 : horizon === "1w" ? 5 : 2.5), 1);
      return "<div class='ms-baro-cell " + cls + "' style='--mag:" + mag.toFixed(2) + "' title='" +
        c.n + " scrips, simple average'>" + (v > 0 ? "+" : "") + v.toFixed(2) + "</div>";
    }

    var html = "<div class='ms-baro-head'><span class='ms-baro-title'>Sector barometer</span>" +
      "<span class='ms-baro-toggles'>" +
      ["1d", "1w", "1y"].map(function (h) {
        return "<button type='button' class='ms-baro-btn" + (h === horizon ? " active" : "") +
          "' data-h='" + h + "'>" + h.toUpperCase() + "</button>";
      }).join("") + "</span></div><div class='ms-baro-grid'>";
    sizes.forEach(function (sz) {
      styles.forEach(function (st) {
        html += cell((grid[sz] || {})[st]);
      });
      html += "<div class='ms-baro-label'>" + sz + "</div>";
    });
    styles.forEach(function (st) { html += "<div class='ms-baro-label'>" + st + "</div>"; });
    html += "<div></div></div>";
    host.innerHTML = html;
    host.querySelectorAll(".ms-baro-btn").forEach(function (btn) {
      btn.addEventListener("click", function () {
        state.horizon = btn.getAttribute("data-h");
        drawBarometer(state.data);
      });
    });
  }

  function scatterSvg(pts, compact, split) {
    /* The divider is the SECTOR MEDIAN, not a hard-coded 50. The caption always
       claimed "above-median on both", but the line was drawn at 50, so a company
       could sit in the green box and still be Low/Low in the table. Quality-gated
       companies are filtered out before they reach here (see drawScatter), so the
       chart and the Quadrant column can never disagree again. */
    var gMid = (split && split.growth != null) ? split.growth : 50;
    var vMid = (split && split.value != null) ? split.value : 50;
    var W = compact ? 540 : 1360, H = compact ? 450 : 560;
    var m = compact ? { l: 40, r: 14, t: 14, b: 36 } : { l: 56, r: 28, t: 20, b: 44 };
    var iw = W - m.l - m.r, ih = H - m.t - m.b;
    function X(v) { return m.l + (v / 100) * iw; }
    function Y(v) { return m.t + (1 - v / 100) * ih; }

    var svg = "<svg viewBox='0 0 " + W + " " + H + "' class='ms-scatter-svg' role='img' " +
      "aria-label='Growth versus Value scatter'>";
    svg += "<rect x='" + X(gMid) + "' y='" + m.t + "' width='" + (m.l + iw - X(gMid)) +
      "' height='" + (Y(vMid) - m.t) + "' class='ms-sc-quad best'/>";
    [0, 20, 40, 60, 80, 100].forEach(function (v) {
      svg += "<line x1='" + X(v) + "' y1='" + m.t + "' x2='" + X(v) + "' y2='" + (m.t + ih) + "' class='ms-sc-grid'/>";
      svg += "<line x1='" + m.l + "' y1='" + Y(v) + "' x2='" + (m.l + iw) + "' y2='" + Y(v) + "' class='ms-sc-grid'/>";
      svg += "<text x='" + X(v) + "' y='" + (m.t + ih + 15) + "' class='ms-sc-tick' text-anchor='middle'>" + v + "</text>";
      svg += "<text x='" + (m.l - 6) + "' y='" + (Y(v) + 4) + "' class='ms-sc-tick' text-anchor='end'>" + v + "</text>";
    });
    svg += "<line x1='" + X(gMid) + "' y1='" + m.t + "' x2='" + X(gMid) + "' y2='" + (m.t + ih) + "' class='ms-sc-mid'/>";
    svg += "<line x1='" + m.l + "' y1='" + Y(vMid) + "' x2='" + (m.l + iw) + "' y2='" + Y(vMid) + "' class='ms-sc-mid'/>";
    // Quadrant labels: I = growth + cheap (sweet spot), II = cheap but slow,
    // III = slow AND expensive (avoid), IV = growing but priced-in.
    [
      { n: "I",   x: X(97), y: Y(97), a: "end",   cap: "High growth · high value", tip: "Above-median on BOTH scores — the sweet spot: growing and still cheap" },
      { n: "II",  x: X(3),  y: Y(97), a: "start", cap: "Low growth · high value",  tip: "Cheap vs peers but growing slower — deep value or value trap" },
      { n: "III", x: X(3),  y: Y(6),  a: "start", cap: "Low growth · low value",   tip: "Below median on both scores — slow AND expensive; avoid zone" },
      { n: "IV",  x: X(97), y: Y(6),  a: "end",   cap: "High growth · low value",  tip: "Growing faster than peers but expensively priced — already discovered" }
    ].forEach(function (q) {
      svg += "<text x='" + q.x + "' y='" + q.y + "' class='ms-sc-quadcap' text-anchor='" + q.a + "'>" +
        q.cap + "<title>" + q.tip + "</title></text>";
    });
    svg += "<text x='" + (m.l + iw / 2) + "' y='" + (H - 4) + "' class='ms-sc-axis' text-anchor='middle'>Growth score</text>";
    svg += "<text x='12' y='" + (m.t + ih / 2) + "' class='ms-sc-axis' text-anchor='middle' " +
      "transform='rotate(-90 12 " + (m.t + ih / 2) + ")'>Value score</text>";

    /* LABEL PLACEMENT.
       A sector like Hydro Power puts 38 companies in one pane, and they cluster.
       The old rule only ever pushed a label straight up, 12px at a time, and
       treated every label as 104px wide when a four-letter ticker is nearer 24.
       Both faults pushed labels far from the dot they belong to, which is the
       drifting text and the empty gaps in the middle of the chart.

       Now a label is tried on a ring around its dot first — above, below, right,
       left, then the diagonals — and only stacks vertically if the whole ring is
       taken. Collision uses the real text width. Anything that still cannot be
       placed cleanly is left off rather than floated somewhere misleading; its
       dot keeps the hover tooltip. Rated and gated companies are placed first so
       the ones worth reading always get a label. */
    var charW = compact ? 5.9 : 6.6, lineH = 11;
    var placed = [];

    function fits(b) {
      if (b.y0 < m.t + 2 || b.y1 > m.t + ih - 1) return false;
      if (b.x0 < m.l + 1 || b.x1 > m.l + iw - 1) return false;
      return !placed.some(function (p) {
        return b.x0 < p.x1 + 2 && b.x1 > p.x0 - 2 && b.y0 < p.y1 + 1 && b.y1 > p.y0 - 1;
      });
    }

    function placeLabel(x, y, text) {
      var w = text.length * charW, i;
      var ring = [
        { dx: 0, dy: -9, a: "middle" }, { dx: 0, dy: 16, a: "middle" },
        { dx: 8, dy: 4, a: "start" }, { dx: -8, dy: 4, a: "end" },
        { dx: 7, dy: -6, a: "start" }, { dx: -7, dy: -6, a: "end" },
        { dx: 7, dy: 14, a: "start" }, { dx: -7, dy: 14, a: "end" }
      ];
      for (i = 1; i <= 3; i++) {
        ring.push({ dx: 0, dy: -9 - i * lineH, a: "middle" });
        ring.push({ dx: 0, dy: 16 + i * lineH, a: "middle" });
      }
      for (i = 0; i < ring.length; i++) {
        var c = ring[i], lx = x + c.dx, ly = y + c.dy;
        var x0 = c.a === "start" ? lx : c.a === "end" ? lx - w : lx - w / 2;
        var box = { x0: x0, x1: x0 + w, y0: ly - lineH + 3, y1: ly + 3 };
        if (fits(box)) {
          placed.push(box);
          return { x: lx, y: ly, a: c.a, far: Math.abs(c.dy) > 20 };
        }
      }
      return null;
    }

    // Dots first, so no label can hide one.
    pts.forEach(function (r) {
      var x = X(r.growth), y = Y(r.value);
      var isGated = !!(r.gates && r.gates.length);
      var cls = isGated ? "gated" : (r.stars >= 4) ? "star" : "";
      svg += "<circle cx='" + x + "' cy='" + y + "' r='" + (compact ? 4 : 5) + "' class='ms-sc-dot " + cls + "'>" +
        "<title>" + esc(r.ticker) + " — Growth " + r.growth + ", Value " + r.value +
        (r.stars != null ? ", " + r.stars + "★" : "") +
        (isGated ? "\nQuality-gated: " + r.gates.join("; ") + "\nNot eligible for High Growth / High Value" : "") +
        "</title></circle>";
    });

    /* Label priority: ungated first, then by stars. With most of a sector
       gated, letting gated names claim ring slots would push the eligible
       ones off the chart entirely. */
    pts.slice().sort(function (a, b) {
      var ag = (a.gates && a.gates.length) ? 1 : 0, bg = (b.gates && b.gates.length) ? 1 : 0;
      return ag - bg || (b.stars || 0) - (a.stars || 0) || (b.value || 0) - (a.value || 0);
    }).forEach(function (r) {
      var x = X(r.growth), y = Y(r.value);
      var p = placeLabel(x, y, r.ticker);
      if (!p) return;
      // Gated names are background context, not the subject of the chart, so
      // their labels fade with their dots.
      var dim = (r.gates && r.gates.length) ? " dim" : "";
      // A label pushed clear of its dot needs a thread back to it.
      if (p.far) {
        svg += "<line x1='" + x + "' y1='" + y + "' x2='" + p.x + "' y2='" +
          (p.y > y ? p.y - 8 : p.y + 3) + "' class='ms-sc-leader" + dim + "'/>";
      }
      svg += "<text x='" + p.x + "' y='" + p.y + "' class='ms-sc-label" + dim +
        "' text-anchor='" + p.a + "'>" + esc(r.ticker) + "</text>";
    });
    return svg + "</svg>";
  }

  /* A gated company is plotted at its real scores but faded to a thin ring:
     the position tells you where it scored, the fade tells you the score cannot
     be trusted. It recedes rather than shouting, because in a sector like Hydro
     Power most companies are gated and an alarm colour on the majority would
     bury the handful worth reading. Hiding them is one click. */
  function gatedToggle(n, on) {
    if (!n) return "";
    return " · <label class='ms-gated-toggle'><input type='checkbox' id='ms-show-gated'" +
      (on ? " checked" : "") + "> show " + n + " quality-gated</label>";
  }

  function drawScatter(data) {
    var host = el("ms-scatter");
    if (!host) return;
    var scored = (data.results || []).filter(function (r) {
      return r.growth != null && r.value != null;
    });
    /* A quality-gated company is barred from the High halves in the table, so it
       must not sit in the green quadrant here either. Drop it from the chart and
       say so in the caption — the full list is still in the table below. */
    var gated = scored.filter(function (r) { return r.gates && r.gates.length; });
    var nGated = gated.length;
    var showGated = state.showGated !== false;
    var pts = showGated ? scored : scored.filter(function (r) { return !(r.gates && r.gates.length); });
    var split = data.quadrant_split || null;
    if (!pts.length) { host.innerHTML = ""; return; }

    var TIERS = [
      { key: "Large", label: "Large cap" },
      { key: "Mid", label: "Mid cap" },
      { key: "Small", label: "Small cap" }
    ];
    var tiered = pts.filter(function (r) {
      return ["Large", "Mid", "Small"].indexOf(r.size) !== -1;
    });
    if (!tiered.length) {
      // Sector with no size tiers at all — one combined chart beats three empty ones.
      host.innerHTML = "<div class='dsx-card ms-scatter-card'>" +
        "<div class='dsx-card-head neutral dsx-ad-head'>" +
        "<span>GROWTH vs VALUE — ALL COMPANIES</span>" +
        "<span class='dsx-ad-sub'>" + pts.length +
        " shown · right = stronger growth, up = better value · dividers are the sector median" +
        " &nbsp; <span class='ms-legend'><i class='ms-leg-dot star'></i>4★/5★" +
        "<i class='ms-leg-dot gated'></i>quality-gated (faded, not eligible)" +
        "<i class='ms-leg-dot'></i>others</span>" + gatedToggle(nGated, showGated) + "</span>" +
        "</div>" + scatterSvg(pts, false, split) + "</div>";
      wireGatedToggle(data);
      return;
    }
    var panes = "";
    TIERS.forEach(function (tier) {
      var group = pts.filter(function (r) { return (r.size || "—") === tier.key; });
      panes += "<div class='ms-sc-pane'><div class='ms-sc-pane-head'>" + tier.label +
        " <span class='ms-dim'>(" + group.length + ")</span></div>" +
        (group.length ? scatterSvg(group, true, split)
                      : "<div class='ms-sc-empty'>No companies in this tier</div>") +
        "</div>";
    });
    var unc = pts.filter(function (r) { return ["Large", "Mid", "Small"].indexOf(r.size) === -1; });
    var uncNote = unc.length ? " · " + unc.length + " unclassified (no market cap) not plotted" : "";
    host.innerHTML = "<div class='dsx-card ms-scatter-card'>" +
      "<div class='dsx-card-head neutral dsx-ad-head'>" +
      "<span>GROWTH vs VALUE BY MARKET CAP</span>" +
      "<span class='dsx-ad-sub'>right = stronger growth, up = better value · dividers are the sector median" + uncNote +
      " &nbsp; <span class='ms-legend'><i class='ms-leg-dot star'></i>4★/5★" +
      "<i class='ms-leg-dot gated'></i>quality-gated (faded, not eligible)" +
      "<i class='ms-leg-dot'></i>others</span>" + gatedToggle(nGated, showGated) + "</span></div>" +
      "<div class='ms-sc-row'>" + panes + "</div></div>";
    wireGatedToggle(data);
  }

  function wireGatedToggle(data) {
    var cb = el("ms-show-gated");
    if (!cb) return;
    cb.addEventListener("change", function () {
      state.showGated = cb.checked;
      drawScatter(data);
    });
  }

  var FILTERS = { q: "", stars: "0", style: "all", size: "all", quality: "all", quadrant: "all", conf: "all" };

  function rowPasses(r) {
    if (FILTERS.q) {
      var q = FILTERS.q.toUpperCase();
      var name = (r.name || "").toUpperCase();
      if (r.ticker.indexOf(q) === -1 && name.indexOf(q) === -1) return false;
    }
    var minStars = parseInt(FILTERS.stars, 10) || 0;
    if (minStars && (r.stars == null || r.stars < minStars)) return false;
    if (FILTERS.style !== "all" && r.style !== FILTERS.style) return false;
    if (FILTERS.quadrant !== "all" && r.quadrant !== FILTERS.quadrant) return false;
    if (FILTERS.size !== "all" && (r.size || "—") !== FILTERS.size) return false;
    var gated = (r.gates || []).length > 0, flagged = (r.flags || []).length > 0;
    if (FILTERS.quality === "clean" && (gated || flagged)) return false;
    if (FILTERS.quality === "gated" && !gated) return false;
    if (FILTERS.quality === "flagged" && !flagged) return false;
    
    return true;
  }

  function filterBar() {
    function group(key, label, opts) {
      var html = "<span class='ms-fgroup'><b>" + label + "</b>";
      opts.forEach(function (o) {
        html += "<button type='button' class='ms-fpill" + (FILTERS[key] === o.v ? " active" : "") +
          "' data-fkey='" + key + "' data-fval='" + o.v + "'>" + o.t + "</button>";
      });
      return html + "</span>";
    }
    return "<div class='ms-filters'>" +
      "<input type='search' id='msf-q' class='dsx-select ms-f' placeholder='Search company…' value='" + esc(FILTERS.q) + "'>" +
      group("stars", "Rating", [
        { v: "0", t: "All" }, { v: "5", t: "5★" }, { v: "4", t: "4★+" }, { v: "3", t: "3★+" }]) +
      group("style", "Style", [
        { v: "all", t: "All" }, { v: "Growth", t: "Growth" }, { v: "Blend", t: "Blend" }, { v: "Value", t: "Value" }]) +
      group("quadrant", "Quadrant", [
        { v: "all", t: "All" }, { v: "High Growth / High Value", t: "HG / HV" },
        { v: "High Growth / Low Value", t: "HG / LV" }, { v: "Low Growth / High Value", t: "LG / HV" },
        { v: "Low Growth / Low Value", t: "LG / LV" }]) +
      group("size", "Size", [
        { v: "all", t: "All" }, { v: "Large", t: "Large" }, { v: "Mid", t: "Mid" }, { v: "Small", t: "Small" }]) +
      group("quality", "Quality", [
        { v: "all", t: "All" }, { v: "clean", t: "Clean" }, { v: "flagged", t: "Flagged" }, { v: "gated", t: "Gated" }]) +
      "<span class='ms-f-count' id='msf-count'></span></div>";
  }

  function bindFilters() {
    var q = el("msf-q");
    if (q) q.addEventListener("input", function () { FILTERS.q = q.value; drawTable(state.data); });
    var bar = document.querySelector(".ms-filters");
    if (!bar) return;
    bar.addEventListener("click", function (e) {
      var pill = e.target.closest ? e.target.closest(".ms-fpill") : null;
      if (!pill) return;
      var key = pill.getAttribute("data-fkey");
      FILTERS[key] = pill.getAttribute("data-fval");
      bar.querySelectorAll(".ms-fpill[data-fkey='" + key + "']").forEach(function (b2) {
        b2.classList.toggle("active", b2 === pill);
      });
      drawTable(state.data);
    });
  }

  function draw(data) {
    state.data = data;
    drawBarometer(data);
    drawScatter(data);
    var sub = el("ms-sub");
    if (sub) {
      sub.textContent = data.period + " · Growth " + (data.mix ? data.mix.growth : 60) +
        "% / Value " + (data.mix ? data.mix.value : 40) + "% · ranked within sector" +
        (data.price_as_of ? " · prices " + data.price_as_of : "");
    }
    var note = el("ms-note");
    if (note) {
      /* Every Value metric here divides by the share price, so which price was
         used is not a footnote — it decides the ranking. Say the date, and say
         how many companies got a traded price, since the rest fall back to the
         price filed with the statement. */
      var priceNote = data.price_as_of
        ? " P/B, P/E, dividend yield and market cap use the close of " + data.price_as_of +
          (data.priced_total && data.priced_live < data.priced_total
            ? ", for " + data.priced_live + " of " + data.priced_total +
              " companies; the rest keep the price filed with the statement."
            : ", not the price filed with the statement.")
        : "";
      /* Suspended and delisted issues keep filing statements, so say plainly
         that they were left out rather than letting the count look short. */
      var activeNote = data.inactive_excluded
        ? " " + data.inactive_excluded + " suspended or delisted " +
          (data.inactive_excluded === 1 ? "issue is" : "issues are") +
          " excluded; only actively traded companies are ranked."
        : "";
      note.textContent = (data.note ||
        "Percentile rank within sector; YTD vs prior-year YTD; missing factors redistribute their weight " +
        "(see confidence). Quality gates cap stars at 2; each soft flag costs 5 combined points.") +
        " Quadrants split at the sector median, and a quality-gated company cannot be High Growth or " +
        "High Value whatever it scored; book value below par is itself a gate." +
        priceNote + activeNote;
    }

    // KPI strip
    var rows = data.results || [];
    var k = el("ms-kpis");
    if (k) {
      var five = rows.filter(function (r) { return r.stars === 5; }).length;
      var four = rows.filter(function (r) { return r.stars === 4; }).length;
      var gated = rows.filter(function (r) { return (r.gates || []).length; }).length;
      var hh = rows.filter(function (r) { return r.quadrant === "High Growth / High Value"; }).length;
      var growthN = rows.filter(function (r) { return r.style === "Growth"; }).length;
      var valueN = rows.filter(function (r) { return r.style === "Value"; }).length;
      k.innerHTML =
        "<div class='dsx-kpi'><span>" + rows.length + "</span>Companies scored</div>" +
        "<div class='dsx-kpi'><span>" + five + " / " + four + "</span>5★ / 4★</div>" +
        "<div class='dsx-kpi'><span>" + hh + "</span>High Growth / High Value</div>" +
        "<div class='dsx-kpi'><span>" + growthN + " / " + valueN + "</span>Growth / Value style</div>" +
        "<div class='dsx-kpi'><span>" + gated + "</span>Quality-gated (capped 2★)</div>";
    }

    var fb = el("ms-filterbar");
    if (fb && !fb.innerHTML) { fb.innerHTML = filterBar(); bindFilters(); }
    drawTable(data);
  }

  function drawTable(data) {
    var t = el("ms-table");
    if (!t || !data) return;
    var rows = (data.results || []).filter(rowPasses);
    var count = el("msf-count");
    if (count) count.textContent = rows.length + " of " + (data.results || []).length + " shown";
    var html = "<thead><tr><th>Company</th><th>Rating</th><th>Combined</th><th>Growth</th>" +
      "<th>Value</th><th>Quadrant</th><th>Style</th><th>Size</th><th>Confidence</th><th>Quality</th></tr></thead><tbody>";
    rows.forEach(function (r, i) {
      var quality = "";
      (r.gates || []).forEach(function (g) {
        quality += "<span class='ms-chip ms-gate' title='Hard gate — stars capped at 2 and barred from High Growth / High Value'>" + esc(g) + "</span>";
      });
      (r.flags || []).forEach(function (f) {
        quality += "<span class='ms-chip ms-flag' title='Soft flag — −5 combined points'>" + esc(f) + "</span>";
      });
      if (!quality) quality = "<span class='ms-dim'>clean</span>";
      html += "<tr class='ms-row' data-i='" + i + "' title='Click for the factor breakdown'>" +
        "<td><span class='ms-ticker'>" + esc(r.ticker) + "</span>" +
        (r.name ? "<span class='ms-name'>" + esc(r.name) + "</span>" : "") + "</td>" +
        "<td>" + stars(r.stars) + "</td>" +
        "<td>" + pct(r.combined) + "</td>" +
        "<td>" + pct(r.growth) + "</td>" +
        "<td>" + pct(r.value) + "</td>" +
        "<td>" + quadChip(r.quadrant) + "</td>" +
        "<td>" + styleChip(r.style) + "</td>" +
        "<td>" + esc(r.size || "—") + "</td>" +
        "<td class='" + (r.low_confidence ? "ms-lowconf" : "") + "'>" + esc(r.confidence || "") + "</td>" +
        "<td class='ms-quality'>" + quality + "</td></tr>" +
        "<tr class='ms-detail' data-for='" + i + "' hidden><td colspan='10'>" + factorRows(r.detail || {}) + "</td></tr>";
    });
    if (!rows.length) {
      html += "<tr><td colspan='10' class='dsx-empty'>No companies match the current filters.</td></tr>";
    }
    html += "</tbody>";
    t.innerHTML = html;

    t.querySelectorAll(".ms-row").forEach(function (row) {
      row.addEventListener("click", function () {
        var d = t.querySelector(".ms-detail[data-for='" + row.getAttribute("data-i") + "']");
        if (d) d.hidden = !d.hidden;
      });
    });
  }

  function fillSectors(sectors, selected) {
    var sel = el("ms-sector");
    if (!sel || sel.options.length) return;
    (sectors || []).forEach(function (s) {
      var o = document.createElement("option");
      o.value = s; o.textContent = s;
      if (s === selected) o.selected = true;
      sel.appendChild(o);
    });
  }

  function load() {
    var t = el("ms-table");
    if (t) t.innerHTML = "<tbody><tr><td class='dsx-empty'>Scoring sector…</td></tr></tbody>";
    var sel = el("ms-sector");
    var url = CFG.scanUrl + (sel && sel.value ? "?sector=" + encodeURIComponent(sel.value) : "");
    fetch(url, { credentials: "same-origin" })
      .then(function (r) { return r.json(); })
      .then(function (d) {
        fillSectors(d.sectors, d.sector);
        if (!d.ok) {
          if (t) t.innerHTML = "<tbody><tr><td class='dsx-empty'>" + esc(d.error || "Scan failed.") + "</td></tr></tbody>";
          return;
        }
        state.loaded = true;
        draw(d);
      })
      .catch(function () {
        if (t) t.innerHTML = "<tbody><tr><td class='dsx-empty'>Could not load the Morning Star scan.</td></tr></tbody>";
      });
  }

  document.addEventListener("DOMContentLoaded", function () {
    var sel = el("ms-sector"), btn = el("ms-refresh");
    if (sel) sel.addEventListener("change", load);
    if (btn) btn.addEventListener("click", load);
    // Lazy: only fetch when the tab is first opened.
    var tab = document.querySelector(".dsx-tab[data-tab='morningstar']");
    if (tab) tab.addEventListener("click", function () { if (!state.loaded) load(); });
  });
})();

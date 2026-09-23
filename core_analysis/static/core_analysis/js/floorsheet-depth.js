/* floorsheet-depth.js — Market Depth tab on the Floorsheet desk.
 *
 * Self-contained: floorsheet-brokers.js toggles the panel by id and skips tabs
 * it has no TABS entry for, so this file only has to react to its own tab
 * click. Raw observations only (Phase 2A-1); no score is computed here.
 */
(function () {
  "use strict";
  var CFG = window.MD_CONFIG || {};
  function el(id) { return document.getElementById(id); }
  function fmt(n, d) { return (n === null || n === undefined) ? "—" : Number(n).toLocaleString("en-US", { maximumFractionDigits: d === undefined ? 0 : d, minimumFractionDigits: d === undefined ? 0 : d }); }
  function esc(s) { return String(s === null || s === undefined ? "" : s).replace(/[&<>"]/g, function (c) { return { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]; }); }
  function get(url, params) {
    var q = new URLSearchParams(params || {}).toString();
    return fetch(url + (q ? "?" + q : ""), { credentials: "same-origin" }).then(function (r) { return r.json(); });
  }

  var state = { dates: [], date: null, overview: null, frames: [], i: 0, symbol: null, timer: null, booted: false };

  /* ── boot ─────────────────────────────────────────────────────────── */
  function boot() {
    if (state.booted) return;
    state.booted = true;
    get(CFG.datesUrl).then(function (d) {
      state.dates = (d && d.dates) || [];
      var sel = el("md-date");
      sel.innerHTML = state.dates.map(function (x) { return "<option value=\"" + x + "\">" + x + "</option>"; }).join("");
      if (!state.dates.length) {
        el("md-hint").textContent = "No market-depth captures stored yet. Run: manage.py sync_market_depth";
        return;
      }
      state.date = state.dates[0];
      loadOverview();
    });
    el("md-date").addEventListener("change", function () { state.date = this.value; loadOverview(); });
    el("md-load").addEventListener("click", function () { loadFrames(el("md-symbol").value); });
    el("md-symbol").addEventListener("change", function () { loadFrames(this.value); });
    el("md-slider").addEventListener("input", function () { show(parseInt(this.value, 10)); });
    el("md-prev").addEventListener("click", function () { stop(); show(state.i - 1); });
    el("md-next").addEventListener("click", function () { stop(); show(state.i + 1); });
    el("md-play").addEventListener("click", function () { state.timer ? stop() : play(); });
  }

  /* ── overview ─────────────────────────────────────────────────────── */
  function loadOverview() {
    el("md-ov-sub").textContent = "loading…";
    get(CFG.overviewUrl, { date: state.date }).then(function (d) {
      state.overview = d;
      var rows = (d && d.rows) || [];
      var sel = el("md-symbol");
      sel.innerHTML = rows.slice().sort(function (a, b) { return a.symbol < b.symbol ? -1 : 1; })
        .map(function (r) { return "<option value=\"" + r.symbol + "\">" + r.symbol + " (" + r.snapshots + ")</option>"; }).join("");
      el("md-ov-sub").textContent = rows.length + " scripts · " + fmt(d.snapshots) + " captures";
      el("md-kpis").innerHTML =
        "<div class='dsx-kpi'><span>" + rows.length + "</span>Scripts captured</div>" +
        "<div class='dsx-kpi'><span>" + fmt(d.snapshots) + "</span>Book captures</div>" +
        "<div class='dsx-kpi'><span>" + rows.reduce(function (a, r) { return a + r.pulls; }, 0) + "</span>Pull events (all scripts)</div>" +
        "<div class='dsx-kpi'><span>" + rows.reduce(function (a, r) { return a + r.adds; }, 0) + "</span>Add events (all scripts)</div>";
      var t = el("md-overview");
      if (!rows.length) { t.innerHTML = "<tbody><tr><td class='dsx-empty'>No captures for this date.</td></tr></tbody>"; return; }
      t.innerHTML = "<thead><tr><th class='l'>Script</th><th>Captures</th><th>Gap (s)</th><th>Best bid</th><th>Best ask</th>" +
        "<th>Bid top-5</th><th>Ask top-5</th><th>Max bid spike</th><th>Max ask spike</th><th>Pulls</th><th>Adds</th><th class='l'>Biggest event</th></tr></thead><tbody>" +
        rows.map(function (r) {
          var b = r.biggest;
          var bt = b ? "<span class='md-tag " + b.kind.toLowerCase() + "'>" + b.kind + "</span> " + b.side + " " + fmt(b.shares) + " @ " + fmt(b.price, 2) + " · " + b.vs_baseline + "× base · " + b.t : "<span class='md-tag none'>none</span>";
          return "<tr data-sym='" + r.symbol + "'><td class='l'><b>" + r.symbol + "</b></td><td>" + r.snapshots + "</td><td>" + fmt(r.median_gap_s) + "</td>" +
            "<td>" + fmt(r.best_bid, 2) + "</td><td>" + fmt(r.best_ask, 2) + "</td><td>" + fmt(r.bid_qty) + "</td><td>" + fmt(r.ask_qty) + "</td>" +
            "<td>" + r.max_bid_spike + "×</td><td>" + r.max_ask_spike + "×</td><td>" + r.pulls + "</td><td>" + r.adds + "</td><td class='l'>" + bt + "</td></tr>";
        }).join("") + "</tbody>";
      t.querySelectorAll("tbody tr").forEach(function (tr) {
        tr.addEventListener("click", function () { sel.value = tr.dataset.sym; loadFrames(tr.dataset.sym); window.scrollTo({ top: el("panel-depth").offsetTop - 40, behavior: "smooth" }); });
      });
    });
  }

  /* ── frames ───────────────────────────────────────────────────────── */
  function loadFrames(symbol) {
    if (!symbol) return;
    stop();
    state.symbol = symbol;
    el("md-hint").textContent = "Loading " + symbol + "…";
    get(CFG.framesUrl, { date: state.date, symbol: symbol }).then(function (d) {
      if (!d || !d.ok) { el("md-hint").textContent = (d && d.error) || "Could not load frames."; return; }
      state.frames = d.frames || [];
      el("md-replay").hidden = !state.frames.length;
      el("md-hint").textContent = symbol + ": " + state.frames.length + " captures, " + d.events + " liquidity events.";
      var s = el("md-slider"); s.max = Math.max(0, state.frames.length - 1); s.value = 0;
      renderEvents();
      show(0);
    });
  }

  function renderEvents() {
    var evs = state.frames.map(function (f, i) { return f.event ? { i: i, f: f } : null; }).filter(Boolean);
    el("md-ev-sub").textContent = evs.length + " event" + (evs.length === 1 ? "" : "s");
    var t = el("md-events");
    if (!evs.length) { t.innerHTML = "<tbody><tr><td class='dsx-empty'>No level changed by half the baseline between any two captures.</td></tr></tbody>"; return; }
    t.innerHTML = "<thead><tr><th class='l'>Time</th><th class='l'>Event</th><th>Side</th><th>Price</th><th>Shares</th><th>% of level</th><th>vs baseline</th><th>Splits</th><th>Gap (s)</th><th>Shares/s</th><th>Mid after</th></tr></thead><tbody>" +
      evs.map(function (x) {
        var e = x.f.event;
        return "<tr data-i='" + x.i + "'><td class='l'>" + x.f.t + "</td><td class='l'><span class='md-tag " + e.kind.toLowerCase() + "'>" + e.kind + "</span></td><td>" + e.side + "</td>" +
          "<td>" + fmt(e.price, 2) + "</td><td>" + fmt(e.shares) + "</td><td>" + (e.pct === null ? "new" : fmt(e.pct, 1) + "%") + "</td><td>" + e.vs_baseline + "×</td>" +
          "<td>" + e.splits_before + " → " + e.splits_after + "</td><td>" + fmt(e.secs) + "</td><td>" + fmt(e.shares_per_sec, 1) + "</td><td>" + fmt(x.f.mid, 2) + "</td></tr>";
      }).join("") + "</tbody>";
    t.querySelectorAll("tbody tr").forEach(function (tr) { tr.addEventListener("click", function () { stop(); show(parseInt(tr.dataset.i, 10)); }); });
  }

  function bookTable(levels, prevLevels, side) {
    var prev = {};
    (prevLevels || []).forEach(function (x) { prev[x.price] = x; });
    var head = "<thead><tr><th class='l'>Lvl</th><th>Price</th><th>Qty</th><th>Splits</th><th>Qty/split</th><th>Δ qty</th></tr></thead>";
    if (!levels.length) return head + "<tbody><tr><td colspan='6' class='dsx-empty'>empty side</td></tr></tbody>";
    return head + "<tbody>" + levels.map(function (x, i) {
      var p = prev[x.price]; var d = p ? x.qty - p.qty : null;
      var cls = d === null ? "md-new" : d > 0 ? "md-up" : d < 0 ? "md-down" : "";
      return "<tr class='md-l" + i + "'><td class='l'>" + (i + 1) + "</td><td>" + fmt(x.price, 2) + "</td><td>" + fmt(x.qty) + "</td><td>" + x.splits + "</td>" +
        "<td>" + (x.splits ? fmt(x.qty / x.splits) : "—") + "</td><td class='" + cls + "'>" + (d === null ? "new" : (d > 0 ? "+" : "") + fmt(d)) + "</td></tr>";
    }).join("") + "</tbody>";
  }

  function obs(k, v, s) { return "<div><div class='k'>" + k + "</div><div class='v'>" + v + "</div>" + (s ? "<div class='s'>" + s + "</div>" : "") + "</div>"; }

  function show(i) {
    if (!state.frames.length) return;
    i = Math.max(0, Math.min(state.frames.length - 1, i));
    state.i = i;
    var f = state.frames[i], p = i > 0 ? state.frames[i - 1] : null;
    el("md-slider").value = i;
    el("md-frame").textContent = state.symbol + " · " + f.t + " · frame " + (i + 1) + "/" + state.frames.length + (f.secs_since_prev !== null ? " · +" + f.secs_since_prev + "s" : "");
    el("md-bid-head").textContent = "top-5 " + fmt(f.bid_qty) + " / total " + fmt(f.total_bids) + " · " + f.bid_splits + " orders";
    el("md-ask-head").textContent = "top-5 " + fmt(f.ask_qty) + " / total " + fmt(f.total_asks) + " · " + f.ask_splits + " orders";
    el("md-bids").innerHTML = bookTable(f.bids, p && p.bids, "bid");
    el("md-asks").innerHTML = bookTable(f.asks, p && p.asks, "ask");

    var e = f.event;
    el("md-obs-sub").textContent = f.t + (p ? " vs " + p.t : " (first capture, no comparison)");
    el("md-obs").innerHTML =
      obs("Mid / spread", fmt(f.mid, 2), f.spread_pct !== null ? "spread " + f.spread_pct + "%" : "one side empty") +
      obs("Bid top-5 vs baseline", f.bid_spike !== null ? f.bid_spike + "×" : "—", "baseline " + fmt(f.bid_baseline) + " (median of prior 20)") +
      obs("Ask top-5 vs baseline", f.ask_spike !== null ? f.ask_spike + "×" : "—", "baseline " + fmt(f.ask_baseline)) +
      obs("Bid Δ since prev", f.bid_delta === null ? "—" : (f.bid_delta > 0 ? "+" : "") + fmt(f.bid_delta), f.bid_delta_pct !== null ? f.bid_delta_pct + "%" : "") +
      obs("Ask Δ since prev", f.ask_delta === null ? "—" : (f.ask_delta > 0 ? "+" : "") + fmt(f.ask_delta), f.ask_delta_pct !== null ? f.ask_delta_pct + "%" : "") +
      obs("Qty per order", "B " + fmt(f.bid_qty_per_split) + " / A " + fmt(f.ask_qty_per_split), "large qty on few orders can leave in one capture") +
      obs("Liquidity event", e ? "<span class='md-tag " + e.kind.toLowerCase() + "'>" + e.kind + " " + e.side + "</span>" : "<span class='md-tag none'>none</span>",
        e ? fmt(e.shares) + " sh @ " + fmt(e.price, 2) + " · " + e.vs_baseline + "× baseline · splits " + e.splits_before + "→" + e.splits_after + " · " + fmt(e.secs) + "s gap" : "");
    var evRows = el("md-events").querySelectorAll("tbody tr[data-i]");
    evRows.forEach(function (tr) { tr.style.outline = parseInt(tr.dataset.i, 10) === i ? "2px solid var(--accent)" : ""; });
  }

  function play() {
    if (state.timer || !state.frames.length) return;
    el("md-play").textContent = "⏸";
    state.timer = setInterval(function () {
      if (state.i >= state.frames.length - 1) { stop(); return; }
      show(state.i + 1);
    }, 600);
  }
  function stop() {
    if (state.timer) clearInterval(state.timer);
    state.timer = null;
    el("md-play").textContent = "▶";
  }

  document.addEventListener("DOMContentLoaded", function () {
    var tab = document.querySelector('.dsx-tab[data-tab="depth"]');
    if (!tab) return;
    tab.addEventListener("click", boot);
    if (new URLSearchParams(window.location.search).get("tab") === "depth") boot();
  });
})();

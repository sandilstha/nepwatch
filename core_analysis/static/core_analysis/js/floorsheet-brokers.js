/* ============================================================================
   Dalal Street X broker analytics — frontend controller.
   Reads bootstrap meta (brokers/symbols/sectors), drives 6 tabs, each backed by
   a JSON endpoint under /floorsheet/api/*. Tables + squarified treemap are
   rendered by hand.
   ========================================================================== */
(function () {
  "use strict";

  var META = window.DSX_BOOTSTRAP || { brokers: [], symbols: [], sectors: [] };
  var API = "/floorsheet/api/";
  // Tab registry — declared up front because tab modules (e.g. flowradar) attach
  // to it well before the favorites block below would otherwise initialise it.
  var TABS = {};

  // ── formatting ───────────────────────────────────────────────────────
  function nf(n) { return (n == null ? 0 : n).toLocaleString("en-IN"); }
  function fmtQty(n) { return nf(Math.round(n || 0)); }
  function fmtRs(n) { return "Rs. " + nf(Math.round(n || 0)); }
  function fmtPrice(n) {
    return "Rs. " + (n || 0).toLocaleString("en-IN", { minimumFractionDigits: 2, maximumFractionDigits: 2 });
  }
  function fmtPct(n) { return (n || 0).toFixed(2) + "%"; }
  function fmtSignedRs(n) {
    var v = n || 0;
    return (v > 0 ? "+" : v < 0 ? "-" : "") + fmtRs(Math.abs(v));
  }
  function fmtSignedPct(n) {
    var v = n || 0;
    return (v > 0 ? "+" : "") + v.toFixed(2) + "%";
  }
  // Compact Rs in NEPSE numbering (crore / lakh) for KPI tiles.
  function fmtRsCompact(n) {
    var v = Math.round(n || 0), s = v < 0 ? "-" : "", a = Math.abs(v);
    if (a >= 1e7) return s + "Rs " + (a / 1e7).toFixed(2) + " Cr";
    if (a >= 1e5) return s + "Rs " + (a / 1e5).toFixed(2) + " L";
    return fmtRs(v);
  }
  function esc(s) {
    return String(s == null ? "" : s).replace(/[&<>"']/g, function (c) {
      return { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c];
    });
  }
  function el(id) { return document.getElementById(id); }
  // Firm name for a broker number (from bootstrap meta), or "" if unmapped.
  function brokerName(b) { return (META.broker_names || {})[String(b)] || ""; }
  // Broker-code table cell that reveals the firm name on hover (native tooltip).
  function brokerCell(key) {
    var nm = brokerName(key);
    var tip = nm ? "#" + key + " — " + nm : "Broker " + key;
    return "<td class='l tkr brk' title='" + esc(tip) + "'>" + esc(key) + "</td>";
  }

  var inflight = {};
  function getJSON(path, params, key) {
    var qs = new URLSearchParams(params || {}).toString();
    var options = { headers: { Accept: "application/json" } };
    var controller = null;
    if (key && window.AbortController) {
      if (inflight[key]) inflight[key].abort();
      controller = new AbortController();
      inflight[key] = controller;
      options.signal = controller.signal;
    }
    return fetch(API + path + (qs ? "?" + qs : ""), options)
      .then(function (r) {
        if (!r.ok) throw new Error("HTTP " + r.status);
        return r.json();
      })
      .then(function (d) {
        if (controller && inflight[key] === controller) delete inflight[key];
        return d;
      }, function (err) {
        if (controller && inflight[key] === controller) delete inflight[key];
        throw err;
      });
  }
  function isAbort(err) { return err && err.name === "AbortError"; }

  function loading(table, cols) {
    table.innerHTML = '<tbody><tr><td colspan="' + cols + '" class="dsx-loading">Loading…</td></tr></tbody>';
  }
  function empty(table, cols, msg) {
    table.innerHTML = '<tbody><tr><td colspan="' + cols + '" class="dsx-empty">' + esc(msg || "No data") + "</td></tr></tbody>";
  }

  // ── populate dropdowns ────────────────────────────────────────────────
  function fillBrokers(sel) {
    sel.innerHTML = "";
    (META.brokers || []).forEach(function (b) {
      var o = document.createElement("option");
      var nm = brokerName(b);
      o.value = b; o.textContent = nm ? b + " — " + nm : b;
      sel.appendChild(o);
    });
  }
  function fillSymbols(sel) {
    sel.innerHTML = "";
    (META.symbols || []).forEach(function (s) {
      var o = document.createElement("option");
      o.value = s.symbol;
      o.textContent = s.name && s.name !== s.symbol ? s.name + " ( " + s.symbol + " )" : s.symbol;
      sel.appendChild(o);
    });
  }
  // ── type-to-filter ticker picker ──────────────────────────────────────
  // A native <select> holding 900 companies is unusable — you cannot type more
  // than one letter before the browser's incremental match resets. This wraps
  // one in a text input + filtered list.
  //
  // The <select> stays in the DOM (hidden) and remains the source of truth: the
  // picker sets `.value` and fires a native `change`, so every existing
  // `el(id).value` read and change listener keeps working untouched.
  var COMBO_MAX = 60;

  function symbolCombo(sel, placeholder) {
    if (!sel || sel.dataset.combo) return;
    sel.dataset.combo = "1";
    var items = (META.symbols || []).map(function (s) {
      return {
        symbol: s.symbol,
        name: s.name || s.symbol,
        label: s.name && s.name !== s.symbol ? s.name + " ( " + s.symbol + " )" : s.symbol
      };
    });

    var wrap = document.createElement("div");
    wrap.className = "dsx-combo";
    var input = document.createElement("input");
    input.type = "text";
    input.className = "dsx-select dsx-combo-input";
    input.setAttribute("autocomplete", "off");
    input.setAttribute("role", "combobox");
    input.setAttribute("aria-expanded", "false");
    input.placeholder = placeholder || "Type a symbol or company…";
    var list = document.createElement("div");
    list.className = "dsx-combo-list dsx-hidden";
    list.setAttribute("role", "listbox");

    sel.parentNode.insertBefore(wrap, sel);
    wrap.appendChild(input);
    wrap.appendChild(list);
    wrap.appendChild(sel);
    sel.classList.add("dsx-hidden");

    var open = false, active = -1, shown = [];

    function labelFor(v) {
      for (var i = 0; i < items.length; i++) if (items[i].symbol === v) return items[i].label;
      return v || "";
    }
    function sync() { input.value = labelFor(sel.value); }
    // Programmatic callers (deep links) set sel.value directly; without this the
    // native select is right while the visible input still shows the old pick.
    sel.comboSync = sync;

    function match(q) {
      q = q.trim().toUpperCase();
      if (!q) return items.slice(0, COMBO_MAX);
      var starts = [], contains = [];
      for (var i = 0; i < items.length; i++) {
        var it = items[i], sy = it.symbol.toUpperCase(), nm = it.name.toUpperCase();
        // Ticker prefix first — typing "NAB" should surface NABIL above any
        // company whose name merely contains those letters.
        if (sy.indexOf(q) === 0) starts.push(it);
        else if (sy.indexOf(q) > -1 || nm.indexOf(q) > -1) contains.push(it);
      }
      return starts.concat(contains).slice(0, COMBO_MAX);
    }

    function render(q) {
      shown = match(q);
      if (!shown.length) {
        list.innerHTML = '<div class="dsx-combo-empty">No match</div>';
      } else {
        list.innerHTML = shown.map(function (it, i) {
          return '<div class="dsx-combo-opt' + (i === active ? " active" : "") +
            '" role="option" data-v="' + esc(it.symbol) + '" data-i="' + i + '">' +
            '<b>' + esc(it.symbol) + '</b><span>' + esc(it.name) + '</span></div>';
        }).join("");
      }
    }
    function show(q) {
      active = -1;
      render(q == null ? "" : q);
      list.classList.remove("dsx-hidden");
      input.setAttribute("aria-expanded", "true");
      open = true;
    }
    function hide() {
      list.classList.add("dsx-hidden");
      input.setAttribute("aria-expanded", "false");
      open = false;
      sync();   // discard half-typed text so the box always shows a real pick
    }
    function pick(v) {
      if (!v) return;
      sel.value = v;
      hide();
      // Native event so the tab's own change listener drives the reload.
      sel.dispatchEvent(new Event("change", { bubbles: true }));
    }
    function moveActive(step) {
      if (!shown.length) return;
      active = (active + step + shown.length) % shown.length;
      render(input.value);
      var node = list.querySelector(".dsx-combo-opt.active");
      if (node) node.scrollIntoView({ block: "nearest" });
    }

    input.addEventListener("focus", function () { this.select(); show(""); });
    input.addEventListener("input", function () { show(this.value); });
    input.addEventListener("keydown", function (e) {
      if (e.key === "ArrowDown") { e.preventDefault(); if (!open) show(this.value); else moveActive(1); }
      else if (e.key === "ArrowUp") { e.preventDefault(); moveActive(-1); }
      else if (e.key === "Enter") {
        e.preventDefault();
        // Enter with nothing highlighted takes the top match, so typing a full
        // ticker and hitting Enter just works.
        pick((shown[active < 0 ? 0 : active] || {}).symbol);
      } else if (e.key === "Escape") { hide(); }
    });
    list.addEventListener("mousedown", function (e) {
      var o = e.target.closest(".dsx-combo-opt");
      if (!o) return;
      e.preventDefault();          // keep focus so blur-hide doesn't race the click
      pick(o.dataset.v);
    });
    document.addEventListener("mousedown", function (e) {
      if (open && !wrap.contains(e.target)) hide();
    });

    sync();
  }

  function fillSectors(sel) {
    sel.innerHTML = '<option value="All">All</option>';
    (META.sectors || []).forEach(function (s) {
      var o = document.createElement("option");
      o.value = s; o.textContent = s;
      sel.appendChild(o);
    });
  }

  // ── segmented control helper ──────────────────────────────────────────
  // group => current value, with onChange callback.
  function segGroup(name, onChange) {
    var wrap = document.querySelector('[data-group="' + name + '"]');
    var state = { value: null };
    if (!wrap) return state;
    var pills = wrap.querySelectorAll(".dsx-pill");
    pills.forEach(function (p) {
      if (p.classList.contains("active")) state.value = p.dataset.val;
      p.addEventListener("click", function () {
        pills.forEach(function (q) { q.classList.remove("active"); });
        p.classList.add("active");
        state.value = p.dataset.val;
        onChange(state.value);
      });
    });
    return state;
  }

  function assign(dst, src) {
    for (var k in src) { if (Object.prototype.hasOwnProperty.call(src, k)) dst[k] = src[k]; }
    return dst;
  }

  // ── shared date-range control ─────────────────────────────────────────
  // Preset dropdown mapped to the NEPSE trading calendar (Current Day / This
  // Week / 1M / 3M / 1Y / Fiscal Year / Custom), namespaced by `prefix`. Presets
  // are resolved to real sessions server-side, so the client only sends the key
  // and never has to do calendar math. "Custom Range" reveals Start/End inputs
  // and applies on Analyze. params() yields the query params for the request.
  var _dateRanges = [];
  function dateRange(prefix, onApply, defaultRange) {
    var preset = el(prefix + "-preset"),
        startI = el(prefix + "-start"),
        endI = el(prefix + "-end"),
        analyze = el(prefix + "-analyze");
    var customFields = [el(prefix + "-custom"), el(prefix + "-custom-end")];
    var state = { range: "today", start: null, end: null };

    function addDays(iso, n) {
      var p = (iso || "").split("-");
      if (p.length !== 3) return iso || "";
      var d = new Date(+p[0], +p[1] - 1, +p[2]);
      d.setDate(d.getDate() + n);
      return d.getFullYear() + "-" + ("0" + (d.getMonth() + 1)).slice(-2) + "-" + ("0" + d.getDate()).slice(-2);
    }
    function showCustom(on) {
      customFields.forEach(function (f) { if (f) f.hidden = !on; });
    }
    function setMax() {
      if (!META.latest_date) return;
      if (startI) startI.max = META.latest_date;
      if (endI) endI.max = META.latest_date;
    }
    function apply(val, fire) {
      if (val === "custom") {
        showCustom(true);
        if (startI && !startI.value && META.latest_date) {
          startI.value = addDays(META.latest_date, -29);  // sensible default window
          if (endI) endI.value = META.latest_date;
        }
        state.range = "custom";
        state.start = startI ? startI.value : null;
        state.end = endI ? endI.value : null;
        return;                 // wait for Analyze
      }
      showCustom(false);
      state.range = val; state.start = null; state.end = null;
      if (fire) onApply();
    }

    if (preset) preset.addEventListener("change", function () { apply(this.value, true); });
    if (analyze) analyze.addEventListener("click", function () {
      if (state.range === "custom") {
        state.start = startI ? startI.value : null;
        state.end = endI ? endI.value : null;
      }
      onApply();
    });

    var def = defaultRange || "today";
    if (preset && def !== "today") preset.value = def;   // reflect non-default preset in the dropdown
    setMax();
    apply(def, false);          // seed defaults; caller fires the first load

    var ctrl = {
      refresh: function () { setMax(); },
      params: function () {
        return state.range === "custom"
          ? { range: "custom", start_date: state.start || "", end_date: state.end || "" }
          : { range: state.range };
      }
    };
    _dateRanges.push(ctrl);
    return ctrl;
  }
  function refreshDateRanges() { _dateRanges.forEach(function (d) { d.refresh(); }); }

  // ── tab switching ─────────────────────────────────────────────────────
  var loaded = {};
  function activateTab(name, afterInit) {
    document.querySelectorAll(".dsx-tab").forEach(function (t) {
      t.classList.toggle("active", t.dataset.tab === name);
    });
    document.querySelectorAll(".dsx-panel").forEach(function (p) {
      p.classList.toggle("active", p.id === "panel-" + name);
    });
    if (TABS[name] && !loaded[name]) { loaded[name] = true; TABS[name].init(); }
    // Runs AFTER init (which fills the selects and seeds state from them) and
    // BEFORE load, so a deep link's symbol survives instead of being overwritten
    // by the tab's own defaults — and still costs only one fetch.
    if (afterInit) afterInit();
    if (TABS[name]) TABS[name].load();
  }

  // ── sortable tables ───────────────────────────────────────────────────
  // Click a column header to sort by that column (numeric desc / text asc on
  // first click, toggles thereafter). Sort state is kept per table id and
  // survives data reloads. Each builder renders its header via sortableHead and
  // is mounted through showTable, which re-sorts + re-draws on header clicks.
  var _tables = {};

  function sortRows(rows, key, dir, type) {
    var out = rows.slice();
    var sign = dir === "asc" ? 1 : -1;
    out.sort(function (a, b) {
      var av = a[key], bv = b[key];
      if (type === "num") return sign * ((+av || 0) - (+bv || 0));
      av = (av == null ? "" : String(av)).toUpperCase();
      bv = (bv == null ? "" : String(bv)).toUpperCase();
      return av < bv ? -sign : av > bv ? sign : 0;
    });
    return out;
  }

  // cols: [{label, key?, type?, cls?}]. Omit key for a non-sortable column.
  function sortableHead(tableId, cols) {
    var st = _tables[tableId] && _tables[tableId].sort;
    var cells = cols.map(function (c) {
      var cls = c.cls || "";
      if (!c.key) return "<th" + (cls ? " class='" + cls + "'" : "") + ">" + c.label + "</th>";
      var arrow = "";
      if (st && st.key === c.key) { cls += " sorted"; arrow = st.dir === "asc" ? " ▲" : " ▼"; }
      cls = ("sortable " + cls).trim();
      return "<th class='" + cls + "' data-sort='" + c.key + "' data-type='" + (c.type || "str") +
        "'>" + c.label + "<span class='dsx-arrow'>" + arrow + "</span></th>";
    }).join("");
    return "<thead><tr>" + cells + "</tr></thead>";
  }

  // Class suffix + arrow markup for a custom-built sortable header cell.
  function _sortMark(tableId, key) {
    var st = _tables[tableId] && _tables[tableId].sort;
    var on = st && st.key === key;
    return { cls: on ? " sorted" : "", arrow: "<span class='dsx-arrow'>" + (on ? (st.dir === "asc" ? " ▲" : " ▼") : "") + "</span>" };
  }

  function _drawTable(reg) {
    var rows = reg.rows || [];
    if (reg.sort && reg.sort.key) rows = sortRows(rows, reg.sort.key, reg.sort.dir, reg.sort.type);
    reg.build(reg.table, rows);
  }

  // Mount/refresh a sortable table. `build(table, sortedRows)` renders it.
  function showTable(table, rows, build) {
    var reg = _tables[table.id];
    if (!reg) {
      reg = _tables[table.id] = { table: table, sort: null };
      table.addEventListener("click", function (e) {
        var th = e.target.closest ? e.target.closest("th[data-sort]") : null;
        if (!th || !table.contains(th)) return;
        var key = th.getAttribute("data-sort"), type = th.getAttribute("data-type") || "str";
        if (reg.sort && reg.sort.key === key) {
          reg.sort = { key: key, dir: reg.sort.dir === "asc" ? "desc" : "asc", type: type };
        } else {
          reg.sort = { key: key, dir: type === "num" ? "desc" : "asc", type: type };
        }
        _drawTable(reg);
      });
    }
    reg.rows = rows;
    reg.build = build;
    _drawTable(reg);
  }

  // ── table builders ────────────────────────────────────────────────────
  var FAV_COLS = [
    { label: "No." },
    { label: "Ticker", key: "key", type: "str", cls: "l" },
    { label: "Quantity", key: "quantity", type: "num" },
    { label: "Amount (Rs)", key: "amount", type: "num" },
    { label: "Average Price (Rs)", key: "avg_price", type: "num" },
    { label: "% Of Desk Flow", key: "pct", type: "num" }
  ];
  function buildFavTable(table, rows) {
    if (!rows || !rows.length) { empty(table, 6); return; }
    var maxPct = rows.reduce(function (m, r) { return Math.max(m, r.pct || 0); }, 0) || 1;
    var body = rows.map(function (r, i) {
      var ratio = Math.max(0, Math.min(1, (r.pct || 0) / maxPct));
      var bar = "<span class='dsx-pctbar' style='width:calc((100% - 24px) * " + ratio.toFixed(3) + ")'></span>";
      return "<tr data-key='" + esc(r.key) + "'><td>" + (i + 1) + "</td><td class='l tkr'>" + esc(r.key) + "</td><td>" +
        fmtQty(r.quantity) + "</td><td>" + fmtRs(r.amount) + "</td><td>" +
        fmtPrice(r.avg_price) + "</td><td class='dsx-pctcell'>" + bar +
        "<span class='dsx-pctnum'>" + fmtPct(r.pct) + "</span></td></tr>";
    }).join("");
    table.innerHTML = sortableHead(table.id, FAV_COLS) + "<tbody>" + body + "</tbody>";
  }

  var BROKER_COLS = [
    { label: "Broker", key: "key", type: "str", cls: "l" },
    { label: "Quantity", key: "quantity", type: "num" },
    { label: "Amount (Rs)", key: "amount", type: "num" },
    { label: "Average Price (Rs)", key: "avg_price", type: "num" },
    { label: "% Of Total", key: "pct", type: "num" }
  ];
  function buildBrokerTable(table, rows) {
    if (!rows || !rows.length) { empty(table, 5); return; }
    var body = rows.map(function (r) {
      return "<tr>" + brokerCell(r.key) + "<td>" + fmtQty(r.quantity) + "</td><td>" +
        fmtRs(r.amount) + "</td><td>" + fmtPrice(r.avg_price) + "</td><td>" + fmtPct(r.pct) + "</td></tr>";
    }).join("");
    table.innerHTML = sortableHead(table.id, BROKER_COLS) + "<tbody>" + body + "</tbody>";
  }

  var HOLD_COLS = [
    { label: "Broker", key: "key", type: "str", cls: "l" },
    { label: "Net Qty", key: "quantity", type: "num" },
    { label: "Avg Buy (Rs)", key: "avg_buy", type: "num" },
    { label: "Avg Sell (Rs)", key: "avg_sell", type: "num" }
  ];
  function buildHoldTable(table, rows) {
    if (!rows || !rows.length) { empty(table, 4); return; }
    var body = rows.map(function (r) {
      var cls = r.quantity >= 0 ? "num-pos" : "num-neg";
      return "<tr>" + brokerCell(r.key) + "<td class='" + cls + "'>" + fmtQty(r.quantity) +
        "</td><td>" + fmtPrice(r.avg_buy) + "</td><td>" + fmtPrice(r.avg_sell) + "</td></tr>";
    }).join("");
    table.innerHTML = sortableHead(table.id, HOLD_COLS) + "<tbody>" + body + "</tbody>";
  }

  // Broker Flow Radar.
  var flowState = { dr: null };
  TABS.flowradar = {
    init: function () {
      flowState.dr = dateRange("flow", function () { TABS.flowradar.load(); });
    },
    load: function () {
      var t = el("flow-table");
      loading(t, 11);
      getJSON("flow-radar/", flowState.dr.params(), "flow-radar")
        .then(function (d) { showTable(t, d.rows || [], buildFlowTable); })
        .catch(function (err) { if (isAbort(err)) return; empty(t, 11, "Error"); });
    }
  };

  var FLOW_COLS = [
    { label: "S.N." },
    { label: "Broker No.", key: "broker", type: "num", cls: "l" },
    { label: "Broker Name", key: "broker_name", type: "str", cls: "l" },
    { label: "Buy Amount (Rs)", key: "buy_amount", type: "num" },
    { label: "Sell Amount (Rs)", key: "sell_amount", type: "num" },
    { label: "Total Amount (Rs)", key: "total_amount", type: "num" },
    { label: "Difference (Rs)", key: "difference", type: "num" },
    { label: "Matching Amount (Rs)", key: "matching_amount", type: "num" },
    { label: "Bias", key: "bias_pct", type: "num" },
    { label: "Match", key: "matching_pct", type: "num" },
    { label: "Stance", key: "stance", type: "str" }
  ];
  function buildFlowTable(table, rows) {
    if (!rows || !rows.length) { empty(table, 11); return; }
    var body = rows.map(function (r, i) {
      var diffCls = r.difference >= 0 ? "num-pos" : "num-neg";
      var stanceCls = r.stance === "Accumulating" ? "buy" : r.stance === "Distributing" ? "sell" : "flat";
      return "<tr><td>" + (i + 1) + "</td><td class='l tkr'>" + esc(r.broker) + "</td><td class='l'>" +
        esc(r.broker_name || "—") + "</td><td>" + fmtRs(r.buy_amount) + "</td><td>" +
        fmtRs(r.sell_amount) + "</td><td>" + fmtRs(r.total_amount) + "</td><td class='" + diffCls + "'>" +
        fmtSignedRs(r.difference) + "</td><td>" + fmtRs(r.matching_amount) + "</td><td class='" + diffCls + "'>" +
        fmtSignedPct(r.bias_pct) + "</td><td>" + fmtPct(r.matching_pct) + "</td><td>" +
        "<span class='dsx-tag " + stanceCls + "'>" + esc(r.stance) + "</span></td></tr>";
    }).join("");
    table.innerHTML = sortableHead(table.id, FLOW_COLS) + "<tbody>" + body + "</tbody>";
  }

  // ─────────────────────────────────────────────────────────────────────
  // TAB: A/D Radar — stock-first accumulation / distribution
  //
  // Inverse of every other tab: not "what did this broker do" but "is this
  // scrip being accumulated". The score is backtested; the evidence strip and
  // the horizon warning are rendered from the payload rather than hard-coded so
  // they cannot drift away from what the model actually measured.
  // ─────────────────────────────────────────────────────────────────────
  var adState = { dr: null, side: "accumulation", data: null };

  // "Candidates" throughout, deliberately: the score narrows where to look, it
  // does not establish that a campaign exists.
  var AD_TITLES = {
    accumulation: "ACCUMULATION CANDIDATES",
    quiet: "QUIET ACCUMULATION CANDIDATES — ABSORPTION WITHOUT MARK-UP",
    distribution: "DISTRIBUTION CANDIDATES",
    all: "ALL SCORED SCRIPS"
  };

  TABS.adradar = {
    init: function () {
      var sec = el("ad-sector");
      if (sec) {
        fillSectors(sec);
        // Land on Commercial Banks rather than the whole market. Selected by
        // value match, not by index: the sector list comes from the floorsheet
        // feed and its order is not guaranteed, so a hard-coded position would
        // silently pick the wrong sector the day the feed reorders. Falls back
        // to "All" if the label is ever absent.
        var want = "Commercial Banks";
        var has = [].some.call(sec.options, function (o) { return o.value === want; });
        sec.value = has ? want : "All";
        sec.addEventListener("change", function () { TABS.adradar.load(); });
      }
      // Default to 1 month, not the shared "Current Day": a single session
      // cannot show accumulation, and the service refuses anything under 5.
      // This MUST go through dateRange's third argument — setting the <select>
      // value directly only changes the visible label, because dateRange seeds
      // its own state from `defaultRange || "today"` and ignores the DOM.
      adState.dr = dateRange("ad", function () { TABS.adradar.load(); }, "1m");
      segGroup("ad-side", function (v) { adState.side = v; drawAd(); });
      var btn = el("ad-analyze");
      if (btn) btn.addEventListener("click", function () { TABS.adradar.load(); });
      var close = el("ad-detail-close");
      if (close) close.addEventListener("click", function () { el("ad-detail").hidden = true; });
      // Row click opens the broker breakdown for that scrip.
      var tbl = el("ad-table");
      if (tbl) tbl.addEventListener("click", function (e) {
        var tr = e.target.closest ? e.target.closest("tr[data-sym]") : null;
        if (tr) showAdDetail(tr.getAttribute("data-sym"));
      });
      initAdAsk();
    },
    load: function () {
      var t = el("ad-table");
      loading(t, 10);
      var p = adState.dr ? adState.dr.params() : { range: "1m" };
      var sec = el("ad-sector");
      if (sec && sec.value) p.sector = sec.value;
      getJSON("accumulation/", p, "ad-scan")
        .then(function (d) { adState.data = d; drawAd(); })
        .catch(function (err) {
          if (isAbort(err)) return;
          adState.data = null;
          empty(t, 10, "Error loading A/D scan");
        });
    }
  };

  // ── Ask the desk ──────────────────────────────────────────────────────────
  // Sends the question to the server, which answers from the SAME scan payload
  // this tab is showing. The model never sees the screen, so any number in the
  // answer can be checked against the table above.
  function csrfToken() {
    // The hidden input FIRST, not the cookie: this project sets
    // CSRF_COOKIE_HTTPONLY = True, so document.cookie never exposes csrftoken
    // and a cookie-first helper silently returns "" and 403s every POST.
    var i = document.querySelector('input[name="csrfmiddlewaretoken"]');
    if (i && i.value) return i.value;
    var m = document.cookie.match(/(?:^|;\s*)csrftoken=([^;]+)/);
    return m ? decodeURIComponent(m[1]) : "";
  }

  function askScopeLabel() {
    var sec = el("ad-sector");
    var d = adState.data || {};
    var bits = [];
    if (d.sessions) bits.push(d.sessions + " sessions");
    if (d.as_of) bits.push("to " + d.as_of);
    if (sec && sec.value && sec.value !== "All") bits.push(sec.value);
    return bits.length ? bits.join(" · ") : "current window";
  }

  function runAdAsk(question) {
    var out = el("ad-ask-out"), btn = el("ad-ask-go");
    if (!out || !question) return;
    out.hidden = false;
    out.innerHTML = "<span class='dsx-ad-ask-wait'>Reading this scan…</span>";
    if (btn) btn.disabled = true;

    var p = adState.dr ? adState.dr.params() : { range: "1m" };
    var sec = el("ad-sector");
    var body = {
      question: question,
      range: p.range || "1m",
      sector: sec && sec.value ? sec.value : "All"
    };
    if (p.start_date) body.start = p.start_date;
    if (p.end_date) body.end = p.end_date;

    fetch("/floorsheet/api/accumulation/ask/", {
      method: "POST",
      headers: { "Content-Type": "application/json", "X-CSRFToken": csrfToken() },
      credentials: "same-origin",
      body: JSON.stringify(body)
    })
      .then(function (r) { return r.json().catch(function () { return { ok: false, error: "Bad response from server." }; }); })
      .then(function (d) {
        if (btn) btn.disabled = false;
        var q = el("ad-ask-quota");
        if (q && typeof d.quota_remaining === "number") {
          q.textContent = d.quota_remaining + " questions left today";
        }
        if (!d.ok) {
          out.innerHTML = "<div class='dsx-ad-ask-err'>" + esc(d.error || "Could not answer.") + "</div>";
          return;
        }
        var g = d.grounding || {};
        out.innerHTML = d.answer_html +
          "<div class='dsx-ad-ask-src'>Answered only from this scan — " +
          esc(String(g.equities_scored || "?")) + " equities scored over " +
          esc(String(g.sessions || "?")) + " sessions to " + esc(String(g.as_of || "?")) +
          "; " + esc(String(g.rows_supplied || "?")) + " ranked rows supplied. " +
          "Model: " + esc(String(d.model || "")) + " via " + esc(String(d.provider || "")) +
          ". Check any figure against the table above.</div>";
      })
      .catch(function () {
        if (btn) btn.disabled = false;
        out.innerHTML = "<div class='dsx-ad-ask-err'>Could not reach the assistant.</div>";
      });
  }

  function initAdAsk() {
    var go = el("ad-ask-go"), q = el("ad-ask-q");
    if (!go || !q || go.dataset.ready) return;
    go.dataset.ready = "1";
    go.addEventListener("click", function () { runAdAsk(q.value.trim()); });
    q.addEventListener("keydown", function (e) {
      if (e.key === "Enter") { e.preventDefault(); runAdAsk(q.value.trim()); }
    });
    [].forEach.call(document.querySelectorAll(".dsx-ad-eg"), function (b) {
      b.addEventListener("click", function () {
        q.value = b.textContent.trim();
        runAdAsk(q.value);
      });
    });
    var scope = el("ad-ask-scope");
    if (scope) scope.textContent = askScopeLabel();
  }

  function adBandTag(r) {
    return "<span class='dsx-band " + esc(r.band) + "'>" + esc(r.band_label) + "</span>";
  }

  // Diverging bar: centre = market median, right = accumulation, left = supply.
  function adScoreBar(pct) {
    var p = Math.max(0, Math.min(100, pct == null ? 50 : pct));
    var half = Math.abs(p - 50);            // 0..50 from the centre
    var side = p >= 50 ? "pos" : "neg";
    var style = p >= 50
      ? "left:50%;width:" + half + "%"
      : "right:50%;width:" + half + "%";
    return "<span class='dsx-adbar'><i class='" + side + "' style='" + style + "'></i></span>";
  }

  var AD_COLS = [
    { label: "Ticker", key: "symbol", type: "str", cls: "l" },
    { label: "Sector", key: "sector", type: "str", cls: "l" },
    { label: "Reading", key: "percentile", type: "num" },
    { label: "Distribution ◂ Position ▸ Accumulation" },
    { label: "Score", key: "score", type: "num" },
    { label: "Absorbed", key: "absorb_pct", type: "num" },
    { label: "Buy Group", key: "group_n", type: "num" },
    { label: "Top Broker", key: "top1_pct", type: "num" },
    { label: "Sell Conc.", key: "sell_hhi", type: "num" },
    { label: "Price Δ", key: "price_chg_pct", type: "num" }
  ];

  // Symbols in the quiet subset for the CURRENT payload. Module-scoped because
  // buildAdTable is handed to showTable() and cannot take another argument.
  var adQuiet = {};

  function buildAdTable(table, rows) {
    if (!rows || !rows.length) { empty(table, 10, "No scrips in this band for the selected window"); return; }
    var body = rows.map(function (r) {
      var pxCls = (r.price_chg_pct || 0) >= 0 ? "num-pos" : "num-neg";
      return "<tr data-sym='" + esc(r.symbol) + "' class='dsx-adrow'>" +
        "<td class='l tkr' title='" + esc(r.name || r.symbol) + "'>" + esc(r.symbol) +
          (adQuiet[r.symbol] && adState.side !== "quiet"
            ? " <span class='dsx-quiet-tag' title='Also in Quiet Accumulation — absorbed without the price rising'>quiet</span>"
            : "") + "</td>" +
        "<td class='l dsx-ad-sec'>" + esc(r.sector || "—") + "</td>" +
        "<td>" + adBandTag(r) + "</td>" +
        "<td class='dsx-adbar-cell'>" + adScoreBar(r.percentile) + "</td>" +
        "<td class='" + (r.score >= 0 ? "num-pos" : "num-neg") + "'>" + (r.score || 0).toFixed(2) + "</td>" +
        "<td title='Top 5 net buyers as a share of all volume traded'>" + fmtPct(r.absorb_pct) + "</td>" +
        "<td title='Brokers doing half of the net buying — spread beats a single desk'>" + (r.group_n || 0) + "</td>" +
        "<td title='Share of net buying done by the single largest broker'>" + fmtPct(r.top1_pct) + "</td>" +
        "<td title='Herfindahl concentration of the selling side (10000 = one seller)'>" + fmtQty(r.sell_hhi) + "</td>" +
        "<td class='" + pxCls + "'>" + (r.price_chg_pct == null ? "—" : fmtSignedPct(r.price_chg_pct)) + "</td></tr>";
    }).join("");
    table.innerHTML = sortableHead(table.id, AD_COLS) + "<tbody>" + body + "</tbody>";
  }

  function drawAd() {
    var t = el("ad-table"), d = adState.data;
    if (!t) return;
    if (!d || !d.ok) {
      empty(t, 10, (d && d.reason) || "No data");
      if (el("ad-kpis")) el("ad-kpis").innerHTML = "";
      if (el("ad-evidence")) el("ad-evidence").innerHTML = "";
      if (el("ad-universe")) el("ad-universe").textContent = (d && d.reason) || "";
      return;
    }
    var rows = adState.side === "accumulation" ? (d.accumulation || [])
      : adState.side === "distribution" ? (d.distribution || [])
      : adState.side === "quiet" ? (d.quiet_accumulation || [])
      : (d.rows || []);

    var title = el("ad-table-title");
    if (title) title.textContent = AD_TITLES[adState.side] || AD_TITLES.accumulation;
    var sub = el("ad-sub");
    if (sub) {
      // Quiet Accumulation is a SUBSET of Accumulation - the same scrips,
      // filtered to those whose price did not rise. The four pills sit side by
      // side and read as mutually exclusive buckets, so the overlap looks like
      // a bug unless the relationship is stated here.
      var window_txt = d.days + " sessions" + (d.as_of ? " to " + d.as_of : "");
      if (adState.side === "quiet") {
        var nAcc = (d.counts || {}).accumulation || 0;
        var nQ = (d.quiet_accumulation || []).length;
        sub.textContent = nQ + " of " + nAcc + " accumulation candidates · price did not rise · " + window_txt;
      } else {
        sub.textContent = window_txt;
      }
    }

    var c = d.counts || {};
    var k = el("ad-kpis");
    if (k) {
      k.innerHTML = [
        kpi("Scrips Scored", nf(c.scored || 0), "ordinary equities passing the liquidity floor"),
        kpi("Accumulating", nf(c.accumulation || 0), "top quintile of the cross-section"),
        kpi("Distributing", nf(c.distribution || 0), "bottom quintile of the cross-section"),
        kpi("Window", d.days + " sessions", "flow measured over this many trading days")
      ].join("");
    }

    var ev = el("ad-evidence"), b = d.backtest || {};
    if (ev && b.spread_pct != null) {
      // The headline is the OUT-OF-SAMPLE result. Showing the in-sample figure
      // as the headline (which this once did) presents a selection artifact as
      // an edge; the in-sample number is kept only as the contrast that makes
      // the decay visible.
      var ins = b.in_sample || {};
      ev.innerHTML =
        (d.window_warning
          ? "<div class='dsx-ev-alert'>" + esc(d.window_warning) + "</div>" : "") +
        // The "not predictive" fact stays on the page but as one quiet line
        // rather than a red banner: it is a standing property of the model, not
        // an incident, and a permanent alert bar trains people to ignore alerts.
        // The numbers underneath still spell it out, and `title` carries the
        // full sentence on hover.
        (b.validated === false
          ? "<div class='dsx-ev-flag' title='" +
            esc("Not a validated predictive signal. In-sample it scored +" +
                (ins.spread_pct || "") + "%; on held-out data +" + b.spread_pct +
                "% (p = " + b.p_value + "), no better than chance. Use it to describe " +
                "who is absorbing, not to forecast price.") + "'>" +
            "Descriptive · not predictive" +
            "<a href='/floorsheet/accumulation/sop/#backtest' target='_blank' " +
            "rel='noopener'>why?</a></div>" : "") +
        // Plain words first. The statistics are all still here, one click away —
        // leading with t/p/n meant the reader met four unfamiliar symbols before
        // reaching anything they could act on.
        "<div class='dsx-ev-plain'>" +
          "<b>Does it predict what happens next? Barely.</b> " +
          "We built the score using 2023–24, then tested it on 2025–26 — years it " +
          "had never seen. Almost all of the apparent advantage vanished." +
        "</div>" +
        "<div class='dsx-ev-compare'>" +
          "<div class='dsx-ev-cmp'><span>On the years it was built from</span>" +
            "<b class='num-pos'>+" + ins.spread_pct + "%</b><i>looks impressive</i></div>" +
          "<div class='dsx-ev-arrow'>→</div>" +
          "<div class='dsx-ev-cmp'><span>On new years it had never seen</span>" +
            "<b>+" + b.spread_pct + "%</b><i>no better than chance</i></div>" +
        "</div>" +
        "<div class='dsx-ev-warn'>" + esc(b.note || "") + "</div>" +
        "<details class='dsx-ev-stats'><summary>Show the statistics</summary>" +
          "<div class='dsx-ev-head'>Held-out test — " + esc(b.sample || "") + "</div>" +
          "<div class='dsx-ev-grid'>" +
          evCell("Out-of-sample spread", "+" + b.spread_pct + "%",
                 "t = " + b.t_stat + " · p = " + b.p_value + " · n = " + nf(b.n)) +
          evCell("In-sample spread", "+" + ins.spread_pct + "%",
                 "t = " + ins.t_stat + " on " + esc(ins.period || "") + " — the period the features were chosen on") +
          evCell("Decay", Math.round(100 - 100 * b.spread_pct / (ins.spread_pct || 1)) + "%",
                 "how much of the in-sample edge disappears on unseen data") +
          evCell("Significant?", (b.p_value != null && b.p_value < 0.05) ? "yes" : "no",
                 "p = " + b.p_value + " at the " + b.horizon_sessions + "-session horizon") +
          "</div>" +
          "<div class='dsx-ev-horizons'>" +
            (b.horizons || []).map(function (h) {
              return "<span><b>" + h.sessions + " sessions</b> out-of-sample +" + h.spread_pct +
                "% · t " + h.t_stat + " · p " + h.p_value + "</span>";
            }).join("") +
          "</div>" +
        "</details>";
    }

    var uni = el("ad-universe");
    if (uni) {
      // If a quintile is larger than the server's cap, the table is showing a
      // subset while the KPI above reports the full count. Say so.
      var tr = d.truncated || {}, cut = 0;
      if (adState.side === "accumulation") cut = tr.accumulation || 0;
      else if (adState.side === "distribution") cut = tr.distribution || 0;
      uni.textContent = (cut
        ? "Showing the strongest " + tr.limit + " of " + (rows.length + cut) +
          " in this band. "
        : "") + (d.universe_note || "");
    }
    adQuiet = {};
    (d.quiet_accumulation || []).forEach(function (q) { adQuiet[q.symbol] = 1; });
    showTable(t, rows, buildAdTable);
  }

  // Same markup the other KPI strips emit, so these tiles inherit the desk's
  // existing styling rather than introducing a parallel look.
  function kpi(label, value, tip) {
    return "<div class='dsx-kpi' title='" + esc(tip || "") + "'><span class='dsx-kpi-label'>" +
      esc(label) + "</span><span class='dsx-kpi-val'>" + esc(value) + "</span>" +
      "<span class='dsx-kpi-sub'>" + esc(tip || "") + "</span></div>";
  }
  function evCell(label, value, tip) {
    var cls = String(value).charAt(0) === "-" ? "num-neg" : "num-pos";
    return "<div class='dsx-ev-cell'><span class='dsx-ev-label'>" + esc(label) + "</span>" +
      "<span class='dsx-ev-value " + cls + "'>" + esc(value) + "</span>" +
      "<span class='dsx-ev-tip'>" + esc(tip) + "</span></div>";
  }

  function brokerChip(x, side) {
    var nm = brokerName(x.broker);
    return "<span class='dsx-chip " + side + "' title='" + esc(nm ? "#" + x.broker + " — " + nm : "Broker " + x.broker) +
      "'>#" + esc(x.broker) + " <b>" + fmtQty(Math.abs(x.net)) + "</b></span>";
  }

  function showAdDetail(symbol) {
    var box = el("ad-detail"), body = el("ad-detail-body");
    if (!box || !body) return;
    box.hidden = false;
    body.innerHTML = "<div class='dsx-loading'>Loading…</div>";
    el("ad-detail-title").textContent = symbol + " — FLOW DETAIL";
    var p = adState.dr ? adState.dr.params() : { range: "1m" };
    p.symbol = symbol;
    getJSON("accumulation/", p, "ad-detail")
      .then(function (d) {
        if (!d.ok) { body.innerHTML = "<div class='dsx-empty'>" + esc(d.reason || "Not scored") + "</div>"; return; }
        var r = d.row || {};
        body.innerHTML =
          "<div class='dsx-ad-detail-top'>" + adBandTag(r) +
            "<span class='dsx-ad-detail-score'>score " + (r.score || 0).toFixed(2) +
            " · " + (r.percentile || 0).toFixed(0) + "th percentile of " + nf(d.universe) + " scrips</span></div>" +
          "<p class='dsx-ad-reading'>" + esc(d.reading || "") + "</p>" +
          "<div class='dsx-ad-sides'>" +
            "<div><h4>Net buyers</h4><div class='dsx-chips'>" +
              (r.top_buyers || []).map(function (x) { return brokerChip(x, "buy"); }).join("") +
              "</div><span class='dsx-ad-meta'>" + (r.buyers_n || 0) + " brokers net long · half the buying done by " +
              (r.group_n || 0) + "</span></div>" +
            "<div><h4>Net sellers</h4><div class='dsx-chips'>" +
              (r.top_sellers || []).map(function (x) { return brokerChip(x, "sell"); }).join("") +
              "</div><span class='dsx-ad-meta'>" + (r.sellers_n || 0) + " brokers net short · concentration " +
              fmtQty(r.sell_hhi) + "</span></div>" +
          "</div>";
      })
      .catch(function (err) {
        if (isAbort(err)) return;
        body.innerHTML = "<div class='dsx-empty'>Error loading detail</div>";
      });
  }

  // ─────────────────────────────────────────────────────────────────────
  // TAB: Broker Favorites
  // ─────────────────────────────────────────────────────────────────────
  var favState = { brokers: [], dr: null, persistSide: "all", persistLb: "1m", persistData: null, persistSort: null };

  TABS.favorites = {
    init: function () {
      // Open on broker 42 (Sani Securities) so the desk shows data immediately
      // instead of an empty "Select a broker" prompt. Falls back to no selection
      // if 42 isn't in the reference list (e.g. delisted), so it degrades cleanly.
      favState.brokers = (META.brokers || []).map(String).indexOf("42") !== -1 ? ["42"] : [];
      buildBrokerMulti("fav", favState, function () { TABS.favorites.load(); });
      favState.dr = dateRange("fav", function () { TABS.favorites.load(); });
      // Sector filter for the persistence card (reloads just that card).
      var psec = el("fav-persist-sector");
      if (psec) {
        fillSectors(psec);
        psec.addEventListener("change", function () { TABS.favorites.loadPersistence(); });
      }
      // Lookback (1W / 1M / 3M) — server-backed, refetches the persistence card.
      segGroup("fav-persist-lb", function (v) { favState.persistLb = v; TABS.favorites.loadPersistence(); });
      // Side filter (All / Accumulating / Distributing) — client-side, no refetch.
      segGroup("fav-persist-side", function (v) { favState.persistSide = v; drawPersistence(); });
      // Click column titles to sort the persistence rows.
      var cols = el("fav-ad-cols");
      if (cols) cols.addEventListener("click", function (e) {
        var h = e.target.closest ? e.target.closest("[data-sort]") : null;
        if (!h || !cols.contains(h)) return;
        var key = h.getAttribute("data-sort"), type = h.getAttribute("data-type") || "num";
        var cur = favState.persistSort;
        favState.persistSort = (cur && cur.key === key)
          ? { key: key, dir: cur.dir === "asc" ? "desc" : "asc", type: type }
          : { key: key, dir: type === "num" ? "desc" : "asc", type: type };
        var dir = favState.persistSort.dir;
        cols.querySelectorAll("[data-sort]").forEach(function (s) { s.classList.remove("sorted-asc", "sorted-desc"); });
        cols.querySelectorAll('[data-sort="' + key + '"]').forEach(function (s) {
          s.classList.add(dir === "asc" ? "sorted-asc" : "sorted-desc");
        });
        drawPersistence();
      });
    },
    load: function () {
      if (!favState.brokers.length) {
        empty(el("fav-buy"), 6, "Select a broker"); empty(el("fav-sell"), 6, "Select a broker");
        renderFavKpis(null);
        if (el("fav-ad")) el("fav-ad").innerHTML = '<div class="dsx-empty">Select a broker</div>';
        if (el("fav-ad-sub")) el("fav-ad-sub").textContent = "";
        return;
      }
      loading(el("fav-buy"), 6); loading(el("fav-sell"), 6);
      var params = assign({ brokers: favState.brokers.join(",") }, favState.dr.params());
      getJSON("favorites/", params, "favorites")
        .then(function (d) {
          renderFavKpis(d);
          showTable(el("fav-buy"), d.buy, buildFavTable);
          showTable(el("fav-sell"), d.sell, buildFavTable);
        })
        .catch(function (err) { if (isAbort(err)) return; empty(el("fav-buy"), 6, "Error"); empty(el("fav-sell"), 6, "Error"); });
      TABS.favorites.loadPersistence();
    },
    loadPersistence: function () {
      var box = el("fav-ad");
      if (!box) return;
      box.innerHTML = '<div class="dsx-loading">Loading…</div>';
      var psec = el("fav-persist-sector");
      getJSON("persistence/", {
        brokers: favState.brokers.join(","), lookback: favState.persistLb || "1m",
        sector: psec ? psec.value : "All"
      }, "fav-persist")
        .then(renderPersistence)
        .catch(function (err) { if (isAbort(err)) return; box.innerHTML = '<div class="dsx-empty">Error</div>'; });
    }
  };

  // KPI strip: the selected desk's stance, all derived from the favorites/
  // response (no extra request). Buy/sell turnover, net flow, breadth, and the
  // single most-concentrated position.
  function renderFavKpis(d) {
    var box = el("fav-kpis");
    if (!box) return;
    if (!d || (!((d.buy || []).length) && !((d.sell || []).length))) { box.innerHTML = ""; return; }
    var buy = d.buy || [], sell = d.sell || [], summary = d.summary || {};
    var sum = function (rows, k) { return rows.reduce(function (s, r) { return s + (r[k] || 0); }, 0); };
    var buyAmt = summary.buy_amount == null ? sum(buy, "amount") : summary.buy_amount;
    var sellAmt = summary.sell_amount == null ? sum(sell, "amount") : summary.sell_amount;
    var net = buyAmt - sellAmt;
    var stocks = {};
    buy.forEach(function (r) { stocks[r.key] = 1; });
    sell.forEach(function (r) { stocks[r.key] = 1; });
    var top = { pct: 0, key: "—", side: "" };
    buy.forEach(function (r) { if ((r.pct || 0) > top.pct) top = { pct: r.pct, key: r.key, side: "buy" }; });
    sell.forEach(function (r) { if ((r.pct || 0) > top.pct) top = { pct: r.pct, key: r.key, side: "sell" }; });

    function tile(label, val, sub, cls) {
      return "<div class='dsx-kpi'><span class='dsx-kpi-label'>" + esc(label) + "</span>" +
        "<span class='dsx-kpi-val " + (cls || "") + "'>" + val + "</span>" +
        "<span class='dsx-kpi-sub'>" + esc(sub || "") + "</span></div>";
    }
    box.innerHTML =
      tile("Buy Turnover", fmtRsCompact(buyAmt), (summary.buy_stocks == null ? buy.length : summary.buy_stocks) + " stocks", "num-pos") +
      tile("Sell Turnover", fmtRsCompact(sellAmt), (summary.sell_stocks == null ? sell.length : summary.sell_stocks) + " stocks", "num-neg") +
      tile("Net Flow", (net >= 0 ? "+" : "") + fmtRsCompact(net),
           net >= 0 ? "Net accumulating" : "Net distributing", net >= 0 ? "num-pos" : "num-neg") +
      tile("Stocks Touched", nf(summary.stocks_touched == null ? Object.keys(stocks).length : summary.stocks_touched), "buy ∪ sell side") +
      tile("Top Concentration", fmtPct(top.pct),
           top.key + " · " + (top.side === "sell" ? "sell" : "buy"), top.side === "sell" ? "num-neg" : "num-pos");
  }

  // Persistent Accumulation / Distribution: multi-session net per stock for the
  // selected desk, with a conviction streak (consecutive same-side sessions) and
  // an all-broker concentration read (HHI). Diverging bar = cumulative net qty.
  function renderPersistence(d) {
    favState.persistData = d || { rows: [] };
    drawPersistence();
  }
  function drawPersistence() {
    var box = el("fav-ad"), sub = el("fav-ad-sub");
    if (!box) return;
    var d = favState.persistData || { rows: [] };
    var side = favState.persistSide || "all";
    var all = d.rows || [];
    var rows = side === "all" ? all : all.filter(function (r) { return r.side === side; });
    var ps = favState.persistSort;
    if (ps) rows = sortRows(rows, ps.key, ps.dir, ps.type);
    if (sub) sub.textContent = d.days ? ("Last " + d.days + " sessions") : "";
    if (!rows.length) {
      box.innerHTML = "<div class='dsx-empty'>" +
        (all.length ? "No " + (side === "buy" ? "accumulating" : "distributing") + " names" : "No multi-day positions") +
        "</div>";
      return;
    }
    var maxAbs = rows.reduce(function (m, r) { return Math.max(m, Math.abs(r.cum_net || 0)); }, 0) || 1;
    box.innerHTML = rows.map(function (r) {
      var pos = r.cum_net >= 0;
      var w = (50 * Math.abs(r.cum_net || 0) / maxAbs).toFixed(2);   // half-track %
      var fill = "<span class='dsx-ad-fill " + (pos ? "buy" : "sell") + "' style='width:" + w + "%'></span>";
      var arrow = r.side === "buy" ? "▲" : r.side === "sell" ? "▼" : "–";
      var streakCls = r.side === "buy" ? "buy" : r.side === "sell" ? "sell" : "flat";
      var streak = "<span class='dsx-streak " + streakCls + "' title='" + r.buy_days + " buy / " +
        r.sell_days + " sell sessions of " + r.active_days + " active'>" + arrow + " " + r.streak + "d</span>";
      var dom = r.dominant ? ("Broker " + r.dominant.broker + " " + fmtPct(r.dominant.pct)) : "—";
      var hhi = "<span class='dsx-hhi risk-" + (r.risk || "low") + "' title='Concentration (HHI) " +
        nf(r.hhi) + " · dominant " + dom + "'>" + nf(r.hhi) + "</span>";
      return "<div class='dsx-ad-row' data-key='" + esc(r.symbol) + "'>" +
        "<span class='dsx-ad-sym'>" + esc(r.symbol) + "</span>" + streak +
        "<span class='dsx-ad-track'>" + fill + "</span>" +
        "<span class='dsx-ad-net " + (pos ? "num-pos" : "num-neg") + "'>" +
          (pos ? "+" : "") + fmtQty(r.cum_net) + "</span>" + hhi + "</div>";
    }).join("");
  }

  // ── research-desk signals (divergence / sector / breadth / two-sided) ──
  function sigEmpty(id, msg) {
    var box = el(id); if (box) box.innerHTML = "<div class='dsx-empty'>" + (msg || "Nothing flagged") + "</div>";
  }
  function renderSignals(d) {
    d = d || {};
    renderDivergence(d.divergence || []);
    renderSectorRotation(d.sectors || []);
    renderBreadth(d.breadth || []);
    renderTwoSided(d.two_sided || []);
  }

  // Price–flow divergence: desk net flow disagrees with the window price move.
  function renderDivergence(rows) {
    if (!rows.length) { sigEmpty("sig-div", "No divergences"); return; }
    el("sig-div").innerHTML = "<table class='dsx-sig-tbl'>" + rows.map(function (r) {
      var accum = r.type === "accum_weak";
      var tag = accum
        ? "<span class='dsx-tag buy' title='Net buying while price fell'>ACCUM ↓px</span>"
        : "<span class='dsx-tag sell' title='Net selling into a rising price'>DISTRIB ↑px</span>";
      var pcCls = r.price_chg >= 0 ? "num-pos" : "num-neg";
      var netCls = r.net >= 0 ? "num-pos" : "num-neg";
      return "<tr><td class='l tkr'>" + esc(r.symbol) + "</td><td class='l'>" + tag + "</td>" +
        "<td class='" + netCls + "'>" + (r.net >= 0 ? "+" : "") + fmtQty(r.net) + "</td>" +
        "<td class='" + pcCls + "'>" + (r.price_chg >= 0 ? "+" : "") + fmtPct(r.price_chg) + "</td></tr>";
    }).join("") + "</table>";
  }

  // Sector rotation: desk net quantity by sector (diverging bars).
  function renderSectorRotation(rows) {
    if (!rows.length) { sigEmpty("sig-sec", "No sector flow"); return; }
    var maxAbs = rows.reduce(function (m, r) { return Math.max(m, Math.abs(r.net || 0)); }, 0) || 1;
    el("sig-sec").innerHTML = rows.map(function (r) {
      var pos = r.net >= 0;
      var w = (50 * Math.abs(r.net || 0) / maxAbs).toFixed(2);
      var fill = "<span class='dsx-ad-fill " + (pos ? "buy" : "sell") + "' style='width:" + w + "%'></span>";
      return "<div class='dsx-sec-row'><span class='dsx-sec-name' title='" + esc(r.sector) + "'>" + esc(r.sector) + "</span>" +
        "<span class='dsx-ad-track'>" + fill + "</span>" +
        "<span class='dsx-ad-net " + (pos ? "num-pos" : "num-neg") + "'>" + (pos ? "+" : "") + fmtQty(r.net) + "</span></div>";
    }).join("");
  }

  // Buyer/seller breadth: distinct brokers net-buying vs net-selling (all brokers).
  function renderBreadth(rows) {
    if (!rows.length) { sigEmpty("sig-breadth"); return; }
    el("sig-breadth").innerHTML = "<table class='dsx-sig-tbl'>" +
      "<thead><tr><th class='l'>Ticker</th><th>Buyers</th><th>Sellers</th><th>Net</th></tr></thead>" +
      rows.map(function (r) {
        var tot = (r.buyers + r.sellers) || 1;
        var bw = (100 * r.buyers / tot).toFixed(1);
        var split = "<span class='dsx-split'><i class='buy' style='width:" + bw + "%'></i></span>";
        var netCls = r.net > 0 ? "num-pos" : r.net < 0 ? "num-neg" : "";
        return "<tr><td class='l tkr'>" + esc(r.symbol) + "</td><td class='num-pos'>" + r.buyers +
          "</td><td class='num-neg'>" + r.sellers + "</td><td class='" + netCls + "'>" +
          (r.net > 0 ? "+" : "") + r.net + " " + split + "</td></tr>";
      }).join("") + "</table>";
  }

  // Two-sided activity: desk both bought and sold the same name (churn %).
  function renderTwoSided(rows) {
    if (!rows.length) { sigEmpty("sig-two", "No two-sided names"); return; }
    el("sig-two").innerHTML = "<table class='dsx-sig-tbl'>" +
      "<thead><tr><th class='l'>Ticker</th><th>Buy</th><th>Sell</th><th>Churn</th></tr></thead>" +
      rows.map(function (r) {
        return "<tr><td class='l tkr'>" + esc(r.symbol) + "</td><td class='num-pos'>" + fmtQty(r.buy) +
          "</td><td class='num-neg'>" + fmtQty(r.sell) + "</td><td><span class='dsx-churn' title='" +
          fmtQty(r.two_sided) + " shares two-sided'>" + r.churn.toFixed(0) + "%</span></td></tr>";
      }).join("") + "</table>";
  }

  // ─────────────────────────────────────────────────────────────────────
  // TAB: Signal Desk (the four research-desk signals, own broker selection)
  // ─────────────────────────────────────────────────────────────────────
  var sigState = { brokers: [], dr: null };
  var SIG_IDS = ["sig-div", "sig-sec", "sig-breadth", "sig-two"];
  function sigSetAll(html) { SIG_IDS.forEach(function (id) { if (el(id)) el(id).innerHTML = html; }); }
  TABS.signals = {
    init: function () {
      // Open on broker 42 (Sani Securities) so the desk shows signals immediately
      // instead of an empty "Select a broker" prompt. Falls back to no selection
      // if 42 isn't in the reference list, so it degrades cleanly.
      sigState.brokers = (META.brokers || []).map(String).indexOf("42") !== -1 ? ["42"] : [];
      buildBrokerMulti("sig", sigState, function () { TABS.signals.load(); });
      // Signals are inherently multi-day; default to Last 1 Month so Price–Flow
      // Divergence (needs ≥2 sessions) isn't empty on open.
      sigState.dr = dateRange("sig", function () { TABS.signals.load(); }, "1m");
    },
    load: function () {
      if (!sigState.brokers.length) { sigSetAll('<div class="dsx-empty">Select a broker</div>'); return; }
      sigSetAll('<div class="dsx-loading">Loading…</div>');
      getJSON("signals/", assign({ brokers: sigState.brokers.join(",") }, sigState.dr.params()), "signals")
        .then(renderSignals)
        .catch(function (err) { if (isAbort(err)) return; sigSetAll('<div class="dsx-empty">Error</div>'); });
    }
  };

  // Multi-select broker checklist (button + searchable checkbox menu). `prefix`
  // namespaces the element ids (fav-/sig-) and `state.brokers` holds the picks,
  // so each tab gets its own independent selector.
  function buildBrokerMulti(prefix, state, onChange) {
    var btn = el(prefix + "-broker-btn"), menu = el(prefix + "-broker-menu"),
        list = el(prefix + "-broker-list"), search = el(prefix + "-broker-search");
    if (!btn || !menu || !list) return;

    function syncLabel() {
      var n = state.brokers.length;
      var one = n === 1 ? (brokerName(state.brokers[0])
        ? "Broker " + state.brokers[0] + " — " + brokerName(state.brokers[0])
        : "Broker " + state.brokers[0]) : "";
      btn.textContent = (n === 0 ? "Select brokers"
        : n === 1 ? one
        : n + " brokers selected") + " ▾";
    }
    // Removable "selected chips" strip, created once just below the button so the
    // current desk is always visible without opening the menu. A handful of picks
    // render as individual removable chips; beyond CHIP_CAP they collapse to one
    // summary pill ("All N brokers" / "N brokers selected") whose × clears the
    // whole selection — so "Select all" shows a tidy single pill, not a wall of
    // 100 chips.
    var CHIP_CAP = 12;
    var chipsEl = btn.parentNode.querySelector(".dsx-multi-chips");
    if (!chipsEl) {
      chipsEl = document.createElement("div");
      chipsEl.className = "dsx-multi-chips";
      btn.parentNode.insertBefore(chipsEl, btn.nextSibling);
    }
    function deselect(bs) {
      state.brokers = state.brokers.filter(function (x) { return x !== bs; });
      commit(); render(search.value.trim()); onChange();
    }
    function clearAll() {
      state.brokers = [];
      commit(); render(search.value.trim()); onChange();
    }
    function makeChip(label, title, ariaX, onX, summary) {
      var chip = document.createElement("span");
      chip.className = "dsx-sel-chip" + (summary ? " summary" : "");
      if (title) chip.title = title;
      var bold = document.createElement("b");
      bold.textContent = label;
      var remove = document.createElement("button");
      remove.type = "button";
      remove.className = "dsx-sel-x";
      remove.setAttribute("aria-label", ariaX);
      remove.textContent = "×";
      chip.appendChild(bold);
      chip.appendChild(remove);
      remove.addEventListener("click", function (e) {
        e.stopPropagation(); onX();
      });
      return chip;
    }
    function renderChips() {
      chipsEl.innerHTML = "";
      var n = state.brokers.length;
      if (!n) return;
      var total = (META.brokers || []).length;
      if (n > CHIP_CAP) {
        var label = (n === total ? "All " + n + " brokers" : n + " brokers selected");
        chipsEl.appendChild(makeChip(label, "Clear all selected brokers",
          "Clear all selected brokers", clearAll, true));
        return;
      }
      state.brokers.forEach(function (bs) {
        var nm = brokerName(bs);
        chipsEl.appendChild(makeChip(bs, nm ? bs + " — " + nm : "",
          "Remove broker " + bs, function () { deselect(bs); }, false));
      });
    }
    // Live "N of M selected" line, created once just above the chip grid.
    var countEl = menu.querySelector(".dsx-multi-count");
    if (!countEl) {
      countEl = document.createElement("div");
      countEl.className = "dsx-multi-count";
      list.parentNode.insertBefore(countEl, list);
    }
    function updateCount() {
      countEl.textContent = state.brokers.length + " of " + (META.brokers || []).length + " selected";
    }
    function commit() { syncLabel(); updateCount(); renderChips(); }
    function render(filter) {
      list.innerHTML = "";
      var shown = 0;
      var f = (filter || "").toLowerCase();
      (META.brokers || []).forEach(function (b) {
        var bs = String(b);
        var nm = brokerName(bs);
        if (f && bs.indexOf(f) === -1 && nm.toLowerCase().indexOf(f) === -1) return;
        shown++;
        var on = state.brokers.indexOf(bs) !== -1;
        var chip = document.createElement("button");
        chip.type = "button";
        chip.className = "dsx-broker-chip" + (on ? " on" : "");
        chip.textContent = bs;
        if (nm) chip.title = bs + " — " + nm;
        chip.setAttribute("aria-pressed", on ? "true" : "false");
        chip.addEventListener("click", function () {
          var idx = state.brokers.indexOf(bs);
          if (idx === -1) state.brokers.push(bs);
          else state.brokers = state.brokers.filter(function (x) { return x !== bs; });
          var sel = state.brokers.indexOf(bs) !== -1;
          chip.classList.toggle("on", sel);
          chip.setAttribute("aria-pressed", sel ? "true" : "false");
          commit(); onChange();
        });
        list.appendChild(chip);
      });
      if (!shown) list.innerHTML = "<div class='dsx-multi-none'>No match</div>";
      updateCount();
    }

    btn.addEventListener("click", function (e) {
      e.stopPropagation();
      menu.hidden = !menu.hidden;
      if (!menu.hidden) { render(search.value.trim()); search.focus(); }
    });
    menu.addEventListener("click", function (e) { e.stopPropagation(); });
    document.addEventListener("click", function () { menu.hidden = true; });
    search.addEventListener("input", function () { render(search.value.trim()); });
    menu.querySelectorAll(".dsx-multi-actions button").forEach(function (b) {
      b.addEventListener("click", function () {
        if (b.dataset.act === "all") {
          state.brokers = (META.brokers || []).map(String);
        } else {
          state.brokers = [];
        }
        render(search.value.trim()); commit(); onChange();
      });
    });

    commit();
  }

  // ─────────────────────────────────────────────────────────────────────
  // TAB: Stock Wise Details
  // ─────────────────────────────────────────────────────────────────────
  var swState = { symbol: null, dr: null };
  TABS.stockwise = {
    init: function () {
      fillSymbols(el("sw-symbol"));
      symbolCombo(el("sw-symbol"));
      swState.symbol = el("sw-symbol").value || ((META.symbols || [])[0] || {}).symbol;
      el("sw-symbol").addEventListener("change", function () { swState.symbol = this.value; TABS.stockwise.load(); });
      swState.dr = dateRange("sw", function () { TABS.stockwise.load(); });
    },
    load: function () {
      if (!swState.symbol) { empty(el("sw-buy"), 5, "No symbols"); return; }
      loading(el("sw-buy"), 5); loading(el("sw-sell"), 5); loading(el("sw-hold"), 4);
      getJSON("stockwise/", assign({ symbol: swState.symbol }, swState.dr.params()), "stockwise")
        .then(function (d) {
          showTable(el("sw-buy"), d.buy, buildBrokerTable);
          showTable(el("sw-sell"), d.sell, buildBrokerTable);
          showTable(el("sw-hold"), d.holdings, buildHoldTable);
        })
        .catch(function (err) { if (isAbort(err)) return; empty(el("sw-buy"), 5, "Error"); empty(el("sw-sell"), 5, "Error"); empty(el("sw-hold"), 4, "Error"); });
    }
  };

  // ─── Broker Flow Map ─────────────────────────────────────────────────
  // Sellers (left) → buyers (right) for ONE stock in ONE time window. The
  // floorsheet carries both counterparties per trade, so grouping by
  // (seller, buyer) yields the real transfer of shares between desks; ribbon
  // width is the value moved. Time filtering is the study: the strip shows
  // where the session's volume sat, and clicking/dragging it sets the window.
  var fmState = { symbol: null, date: null, from: "", to: "", data: null, focus: null,
                  range: "today", start: "", end: "" };

  var SVG_NS = "http://www.w3.org/2000/svg";
  function svgEl(tag, attrs) {
    var e = document.createElementNS(SVG_NS, tag);
    for (var k in attrs) if (attrs.hasOwnProperty(k)) e.setAttribute(k, attrs[k]);
    return e;
  }
  /* ── Categorical palette ───────────────────────────────────────────────
   * A fixed 8-hue categorical set, not generated hues. Hashing a broker
   * number to an arbitrary HSL angle (the first cut) produced clashing,
   * muddy neighbours and pairs no colourblind reader could separate.
   * These eight are validated: worst adjacent CVD deltaE 9.1 light / 8.4
   * dark (>=8 target) and worst normal-vision deltaE 19.6 / 19.3 (>=15
   * floor), measured against this page's own surfaces (#ffffff / #07142e).
   * Aqua, yellow and magenta sit under 3:1 on white — permitted here because
   * every node carries a direct label and the flows table repeats the data.
   * "Other" is deliberately a neutral grey: it is a pool, not an identity. */
  var CAT_LIGHT = ["#2a78d6", "#eb6834", "#1baf7a", "#eda100",
                   "#e87ba4", "#008300", "#4a3aa7", "#e34948"];
  var CAT_DARK  = ["#3987e5", "#d95926", "#199e70", "#c98500",
                   "#d55181", "#008300", "#9085e9", "#e66767"];
  var OTHER_LIGHT = "#8b97a6", OTHER_DARK = "#6b7684";

  function isDarkTheme() {
    var t = document.documentElement.getAttribute("data-theme");
    if (t === "light") return false;
    if (t === "dark") return true;
    return !(window.matchMedia && window.matchMedia("(prefers-color-scheme: light)").matches);
  }

  // Slots are allocated PER COLUMN. Both sides can show 8 desks each, so up to
  // 16 nodes compete for 8 hues — allocating from one shared pool would silently
  // hand two brokers in the SAME column the same colour, which is the one
  // ambiguity that actually misleads. Per-column allocation guarantees every
  // node in a column is distinct; a hue repeating across the gap is unambiguous
  // because the columns are far apart and every node is labelled.
  // A broker's preferred slot comes from its own number, so a desk tends to keep
  // its colour across stocks and time windows, and a broker that both buys and
  // sells is given the same colour on both sides where the slot is free.
  var fmColors = { L: {}, R: {} };
  function assignColors(d) {
    var dark = isDarkTheme();
    var pal = dark ? CAT_DARK : CAT_LIGHT;
    var other = dark ? OTHER_DARK : OTHER_LIGHT;

    function prefSlot(k) {
      var num = parseInt(k, 10);
      return (isNaN(num) ? String(k).length : num) % pal.length;
    }
    // Allocate within one column: preferred slot, else the next free one.
    function allocate(nodes, pinned) {
      var used = {}, out = {};
      // Honour cross-side pins first so a two-way broker keeps one identity.
      (nodes || []).forEach(function (n) {
        if (n.key === "Other") return;
        var want = pinned && pinned[n.key];
        if (want == null) return;
        if (!used[want]) { used[want] = 1; out[n.key] = want; }
      });
      (nodes || []).forEach(function (n) {
        if (n.key === "Other" || out[n.key] != null) return;
        var slot = prefSlot(n.key);
        for (var i = 0; i < pal.length && used[slot]; i++) slot = (slot + 1) % pal.length;
        used[slot] = 1;
        out[n.key] = slot;
      });
      return out;
    }

    var lSlots = allocate(d.sellers, null);
    var rSlots = allocate(d.buyers, lSlots);

    function toHex(slots) {
      var m = {};
      Object.keys(slots).forEach(function (k) { m[k] = pal[slots[k]]; });
      m["Other"] = other;
      return m;
    }
    fmColors = { L: toHex(lSlots), R: toHex(rSlots) };
  }

  function brokerColor(key, side) {
    var m = fmColors[side === "R" ? "R" : "L"] || {};
    return m[key] || (isDarkTheme() ? OTHER_DARK : OTHER_LIGHT);
  }
  // Chart labels stay as bare broker numbers — firm names are long enough to
  // clip the column and crowd the ribbons. The full name lives in the tooltip
  // (fmFull) and in the flows table.
  function fmLabel(key) {
    return key === "Other" ? "Other" : String(key);
  }
  function fmFull(key) {
    if (key === "Other") return "Other brokers";
    var nm = brokerName(key);
    return nm ? "#" + key + " " + nm : "Broker " + key;
  }

  function renderFlowTiles(d) {
    var box = el("fm-tiles"); if (!box) return;
    var t = d.totals || {};
    function tile(k, v, s) {
      return '<div class="dsx-flow-tile"><div class="k">' + k + '</div><div class="v">' + v +
        '</div><div class="s">' + (s || "") + "</div></div>";
    }
    var win = (d.window && (d.window.from || d.window.to))
      ? ((d.window.from || d.session.first) + " – " + (d.window.to || d.session.last))
      : "full session " + d.session.first + " – " + d.session.last;
    box.innerHTML =
      tile("Turnover", fmtRsCompact(t.amount), win) +
      tile("Shares", fmtQty(t.quantity), (t.trades || 0) + " trades") +
      tile("Selling desks", (d.sellers || []).length, "top brokers + Other") +
      tile("Buying desks", (d.buyers || []).length, "top brokers + Other") +
      tile("Flows", (d.pairs || d.links || []).length, "seller → buyer pairs");
  }

  // Volume-by-time strip. Click one bar = that bucket; drag = a range.
  function renderTimeline(d) {
    var box = el("fm-timeline"); if (!box) return;
    var tl = d.timeline || [];
    if (!tl.length) { box.innerHTML = '<div class="dsx-tl-empty">No volume in this window.</div>'; return; }
    // Buckets are 5-minute clock slots for one session, trading days for a range.
    var byDate = d.bucket_unit === "date";
    var max = tl.reduce(function (m, b) { return Math.max(m, b.amount || 0); }, 0) || 1;
    box.innerHTML = tl.map(function (b) {
      var h = Math.max(3, Math.round(52 * (b.amount || 0) / max));
      var inWin = byDate
        ? (!fmState.start || b.start >= fmState.start) && (!fmState.end || b.start <= fmState.end)
        : (!fmState.from || b.start >= fmState.from.slice(0, 5)) &&
          (!fmState.to || b.start <= fmState.to.slice(0, 5));
      return '<div class="dsx-tl-bar' + (inWin ? " in" : "") + '" data-start="' + b.start +
        '" style="height:' + h + 'px" title="' + b.start + " · " + fmtRsCompact(b.amount) +
        " · " + b.trades + ' trades"></div>';
    }).join("");

    // Drag across bars to select a window; a plain click picks one bucket.
    var dragFrom = null;
    box.onmousedown = function (e) {
      var bar = e.target.closest(".dsx-tl-bar");
      if (bar) dragFrom = bar.dataset.start;
    };
    box.onmouseup = function (e) {
      var bar = e.target.closest(".dsx-tl-bar");
      if (!bar || !dragFrom) { dragFrom = null; return; }
      var a = dragFrom, b = bar.dataset.start;
      if (a > b) { var t2 = a; a = b; b = t2; }
      if (byDate) {
        // Dragging days narrows the DATE range and leaves the intraday window
        // alone, so "opening 30m" survives a zoom into a shorter span.
        fmState.range = "custom";
        fmState.start = a;
        fmState.end = b;
        el("fm-range").value = "custom";
        el("fm-start").value = a;
        el("fm-end").value = b;
        syncRangeInputs();
      } else {
        // End of the last selected bucket. Width comes from the response, so this
        // stays correct if the server's BUCKET_MINUTES ever changes.
        var width = (fmState.data && fmState.data.bucket_minutes) || 5;
        var tot = parseInt(b.slice(0, 2), 10) * 60 + parseInt(b.slice(3), 10) + width;
        var hh = Math.floor(tot / 60), mm = tot % 60;
        fmState.from = a + ":00";
        fmState.to = ("0" + hh).slice(-2) + ":" + ("0" + mm).slice(-2) + ":00";
        el("fm-from").value = fmState.from;
        el("fm-to").value = fmState.to;
        setPreset(null);
      }
      dragFrom = null;
      TABS.flowmap.load();
    };
  }

  // `override` (playback) supplies the links accumulated so far. Node geometry
  // always comes from the full-session totals so the columns hold still while
  // the ribbons grow — a per-frame layout would make brokers jump every step.
  // The map is BUILT once per dataset and then UPDATED in place for playback.
  // Re-creating the SVG every frame (the first cut of this) meant CSS could
  // never transition anything, so the animation read as a static picture.
  var fmSvg = null;   // { geom, ribbons: {key: path}, bars: {side|key: rect}, order: [] }

  function sankeyGeometry(d) {
    var svg = el("fm-svg");
    var sellers = d.sellers || [], buyers = d.buyers || [];
    var W = svg.clientWidth || svg.parentNode.clientWidth || 900;
    // colInset only has to clear a broker number now, not a firm name.
    var padT = 34, padB = 18, gap = 4, nodeW = 14, colInset = 48;
    var MIN_H = 15;                       // enough to seat a label legibly
    var rows = Math.max(sellers.length, buyers.length);
    // Height must fit every node at MIN_H, otherwise the small desks collapse
    // into each other and their labels overlap into an unreadable pile.
    var H = Math.max(460, rows * (MIN_H + gap) + padT + padB, rows * 34 + padT + padB);
    var usable = H - padT - padB;

    // Proportional heights, but nothing smaller than MIN_H. The shortfall that
    // creates is taken back from the nodes that are above the floor, in
    // proportion to their size, so the column still totals the same height.
    function layout(nodes) {
      var total = nodes.reduce(function (s, n) { return s + (n.amount || 0); }, 0) || 1;
      var free = usable - gap * Math.max(0, nodes.length - 1);
      var raw = nodes.map(function (n) { return free * (n.amount || 0) / total; });

      var deficit = 0, flexTotal = 0;
      raw.forEach(function (h) {
        if (h < MIN_H) deficit += MIN_H - h;
        else flexTotal += h - MIN_H;
      });
      var out = {}, y = padT;
      nodes.forEach(function (n, i) {
        var h = raw[i];
        if (h < MIN_H) h = MIN_H;
        else if (flexTotal > 0) h -= (h - MIN_H) / flexTotal * deficit;
        out[n.key] = { y: y, h: Math.max(MIN_H, h), node: n };
        y += out[n.key].h + gap;
      });
      return out;
    }
    return {
      W: W, H: H, nodeW: nodeW,
      xL: colInset, xR: W - colInset - nodeW,
      L: layout(sellers), R: layout(buyers)
    };
  }

  function ribbonPath(g, y0, y1, ah, bh) {
    var x0 = g.xL + g.nodeW, x1 = g.xR, cx = (x0 + x1) / 2;
    return "M" + x0 + "," + y0 +
      "C" + cx + "," + y0 + " " + cx + "," + y1 + " " + x1 + "," + y1 +
      "L" + x1 + "," + (y1 + bh) +
      "C" + cx + "," + (y1 + bh) + " " + cx + "," + (y0 + ah) + " " + x0 + "," + (y0 + ah) + "Z";
  }

  // Recompute ribbon + bar geometry for whatever slice of flow is showing.
  // Slots stay put (full-session share) while bars FILL and ribbons THICKEN,
  // so the columns never jump between frames.
  function updateSankey(links) {
    if (!fmSvg || !fmState.data) return;
    var g = fmSvg.geom, d = fmState.data;
    var sellFull = {}, buyFull = {};
    (d.sellers || []).forEach(function (n) { sellFull[n.key] = n.amount || 0; });
    (d.buyers || []).forEach(function (n) { buyFull[n.key] = n.amount || 0; });

    var sellNow = {}, buyNow = {};
    links.forEach(function (l) {
      sellNow[l.seller] = (sellNow[l.seller] || 0) + l.amount;
      buyNow[l.buyer] = (buyNow[l.buyer] || 0) + l.amount;
    });

    // Bars fill from the top of their slot in proportion to volume traded so far.
    Object.keys(g.L).forEach(function (k) {
      var r = fmSvg.bars["L|" + k]; if (!r) return;
      var frac = Math.min(1, (sellNow[k] || 0) / (sellFull[k] || 1));
      r.setAttribute("height", Math.max(0.5, g.L[k].h * frac));
    });
    Object.keys(g.R).forEach(function (k) {
      var r = fmSvg.bars["R|" + k]; if (!r) return;
      var frac = Math.min(1, (buyNow[k] || 0) / (buyFull[k] || 1));
      r.setAttribute("height", Math.max(0.5, g.R[k].h * frac));
    });

    var cursorL = {}, cursorR = {};
    Object.keys(g.L).forEach(function (k) { cursorL[k] = g.L[k].y; });
    Object.keys(g.R).forEach(function (k) { cursorR[k] = g.R[k].y; });

    var byKey = {};
    links.forEach(function (l) { byKey[l.seller + "|" + l.buyer] = l; });

    // Walk in creation order so ribbon stacking stays stable across frames.
    fmSvg.order.forEach(function (key) {
      var path = fmSvg.ribbons[key];
      if (!path) return;
      var lk = byKey[key];
      if (!lk || !lk.amount) { path.setAttribute("opacity", 0); return; }
      var s = lk.seller, b = lk.buyer;
      if (!g.L[s] || !g.R[b]) return;
      var ah = g.L[s].h * (lk.amount / (sellFull[s] || 1));
      var bh = g.R[b].h * (lk.amount / (buyFull[b] || 1));
      var y0 = cursorL[s], y1 = cursorR[b];
      cursorL[s] += ah; cursorR[b] += bh;
      path.setAttribute("opacity", 1);
      path.setAttribute("d", ribbonPath(g, y0, y1, ah, bh));
      var t = path.firstChild;
      if (t) {
        t.textContent = fmFull(s) + "  →  " + fmFull(b) +
          "\n" + fmtQty(lk.quantity) + " sh · " + fmtRsCompact(lk.amount) +
          " · avg Rs " + (lk.avg_rate == null ? "—" : lk.avg_rate) +
          " · " + lk.trades + " trades";
      }
    });
  }

  function renderSankey(d, override) {
    var svg = el("fm-svg"); if (!svg) return;
    while (svg.firstChild) svg.removeChild(svg.firstChild);
    fmSvg = null;

    var sellers = d.sellers || [], buyers = d.buyers || [], all = d.links || [];
    if (!sellers.length || !buyers.length) {
      var t0 = svgEl("text", { x: 20, y: 40, "class": "node-sub" });
      t0.textContent = "No flows in this window.";
      svg.appendChild(t0);
      return;
    }

    assignColors(d);
    var g = sankeyGeometry(d);
    svg.setAttribute("viewBox", "0 0 " + g.W + " " + g.H);
    svg.setAttribute("height", g.H);

    var ordered = all.slice().sort(function (a, b) { return b.amount - a.amount; });
    var refs = { geom: g, ribbons: {}, bars: {}, order: [] };

    // Each ribbon fades from its seller's colour into its buyer's, so you can
    // read both ends of a flow without tracing the whole curve.
    var defs = svgEl("defs");
    svg.appendChild(defs);

    // One path per session flow, created up-front so playback only mutates
    // attributes — that is what lets the CSS transition actually animate.
    ordered.forEach(function (lk) {
      if (!g.L[lk.seller] || !g.R[lk.buyer]) return;
      var key = lk.seller + "|" + lk.buyer;
      if (refs.ribbons[key]) return;

      var gid = "fmg-" + String(lk.seller).replace(/\W/g, "") + "-" + String(lk.buyer).replace(/\W/g, "");
      var grad = svgEl("linearGradient", {
        id: gid, x1: "0%", x2: "100%", y1: "0%", y2: "0%"
      });
      grad.appendChild(svgEl("stop", { offset: "0%", "stop-color": brokerColor(lk.seller, "L") }));
      grad.appendChild(svgEl("stop", { offset: "100%", "stop-color": brokerColor(lk.buyer, "R") }));
      defs.appendChild(grad);

      var p = svgEl("path", { "class": "ribbon anim", fill: "url(#" + gid + ")", opacity: 0, d: "" });
      p.appendChild(svgEl("title"));
      svg.appendChild(p);
      refs.ribbons[key] = p;
      refs.order.push(key);
    });

    function drawCol(map, x, side) {
      Object.keys(map).forEach(function (key) {
        var s = map[key], n = s.node;
        var grp = svgEl("g", { "class": "node" });
        // Faint full-session outline behind the filling bar, so you can see how
        // much of each desk's day has happened at any point in the playback.
        grp.appendChild(svgEl("rect", {
          x: x, y: s.y, width: g.nodeW, height: s.h, rx: 3,
          fill: brokerColor(key, side), "fill-opacity": 0.18
        }));
        var bar = svgEl("rect", {
          x: x, y: s.y, width: g.nodeW, height: s.h, rx: 3,
          fill: brokerColor(key, side), "class": "anim"
        });
        grp.appendChild(bar);
        refs.bars[side + "|" + key] = bar;

        // Label is the broker number only — avg price and turnover live in the
        // hover tooltip and the flows table, so the columns stay uncluttered.
        var tx = side === "L" ? x - 8 : x + g.nodeW + 8;
        var anchor = side === "L" ? "end" : "start";
        var t1 = svgEl("text", { x: tx, y: s.y + s.h / 2 + 4, "text-anchor": anchor, "class": "node-label" });
        t1.textContent = fmLabel(key);
        grp.appendChild(t1);
        var gt = svgEl("title");
        gt.textContent = fmFull(key) + "\n" + fmtQty(n.quantity) + " sh · " + fmtRsCompact(n.amount) +
          " · avg Rs " + (n.avg_rate == null ? "—" : n.avg_rate) + " · " + n.trades + " trades";
        grp.appendChild(gt);

        function spotlight(on) {
          refs.order.forEach(function (k2) {
            var parts = k2.split("|");
            var hit = side === "L" ? parts[0] === String(key) : parts[1] === String(key);
            refs.ribbons[k2].classList.toggle("dim", on && !hit);
            refs.ribbons[k2].classList.toggle("lit", on && hit);
          });
        }
        // Hover previews a desk's flows; click pins it so you can move the
        // mouse away and still read the highlighted paths.
        grp.addEventListener("mouseenter", function () { if (!fmState.focus) spotlight(true); });
        grp.addEventListener("mouseleave", function () { if (!fmState.focus) spotlight(false); });
        grp.addEventListener("click", function () {
          var want = side + ":" + key;
          if (fmState.focus === want) { fmState.focus = null; spotlight(false); }
          else { fmState.focus = want; spotlight(true); }
        });
        svg.appendChild(grp);
      });
    }
    drawCol(g.L, g.xL, "L");
    drawCol(g.R, g.xR, "R");

    var capL = svgEl("text", { x: g.xL + g.nodeW, y: 18, "text-anchor": "end", "class": "col-cap" });
    capL.textContent = "Sellers";
    svg.appendChild(capL);
    var capR = svgEl("text", { x: g.xR, y: 18, "class": "col-cap" });
    capR.textContent = "Buyers";
    svg.appendChild(capR);

    fmSvg = refs;
    updateSankey(override || all);
  }

  var FLOW_LINK_COLS = [
    { label: "Seller", key: "seller", type: "str", cls: "l" },
    { label: "Buyer", key: "buyer", type: "str", cls: "l" },
    { label: "Shares", key: "quantity", type: "num" },
    { label: "Value (Rs)", key: "amount", type: "num" },
    { label: "Avg Price", key: "avg_rate", type: "num" },
    { label: "Trades", key: "trades", type: "num" }
  ];
  // Volume leaderboards. Ranked by shares, real broker numbers only — these are
  // independent of the diagram's turnover-ranked, "Other"-pooled nodes.
  var FLOW_RANK_COLS = [
    { label: "#", key: "rank", type: "num", cls: "l" },
    { label: "Broker", key: "key", type: "str", cls: "l" },
    { label: "Shares", key: "quantity", type: "num" },
    { label: "% of vol", key: "pct", type: "num" },
    { label: "Value (Rs)", key: "amount", type: "num" },
    { label: "Avg Price", key: "avg_rate", type: "num" },
    { label: "Trades", key: "trades", type: "num" }
  ];
  function buildFlowRankTable(table, rows) {
    if (!rows || !rows.length) { empty(table, 7); return; }
    var body = rows.map(function (r) {
      return "<tr><td class='l'>" + r.rank +
        "</td><td class='l tkr brk' title='" + esc(fmFull(r.key)) + "'>" + esc(r.key) +
        "</td><td>" + fmtQty(r.quantity) +
        "</td><td>" + (r.pct == null ? "—" : r.pct + "%") +
        "</td><td>" + fmtRs(r.amount) +
        "</td><td>" + (r.avg_rate == null ? "—" : r.avg_rate) +
        "</td><td>" + r.trades + "</td></tr>";
    }).join("");
    table.innerHTML = sortableHead(table.id, FLOW_RANK_COLS) + "<tbody>" + body + "</tbody>";
  }

  function buildFlowLinkTable(table, rows) {
    if (!rows || !rows.length) { empty(table, 6); return; }
    // Real broker numbers, not the chart's pooled "Other" — so the cap has to be
    // high enough to cover a busy stock's full pair list.
    var body = rows.slice(0, 300).map(function (r) {
      return "<tr><td class='l tkr brk' title='" + esc(fmFull(r.seller)) + "'>" + esc(r.seller) +
        "</td><td class='l tkr brk' title='" + esc(fmFull(r.buyer)) + "'>" + esc(r.buyer) +
        "</td><td>" + fmtQty(r.quantity) + "</td><td>" + fmtRs(r.amount) +
        "</td><td>" + (r.avg_rate == null ? "—" : r.avg_rate) + "</td><td>" + r.trades + "</td></tr>";
    }).join("");
    table.innerHTML = sortableHead(table.id, FLOW_LINK_COLS) + "<tbody>" + body + "</tbody>";
  }

  /* ── Playback: step through the session, ribbons accumulating ──────────
   * Frames arrive with the main response, so stepping is pure client work —
   * no request per frame. Links are summed from frame 0 up to the current
   * index, giving "who had bought from whom by HH:MM". */
  var fmPlay = { frames: [], idx: 0, timer: null };

  function cumulativeLinks(upto) {
    var agg = {};
    for (var i = 0; i <= upto && i < fmPlay.frames.length; i++) {
      var fr = fmPlay.frames[i];
      for (var j = 0; j < fr.links.length; j++) {
        var l = fr.links[j], k = l.seller + "|" + l.buyer;
        var slot = agg[k] || (agg[k] = {
          seller: l.seller, buyer: l.buyer, quantity: 0, amount: 0, trades: 0
        });
        slot.quantity += l.quantity; slot.amount += l.amount; slot.trades += l.trades;
      }
    }
    return Object.keys(agg).map(function (k) {
      var v = agg[k];
      v.avg_rate = v.quantity ? Math.round(v.amount / v.quantity * 100) / 100 : null;
      return v;
    });
  }

  function showFrame(i) {
    if (!fmState.data || !fmPlay.frames.length) return;
    fmPlay.idx = Math.max(0, Math.min(i, fmPlay.frames.length - 1));
    var fr = fmPlay.frames[fmPlay.idx];
    var links = cumulativeLinks(fmPlay.idx);
    var qty = 0, amt = 0;
    links.forEach(function (l) { qty += l.quantity; amt += l.amount; });
    var pct = Math.round(100 * amt / (fmState.data.totals.amount || 1));

    el("fm-scrub").value = fmPlay.idx;
    // Clock carries the traded volume so far, not just the time.
    el("fm-clock").textContent = fr.end + " · " + fmtQty(qty) + " sh · " + pct + "%";
    // Tween the existing SVG rather than rebuilding it, so the bars fill and
    // the ribbons thicken smoothly instead of snapping.
    updateSankey(links);

    // Mark elapsed buckets on the volume strip.
    var bars = el("fm-timeline").querySelectorAll(".dsx-tl-bar");
    [].forEach.call(bars, function (b, bi) { b.classList.toggle("in", bi <= fmPlay.idx); });
  }

  function stopPlay() {
    if (fmPlay.timer) { clearInterval(fmPlay.timer); fmPlay.timer = null; }
    var b = el("fm-play"); if (b) b.textContent = "▶ Play";
  }

  function startPlay() {
    if (!fmPlay.frames.length) return;
    stopPlay();
    if (fmPlay.idx >= fmPlay.frames.length - 1) fmPlay.idx = -1;   // replay from open
    var speed = parseInt(el("fm-speed").value, 10) || 450;
    el("fm-play").textContent = "⏸ Pause";
    fmPlay.timer = setInterval(function () {
      if (fmPlay.idx >= fmPlay.frames.length - 1) { stopPlay(); return; }
      showFrame(fmPlay.idx + 1);
    }, speed);
  }

  function resetPlay() {
    stopPlay();
    if (!fmState.data) return;
    fmPlay.idx = fmPlay.frames.length ? fmPlay.frames.length - 1 : 0;
    el("fm-scrub").value = fmPlay.idx;
    el("fm-clock").textContent = fmSpanLabel();
    renderSankey(fmState.data);
    renderTimeline(fmState.data);
  }

  // "full session" is wrong once a range can span months.
  function fmSpanLabel() {
    var d = fmState.data;
    if (d && d.bucket_unit === "date") {
      return "all " + ((d.range && d.range.sessions) || 0) + " sessions";
    }
    return "full session";
  }

  function setPreset(name) {
    var box = el("fm-presets"); if (!box) return;
    [].forEach.call(box.querySelectorAll(".dsx-preset"), function (b) {
      b.classList.toggle("active", b.dataset.preset === name);
    });
  }

  // The Session picker only means something for a single session; custom needs a
  // start/end pair. Show exactly the inputs the current timeframe uses.
  function syncRangeInputs() {
    var rk = fmState.range || "today";
    el("fm-date-wrap").classList.toggle("dsx-hidden", rk !== "today");
    el("fm-start-wrap").classList.toggle("dsx-hidden", rk !== "custom");
    el("fm-end-wrap").classList.toggle("dsx-hidden", rk !== "custom");
  }

  TABS.flowmap = {
    init: function () {
      fillSymbols(el("fm-symbol"));
      symbolCombo(el("fm-symbol"));
      fmState.symbol = el("fm-symbol").value || ((META.symbols || [])[0] || {}).symbol;
      if (META.latest_date) el("fm-date").value = META.latest_date;

      el("fm-symbol").addEventListener("change", function () {
        fmState.symbol = this.value; TABS.flowmap.load();
      });
      el("fm-date").addEventListener("change", function () {
        fmState.date = this.value; TABS.flowmap.load();
      });

      syncRangeInputs();
      el("fm-range").addEventListener("change", function () {
        fmState.range = this.value;
        syncRangeInputs();
        if (this.value === "custom") {
          // Seed a sensible span so the first custom pick isn't two blank boxes.
          var last = (fmState.data && fmState.data.range && fmState.data.range.last) ||
            el("fm-date").value || META.latest_date;
          if (last && !el("fm-end").value) el("fm-end").value = last;
          if (last && !el("fm-start").value) {
            var dt = new Date(last + "T00:00:00");
            dt.setDate(dt.getDate() - 29);
            el("fm-start").value = dt.toISOString().slice(0, 10);
          }
          fmState.start = el("fm-start").value;
          fmState.end = el("fm-end").value;
        }
        TABS.flowmap.load();
      });
      el("fm-start").addEventListener("change", function () {
        fmState.start = this.value; TABS.flowmap.load();
      });
      el("fm-end").addEventListener("change", function () {
        fmState.end = this.value; TABS.flowmap.load();
      });
      el("fm-run").addEventListener("click", function () {
        fmState.from = el("fm-from").value;
        fmState.to = el("fm-to").value;
        setPreset(null);
        TABS.flowmap.load();
      });

      el("fm-presets").addEventListener("click", function (e) {
        var b = e.target.closest(".dsx-preset"); if (!b) return;
        var p = b.dataset.preset, d = fmState.data;
        var first = (d && d.session && d.session.first) || "10:30:00";
        var last = (d && d.session && d.session.last) || "15:00:00";
        function shift(hhmmss, mins) {
          var pr = hhmmss.split(":"), tot = (+pr[0]) * 60 + (+pr[1]) + mins;
          var hh = Math.max(0, Math.floor(tot / 60)), mm = ((tot % 60) + 60) % 60;
          return ("0" + hh).slice(-2) + ":" + ("0" + mm).slice(-2) + ":00";
        }
        if (p === "full") { fmState.from = ""; fmState.to = ""; }
        else if (p === "open") { fmState.from = first; fmState.to = shift(first, 30); }
        else if (p === "morning") { fmState.from = first; fmState.to = "12:00:00"; }
        else if (p === "afternoon") { fmState.from = "12:00:00"; fmState.to = last; }
        else if (p === "close") { fmState.from = shift(last, -30); fmState.to = last; }
        el("fm-from").value = fmState.from;
        el("fm-to").value = fmState.to;
        setPreset(p);
        TABS.flowmap.load();
      });

      el("fm-play").addEventListener("click", function () {
        if (fmPlay.timer) stopPlay(); else startPlay();
      });
      el("fm-reset").addEventListener("click", resetPlay);
      el("fm-scrub").addEventListener("input", function () {
        stopPlay();
        showFrame(parseInt(this.value, 10) || 0);
      });
      el("fm-speed").addEventListener("change", function () {
        if (fmPlay.timer) startPlay();          // restart at the new cadence
      });

      window.addEventListener("resize", function () {
        clearTimeout(window.__fmR);
        window.__fmR = setTimeout(function () {
          if (!fmState.data) return;
          renderSankey(fmState.data, fmPlay.timer ? cumulativeLinks(fmPlay.idx) : null);
        }, 180);
      });

      // The two palettes are stepped for their own surface, so a theme flip has
      // to repaint — the dark steps are not legible on the light surface.
      new MutationObserver(function () {
        if (!fmState.data) return;
        renderSankey(fmState.data, fmPlay.frames.length ? cumulativeLinks(fmPlay.idx) : null);
      }).observe(document.documentElement, { attributes: true, attributeFilter: ["data-theme"] });
    },

    load: function () {
      if (!fmState.symbol) { el("fm-hint").textContent = "No symbols available."; return; }
      stopPlay();
      var params = { symbol: fmState.symbol, timeline: 1, frames: 1 };
      var rk = fmState.range || "today";
      if (rk === "custom") {
        // Incomplete custom dates would 400; wait for both before querying.
        if (!fmState.start || !fmState.end) {
          el("fm-hint").textContent = "Pick a start and end date.";
          return;
        }
        params.range = "custom";
        params.start_date = fmState.start;
        params.end_date = fmState.end;
      } else if (rk !== "today") {
        params.range = rk;
      } else if (fmState.date) {
        params.date = fmState.date;
      }
      if (fmState.from) params.from = fmState.from;
      if (fmState.to) params.to = fmState.to;

      el("fm-hint").textContent = "Loading…";
      loading(el("fm-links"), 6);
      loading(el("fm-top-sell"), 7);
      loading(el("fm-top-buy"), 7);

      getJSON("flow-map/", params, "flowmap")
        .then(function (d) {
          if (!d || !d.ok) {
            el("fm-hint").textContent = (d && d.error) || "No data.";
            el("fm-tiles").innerHTML = "";
            empty(el("fm-links"), 6, "No flows");
            empty(el("fm-top-sell"), 7, "No flows");
            empty(el("fm-top-buy"), 7, "No flows");
            var s = el("fm-svg"); while (s.firstChild) s.removeChild(s.firstChild);
            return;
          }
          fmState.data = d;
          fmState.focus = null;
          if (!el("fm-date").value) el("fm-date").value = d.date;
          var r = d.range || {};
          // One session reads as a date + clock window; a range reads as the
          // span and how many sessions actually traded inside it.
          el("fm-hint").textContent = d.bucket_unit === "date"
            ? d.symbol + " · " + r.first + " → " + r.last + " · " + r.sessions +
              " session" + (r.sessions === 1 ? "" : "s")
            : d.symbol + " · " + d.date + " · session " + d.session.first + "–" + d.session.last;
          el("fm-tl-head").textContent = d.bucket_unit === "date"
            ? "TRADED VALUE BY DAY — click or drag a range to filter"
            : "TRADED VALUE BY TIME — click or drag a range to filter";
          renderFlowTiles(d);
          renderTimeline(d);
          renderSankey(d);
          showTable(el("fm-links"), d.pairs || d.links, buildFlowLinkTable);
          showTable(el("fm-top-sell"), d.top_sellers, buildFlowRankTable);
          showTable(el("fm-top-buy"), d.top_buyers, buildFlowRankTable);

          // Arm playback with this session's frames.
          fmPlay.frames = d.frames || [];
          fmPlay.idx = Math.max(0, fmPlay.frames.length - 1);
          var scrub = el("fm-scrub");
          scrub.max = Math.max(0, fmPlay.frames.length - 1);
          scrub.value = fmPlay.idx;
          scrub.disabled = !fmPlay.frames.length;
          el("fm-play").disabled = !fmPlay.frames.length;
          el("fm-clock").textContent = fmPlay.frames.length ? fmSpanLabel() : "no frames";
        })
        .catch(function (err) {
          if (isAbort(err)) return;
          el("fm-hint").textContent = "Could not load the flow map.";
          empty(el("fm-links"), 6, "Error");
          empty(el("fm-top-sell"), 7, "Error");
          empty(el("fm-top-buy"), 7, "Error");
        });
    }
  };

  // ─────────────────────────────────────────────────────────────────────
  // TAB: Hotstocks
  // ─────────────────────────────────────────────────────────────────────
  var hotState = { sector: "All", dr: null };
  TABS.hotstocks = {
    init: function () {
      fillSectors(el("hot-sector"));
      el("hot-sector").addEventListener("change", function () { hotState.sector = this.value; TABS.hotstocks.load(); });
      hotState.dr = dateRange("hot", function () { TABS.hotstocks.load(); });
    },
    load: function () {
      var t = el("hot-table");
      loading(t, 10);
      getJSON("hotstocks/", assign({ sector: hotState.sector }, hotState.dr.params()), "hotstocks")
        .then(function (d) {
          showTable(t, d.rows || [], buildHotTable);
        })
        .catch(function (err) { if (isAbort(err)) return; empty(t, 10, "Error"); });
    }
  };

  var HOT_COLS = [
    { label: "No." },
    { label: "Ticker", key: "symbol", type: "str", cls: "l" },
    { label: "Sector", key: "sector", type: "str", cls: "l" },
    { label: "Quantity", key: "quantity", type: "num" },
    { label: "Amount (Rs)", key: "amount", type: "num" },
    { label: "Avg Price", key: "avg_price", type: "num" },
    { label: "Buyers", key: "buyers", type: "num" },
    { label: "Sellers", key: "sellers", type: "num" },
    { label: "Top Buy" },
    { label: "Top Sell" }
  ];
  function buildHotTable(table, rows) {
    if (!rows || !rows.length) { empty(table, 10); return; }
    var body = rows.map(function (r, i) {
      var tb = r.top_buy ? r.top_buy.broker + " (" + fmtPct(r.top_buy.pct) + ")" : "—";
      var ts = r.top_sell ? r.top_sell.broker + " (" + fmtPct(r.top_sell.pct) + ")" : "—";
      return "<tr><td>" + (i + 1) + "</td><td class='l tkr'>" + esc(r.symbol) + "</td><td class='l'>" +
        esc(r.sector || "") + "</td><td>" + fmtQty(r.quantity) + "</td><td>" + fmtRs(r.amount) +
        "</td><td>" + fmtPrice(r.avg_price) + "</td><td>" + r.buyers + "</td><td>" + r.sellers +
        "</td><td class='num-pos'>" + tb + "</td><td class='num-neg'>" + ts + "</td></tr>";
    }).join("");
    table.innerHTML = sortableHead(table.id, HOT_COLS) + "<tbody>" + body + "</tbody>";
  }

  // ─────────────────────────────────────────────────────────────────────
  // TAB: Net Holding (treemap)
  // ─────────────────────────────────────────────────────────────────────
  var nhState = { brokers: [], excludeMf: false, sector: "All", dr: null };
  TABS.netholding = {
    init: function () {
      // Open on broker 42 (Sani Securities); falls back to no selection if 42
      // isn't in the reference list, so it degrades cleanly.
      nhState.brokers = (META.brokers || []).map(String).indexOf("42") !== -1 ? ["42"] : [];
      buildBrokerMulti("nh", nhState, function () { TABS.netholding.load(); });
      fillSectors(el("nh-sector"));
      el("nh-sector").addEventListener("change", function () { nhState.sector = this.value; TABS.netholding.load(); });
      el("nh-exclude-mf").addEventListener("change", function () { nhState.excludeMf = this.checked; TABS.netholding.load(); });
      nhState.dr = dateRange("nh", function () { TABS.netholding.load(); });
    },
    load: function () {
      var box = el("nh-treemap");
      box.innerHTML = '<div class="dsx-loading">Loading…</div>';
      if (!nhState.brokers.length) { box.innerHTML = '<div class="dsx-empty">Select a broker</div>'; return; }
      getJSON("netholding/", assign({
        brokers: nhState.brokers.join(","),
        exclude_mf: nhState.excludeMf ? 1 : 0, sector: nhState.sector
      }, nhState.dr.params()), "netholding").then(function (d) {
        renderTreemap(box, (d.items || []));
      }).catch(function (err) { if (isAbort(err)) return; box.innerHTML = '<div class="dsx-empty">Error</div>'; });
    }
  };

  // Squarified treemap (Bruls, Huizing, van Wijk).
  function renderTreemap(box, items) {
    box.innerHTML = "";
    if (!items.length) { box.innerHTML = '<div class="dsx-empty">No net positions</div>'; return; }
    var W = box.clientWidth || 1000, H = box.clientHeight || 640;
    var total = items.reduce(function (s, it) { return s + it.size; }, 0) || 1;
    var scale = (W * H) / total;
    var data = items.map(function (it) { return { it: it, area: it.size * scale }; });

    var x = 0, y = 0, w = W, h = H, i = 0;
    function worst(row, len) {
      var sum = 0, mn = Infinity, mx = 0;
      for (var k = 0; k < row.length; k++) { sum += row[k].area; mn = Math.min(mn, row[k].area); mx = Math.max(mx, row[k].area); }
      var s2 = sum * sum, l2 = len * len;
      return Math.max((l2 * mx) / s2, s2 / (l2 * mn));
    }
    function layoutRow(row, len, horiz) {
      var sum = row.reduce(function (s, r) { return s + r.area; }, 0);
      var thick = sum / len;
      var off = 0;
      row.forEach(function (r) {
        var side = r.area / thick;
        var cx, cy, cw, ch;
        if (horiz) { cx = x; cy = y + off; cw = thick; ch = side; }
        else { cx = x + off; cy = y; cw = side; ch = thick; }
        drawCell(box, r.it, cx, cy, cw, ch);
        off += side;
      });
      if (horiz) { x += thick; w -= thick; } else { y += thick; h -= thick; }
    }

    var row = [];
    while (i < data.length) {
      var horiz = w >= h;     // lay along the shorter side
      var len = horiz ? h : w;
      var withNew = row.concat([data[i]]);
      if (row.length === 0 || worst(row, len) >= worst(withNew, len)) {
        row = withNew; i++;
      } else {
        layoutRow(row, len, horiz); row = [];
      }
    }
    if (row.length) layoutRow(row, (w >= h ? h : w), w >= h);
    mountTreemapTip(box);
  }

  function drawCell(box, it, x, y, w, h) {
    var d = document.createElement("div");
    d.className = "dsx-tm-cell " + (it.side === "buy" ? "buy" : "sell");
    d.style.left = x + "px"; d.style.top = y + "px";
    d.style.width = Math.max(0, w - 1) + "px"; d.style.height = Math.max(0, h - 1) + "px";
    // Data for the hover/tap tooltip (buy / sell / net shares).
    d.setAttribute("data-sym", it.symbol);
    d.setAttribute("data-buy", it.buy != null ? it.buy : 0);
    d.setAttribute("data-sell", it.sell != null ? it.sell : 0);
    d.setAttribute("data-net", it.net);
    // Native title as an accessible fallback.
    d.title = it.symbol + " — Buy " + nf(it.buy || 0) + " / Sell " + nf(it.sell || 0) +
      " / Net " + (it.net > 0 ? "+" : "") + nf(it.net);
    if (w > 34 && h > 18) {
      var fs = Math.max(9, Math.min(15, Math.sqrt(w * h) / 6));
      var label = document.createElement("span");
      label.className = "dsx-tm-label";
      label.style.fontSize = fs + "px";
      label.textContent = it.symbol;
      d.appendChild(label);
    }
    box.appendChild(d);
  }

  // Hover/tap tooltip for the treemap: shows Buy / Sell / Net for a cell. Box
  // listeners are wired once; the tip node is re-appended after each re-render
  // (renderTreemap clears box.innerHTML).
  function mountTreemapTip(box) {
    var tip = box._dsxTip;
    if (!tip) {
      tip = box._dsxTip = document.createElement("div");
      tip.className = "dsx-tm-tip";
      tip.hidden = true;

      var show = function (cell, clientX, clientY) {
        var net = +cell.getAttribute("data-net") || 0;
        tip.innerHTML =
          '<div class="dsx-tm-tip-sym">' + esc(cell.getAttribute("data-sym")) + '</div>' +
          '<div class="dsx-tm-tip-row"><span>Buy</span><b class="num-pos">' + fmtQty(+cell.getAttribute("data-buy") || 0) + '</b></div>' +
          '<div class="dsx-tm-tip-row"><span>Sell</span><b class="num-neg">' + fmtQty(+cell.getAttribute("data-sell") || 0) + '</b></div>' +
          '<div class="dsx-tm-tip-row net"><span>Net</span><b class="' + (net >= 0 ? "num-pos" : "num-neg") + '">' +
            (net > 0 ? "+" : "") + fmtQty(net) + '</b></div>';
        tip.hidden = false;
        var r = box.getBoundingClientRect();
        var px = clientX - r.left + 14, py = clientY - r.top + 14;
        px = Math.max(6, Math.min(px, r.width - tip.offsetWidth - 6));
        py = Math.max(6, Math.min(py, r.height - tip.offsetHeight - 6));
        tip.style.left = px + "px"; tip.style.top = py + "px";
      };
      var hide = function () { tip.hidden = true; };
      var cellAt = function (e) {
        var c = e.target.closest ? e.target.closest(".dsx-tm-cell") : null;
        return c && box.contains(c) ? c : null;
      };

      box.addEventListener("mousemove", function (e) {
        var c = cellAt(e); if (c) show(c, e.clientX, e.clientY); else hide();
      });
      box.addEventListener("mouseleave", hide);
      // Touch / tap: show for the tapped cell, hide when tapping empty space.
      box.addEventListener("click", function (e) {
        var c = cellAt(e); if (c) show(c, e.clientX, e.clientY); else hide();
      });
    }
    tip.hidden = true;
    box.appendChild(tip);     // re-attach after innerHTML reset
  }

  // ─────────────────────────────────────────────────────────────────────
  // TAB: Broker Concentration
  // ─────────────────────────────────────────────────────────────────────
  var concState = { sector: "All", dr: null };
  TABS.concentration = {
    init: function () {
      fillSectors(el("conc-sector"));
      el("conc-sector").addEventListener("change", function () { concState.sector = this.value; TABS.concentration.load(); });
      concState.dr = dateRange("conc", function () { TABS.concentration.load(); });
    },
    load: function () {
      var t = el("conc-table");
      loading(t, 10);
      getJSON("concentration/", assign({ sector: concState.sector }, concState.dr.params()), "concentration")
        .then(function (d) {
          showTable(t, d.rows || [], buildConcTable);
        })
        .catch(function (err) { if (isAbort(err)) return; empty(t, 10, "Error"); });
    }
  };

  // Grouped two-row header; Ticker / Total Traded / both Sum-Top-3 columns sort.
  function buildConcTable(table, rows) {
    if (!rows || !rows.length) { empty(table, 10); return; }
    var id = table.id;
    var tk = _sortMark(id, "symbol"), tt = _sortMark(id, "total"),
        bs = _sortMark(id, "buy_sum"), ss = _sortMark(id, "sell_sum");
    var head = "<thead>" +
      "<tr><th rowspan='2' class='l sortable" + tk.cls + "' data-sort='symbol' data-type='str'>Ticker" + tk.arrow + "</th>" +
      "<th rowspan='2' class='sortable" + tt.cls + "' data-sort='total' data-type='num'>Total Traded" + tt.arrow + "</th>" +
      "<th colspan='4' class='grp'>Top Broker On Buy Side</th>" +
      "<th colspan='4' class='grp'>Top Broker On Sell Side</th></tr>" +
      "<tr><th class='grp'>1st</th><th>2nd</th><th>3rd</th>" +
      "<th class='sortable" + bs.cls + "' data-sort='buy_sum' data-type='num'>Sum Top 3" + bs.arrow + "</th>" +
      "<th class='grp'>1st</th><th>2nd</th><th>3rd</th>" +
      "<th class='sortable" + ss.cls + "' data-sort='sell_sum' data-type='num'>Sum Top 3" + ss.arrow + "</th></tr></thead>";
    function cell(arr, idx) {
      var b = arr[idx];
      return b ? b.broker + " (" + fmtPct(b.pct) + ")" : "—";
    }
    var body = rows.map(function (r) {
      return "<tr><td class='l tkr'>" + esc(r.symbol) + "</td><td>" + fmtQty(r.total) + "</td>" +
        "<td class='grp num-pos'>" + cell(r.buy, 0) + "</td><td class='num-pos'>" + cell(r.buy, 1) +
        "</td><td class='num-pos'>" + cell(r.buy, 2) + "</td><td class='buysum'>" + fmtPct(r.buy_sum) + "</td>" +
        "<td class='grp num-neg'>" + cell(r.sell, 0) + "</td><td class='num-neg'>" + cell(r.sell, 1) +
        "</td><td class='num-neg'>" + cell(r.sell, 2) + "</td><td class='sellsum'>" + fmtPct(r.sell_sum) + "</td></tr>";
    }).join("");
    table.innerHTML = head + "<tbody>" + body + "</tbody>";
  }

  // ── boot ──────────────────────────────────────────────────────────────
  document.querySelectorAll(".dsx-tab").forEach(function (t) {
    t.addEventListener("click", function () { activateTab(t.dataset.tab); });
  });

  /* Deep links: ?tab=&symbol=&range= — so other desks (Stock 360) can hand a
   * symbol straight to the right tab instead of dropping the reader on the
   * default one with nothing selected. The tab must exist and the symbol is set
   * BEFORE init reads the select, since init seeds its state from that value. */
  function readDeepLink() {
    var q = new URLSearchParams(window.location.search);
    var tab = (q.get("tab") || "").replace(/[^a-z]/gi, "");
    if (!tab || !document.querySelector('.dsx-tab[data-tab="' + tab + '"]')) return null;
    return {
      tab: tab,
      symbol: (q.get("symbol") || "").trim().toUpperCase(),
      range: (q.get("range") || "").trim().toLowerCase()
    };
  }

  function applyDeepLink(dl) {
    if (dl.tab !== "flowmap") return;
    var sel = el("fm-symbol"), rng = el("fm-range");
    if (dl.symbol && sel) {
      sel.value = dl.symbol;
      // A symbol the floorsheet does not carry leaves the select unchanged;
      // don't push a state the control cannot show.
      if (sel.value === dl.symbol) {
        fmState.symbol = dl.symbol;
        if (sel.comboSync) sel.comboSync();
      }
    }
    if (dl.range && rng) {
      // Only honour a range the control actually offers — an unknown key would
      // leave the select showing one window while state held another.
      var ok = [].some.call(rng.options, function (o) { return o.value === dl.range; });
      if (ok) { rng.value = dl.range; fmState.range = dl.range; }
    }
  }

  function start() {
    var upd = el("fs-updated");
    if (upd && META.latest_date) upd.textContent = "Last Updated On " + META.latest_date;
    var banner = el("dsx-banner");
    if (banner && META.ok) banner.hidden = true;
    var dl = readDeepLink();
    // Landing tab. Must match the button/panel carrying `active` in the
    // template — if the two disagree the page renders one tab highlighted and a
    // different one populated.
    activateTab(dl ? dl.tab : "adradar",
                dl ? function () { applyDeepLink(dl); } : null);
  }

  // If the page was served before today's aggregate was built (cache cold), the
  // bootstrap meta is empty — fetch it now (this triggers the first build) and
  // populate the dropdowns once it lands.
  function hasUsableMeta() {
    return META && ((META.brokers || []).length || (META.symbols || []).length);
  }

  function refreshMeta() {
    getJSON("meta/", {}, "meta")
      .then(function (m) {
        META = m || META;
        var upd = el("fs-updated");
        if (upd && META.latest_date) upd.textContent = "Last Updated On " + META.latest_date;
        var banner = el("dsx-banner");
        if (banner && META.ok) banner.hidden = true;
        refreshDateRanges();
      })
      .catch(function () {});
  }

  if (hasUsableMeta()) {
    start();
    refreshMeta();
  } else {
    var upd = el("fs-updated");
    if (upd) upd.textContent = "Loading floorsheet…";
    getJSON("meta/", {}, "meta")
      .then(function (m) { META = m || META; start(); })
      .catch(function () { start(); });
  }
})();

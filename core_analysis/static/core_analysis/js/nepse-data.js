/* NEPSE Data — generic report table.
 *
 * One implementation drives all nine reports. `window.ND` carries the column
 * spec and which filter controls the page rendered; everything else (sorting,
 * row filtering, formatting) is column-type driven, so a new report needs
 * no JavaScript at all.
 */
(function () {
  "use strict";

  var ND = window.ND || {};
  var state = { rows: [], meta: {}, sort: null, dir: 1, q: "", symbols: [],
                page: 1, pageSize: 500 };

  function el(id) { return document.getElementById(id); }
  function esc(s) {
    return String(s == null ? "" : s).replace(/[&<>"']/g, function (c) {
      return { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c];
    });
  }

  // ── formatting, by column type ─────────────────────────────────────────
  function nf(v, d) {
    if (v == null || v === "") return "—";
    return Number(v).toLocaleString("en-IN", {
      minimumFractionDigits: d, maximumFractionDigits: d
    });
  }
  // Rs figures are stored in full rupees; compact them so a 12-digit turnover
  // does not force the table sideways.
  function rs(v) {
    if (v == null || v === "") return "—";
    var n = Number(v), a = Math.abs(n);
    if (a >= 1e9) return (n / 1e9).toFixed(2) + " B";
    if (a >= 1e7) return (n / 1e7).toFixed(2) + " Cr";
    if (a >= 1e5) return (n / 1e5).toFixed(2) + " L";
    return nf(n, 0);
  }
  function fmt(v, type) {
    switch (type) {
      case "int": return v == null ? "—" : nf(v, 0);
      case "num": return nf(v, 2);
      case "rs": return rs(v);
      case "pct": return v == null || v === "" ? "—" : nf(v, 2) + "%";
      case "signed":
        if (v == null || v === "") return "—";
        return (Number(v) > 0 ? "+" : "") + nf(v, 2);
      default: return v == null || v === "" ? "—" : esc(v);
    }
  }
  function cls(type, v) {
    var c = (type === "str" || type === "date") ? "l" : "";
    if ((type === "signed" || type === "pct") && v != null && v !== "") {
      if (Number(v) > 0) c += " up";
      else if (Number(v) < 0) c += " down";
    }
    return c.trim();
  }

  // ── table ──────────────────────────────────────────────────────────────
  function visibleRows() {
    var q = state.q.trim().toLowerCase(), rows = state.rows;
    if (q) {
      rows = rows.filter(function (r) {
        for (var i = 0; i < ND.columns.length; i++) {
          var v = r[ND.columns[i].key];
          if (v != null && String(v).toLowerCase().indexOf(q) > -1) return true;
        }
        return false;
      });
    }
    if (state.sort) {
      var col = null;
      for (var i = 0; i < ND.columns.length; i++) {
        if (ND.columns[i].key === state.sort) col = ND.columns[i];
      }
      var numeric = col && ["num", "int", "rs", "pct", "signed"].indexOf(col.type) > -1;
      // Copy before sorting: sorting `state.rows` in place would make the
      // unsorted order unrecoverable and fight the filter above.
      rows = rows.slice().sort(function (a, b) {
        var x = a[state.sort], y = b[state.sort];
        if (x == null) return 1;          // blanks sink, either direction
        if (y == null) return -1;
        if (numeric) return (Number(x) - Number(y)) * state.dir;
        return String(x).localeCompare(String(y)) * state.dir;
      });
    }
    return rows;
  }

  function render() {
    var table = el("nd-table");
    // `matched` is everything the filter+sort produced; `rows` is just the slice
    // painted into the DOM. Keeping them separate is what makes 44k rows usable:
    // the browser only ever builds one page of elements.
    var m0 = state.meta || {};
    // Server-paged reports (the floor sheet) already hold exactly one page, so
    // slicing again locally would page a page. Their pager re-fetches instead.
    var srv = !!m0.server_paged;
    var matched = visibleRows();
    var size = state.pageSize;
    var pages, start, rows;
    if (srv) {
      // pages is null when the range was too wide to count cheaply; the pager
      // then relies on has_next instead of a known last page.
      pages = m0.pages || null;
      state.page = m0.page || 1;
      start = ((state.page - 1) * (m0.page_size || matched.length));
      rows = matched;
    } else {
      pages = size === "all" ? 1 : Math.max(1, Math.ceil(matched.length / size));
      if (state.page > pages) state.page = pages;
      start = size === "all" ? 0 : (state.page - 1) * size;
      rows = size === "all" ? matched : matched.slice(start, start + size);
    }
    var head = "<thead><tr>" + ND.columns.map(function (c) {
      var active = state.sort === c.key;
      return '<th class="' + ((c.type === "str" || c.type === "date") ? "l" : "") +
        (active ? " sorted" : "") + '" data-k="' + esc(c.key) + '">' + esc(c.label) +
        (active ? (state.dir > 0 ? " ▲" : " ▼") : "") + "</th>";
    }).join("") + "</tr></thead>";

    var body;
    if (!rows.length) {
      body = '<tbody><tr><td colspan="' + ND.columns.length +
        '" class="dsx-empty">No rows.</td></tr></tbody>';
    } else {
      body = "<tbody>" + rows.map(function (r) {
        return "<tr>" + ND.columns.map(function (c) {
          var v = r[c.key];
          return '<td class="' + cls(c.type, v) + '">' + fmt(v, c.type) + "</td>";
        }).join("") + "</tr>";
      }).join("") + "</tbody>";
    }
    table.innerHTML = head + body;

    var m = state.meta || {};
    var bits = [];
    if (m.date) bits.push(m.date);
    if (m.symbol) bits.push(m.symbol);
    if (m.from && m.to) bits.push(m.from + " → " + m.to);
    if (m.n) bits.push(m.n + " sessions");
    // Always state the total; showing only the page count reads as "that's all
    // the data there is".
    var n = function (x) { return Number(x).toLocaleString("en-IN"); };
    if (srv) {
      // Never invent a total: an uncounted range says so rather than implying
      // the current page is everything.
      if (m.total != null) bits.push(n(m.total) + " trade" + (m.total === 1 ? "" : "s"));
      if (rows.length) bits.push("showing " + n(start + 1) + "–" + n(start + rows.length));
      if (m.total == null) bits.push("total not counted over this range");
    } else if (matched.length === state.rows.length) {
      bits.push(n(state.rows.length) + " row" + (state.rows.length === 1 ? "" : "s"));
    } else {
      bits.push(n(matched.length) + " of " + n(state.rows.length) + " rows match");
    }
    if (!srv && size !== "all" && matched.length > size) {
      bits.push("showing " + n(start + 1) + "–" + n(start + rows.length));
    }
    el("nd-caption").textContent = bits.join("  ·  ");

    // "All" cannot be honoured when the server pages — it would mean shipping
    // millions of rows — so drop the option rather than leave a control that
    // silently does something else.
    if (srv) {
      // Trade-time / largest-trade ordering only applies within one session —
      // across dates it would cost a filesort of millions of rows. Disable it
      // rather than leave a control that quietly does nothing.
      var ord = el("nd-order");
      if (ord) {
        ord.disabled = m.single_day === false;
        ord.title = ord.disabled
          ? "Available for a single session — set From and To to the same date."
          : "";
      }
    }

    var pager = el("nd-pager");
    pager.hidden = srv
      ? (state.page <= 1 && !m.has_next && !(pages > 1))
      : (pages <= 1 || size === "all");
    if (!pager.hidden) {
      el("nd-page-info").textContent = pages
        ? "Page " + n(state.page) + " of " + n(pages)
        : "Page " + n(state.page);
      var atEnd = srv ? !m.has_next : state.page >= pages;
      pager.querySelectorAll(".nd-page-btn").forEach(function (b) {
        var g = b.dataset.go;
        if (g === "first" || g === "prev") b.disabled = state.page <= 1;
        else if (g === "last") b.disabled = !pages || state.page >= pages;  // needs a known end
        else b.disabled = atEnd;
      });
    }

    // Anything the server wants said out loud — a stale-data fallback, or a
    // row cap that would otherwise look like the report simply ending.
    var note = el("nd-note"), msgs = [];
    if (m.official && m.official.market_capitalization) {
        var o = m.official;
        msgs.push("Exchange-published totals for " + (m.date || "the session") + ": market cap " +
            rs(o.market_capitalization) +
            (o.float_market_capitalization ? " · float " + rs(o.float_market_capitalization) : "") +
            (o.sensitive_market_capitalization ? " · sensitive " + rs(o.sensitive_market_capitalization) : "") +
            ".");
    }
    if (m.warning) msgs.push(m.warning);
    if (m.capped) {
      // Name WHICH rows survived the cap — "5,000 of 44,651" alone would leave
      // it ambiguous whether the rest were early trades or small ones.
      msgs.push("Showing the " + (m.order === "largest" ? "largest" : "most recent") + " " +
        m.limit.toLocaleString("en-IN") + " of " + m.total.toLocaleString("en-IN") +
        " trades. Filter by symbol or broker to see them all.");
    }
    note.hidden = !msgs.length;
    note.textContent = msgs.join(" ");
  }

  function load(opts) {
    opts = opts || {};
    // Any change other than paging invalidates the current page number.
    if (!opts.keepPage) state.page = 1;
    var p = new URLSearchParams();
    ["date", "symbol", "start", "end", "n", "broker", "order"].forEach(function (k) {
      var node = el("nd-" + k);
      if (node && node.value) p.set(k, node.value);
    });
    // Harmless on client-paged reports, which ignore both.
    p.set("page", state.page);
    p.set("page_size", state.pageSize);   // may be "all"
    var idx = el("nd-index");
    if (idx && idx.value) p.set("sector", idx.value);
    var all = el("nd-all");
    if (all && all.value) p.set("all", all.value);

    el("nd-caption").textContent = "Loading…";
    el("nd-table").innerHTML = '<tbody><tr><td class="dsx-loading">Loading…</td></tr></tbody>';

    fetch(ND.api + (p.toString() ? "?" + p.toString() : ""), {
      headers: { "X-Requested-With": "XMLHttpRequest" }
    })
      .then(function (r) { return r.json(); })
      .then(function (d) {
        if (!d || !d.ok) throw new Error((d && d.error) || "failed");
        state.rows = d.rows || [];
        state.meta = d.meta || {};
        if (!(d.meta && d.meta.server_paged)) state.page = 1;
        render();
      })
      .catch(function () {
        state.rows = []; state.meta = {};
        el("nd-caption").textContent = "Could not load this report.";
        el("nd-table").innerHTML = '<tbody><tr><td class="dsx-empty">' +
          "Could not load this report. Reload the page and try again.</td></tr></tbody>";
        el("nd-note").hidden = true;
      });
  }

  // ── symbol picker (type-to-filter; 500+ options is unusable as a select) ──
  var ALL_LABEL = "— All symbols —";

  function fillSymbols(sel, symbols) {
    sel.innerHTML = "";
    if (ND.symbolOptional) {
      var a = document.createElement("option");
      a.value = ""; a.textContent = ALL_LABEL;
      sel.appendChild(a);
    }
    symbols.forEach(function (s) {
      var o = document.createElement("option");
      o.value = s.symbol;
      o.textContent = s.name && s.name !== s.symbol ? s.name + " ( " + s.symbol + " )" : s.symbol;
      sel.appendChild(o);
    });
  }

  function combo(sel, symbols) {
    var items = symbols.map(function (s) {
      return {
        symbol: s.symbol, name: s.name || s.symbol,
        label: s.name && s.name !== s.symbol ? s.name + " ( " + s.symbol + " )" : s.symbol
      };
    });
    // "All" has to be reachable from the picker too, otherwise clearing a symbol
    // filter would be impossible once one is chosen.
    if (ND.symbolOptional) {
      items.unshift({ symbol: "", name: "every symbol in the session", label: ALL_LABEL });
    }
    var wrap = document.createElement("div");
    wrap.className = "dsx-combo";
    var input = document.createElement("input");
    input.type = "text";
    input.className = "dsx-select dsx-combo-input";
    input.setAttribute("autocomplete", "off");
    input.placeholder = "Type a symbol or company…";
    var list = document.createElement("div");
    list.className = "dsx-combo-list dsx-hidden";

    sel.parentNode.insertBefore(wrap, sel);
    wrap.appendChild(input); wrap.appendChild(list); wrap.appendChild(sel);
    sel.classList.add("dsx-hidden");

    var open = false, active = -1, shown = [];
    function labelFor(v) {
      for (var i = 0; i < items.length; i++) if (items[i].symbol === v) return items[i].label;
      return v || "";
    }
    function sync() { input.value = labelFor(sel.value); }
    function match(q) {
      q = q.trim().toUpperCase();
      if (!q) return items.slice(0, 60);
      var st = [], co = [];
      items.forEach(function (it) {
        var sy = it.symbol.toUpperCase(), nm = it.name.toUpperCase();
        if (sy.indexOf(q) === 0) st.push(it);
        else if (sy.indexOf(q) > -1 || nm.indexOf(q) > -1) co.push(it);
      });
      return st.concat(co).slice(0, 60);
    }
    function draw(q) {
      shown = match(q);
      list.innerHTML = shown.length
        ? shown.map(function (it, i) {
            // The "all" row has no ticker, so it shows its label instead of an
            // empty bold cell followed by a stray description.
            var body = it.symbol
              ? "<b>" + esc(it.symbol) + "</b><span>" + esc(it.name) + "</span>"
              : "<b>" + esc(it.label) + "</b>";
            return '<div class="dsx-combo-opt' + (i === active ? " active" : "") +
              '" data-v="' + esc(it.symbol) + '">' + body + "</div>";
          }).join("")
        : '<div class="dsx-combo-empty">No match</div>';
    }
    function show(q) { active = -1; draw(q || ""); list.classList.remove("dsx-hidden"); open = true; }
    function hide() { list.classList.add("dsx-hidden"); open = false; sync(); }
    function pick(v) {
      // "" is a legitimate choice (the "all symbols" row) wherever the symbol is
      // an optional filter, so only a genuinely absent value is ignored.
      if (v == null) return;
      if (!v && !ND.symbolOptional) return;
      sel.value = v; hide(); load();
    }
    function move(step) {
      if (!shown.length) return;
      active = (active + step + shown.length) % shown.length;
      draw(input.value);
      var n = list.querySelector(".dsx-combo-opt.active");
      if (n) n.scrollIntoView({ block: "nearest" });
    }
    input.addEventListener("focus", function () { this.select(); show(""); });
    input.addEventListener("input", function () { show(this.value); });
    input.addEventListener("keydown", function (e) {
      if (e.key === "ArrowDown") { e.preventDefault(); open ? move(1) : show(this.value); }
      else if (e.key === "ArrowUp") { e.preventDefault(); move(-1); }
      else if (e.key === "Enter") { e.preventDefault(); pick((shown[active < 0 ? 0 : active] || {}).symbol); }
      else if (e.key === "Escape") { hide(); }
    });
    list.addEventListener("mousedown", function (e) {
      var o = e.target.closest(".dsx-combo-opt");
      if (!o) return;
      e.preventDefault();
      pick(o.dataset.v);
    });
    document.addEventListener("mousedown", function (e) {
      if (open && !wrap.contains(e.target)) hide();
    });
    sync();
  }

  // ── boot ───────────────────────────────────────────────────────────────
  function seedDates() {
    var d = el("nd-date");
    if (d && !d.value) {
      d.value = ND.slug === "floor-sheet" ? (ND.latest.floorsheet || ND.latest.price)
        : ND.slug === "indices" ? (ND.latest.index || ND.latest.price)
        : ND.latest.price;
    }
    var end = el("nd-end"), start = el("nd-start");
    if (ND.slug === "floor-sheet") {
      // Open on ONE session. A 90-day default would exceed the unfiltered span
      // limit and greet you with a "narrow the range" message instead of data.
      var fd = ND.latest.floorsheet || ND.latest.price;
      if (end && !end.value) end.value = fd;
      if (start && !start.value) start.value = fd;
      return;
    }
    if (end && !end.value) end.value = ND.latest.price;
    if (start && !start.value && ND.latest.price) {
      var dt = new Date(ND.latest.price + "T00:00:00");
      dt.setDate(dt.getDate() - 89);
      start.value = dt.toISOString().slice(0, 10);
    }
  }

  document.addEventListener("DOMContentLoaded", function () {
    seedDates();

    ["nd-date", "nd-start", "nd-end", "nd-n", "nd-index", "nd-all", "nd-broker",
     "nd-order"].forEach(function (id) {
      var n = el(id);
      if (n) n.addEventListener("change", load);
    });

    var q = el("nd-q");
    if (q) {
      // Filtering is client-side over rows already loaded, so it can run on
      // every keystroke without touching the network. Reset to page 1 — staying
      // on page 12 of a now-3-page result would show an empty table.
      q.addEventListener("input", function () {
        state.q = this.value; state.page = 1; render();
      });
    }

    var ps = el("nd-page-size");
    if (ps) {
      state.pageSize = ps.value === "all" ? "all" : parseInt(ps.value, 10);
      ps.addEventListener("change", function () {
        state.pageSize = this.value === "all" ? "all" : parseInt(this.value, 10);
        state.page = 1;
        // Server-paged reports need a new slice; client-paged ones already hold
        // every row and can just repaint.
        if (state.meta && state.meta.server_paged) load();
        else render();
      });
    }

    el("nd-pager").addEventListener("click", function (e) {
      var b = e.target.closest(".nd-page-btn");
      if (!b || b.disabled) return;
      var srv = !!(state.meta && state.meta.server_paged);
      var pages = srv
        ? state.meta.pages
        : (state.pageSize === "all"
            ? 1 : Math.max(1, Math.ceil(visibleRows().length / state.pageSize)));
      var go = b.dataset.go;
      if (go === "first") state.page = 1;
      else if (go === "prev") state.page = Math.max(1, state.page - 1);
      // With no known page count, Next simply advances — has_next already
      // decided the button was live.
      else if (go === "next") state.page = pages ? Math.min(pages, state.page + 1) : state.page + 1;
      else if (go === "last" && pages) state.page = pages;
      // Server-paged reports hold only the current page, so moving must re-query.
      if (srv) load({ keepPage: true });
      else render();
      el("nd-table").scrollIntoView({ block: "start" });
    });

    el("nd-table").addEventListener("click", function (e) {
      var th = e.target.closest("th[data-k]");
      if (!th) return;
      var k = th.dataset.k;
      if (state.sort === k) state.dir = -state.dir;
      else { state.sort = k; state.dir = 1; }
      state.page = 1;   // a re-sort makes the old page number meaningless
      render();
    });

    var symSel = el("nd-symbol");
    if (symSel) {
      fetch(ND.symbolsUrl, { headers: { "X-Requested-With": "XMLHttpRequest" } })
        .then(function (r) { return r.json(); })
        .then(function (d) {
          var syms = (d && d.symbols) || [];
          fillSymbols(symSel, syms);
          // Only force a symbol where the report cannot return anything without
          // one. Defaulting the floor sheet to a company hid the whole session
          // behind an arbitrary first-alphabetical pick.
          if (!ND.symbolOptional && !symSel.value && syms.length) {
            symSel.value = syms[0].symbol;
          }
          combo(symSel, syms);
          load();
        })
        .catch(function () { load(); });
    } else {
      load();
    }
  });
})();

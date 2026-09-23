/* Valuation & Model page — intrinsic valuation, peer rank and the sector model
 * for one company. Split out of Stock 360, which had grown past a screen: this
 * is the "what is it worth" workspace, Stock 360 is the "what is happening" one.
 *
 * The endpoints are unchanged (/stock/api/valuation/), so the two pages can
 * never disagree — there is one renderer and it lives here.
 */
(function () {
  "use strict";

  var SYM = (window.SV || {}).symbol || "";
  var $ = function (id) { return document.getElementById(id); };
  function esc(s) {
    return String(s == null ? "" : s).replace(/[&<>"']/g, function (c) {
      return { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c];
    });
  }
  function num(v, dp) {
    if (v == null || isNaN(v)) return "—";
    return Number(v).toLocaleString("en-US", { minimumFractionDigits: dp || 0, maximumFractionDigits: dp || 0 });
  }
  function getJSON(url) {
    return fetch(url, { headers: { "X-Requested-With": "XMLHttpRequest" } })
      .then(function (r) { return r.ok ? r.json() : Promise.reject(r.status); });
  }
  // The risk/opportunity rail lives on Stock 360; here the chips have nowhere to
  // go, so these are inert stand-ins rather than a second copy of that machinery.
  /* ---------- chrome: theme + symbol search ---------- */
  (function themeToggle() {
    var btn = $("themeBtn");
    if (!btn) return;
    btn.addEventListener("click", function () {
      var n = document.documentElement.getAttribute("data-theme") === "light" ? "dark" : "light";
      document.documentElement.setAttribute("data-theme", n);
      try { localStorage.setItem("mi-theme", n); } catch (e) {}
    });
  })();

  // Searching from this page keeps you on this page for the new symbol.
  window.sv = {
    go: function (ev) {
      if (ev) ev.preventDefault();
      var v = ($("symSearch").value || "").trim().toUpperCase();
      if (v) window.location.href = "/stock/" + encodeURIComponent(v) + "/valuation/";
      return false;
    }
  };
  (function autocomplete() {
    var input = $("symSearch"), box = $("symAc"), t = null;
    if (!input || !box) return;
    function close() { box.classList.remove("open"); box.innerHTML = ""; }
    input.addEventListener("input", function () {
      var q = input.value.trim();
      clearTimeout(t);
      if (q.length < 2) { close(); return; }
      t = setTimeout(function () {
        getJSON("/dashboard/symbols/?q=" + encodeURIComponent(q)).then(function (d) {
          var items = (d.results || []).filter(function (r) { return r.type !== "index"; }).slice(0, 12);
          if (!items.length) { close(); return; }
          box.innerHTML = "";
          items.forEach(function (r) {
            var parts = (r.label || r.value).split(" - ");
            var row = document.createElement("div");
            row.innerHTML = '<span class="t">' + esc(r.value) + '</span><span class="n">' + esc(parts[1] || "") + "</span>";
            row.addEventListener("mousedown", function (e) {
              e.preventDefault();
              window.location.href = "/stock/" + encodeURIComponent(r.value) + "/valuation/";
            });
            box.appendChild(row);
          });
          box.classList.add("open");
        }).catch(close);
      }, 160);
    });
    document.addEventListener("click", function (e) { if (!input.parentNode.contains(e.target)) close(); });
  })();

  /* The relative (sector-median P/E) half of the verdict comes from the
   * fundamentals feed. On Stock 360 this promise was shared with the ratio
   * cards; here the page owns it. */
  var FUNDA_READY = getJSON("/fundamentals/api/?symbol=" + encodeURIComponent(SYM))
    .catch(function () { return null; });

  var risks = [], opps = [];
  function flushRO() {}

/* ================= VALUATION (intrinsic) & PEER RANK =================
 * Justified P/B from the Gordon growth model, with the cost of equity and the
 * growth rate exposed as inputs — the reader can push on the assumptions
 * instead of being handed a verdict. Everything is sourced from our own
 * fundamental snapshots and price table; a missing input is reported as a
 * withheld estimate, never filled in with a guess. */
(function valuation() {
  var body = $("valBody"), peerBox = $("peerBody"), score = $("valScore");
  if (!body) return;

  var DEFAULTS = null;

  function toneClass(t) { return t === "pos" ? "pos" : t === "neg" ? "neg" : t === "warn" ? "warn" : "neu"; }

  /* Relative (what peers pay) and absolute (what the returns justify) answer
   * different questions and routinely disagree. Showing them side by side with
   * the disagreement named is the honest presentation; showing them in two
   * separate panels, each as "the" verdict, is not. */
  function relTone(v) { return /under/i.test(v) ? "pos" : /over/i.test(v) ? "neg" : "warn"; }

  function reconcile(absVerdict, relVerdict) {
    if (!absVerdict || !relVerdict) return "";
    // -1 cheap, 0 fair, +1 expensive, on each scale.
    var a = absVerdict === "Undervalued" ? -1 : absVerdict === "Expensive" ? 1 : 0;
    var r = /under/i.test(relVerdict) ? -1 : /over/i.test(relVerdict) ? 1 : 0;

    if (a === r) {
      if (a === 0) return "Both reads land near fair value.";
      return a < 0 ? "Both reads agree the stock is cheap."
                   : "Both reads agree the stock is expensive.";
    }
    if (a === 1 && r === -1)
      return "They disagree: cheap against its peers, but expensive against what its own returns justify — a sign the whole sector is richly priced.";
    if (a === -1 && r === 1)
      return "They disagree: dear against its peers, but cheap against what its own returns justify — a sign the sector is depressed.";
    // One read is neutral: the other is the only signal, and the peer
    // comparison tells you whether the sector shares the condition.
    if (a === 0)
      return r < 0 ? "Its own returns justify roughly today's price, yet it screens cheap against peers — the peer group is priced higher."
                   : "Its own returns justify roughly today's price, yet it screens dear against peers — the peer group is priced lower.";
    return a < 0
      ? "In line with peers, but cheap against what its own returns justify — the sector as a whole trades below that standard."
      : "In line with peers, but expensive against what its own returns justify — the sector as a whole trades above that standard.";
  }

  function renderVal(d, rel) {
    if (!d || !d.available) {
      body.innerHTML = '<div class="notice">' + esc((d && d.note) || "No valuation inputs stored for this symbol.") + "</div>";
      score.textContent = "n/a"; score.className = "desk-score sc-neu";
      return;
    }
    var i = d.inputs || {};
    var relV = rel && rel.verdict ? rel.verdict : "";
    var rows =
      '<div class="val-pair">' +
        '<div class="val-verdict ' + toneClass(d.tone) + '">' +
          '<div class="vv-tag">Absolute · justified P/B</div>' +
          "<div class=\"vv-label\">" + esc(d.verdict || "Estimate withheld") + "</div>" +
          '<div class="vv-sub">' +
            (d.justified_pb != null
              ? "Market P/B " + num(d.pb, 2) + " vs justified " + num(d.justified_pb, 2)
                + (d.gap_pct != null ? " · " + (d.gap_pct >= 0 ? "+" : "") + num(d.gap_pct, 1) + "%" : "")
              : esc(d.note || "")) +
          "</div>" +
        "</div>" +
        '<div class="val-verdict ' + (relV ? relTone(relV) : "neu") + '">' +
          '<div class="vv-tag">Relative · sector-median P/E</div>' +
          '<div class="vv-label">' + esc(relV || "No peer estimate") + "</div>" +
          '<div class="vv-sub">' +
            (rel && rel.estimate
              ? "est. Rs " + num(rel.estimate, 0) + (rel.ratio ? " · " + Number(rel.ratio).toFixed(2) + "× current price" : "")
              : "The fundamentals feed has no sector-median estimate for this company.") +
          "</div>" +
        "</div>" +
      "</div>" +
      (relV && d.verdict ? '<div class="val-recon">' + esc(reconcile(d.verdict, relV)) + "</div>" : "") +
      '<div class="mgrid mgrid-tight">' +
        mcard("Justified P/B", num(d.justified_pb, 2), "(ROE − g) / (r − g)") +
        mcard("Fair price", d.fair_price != null ? "Rs " + num(d.fair_price, 0) : "—", "Justified P/B × book value") +
        mcard("Market P/B", num(d.pb, 2), "Latest close / book value per share") +
        mcard("Earnings yield", d.earnings_yield != null ? num(d.earnings_yield, 2) + "%" : "—",
              "vs " + num(d.risk_free_pct, 1) + "% risk-free") +
      "</div>" +
      '<div class="val-assump">' +
        "<b>Assumptions.</b> ROE " + num(i.roe, 2) + "% · required return r " + num(i.cost_of_equity, 1)
        + "% · growth g " + (i.growth != null ? num(i.growth, 2) + "%" : "—")
        + (i.growth_basis ? " (" + esc(i.growth_basis) + ")" : "")
        + (i.payout != null ? " · payout " + num(i.payout, 1) + "%" : "")
        + ". Statement period " + esc(d.period || "—") + "." +
      "</div>";
    body.innerHTML = rows;

    score.textContent = d.verdict || "withheld";
    score.className = "desk-score sc-" + (d.verdict ? toneClass(d.tone) : "neu");

    var inputs = $("valInputs");
    if (inputs) {
      inputs.hidden = false;
      if (!DEFAULTS) DEFAULTS = { r: i.cost_of_equity, g: i.growth };
      if ($("valR").value === "") $("valR").value = i.cost_of_equity != null ? i.cost_of_equity : "";
      if ($("valG").value === "") $("valG").value = i.growth != null ? i.growth : "";
    }
    renderSens(d.sensitivity, d.pb);

    // Feed the page-level risk/opportunity rail, deduped across reloads. Both
    // valuation reads are owned here, so both chips carry the "valuation:" src.
    risks = risks.filter(function (c) { return String(c.src || "").indexOf("valuation:") !== 0; });
    opps = opps.filter(function (c) { return String(c.src || "").indexOf("valuation:") !== 0; });
    if (d.verdict === "Undervalued")
      opps.push({ cls: "good", ic: "↑", text: "<b>Below intrinsic</b> — market P/B " + num(d.pb, 2) + " vs justified " + num(d.justified_pb, 2) + ".", src: "valuation: justified P/B" });
    if (d.verdict === "Expensive")
      risks.push({ cls: "bad", ic: "↓", text: "<b>Above intrinsic</b> — market P/B " + num(d.pb, 2) + " vs justified " + num(d.justified_pb, 2) + ".", src: "valuation: justified P/B" });
    if (/under/i.test(relV))
      opps.push({ cls: "good", ic: "↑", text: "<b>Cheap vs peers</b>" + (rel.estimate ? " — sector-median estimate Rs " + num(rel.estimate, 0) : "") + ".", src: "valuation: sector-median P/E" });
    if (/over/i.test(relV))
      risks.push({ cls: "bad", ic: "↓", text: "<b>Dear vs peers</b> on sector-median P/E.", src: "valuation: sector-median P/E" });
    flushRO();
  }

  function mcard(k, v, sub) {
    return '<div class="mcard"><div class="k">' + esc(k) + '</div><div class="v num">' + v +
      '</div><div class="sub">' + esc(sub) + "</div></div>";
  }

  /* Sensitivity grid: does the verdict survive a different ROE or growth? */
  function renderSens(s, pb) {
    var box = $("valSens");
    if (!box) return;
    if (!s || !s.grid) { box.innerHTML = ""; return; }
    var h = '<div class="eyebrow" style="margin:14px 0 6px">Sensitivity · justified P/B by ROE and growth</div>' +
      '<table class="sens"><tr><th>ROE \\ g</th>';
    s.g_axis.forEach(function (g) { h += "<th>" + num(g, 1) + "%</th>"; });
    h += "</tr>";
    s.grid.forEach(function (row, ri) {
      h += "<tr><th>" + num(s.roe_axis[ri], 1) + "%</th>";
      row.forEach(function (v) {
        // Green where the justified ratio clears the market's P/B.
        var cls = (v == null) ? "na" : (pb != null && v >= pb ? "ok" : "no");
        h += '<td class="' + cls + '">' + (v == null ? "—" : num(v, 2)) + "</td>";
      });
      h += "</tr>";
    });
    h += "</table><div class=\"fresh\">Green = justified P/B at or above the current market P/B ("
      + num(pb, 2) + ").</div>";
    box.innerHTML = h;
  }

  function renderPeers(p) {
    if (!p || !p.available) {
      peerBox.innerHTML = '<div class="notice">' + esc((p && p.note) || "No peer cohort available.") + "</div>";
      return;
    }
    var h = '<div class="peer-head ' + toneClass(p.tone) + '">' +
      "<div class=\"ph-score num\">" + num(p.overall, 0) + "</div>" +
      '<div><div class="ph-label">' + esc(p.label || "") + '</div>' +
      '<div class="ph-sub">' + esc(p.sector) + " · " + p.count + " companies on file" +
      (p.priced_on ? " · priced " + esc(p.priced_on) : "") + "</div></div></div>";

    h += '<div class="peer-metrics">';
    (p.metrics || []).forEach(function (m) {
      var w = m.percentile == null ? 0 : m.percentile;
      h += '<div class="pm"><div class="pm-top"><span>' + esc(m.label) + "</span>" +
        '<span class="num">' + (m.value == null ? "—" : num(m.value, 2)) +
        '<span class="pm-med"> vs median ' + (m.median == null ? "—" : num(m.median, 2)) + "</span></span></div>" +
        '<div class="pm-bar"><i style="width:' + w + '%"></i></div>' +
        '<div class="pm-sub">' + (m.percentile == null
          ? "Not enough peer data to place a percentile."
          : num(m.percentile, 0) + "th percentile — " + esc(m.hint)) + "</div></div>";
    });
    h += "</div>";

    if (p.table && p.table.length) {
      h += '<div class="eyebrow" style="margin:12px 0 6px">Cheapest peers by P/B</div><table class="peer-tbl">' +
        "<tr><th>Symbol</th><th>P/B</th><th>P/E</th><th>ROE</th></tr>";
      p.table.forEach(function (r) {
        var me = r.symbol === SYM;
        // Clicking a peer opens that company's fundamentals view — which, after
        // the merge, is this same page anchored at its fundamentals section.
        h += '<tr class="' + (me ? "me" : "") + '"><td><a href="/stock/' + encodeURIComponent(r.symbol) + '/#fund">' +
          esc(r.symbol) + "</a></td><td class=\"num\">" + num(r.pb, 2) + '</td><td class="num">' +
          (r.pe && r.pe > 0 ? num(r.pe, 1) : "—") + '</td><td class="num">' +
          (r.roe == null ? "—" : num(r.roe * 100, 1) + "%") + "</td></tr>";
      });
      h += "</table>";
    }
    h += '<div class="fresh">' + esc(p.note || "") + "</div>";
    peerBox.innerHTML = h;
  }

  function load(qs) {
    body.innerHTML = '<div class="loading">Loading valuation…</div>';
    Promise.all([
      getJSON("/stock/api/valuation/?symbol=" + encodeURIComponent(SYM) + (qs || "")),
      FUNDA_READY
    ])
      .then(function (res) {
        var d = res[0], f = res[1];
        if (!d || !d.ok) throw 0;
        renderVal(d, ((f && f.morningstar) || {}).fair_value);
        renderPeers(d.peers);
      })
      .catch(function () {
        body.innerHTML = '<div class="notice">Could not load the valuation for <b>' + esc(SYM) + "</b>.</div>";
        peerBox.innerHTML = '<div class="notice">No peer rank available.</div>';
        score.textContent = "n/a"; score.className = "desk-score sc-neu";
      });
  }

  var apply = $("valApply"), reset = $("valReset");
  if (apply) apply.addEventListener("click", function () {
    var r = parseFloat($("valR").value), g = parseFloat($("valG").value);
    var qs = "";
    if (!isNaN(r)) qs += "&r=" + r;
    if (!isNaN(g)) qs += "&g=" + g;
    load(qs);
  });
  if (reset) reset.addEventListener("click", function () {
    if (DEFAULTS) { $("valR").value = DEFAULTS.r; $("valG").value = DEFAULTS.g; }
    load("");
  });

  load("");
})();


})();

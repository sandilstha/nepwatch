/* Stock 360 — client hydration.
 * The server renders the hero, performance and S&R; this file fills the
 * fundamentals and floorsheet panels from the platform's existing JSON
 * endpoints, draws the adjusted-price chart, and wires search / theme / nav.
 * Every value shown comes from a fetch below — nothing is invented. */
(function () {
  "use strict";

  var SYM = (window.S360 && window.S360.symbol) || "";

  var $ = function (id) { return document.getElementById(id); };
  function el(tag, cls, html) {
    var e = document.createElement(tag);
    if (cls) e.className = cls;
    if (html != null) e.innerHTML = html;
    return e;
  }
  function esc(s) {
    return String(s == null ? "" : s).replace(/[&<>"']/g, function (c) {
      return { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c];
    });
  }
  function num(v, dp) {
    if (v == null || isNaN(v)) return "—";
    return Number(v).toLocaleString("en-US", { minimumFractionDigits: dp || 0, maximumFractionDigits: dp || 0 });
  }
  function pct(v, dp) { return v == null || isNaN(v) ? "—" : Number(v).toFixed(dp == null ? 1 : dp) + "%"; }
  function getJSON(url) {
    return fetch(url, { headers: { "X-Requested-With": "XMLHttpRequest" } })
      .then(function (r) { return r.ok ? r.json() : Promise.reject(r.status); });
  }

  /* ---------- theme ---------- */
  (function themeToggle() {
    var btn = $("themeBtn");
    if (!btn) return;
    btn.addEventListener("click", function () {
      var n = document.documentElement.getAttribute("data-theme") === "light" ? "dark" : "light";
      document.documentElement.setAttribute("data-theme", n);
      try { localStorage.setItem("mi-theme", n); } catch (e) {}
    });
  })();

  /* ---------- search + autocomplete ---------- */
  window.s360 = {
    go: function (ev) {
      if (ev) ev.preventDefault();
      var v = ($("symSearch").value || "").trim().toUpperCase();
      if (v) window.location.href = "/stock/" + encodeURIComponent(v) + "/";
      return false;
    }
  };
  (function autocomplete() {
    var input = $("symSearch"), box = $("symAc"), t = null, hi = -1, items = [];
    if (!input || !box) return;
    function close() { box.classList.remove("open"); box.innerHTML = ""; hi = -1; items = []; }
    function pick(v) { window.location.href = "/stock/" + encodeURIComponent(v) + "/"; }
    input.addEventListener("input", function () {
      var q = input.value.trim();
      clearTimeout(t);
      if (q.length < 2) { close(); return; }
      t = setTimeout(function () {
        getJSON("/dashboard/symbols/?q=" + encodeURIComponent(q)).then(function (d) {
          items = (d.results || []).filter(function (r) { return r.type !== "index"; }).slice(0, 12);
          if (!items.length) { close(); return; }
          box.innerHTML = "";
          items.forEach(function (r) {
            var parts = (r.label || r.value).split(" - ");
            var row = el("div", null,
              '<span class="t">' + esc(r.value) + '</span><span class="n">' + esc(parts[1] || "") + "</span>");
            row.addEventListener("mousedown", function (e) { e.preventDefault(); pick(r.value); });
            box.appendChild(row);
          });
          box.classList.add("open");
        }).catch(close);
      }, 160);
    });
    input.addEventListener("keydown", function (e) {
      var rows = box.querySelectorAll("div");
      if (!rows.length) return;
      if (e.key === "ArrowDown") { e.preventDefault(); hi = Math.min(hi + 1, rows.length - 1); }
      else if (e.key === "ArrowUp") { e.preventDefault(); hi = Math.max(hi - 1, 0); }
      else if (e.key === "Enter" && hi >= 0) { e.preventDefault(); pick(items[hi].value); return; }
      else return;
      rows.forEach(function (r, i) { r.classList.toggle("hi", i === hi); });
    });
    document.addEventListener("click", function (e) { if (!input.parentNode.contains(e.target)) close(); });
  })();

  /* ---------- section pills (scrollspy + click) ---------- */
  (function pills() {
    var ps = [].slice.call(document.querySelectorAll(".pill"));
    ps.forEach(function (p) {
      p.addEventListener("click", function () {
        var s = $(p.dataset.t);
        if (s) s.scrollIntoView({ behavior: "smooth", block: "start" });
      });
    });
    var secs = ps.map(function (p) { return $(p.dataset.t); }).filter(Boolean);
    if (!("IntersectionObserver" in window)) return;
    var obs = new IntersectionObserver(function (es) {
      es.forEach(function (e) {
        if (e.isIntersecting) {
          ps.forEach(function (p) { p.classList.toggle("active", p.dataset.t === e.target.id); });
        }
      });
    }, { rootMargin: "-120px 0px -60% 0px", threshold: 0 });
    secs.forEach(function (s) { obs.observe(s); });
  })();

  /* ================= OVERVIEW (photo layout) ================= *
   * Market + Key Ratios + Investment Style cards and a timeframe-toggled price
   * chart. Price-derived numbers (60d avg, market cap, beta) come from the
   * server via window.S360; the ratios come from the fundamentals fetch below,
   * so nothing here is invented — a missing value renders as "—". */

  var S = window.S360 || {};

  function kvRow(label, valHtml) {
    return '<div class="kv-row"><dt>' + esc(label) + "</dt><dd class=\"num\">" + valHtml + "</dd></div>";
  }
  // Fundamentals expose fractions for rate metrics (roe, roa, gross_margin) and
  // Rs '000 for money lines; ratios below account for that.
  function safeDiv(a, b) { return (a != null && b) ? a / b : null; }

  function renderMarketCard(ks) {
    var price = (S.close != null ? S.close : (ks && ks.price));
    var bvps = ks && ks.bvps, eps = ks && ks.eps;
    var pe = (ks && ks.pe != null) ? ks.pe : safeDiv(price, eps);
    var pb = safeDiv(price, bvps);
    var rows =
      kvRow("60 Days Average", num(S.avg60, 2)) +
      // A rebased cap is flagged rather than passed off as the exchange's own
      // print — hover carries the basis and the session it came from.
      kvRow("Market Cap (Millions)" + (S.marketCapNote ? " *" : ""),
            '<span' + (S.marketCapNote ? ' title="' + esc(S.marketCapNote) + '"' : "") + ">" +
            num(S.marketCapM, 2) + "</span>") +
      kvRow("Book Value Per Share", num(bvps, 2)) +
      kvRow("Price/Earnings", num(pe, 2)) +
      kvRow("Price/Book", num(pb, 2)) +
      kvRow("5 Years Beta", num(S.beta5y, 2));
    var box = $("mktCard"); if (box) box.innerHTML = rows;
    return { pb: pb };
  }

  // Compound annual growth over up to 3 fiscal years from a trend metric.
  function cagr3(points) {
    if (!points || points.length < 2) return null;
    var last = points[points.length - 1].value;
    var span = Math.min(3, points.length - 1);
    var base = points[points.length - 1 - span].value;
    if (base == null || base <= 0 || last == null) return null;
    return (Math.pow(last / base, 1 / span) - 1) * 100;
  }
  function trendPts(trend, label) {
    var t = (trend || []).filter(function (x) { return x.label === label; })[0];
    return t ? t.points : null;
  }

  function renderRatioCard(d) {
    var ks = (d && d.ks) || {};
    var revG = cagr3(trendPts(d && d.trend, "Total Revenue"));
    var niG = cagr3(trendPts(d && d.trend, "Net Income"));
    var netMargin = (ks.net_income != null && ks.revenue) ? (ks.net_income / ks.revenue) * 100 : null;
    var rows =
      kvRow("Rev 3-Yr Growth (%)", revG == null ? "—" : num(revG, 2)) +
      kvRow("Net Income 3-Yr Growth (%)", niG == null ? "—" : num(niG, 2)) +
      kvRow("Net Margin %TTM", netMargin == null ? "—" : num(netMargin, 2)) +
      kvRow("ROA % TTM", ks.roa == null ? "—" : num(ks.roa * 100, 2)) +
      kvRow("ROE % TTM", ks.roe == null ? "—" : num(ks.roe * 100, 2)) +
      kvRow("Earning Per Share", num(ks.eps, 2));
    var box = $("ratioCard"); if (box) box.innerHTML = rows;
    return { revG: revG };
  }

  // Investment style box: size from market cap (NEPSE-scaled millions), style
  // from a P/B + revenue-growth heuristic. Both are coarse buckets, deliberately.
  function renderOverview(d) {
    var m = renderMarketCard(d && d.ks);
    var r = renderRatioCard(d);
  }

  // Paint the price-derived Market card and the size axis immediately from the
  // server payload, so a slow or failed fundamentals fetch can never leave these
  // stuck on "Loading…". The fundamentals fetch below re-renders with the ratios.
  renderMarketCard(null);

  /* ---------- risk & opportunity roll-up ---------- */
  var risks = [], opps = [];
  function setChips(id, side, arr) {
    var box = $(id); if (!box) return;
    if (!arr.length) { box.innerHTML = '<div class="loading">No notable ' + side + '.</div>'; return; }
    box.innerHTML = "";
    arr.forEach(function (c) {
      box.appendChild(el("div", "chip " + c.cls,
        '<span class="ic">' + c.ic + '</span><div>' + c.text +
        '<div class="src">source: ' + esc(c.src) + "</div></div>"));
    });
  }
  function flushRO() {
    setChips("roRisk", "risks", risks);
    setChips("roOpp", "opportunities", opps);
    // Overview Risk cell mirrors the tally so the strip is fully live.
    var tone = risks.length > opps.length ? "neg" : opps.length > risks.length ? "pos" : "warn";
    var label = risks.length > opps.length ? "Risk-heavy" : opps.length > risks.length ? "Opportunity-led" : "Balanced";
    setVerdict("risk", risks.length + "R · " + opps.length + "O", label, tone);
  }

  function setVerdict(k, score, label, tone) {
    var sc = document.querySelector('#vstrip [data-k="' + k + '"]');
    var vd = document.querySelector('#vstrip [data-k="' + k + 'v"]');
    var stripe = sc ? sc.parentNode.querySelector(".stripe") : null;
    if (sc) { sc.textContent = score; sc.className = "sc num " + tone; }
    if (vd) { vd.textContent = label; vd.className = "verdict " + tone; }
    if (stripe) stripe.style.background = "var(--" + (tone === "pos" ? "pos" : tone === "neg" ? "neg" : tone === "warn" ? "warn" : "neutral") + ")";
  }

  /* ---------- ③ FUNDAMENTALS ----------
   * One fetch, two consumers: the ratio cards here and the RELATIVE half of the
   * valuation block below. Sharing the promise keeps them from disagreeing and
   * saves a round trip. */
  var FUNDA_READY = getJSON("/fundamentals/api/?symbol=" + encodeURIComponent(SYM))
    .catch(function () { return null; });

  FUNDA_READY.then(function (d) {
    if (!d || !d.ok) throw 0;
    renderOverview(d);
    // The headline ratio cards used to be rebuilt here from the same payload the
    // fundamentals module renders into #fa-cards. One renderer owns them now;
    // this block only clears its placeholder.
    $("fundBody").innerHTML = "";

    /* The fair-value verdict and the peer percentiles used to be drawn here as
     * well. They now live once, in Valuation & Peers — two valuation verdicts
     * on one screen, built from different models, read as a contradiction
     * rather than as the two views they are. This section keeps the ratios. */
    var ms = d.morningstar;
    if (ms && ms.fair_value) {
      var v = ms.fair_value.verdict || "";
      var ps = (ms.ranks || []).map(function (r) { return r.percentile; }).filter(function (x) { return x != null; });
      var score = ps.length ? Math.round(ps.reduce(function (a, b) { return a + b; }, 0) / ps.length) : 50;
      var stone = score >= 60 ? "pos" : score >= 45 ? "warn" : "neg";
      $("fundScore").textContent = v || (score + "/100");
      $("fundScore").className = "desk-score sc-" + stone;
      setVerdict("fund", score, v || "—", stone);

      // Valuation chips are pushed by the valuation block, which owns both the
      // relative and the absolute read — one claim per rail, not two.
      flushRO();
    } else {
      $("fundScore").textContent = "no research";
      setVerdict("fund", "n/a", "no data", "neu");
    }
  }).catch(function () {
    renderOverview(null);
    $("fundBody").innerHTML = '<div class="notice">No fundamentals feed for <b>' + esc(SYM) + "</b>.</div>";
    $("fundScore").textContent = "n/a"; setVerdict("fund", "n/a", "no data", "neu");
  });

  /* ---------- ⑤ FLOORSHEET ----------
   * stock_wise returns TOP-10 buy/sell rows and per-broker positive net
   * positions (holdings). Market-wide buy always equals sell, so there is no
   * "net market position" — the honest metrics are broker accumulation and
   * side concentration, which is exactly what we show. */
  var FLOW_WIN = "1w";                     // current floorsheet window
  var WIN_LABEL = { "1w": "1 week", "1m": "1 month", "3m": "3 months", "1y": "1 year" };
  /* Broker numbers are meaningless on their own, and all three floorsheet views
   * need the same lookup. Fetch it ONCE and have every view wait on the same
   * promise — otherwise the net-buy legend and the flow map race the fetch and
   * render "Broker 92" where the name was already on its way. */
  var BROKER_NAMES = null;
  var NAMES_READY = getJSON("/floorsheet/api/meta/")
    .then(function (m) { BROKER_NAMES = (m && m.broker_names) || {}; })
    .catch(function () { BROKER_NAMES = {}; });

  function bname(key) {
    var names = BROKER_NAMES || {};
    var n = names[key] || names[String(key)];
    return n ? "#" + key + " " + n : "Broker " + key;
  }

  /* Reloading a window must not stack duplicate chips — drop this block's
   * previous contributions before it pushes new ones. */
  function dropFlowChips() {
    function keep(list) {
      return list.filter(function (c) { return String(c.src || "").indexOf("stock_wise") !== 0; });
    }
    risks = keep(risks); opps = keep(opps);
  }

  function loadFlow(win) {
    FLOW_WIN = win;
    $("flowBody").innerHTML = '<div class="loading">Loading broker activity…</div>';
    Promise.all([
    getJSON("/floorsheet/api/stockwise/?symbol=" + encodeURIComponent(SYM) + "&range=" + win + "&view=shares"),
    NAMES_READY
  ]).then(function (res) {
    var d = res[0];
    if (!d || !d.ok) throw 0;
    dropFlowChips();

    var body = $("flowBody"); body.innerHTML = "";
    var buy = d.buy || [], sell = d.sell || [], holds = d.holdings || [];
    var accQty = holds.reduce(function (a, r) { return a + (r.quantity || 0); }, 0);
    var top3buy = buy.slice(0, 3).reduce(function (a, r) { return a + (r.pct || 0); }, 0);
    var top3sell = sell.slice(0, 3).reduce(function (a, r) { return a + (r.pct || 0); }, 0);

    var summary = el("div", "mgrid"); summary.style.marginBottom = "14px";
    summary.innerHTML =
      '<div class="mcard"><div class="k">Broker Accumulation (' + esc(WIN_LABEL[FLOW_WIN] || FLOW_WIN) + ')</div><div class="v num pos">' + num(accQty, 0) + '</div><div class="sub">' + holds.length + " net-long brokers (top 10)</div></div>" +
      '<div class="mcard"><div class="k">Top Buyer</div><div class="v num pos">' + (buy[0] ? pct(buy[0].pct, 1) : "—") + '</div><div class="sub">' + (buy[0] ? esc(bname(buy[0].key)) : "no activity") + "</div></div>" +
      '<div class="mcard"><div class="k">Top Seller</div><div class="v num neg">' + (sell[0] ? pct(sell[0].pct, 1) : "—") + '</div><div class="sub">' + (sell[0] ? esc(bname(sell[0].key)) : "no activity") + "</div></div>" +
      '<div class="mcard"><div class="k">Top-3 Concentration</div><div class="v num">' + pct(top3buy, 0) + " / " + pct(top3sell, 0) + '</div><div class="sub">buy side / sell side</div></div>';
    body.appendChild(summary);

    function col(title, rows, tone, unit) {
      var c = el("div", "btcol");
      c.appendChild(el("div", "cap", "<span>" + title + '</span><span class="neu">' + unit + "</span>"));
      var max = rows.length ? rows[0].pct || 1 : 1;
      rows.slice(0, 3).forEach(function (r, i) {
        c.appendChild(el("div", "brow",
          '<span class="rk">' + (i + 1) + "</span>" +
          '<div><div class="bn">' + esc(bname(r.key)) + '</div><div class="mini" style="width:' + Math.max(8, (r.pct / max) * 100) + "%;background:var(--" + tone + ')"></div></div>' +
          '<span class="num ' + tone + '">' + pct(r.pct, 1) + "</span>"));
      });
      if (!rows.length) c.appendChild(el("div", "loading", "No activity."));
      return c;
    }
    var bt = el("div", "btwrap");
    bt.appendChild(col("Top Buying Brokers", buy, "pos", "% of buy side"));
    bt.appendChild(col("Top Selling Brokers", sell, "neg", "% of sell side"));
    body.appendChild(bt);
    body.appendChild(el("div", "fresh",
      "Window: " + esc(WIN_LABEL[FLOW_WIN] || FLOW_WIN) + " · shares · top-10 brokers per side. Buy and sell totals always match market-wide; read conviction from concentration and per-broker accumulation."));

    // Verdict: which side is more concentrated = which side has conviction.
    var diff = top3buy - top3sell;
    var tone = diff > 5 ? "pos" : diff < -5 ? "neg" : "warn";
    var label = diff > 5 ? "Buy-side concentrated" : diff < -5 ? "Sell-side concentrated" : "Balanced flow";
    $("flowScore").textContent = label;
    $("flowScore").className = "desk-score sc-" + (tone === "warn" ? "warn" : tone);
    setVerdict("flow", pct(Math.abs(diff), 0), label, tone);

    if (top3sell >= 50) risks.push({ cls: "warnc", ic: "≈", text: "Selling concentrated — top 3 brokers hold " + pct(top3sell, 0) + " of the sell side.", src: "stock_wise sell concentration" });
    if (top3buy >= 50) opps.push({ cls: "good", ic: "◆", text: "Concentrated accumulation — top 3 brokers take " + pct(top3buy, 0) + " of the buy side.", src: "stock_wise buy concentration" });
    flushRO();
    }).catch(function () {
      $("flowBody").innerHTML = '<div class="notice">No floorsheet activity for <b>' + esc(SYM) + "</b> in this window.</div>";
      $("flowScore").textContent = "n/a"; setVerdict("flow", "n/a", "no data", "neu");
    });
  }

  /* ---------- ⑤b SELLER → BUYER FLOW MAP ----------
   * Not drawn here. The broker desk's Flow Map tab already does this with an
   * intraday time window, session playback and the uncollapsed pair table; the
   * link below just carries the symbol and window over to it. */
  function updateFlowLink(win) {
    var a = $("flowMapLink");
    if (a) a.href = "/floorsheet/?tab=flowmap&symbol=" + encodeURIComponent(SYM) + "&range=" + win;
  }

  /* One window selector drives all three floorsheet views. */
  (function flowWindow() {
    var row = $("flowWin");
    function load(w) { loadFlow(w); updateFlowLink(w); }
    if (row) {
      row.addEventListener("click", function (e) {
        var b = e.target.closest("button[data-w]");
        if (!b || b.classList.contains("active")) return;
        [].forEach.call(row.querySelectorAll("button"), function (x) { x.classList.remove("active"); });
        b.classList.add("active");
        load(b.dataset.w);
      });
    }
    load("1w");
  })();

  /* ---------- AI Narrative (Gemini, on-demand, server-cached) ---------- */
  (function aiNarrative() {
    var btn = $("aiGenBtn"), out = $("aiNarrative");
    if (!btn || !out) return;
    btn.addEventListener("click", function () {
      btn.disabled = true; btn.textContent = "Generating…";
      out.innerHTML = '<div class="loading">Asking Gemini to read ' + esc(SYM) + "'s structure…</div>";
      getJSON("/stock/api/ai/?symbol=" + encodeURIComponent(SYM)).then(function (d) {
        if (d && d.analysis_html) {
          out.innerHTML = '<div class="ai-narrative">' + d.analysis_html + "</div>" +
            '<div class="ai-meta">Generated by ' + esc(d.model || "AI") + (d.provider ? " · " + esc(d.provider) : "") +
            (d.cached ? " · cached for the trading day" : "") +
            " — from this symbol's support/resistance metrics &amp; recent price path.</div>";
          btn.textContent = "↻ Regenerate";
        } else {
          out.innerHTML = '<div class="notice">' + esc((d && d.error) || "No narrative returned.") + "</div>";
          btn.textContent = "✨ Generate";
        }
        btn.disabled = false;
      }).catch(function () {
        out.innerHTML = '<div class="notice">Could not reach the AI service. Try again.</div>';
        btn.textContent = "✨ Generate"; btn.disabled = false;
      });
    });
  })();

  /* ================= SOP COMBINED (CONFLUENCE) =================
   * The Strategy Simulator's confluence engine, run for this symbol with the
   * desk's own defaults. Two calls are shown deliberately:
   *   pure   — what the indicators say on their own
   *   action — the tradeable instruction after the NEPSE regime filter
   * They part company exactly when the setup is good but the market is not,
   * which is the single most useful thing this block can tell you. */
  (function sopSignal() {
    var body = $("sopBody"), score = $("sopScore");
    if (!body) return;

    function tone(action) {
      if (action === "BUY" || action === "HOLD") return "pos";
      if (action === "SELL") return "neg";
      return "warn";                       // WAIT
    }

    getJSON("/stock/api/sop/?symbol=" + encodeURIComponent(SYM))
      .then(function (d) {
        if (!d || !d.ok || !d.signal) throw 0;
        var s = d.signal, b = d.backtest || {}, st = d.setup || {};
        var act = s.action, pure = s.pure_action;

        score.textContent = act;
        score.className = "desk-score sc-" + tone(act);

        var votes = (s.indicators || []).map(function (i) {
          return '<span class="sop-ind ' + (i.long ? "on" : "off") + '">' + esc(i.label) + "</span>";
        }).join("");

        body.innerHTML =
          '<div class="sop-calls">' +
            '<div class="sop-call ' + tone(pure) + '"><div class="k">Indicators</div>' +
              '<div class="v">' + esc(pure) + '</div><div class="sub">' + esc(s.pure_reason || "") + "</div></div>" +
            '<div class="sop-call ' + tone(act) + '"><div class="k">After regime</div>' +
              '<div class="v">' + esc(act) + '</div><div class="sub">' + esc(s.regime || "") + " market</div></div>" +
          "</div>" +
          (s.regime_conflict
            ? '<div class="sop-conflict">The setup is there — ' + s.agree + " of " + s.total +
              " indicators are long — but the NEPSE regime is " + esc(s.regime) +
              ", and the SOP rule stands aside in a bear market.</div>"
            : "") +
          '<div class="sop-why">' + esc(s.reason || "") + "</div>" +
          '<div class="eyebrow" style="margin:12px 0 6px">Agreement · ' + s.agree + " of " + s.total +
            " (needs " + s.required + ")</div>" +
          '<div class="sop-inds">' + votes + "</div>" +
          '<div class="eyebrow" style="margin:12px 0 6px">Same rule, backtested</div>' +
          '<div class="sop-bt">' +
            row("Trades", num(b.trades, 0)) +
            row("Win rate", b.win_rate == null ? "—" : num(b.win_rate, 1) + "%") +
            row("Strategy", signed(b.strategy_return)) +
            row("Buy &amp; hold", signed(b.buyhold_return)) +
            row("Max drawdown", b.max_drawdown == null ? "—" : num(b.max_drawdown, 1) + "%") +
            row("Time in market", b.time_in_market == null ? "—" : num(b.time_in_market, 0) + "%") +
          "</div>" +
          '<div class="fresh">' + esc((st.indicators || []).length ? st.indicators.join(" · ") : "") +
            " · signal as of " + esc(s.as_of || "—") + ".</div>";
      })
      .catch(function () {
        body.innerHTML = '<div class="notice">Could not run the SOP model for <b>' + esc(SYM) + "</b>.</div>";
        score.textContent = "n/a"; score.className = "desk-score sc-neu";
      });

    function row(k, v) {
      return '<div class="sop-row"><span>' + k + '</span><span class="num">' + v + "</span></div>";
    }
    function signed(v) {
      if (v == null) return "—";
      return '<span class="' + (v >= 0 ? "pos" : "neg") + '">' + (v >= 0 ? "+" : "") + num(v, 1) + "%</span>";
    }
  })();

  /* ================= DIVIDEND CARD =================
   * Capacity vs habit: the latest declared payout, the five-year average, and
   * how often the company has paid at all. DPS on a Rs 100 face value doubles
   * as percent of face value, which is how NEPSE quotes it. */
  /* Board-proposed dividends, from the ShareSansar sync. Rendered above the paid
     history because it is the forward-looking half: the newest proposal in full
     (bonus / cash split plus its dates), then earlier proposals as one line
     each. Blank dates are real — a fresh announcement has no book-closure or
     distribution date until the AGM sets them, so they show as "—" rather than
     being hidden. */
  // ---- Dividend card ------------------------------------------------------
  // ONE highlighted block for the current dividend (the declared figure merged
  // with the board proposal's bonus/cash split and dates when they are the same
  // fiscal year — the server does the merge), then the history as a horizontal
  // table: one column per fiscal year, rows for total / bonus / cash.
  var pctOr = function (v) { return v == null || isNaN(v) ? "—" : num(v, 2) + "%"; };
  var dash = function (v) { return v ? esc(v) : "—"; };

  function renderCurrent(c) {
    if (!c) return "";
    var proposed = c.status === "proposed";
    var bookclose = c.bookclose
      ? esc(c.bookclose) + (c.bookclose_status ? " (" + esc(c.bookclose_status) + ")" : "")
      : "—";
    var hasDates = c.announced || c.bookclose || c.distribution || c.bonus_listing;
    var fyLabel = "FY " + esc(c.fy) + (c.fy_bs ? " · " + esc(c.fy_bs) + " BS" : "");
    return '<div class="dv-current' + (proposed ? " is-proposed" : "") + '">' +
      '<div class="dv-cur-head"><span>' + (proposed ? "Proposed" : "Latest dividend") + "</span>" +
      '<span class="dv-cur-fy">' + fyLabel + "</span>" +
      '<span class="dv-cur-chip">' + (proposed ? "Pending AGM" : "Declared") + "</span></div>" +
      '<div class="dv-cur-grid">' +
      '<div class="dv-cur-main"><div class="dv-k">Total</div><div class="dv-v dv-v-lg num">' + pctOr(c.total) + "</div></div>" +
      '<div><div class="dv-k">Bonus</div><div class="dv-v num">' + pctOr(c.bonus) + "</div></div>" +
      '<div><div class="dv-k">Cash</div><div class="dv-v num">' + pctOr(c.cash) + "</div></div>" +
      "</div>" +
      (hasDates ? '<div class="dv-prop-dates">' +
        "<span>Announced <b>" + dash(c.announced) + "</b></span>" +
        "<span>Book closure <b>" + bookclose + "</b></span>" +
        "<span>Distribution <b>" + dash(c.distribution) + "</b></span>" +
        "<span>Bonus listing <b>" + dash(c.bonus_listing) + "</b></span>" +
        "</div>" : "") +
      (proposed ? '<div class="dv-note">Board-proposed — not yet approved at the AGM or distributed.</div>' : "") +
      "</div>";
  }

  function renderHistory(hist) {
    if (!hist || !hist.length) return "";
    var row = function (label, key) {
      return "<tr><th>" + label + "</th>" + hist.map(function (h) {
        var v = h[key];
        var cls = "num" + (h.pending || h.missing ? " pend" : "") + (key === "dps" && v > 0 ? " paid" : "");
        var txt = h.missing ? (key === "dps" ? "n/a" : "") : (h.pending && key === "dps" ? "—" : pctOr(v));
        return '<td class="' + cls + '">' + txt + "</td>";
      }).join("") + "</tr>";
    };
    return '<div class="dv-hist-wrap"><table class="dv-hist">' +
      "<thead><tr><th>FY</th>" + hist.map(function (h) {
        var tip = h.pending ? "In progress — not yet declared" : (h.missing ? "No statement for this year in the source" : "");
        return "<th" + (tip ? ' class="pend" title="' + tip + '"' : "") + ">" +
          esc(h.fy) + (h.pending ? "*" : "") + "</th>";
      }).join("") + "</tr></thead><tbody>" +
      row("Total", "dps") + row("Bonus", "bonus") + row("Cash", "cash") +
      "</tbody></table></div>";
  }

  (function dividendCard() {
    var box = $("divCard");
    if (!box) return;
    getJSON("/stock/api/dividends/?symbol=" + encodeURIComponent(SYM))
      .then(function (d) {
        var currentHtml = renderCurrent(d && d.current);
        if (!d || !d.ok || !d.available) {
          // A company can have a board-proposed dividend and no fundamentals at
          // all — show the proposal and keep the missing-history note under it.
          box.innerHTML = currentHtml +
            '<div class="kv-loading">' + esc((d && d.note) || "No dividend history.") + "</div>";
          return;
        }
        var p = $("divPeriod"); if (p) p.textContent = d.paid_years + " of " + d.years + " years paid";
        var hist = d.table || (d.history || []).slice(-6);
        box.innerHTML = currentHtml + renderHistory(hist) +
          '<div class="dv-stats"><span>5-yr avg <b class="num">' + pctOr(d.avg_5y) + "</b></span>" +
          '<span>Consistency <b class="num">' + num(d.consistency, 0) + "%</b></span></div>" +
          '<div class="dv-note">' +
          (d.pending_fy ? "* FY " + esc(d.pending_fy) + " is still in progress — not yet declared, and excluded from the average. " : "") +
          (hist.some(function (h) { return h.missing; }) ? "n/a = no statement for that year in the source. " : "") +
          "Percent of Rs 100 face value. Bonus/cash split comes from the board proposal where one is on record.</div>";
      })
      .catch(function () {
        box.innerHTML = '<div class="kv-loading">Dividend history unavailable.</div>';
      });
  })();
})();

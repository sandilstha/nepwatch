/* ============================================================================
   Risk & Portfolio Desk — frontend controller.
   Fetches the per-user valuation/risk payload from /portfolio/api/data/ and
   renders the KPI strip, sector-exposure bars, concentration card and holdings
   table. Read-only; the import itself is a plain multipart form POST.
   ========================================================================== */
(function () {
  "use strict";
  bindFileInputs();
  bindHelpLinks();
  bindPortfolioSelector();
  bindPortfolioRename();    // archived-row rename — runs even with no holdings
  bindPortfolioDelete();    // safe confirmation for permanent portfolio deletion
  initApprovals();          // admin notification bell — runs even with no holdings
  if (!window.PF_HAS_HOLDINGS) return;

  function el(id) { return document.getElementById(id); }
  function nf(n) { return (n == null ? 0 : n).toLocaleString("en-IN"); }
  function rs(n) { return "Rs " + nf(Math.round(n || 0)); }
  function pct(n) { return (n == null ? 0 : n).toFixed(2) + "%"; }
  function rsCompact(n) {
    var v = Math.round(n || 0), s = v < 0 ? "-" : "", a = Math.abs(v);
    if (a >= 1e7) return s + "Rs " + (a / 1e7).toFixed(2) + " Cr";
    if (a >= 1e5) return s + "Rs " + (a / 1e5).toFixed(2) + " L";
    return rs(v);
  }
  function signedRs(n) {
    var v = Math.round(n || 0);
    return (v >= 0 ? "+" : "-") + "Rs " + nf(Math.abs(v));
  }
  function liqRiskLabel(key) {
    return ({
      liquid: "Low",
      moderate: "Moderate",
      illiquid: "High",
      untradeable: "Very High"
    })[key] || key || "Very High";
  }
  function esc(s) {
    return String(s == null ? "" : s).replace(/[&<>"']/g, function (c) {
      return { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c];
    });
  }

  // The SOP is reached from the section-heading (?) icons in the template and the
  // "methodology" link in the summary note; both open in a new tab so the user
  // never loses their scenario input / filter state on the desk.
  var SOP_URL = window.PF_SOP_URL || "/portfolio/sop/";
  function bindHelpLinks() {           // template-rendered icons get the same behaviour
    Array.prototype.forEach.call(document.querySelectorAll("a.pf-help"), function (a) {
      a.target = "_blank";
      a.rel = "noopener";
    });
  }

  function bindFileInputs() {
    Array.prototype.forEach.call(document.querySelectorAll(".pf-file-input"), function (input) {
      var wrap = input.closest(".pf-upload");
      var name = wrap ? wrap.querySelector(".pf-file-name") : null;
      if (!name) return;
      function syncName() {
        var label = input.files && input.files.length
          ? input.files[0].name
          : (name.getAttribute("data-empty-label") || "No file selected");
        name.textContent = label;
        name.title = label;
      }
      input.addEventListener("change", syncName);
      input.addEventListener("change", function () {
        var form = input.form;
        if (
          !form ||
          !form.hasAttribute("data-pf-auto-upload") ||
          !input.files ||
          !input.files.length
        ) return;
        Array.prototype.forEach.call(
          form.querySelectorAll('button[type="submit"]'),
          function (button) {
            button.disabled = true;
            button.textContent = "Importing…";
          }
        );
        if (form.requestSubmit) form.requestSubmit();
        else form.submit();
      });
      syncName();
    });
  }

  function bindPortfolioSelector() {
    var selector = document.querySelector("[data-pf-selector]");
    if (!selector || selector._pfBound) return;
    selector._pfBound = true;
    selector.addEventListener("change", function () {
      if (selector.form) selector.form.submit();
    });
  }

  // ── Inline portfolio rename ───────────────────────────────────────────
  // Archived rows carry [data-pf-rename] with the portfolio id + current name.
  // We prompt
  // for a new name and POST the existing "rename" action to /portfolio/manage/
  // (same endpoint the manager panel's Rename form uses) — the server does the
  // final normalise / dup-name check and flashes the result.
  function bindPortfolioRename() {
    var manageUrl = window.PF_MANAGE_URL;
    if (!manageUrl) return;
    document.addEventListener("click", function (e) {
      var btn = e.target.closest ? e.target.closest("[data-pf-rename]") : null;
      if (!btn) return;
      e.preventDefault();
      e.stopPropagation();
      var id = btn.getAttribute("data-id");
      var current = btn.getAttribute("data-name") || "";
      if (!id) return;
      var next = window.prompt("Rename portfolio", current);
      if (next == null) return;                       // cancelled
      next = next.replace(/\s+/g, " ").trim();
      if (!next || next === current.trim()) return;   // empty or unchanged
      submitManage(manageUrl, { action: "rename", portfolio_id: id, name: next });
    });
  }

  // Confirm permanent deletion without interpolating a user-controlled
  // portfolio name into inline JavaScript.
  function bindPortfolioDelete() {
    function confirmDelete(name) {
      return window.confirm(
        'Delete "' + (name || "this portfolio") +
        '" and all of its holdings, WACC and broker-ledger rows? This cannot be undone.'
      );
    }

    document.addEventListener("submit", function (e) {
      var form = e.target.closest ? e.target.closest("[data-pf-delete]") : null;
      if (form && !confirmDelete(form.getAttribute("data-name"))) {
        e.preventDefault();
      }
    });

    document.addEventListener("click", function (e) {
      var button = e.target.closest ? e.target.closest("[data-pf-delete-button]") : null;
      if (button && !confirmDelete(button.getAttribute("data-name"))) {
        e.preventDefault();
      }
    });
  }

  // Build and submit a throwaway POST form to the portfolio-manage endpoint.
  function submitManage(url, fields) {
    var form = document.createElement("form");
    form.method = "post";
    form.action = url;
    form.style.display = "none";
    fields.csrfmiddlewaretoken = window.PF_CSRF || "";
    Object.keys(fields).forEach(function (name) {
      var input = document.createElement("input");
      input.type = "hidden";
      input.name = name;
      input.value = fields[name];
      form.appendChild(input);
    });
    document.body.appendChild(form);
    form.submit();
  }

  // ── Admin notification bell — pending account requests ────────────────
  // Staff only. Polls the approvals API, renders a dropdown, and approves /
  // rejects inline (same server path as the admin action). Silently no-ops for
  // non-staff, so ordinary users never see it.
  function initApprovals() {
    var wrap = el("pf-bell-wrap");
    if (!wrap || !window.PF_IS_APPROVER) return;
    var bell = el("pf-bell"), panel = el("pf-notif"), badge = el("pf-bell-badge");
    var list = el("pf-notif-list"), count = el("pf-notif-count");
    var open = false, busy = false;

    function setBadge(n) {
      if (!badge) return;
      badge.textContent = n > 99 ? "99+" : String(n);
      badge.classList.toggle("hidden", !n);
      if (bell) bell.classList.toggle("has-pending", !!n);
    }

    function fmtWhen(iso) {
      if (!iso) return "";
      var t = Date.parse(iso);
      if (isNaN(t)) return "";
      var mins = Math.round((Date.now() - t) / 60000);
      if (mins < 1) return "just now";
      if (mins < 60) return mins + "m ago";
      var hrs = Math.round(mins / 60);
      if (hrs < 24) return hrs + "h ago";
      return Math.round(hrs / 24) + "d ago";
    }

    function renderList(reqs) {
      if (count) count.textContent = reqs.length ? reqs.length + " pending" : "";
      if (!reqs.length) {
        list.innerHTML = "<div class='pf-notif-empty'>No pending requests 🎉</div>";
        return;
      }
      list.innerHTML = reqs.map(function (r) {
        return "<div class='pf-notif-item' data-id='" + r.id + "'>" +
          "<div class='pf-notif-who'>" +
            "<span class='pf-notif-user'>" + esc(r.username) + "</span>" +
            "<span class='pf-notif-email' title='" + esc(r.email) + "'>" + esc(r.email) + "</span>" +
            "<span class='pf-notif-when'>" + esc(fmtWhen(r.requested_at)) + "</span>" +
          "</div>" +
          "<div class='pf-notif-actions'>" +
            "<button type='button' class='pf-btn pf-btn-approve' data-action='approve' data-id='" + r.id + "'>Approve</button>" +
            "<button type='button' class='pf-btn pf-btn-reject' data-action='reject' data-id='" + r.id + "'>Reject</button>" +
          "</div></div>";
      }).join("");
    }

    function load() {
      fetch(window.PF_APPROVALS_URL, { headers: { Accept: "application/json" }, credentials: "same-origin" })
        .then(function (r) { return r.json(); })
        .then(function (d) {
          if (!d || !d.ok || !d.is_approver) return;
          setBadge(d.pending_count || 0);
          renderList(d.requests || []);
        })
        .catch(function () {});
    }

    function act(id, action, btn) {
      if (busy) return;
      if (action === "reject" && !window.confirm("Reject this account request? The user stays inactive.")) return;
      busy = true;
      var item = btn.closest(".pf-notif-item");
      if (item) item.classList.add("pf-notif-busy");
      var body = "id=" + encodeURIComponent(id) + "&action=" + encodeURIComponent(action);
      fetch(window.PF_APPROVAL_ACTION_URL, {
        method: "POST",
        headers: {
          "X-CSRFToken": window.PF_CSRF || "",
          "Content-Type": "application/x-www-form-urlencoded",
          Accept: "application/json"
        },
        credentials: "same-origin",
        body: body
      })
        .then(function (r) { return r.json(); })
        .then(function (d) {
          busy = false;
          if (item) item.classList.remove("pf-notif-busy");
          if (!d || !d.ok) return;
          setBadge(d.pending_count || 0);
          renderList(d.requests || []);
        })
        .catch(function () {
          busy = false;
          if (item) item.classList.remove("pf-notif-busy");
        });
    }

    function toggle(next) {
      open = (next == null) ? !open : next;
      panel.hidden = !open;
      bell.setAttribute("aria-expanded", open ? "true" : "false");
      if (open) load();
    }

    bell.addEventListener("click", function (e) { e.stopPropagation(); toggle(); });
    panel.addEventListener("click", function (e) {
      var btn = e.target.closest ? e.target.closest("[data-action]") : null;
      e.stopPropagation();
      if (btn) act(btn.getAttribute("data-id"), btn.getAttribute("data-action"), btn);
    });
    document.addEventListener("click", function () { if (open) toggle(false); });
    document.addEventListener("keydown", function (e) { if (e.key === "Escape" && open) toggle(false); });

    load();                     // initial badge + list
    setInterval(load, 60000);   // refresh once a minute
  }

  function loadPortfolio(params) {
    var url = new URL(window.PF_DATA_URL || "/portfolio/api/data/", window.location.origin);
    Object.keys(params || {}).forEach(function (key) {
      url.searchParams.set(key, params[key]);
    });
    return fetch(url.toString(), { headers: { Accept: "application/json" } })
      .then(function (r) { return r.json(); })
      .then(render)
      .catch(function () {
        var k = el("pf-kpis");
        if (k) k.innerHTML = "<div class='pf-error'>Could not load portfolio data.</div>";
      });
  }

  loadPortfolio();

  function render(d) {
    if (!d || !d.ok) {
      var k = el("pf-kpis");
      if (k) k.innerHTML = "<div class='pf-error'>" + esc((d && d.error) || "No data") + "</div>";
      return;
    }
    renderKpis(d);
    renderSnapshot(d.pl_snapshot, d.sectors || []);
    renderSummaryDesk(d);
    renderReliability(d.reliability, d.data_quality);
    renderCompliance(d.compliance);
    renderRisk(d.risk, d.nepse_index);
    renderFactors(d.factors);
    renderPerformance(d.performance);
    renderCorrelation(d.correlation);
    renderLiquidity(d.liquidity);
    renderSectorAllocation(d.sectors || [], d.total_value);
    renderSectorRisk(d.factors);
    renderConc(d);
    if (el("pf-asof")) el("pf-asof").textContent = d.as_of ? "Priced at " + d.as_of + " close" : "";
  }

  function tile(label, val, sub, cls, help) {
    return "<div class='pf-kpi'><span class='pf-kpi-label'>" + esc(label) + (help || "") + "</span>" +
      "<span class='pf-kpi-val " + (cls || "") + "'>" + val + "</span>" +
      "<span class='pf-kpi-sub'>" + esc(sub || "") + "</span></div>";
  }

  function renderKpis(d) {
    var box = el("pf-kpis");
    if (!box) return;
    var cost = d.cost || {};
    var accounting = d.accounting || {};
    var na = "\u2014";
    var netWorth = accounting.has_ledger ? accounting.total_equity : d.total_value;
    var dayRows = (d.rows || []).filter(function (row) {
      if (row.day_pl == null) return false;
      return cost.has_cost ? row.cost_value != null : row.previous_close > 0;
    });
    var dayPl = dayRows.length ? dayRows.reduce(function (sum, row) {
      return sum + (row.day_pl || 0);
    }, 0) : null;
    var dayBase = dayRows.reduce(function (sum, row) {
      return sum + (cost.has_cost
        ? (row.cost_value || 0)
        : ((row.previous_close || 0) * (row.quantity || 0)));
    }, 0);
    var dayPct = dayPl == null || !dayBase ? null : 100 * dayPl / dayBase;
    var dayCoverage = dayRows.length && dayRows.length < (d.holdings_count || 0)
      ? " · " + dayRows.length + "/" + d.holdings_count + " holdings" : "";
    var daySub = dayPct == null ? "daily change unavailable" :
      (dayPct >= 0 ? "+" : "") + dayPct.toFixed(2) + "% vs " +
      (cost.has_cost ? "investment" : "previous value") + dayCoverage;
    var unrealizedSub = cost.paper_pl_pct == null ? "import WACC to calculate" :
      (cost.paper_pl_pct >= 0 ? "+" : "") + cost.paper_pl_pct.toFixed(2) + "%";
    var realized = accounting.has_ledger ? accounting.realized_pl : null;
    var html = tile("Investment", cost.has_cost ? rsCompact(cost.book_value) : na,
      cost.has_cost ? cost.covered_count + " costed holdings" : "WACC not imported");
    html += tile("Latest Market Value", rsCompact(d.total_value), d.holdings_count + " holdings");
    html += tile("Net Worth", rsCompact(netWorth),
      accounting.has_ledger ? "market value + broker cash" : "market value");
    html += tile("Day Gain/Loss", dayPl == null ? na : signedRs(dayPl), daySub,
      dayPl == null ? "" : (dayPl >= 0 ? "num-pos" : "num-neg"));
    html += tile("Unrealized Gain/Loss", cost.paper_pl == null ? na : signedRs(cost.paper_pl),
      unrealizedSub, cost.paper_pl == null ? "" : (cost.paper_pl >= 0 ? "num-pos" : "num-neg"));
    html += tile("Realized Gain/Loss", realized == null ? na : signedRs(realized),
      accounting.has_ledger ? "all-time from broker ledger" : "import ledger to calculate",
      realized == null ? "" : (realized >= 0 ? "num-pos" : "num-neg"));
    box.innerHTML = html;
  }

  // ── Stock Holdings — Beta Forecast & VaR ──────────────────────────────
  // A broker-style holdings desk driven by a "what if NEPSE moves to X" scenario.
  // The whole recompute is client-side (beta × index move) so Recalculate is
  // instant and never refetches. VaR/loss come pre-computed from the payload.
  var SUMMARY_DEFAULTS = {
    tab: "top", query: "", sector: "", liquidity: "", sort: "value", dir: "desc"
  };
  var state = {
    data: null,
    summary: {
      tab: SUMMARY_DEFAULTS.tab,
      query: SUMMARY_DEFAULTS.query,
      sector: SUMMARY_DEFAULTS.sector,
      liquidity: SUMMARY_DEFAULTS.liquidity,
      sort: SUMMARY_DEFAULTS.sort,
      dir: SUMMARY_DEFAULTS.dir
    }
  };

  function snf(n) {                    // signed, thousands-grouped integer
    var v = Math.round(n || 0);
    return (v >= 0 ? "+" : "") + nf(v);
  }

  // Beta-forecast one holding for a given NEPSE % move.
  function computeExp(r, changePct) {
    if (r.beta == null || r.price == null) {
      return { expPrice: r.price, expValue: r.value, gain: 0 };
    }
    var expPrice = r.price * (1 + r.beta * changePct / 100);
    var expValue = (r.quantity || 0) * expPrice;
    return { expPrice: expPrice, expValue: expValue, gain: expValue - (r.value || 0) };
  }

  function resetSummaryFilters() {
    state.summary = {
      tab: SUMMARY_DEFAULTS.tab,
      query: SUMMARY_DEFAULTS.query,
      sector: SUMMARY_DEFAULTS.sector,
      liquidity: SUMMARY_DEFAULTS.liquidity,
      sort: SUMMARY_DEFAULTS.sort,
      dir: SUMMARY_DEFAULTS.dir
    };
  }

  function bindSummaryFilters(rows) {
    syncSummarySectorOptions(rows || []);
    Array.prototype.forEach.call(document.querySelectorAll("[data-pf-holdings-tab]"), function (button) {
      if (button._pfBound) return;
      button._pfBound = true;
      button.addEventListener("click", function () {
        var tab = button.getAttribute("data-pf-holdings-tab") || "top";
        state.summary.tab = tab;
        if (tab === "gaining") {
          state.summary.sort = "day";
          state.summary.dir = "desc";
        } else if (tab === "losing") {
          state.summary.sort = "day";
          state.summary.dir = "asc";
        } else {
          state.summary.sort = "value";
          state.summary.dir = "desc";
        }
        syncSummaryFilterControls();
        recalcSummary();
      });
    });
    bindSummaryControl("pf-sum-query", "input", function (node) {
      state.summary.query = node.value || "";
      recalcSummary();
    });
    bindSummaryControl("pf-sum-sector-filter", "change", function (node) {
      state.summary.sector = node.value || "";
      recalcSummary();
    });
    bindSummaryControl("pf-sum-liq-filter", "change", function (node) {
      state.summary.liquidity = node.value || "";
      recalcSummary();
    });
    bindSummaryControl("pf-sum-sort", "change", function (node) {
      var next = node.value || SUMMARY_DEFAULTS.sort;
      if (state.summary.sort !== next) {
        state.summary.sort = next;
        state.summary.dir = next === "symbol" ? "asc" : "desc";
        syncSummaryFilterControls();
      }
      recalcSummary();
    });
    bindSummaryControl("pf-sum-dir", "click", function () {
      state.summary.dir = state.summary.dir === "asc" ? "desc" : "asc";
      syncSummaryFilterControls();
      recalcSummary();
    });
    bindSummaryControl("pf-sum-reset", "click", function () {
      resetSummaryFilters();
      syncSummaryFilterControls();
      recalcSummary();
    });
    syncSummaryFilterControls();
  }

  function bindSummaryControl(id, eventName, handler) {
    var node = el(id);
    if (!node || node._pfBound) return;
    node._pfBound = true;
    node.addEventListener(eventName, function () { handler(node); });
  }

  function syncSummarySectorOptions(rows) {
    var select = el("pf-sum-sector-filter");
    if (!select) return;
    var seen = {}, sectors = [];
    rows.forEach(function (r) {
      var sec = r.sector || "Other";
      if (!seen[sec]) { seen[sec] = true; sectors.push(sec); }
    });
    sectors.sort(function (a, b) { return a.localeCompare(b); });
    if (state.summary.sector && !seen[state.summary.sector]) state.summary.sector = "";
    select.innerHTML = "<option value=''>All sectors</option>" + sectors.map(function (sec) {
      return "<option value=\"" + esc(sec) + "\">" + esc(sec) + "</option>";
    }).join("");
  }

  function syncSummaryFilterControls() {
    var q = el("pf-sum-query");
    var sec = el("pf-sum-sector-filter");
    var liq = el("pf-sum-liq-filter");
    var sort = el("pf-sum-sort");
    var dir = el("pf-sum-dir");
    if (q) q.value = state.summary.query;
    if (sec) sec.value = state.summary.sector;
    if (liq) liq.value = state.summary.liquidity;
    if (sort) sort.value = state.summary.sort;
    if (dir) {
      dir.textContent = state.summary.dir === "asc" ? "Asc" : "Desc";
      dir.setAttribute("aria-pressed", state.summary.dir === "asc" ? "true" : "false");
    }
    Array.prototype.forEach.call(document.querySelectorAll("[data-pf-holdings-tab]"), function (button) {
      var active = button.getAttribute("data-pf-holdings-tab") === state.summary.tab;
      button.classList.toggle("active", active);
      button.setAttribute("aria-selected", active ? "true" : "false");
    });
  }

  function summaryMetric(r, key, changePct) {
    var e = (key === "expPrice" || key === "expValue" || key === "gain") ? computeExp(r, changePct) : null;
    if (key === "symbol") return r.symbol || "";
    if (key === "quantity") return r.quantity;
    if (key === "wacc") return r.wacc;
    if (key === "price") return r.price;
    if (key === "value") return r.value;
    if (key === "weight") return r.weight;
    if (key === "day") return r.day_pl;
    if (key === "unrealized") return r.pl;
    if (key === "cost") return r.cost_value;
    if (key === "vol") return r.vol;
    if (key === "beta") return r.beta;
    if (key === "dtl") return r.dtl;
    if (key === "expPrice") return e.expPrice;
    if (key === "expValue") return e.expValue;
    if (key === "gain") return e.gain;
    if (key === "var1w") return r.var_1w_pct == null ? null : Math.abs(r.var_1w_pct);
    if (key === "loss1w") return r.loss_1w == null ? null : Math.abs(r.loss_1w);
    if (key === "var1m") return r.var_1m_pct == null ? null : Math.abs(r.var_1m_pct);
    if (key === "loss1m") return r.loss_1m == null ? null : Math.abs(r.loss_1m);
    return r.value;
  }

  function filteredSummaryRows(rows, changePct) {
    var f = state.summary;
    var q = (f.query || "").toLowerCase().trim();
    var wrapped = rows.map(function (r, idx) { return { row: r, idx: idx }; }).filter(function (item) {
      var r = item.row;
      if (f.tab === "gaining" && !(r.day_pl > 0)) return false;
      if (f.tab === "losing" && !(r.day_pl < 0)) return false;
      var hay = [r.symbol, r.company, r.sector].join(" ").toLowerCase();
      if (q && hay.indexOf(q) === -1) return false;
      if (f.sector && (r.sector || "Other") !== f.sector) return false;
      if (f.liquidity && (r.liq_tier || "untradeable") !== f.liquidity) return false;
      return true;
    });
    wrapped.sort(function (a, b) {
      var av = summaryMetric(a.row, f.sort, changePct);
      var bv = summaryMetric(b.row, f.sort, changePct);
      var an = av == null || av === "";
      var bn = bv == null || bv === "";
      var cmp;
      if (an && bn) return a.idx - b.idx;
      if (an) return 1;
      if (bn) return -1;
      if (typeof av === "string" || typeof bv === "string") cmp = String(av).localeCompare(String(bv));
      else cmp = av - bv;
      if (cmp === 0) cmp = a.idx - b.idx;
      return f.dir === "asc" ? cmp : -cmp;
    });
    return wrapped.map(function (item) { return item.row; });
  }

  function updateSummaryCount(count, total) {
    var c = el("pf-sum-count");
    if (c) c.textContent = count + " of " + total + " holdings";
  }

  function renderSummaryDesk(d) {
    state.data = d;
    bindSummaryFilters(d.rows || []);
    var idx = (d.nepse_index || {}).value;
    var cur = el("pf-idx-current"), exp = el("pf-idx-expected");
    if (cur) cur.value = idx == null ? "—" : nf(idx);
    if (exp && !exp.value) exp.value = idx == null ? "" : idx;
    if (exp && !exp._bound) {
      exp._bound = true;
      exp.addEventListener("input", recalcSummary);
    }
    recalcSummary();
  }

  function recalcSummary() {
    var d = state.data;
    if (!d) return;
    var rows = d.rows || [];
    var idx = (d.nepse_index || {}).value;
    var expEl = el("pf-idx-expected");
    var expected = expEl ? parseFloat(expEl.value) : idx;
    if (isNaN(expected)) expected = idx;
    var changePct = (idx && idx > 0) ? (expected - idx) / idx * 100 : 0;

    var chEl = el("pf-idx-change");
    if (chEl) {
      chEl.textContent = (changePct >= 0 ? "+" : "") + changePct.toFixed(2) + "%";
      chEl.className = "pf-idx-change " + (changePct >= 0 ? "num-pos" : "num-neg");
    }

    var totVal = d.total_value || 0, totExp = 0, totL1w = 0, totL1m = 0, costedExp = 0;
    rows.forEach(function (r) {
      var e = computeExp(r, changePct);
      totExp += e.expValue || 0;
      totL1w += r.loss_1w || 0;
      totL1m += r.loss_1m || 0;
      if (r.cost_value != null) costedExp += e.expValue || 0;   // costed subset, for expected unrealised P/L
    });

    var cost = d.cost || {};
    var tiles = el("pf-sum-tiles");
    if (tiles) {
      var html = "";
      html += tile("Expected Value", rsCompact(totExp),
        (changePct >= 0 ? "+" : "") + changePct.toFixed(2) + "% NEPSE scenario");
      if (cost.has_cost) {
        html += tile("Expected Unrealized P/L", signedRs(costedExp - cost.book_value), "at scenario",
          (costedExp - cost.book_value) >= 0 ? "num-pos" : "num-neg");
      }
      html += tile("Scenario P/L", signedRs(totExp - totVal), "vs current value",
        (totExp - totVal) >= 0 ? "num-pos" : "num-neg");
      // Prefer the diversified portfolio VaR (Z·σ_p·√h, correlation-aware) from the
      // Risk block; fall back to the undiversified sum of per-holding VaRs on thin
      // history where the portfolio return series is too short for a portfolio VaR.
      var pvar = (d.risk && d.risk.ok && d.risk.var) ? d.risk.var : null;
      var l1w = pvar ? -pvar.param_95_1w_rs : totL1w;
      var l1m = pvar ? -pvar.param_95_1m_rs : totL1m;
      var vsub = pvar ? "diversified VaR" : "undiversified VaR";
      html += tile("Loss @ 1W · 95%", rsCompact(l1w), vsub, "num-neg");
      html += tile("Loss @ 1M · 95%", rsCompact(l1m), vsub, "num-neg");
      tiles.innerHTML = html;
    }

    var tableRows = filteredSummaryRows(rows, changePct);
    updateSummaryCount(tableRows.length, rows.length);
    renderSummaryTable(tableRows, changePct);

    var note = el("pf-sum-note");
    if (note) {
      var base = "Expected price = LTP × (1 + β × NEPSE change%); Gain/Loss is the scenario move vs current value. " +
        "β is estimated on weekly returns vs the NEPSE Index (thin-trading robust — <a href='" + SOP_URL + "#beta' target='_blank' rel='noopener'>methodology</a>). " +
        "Per-holding VaR is parametric (95%, √-time) on each holding's annualised volatility — 1W ≈ 5 sessions, 1M ≈ 20. " +
        "The Loss tiles show the DIVERSIFIED portfolio VaR (Z·σₚ·√h on the portfolio's own return series, so correlation lowers it below the sum of the columns); " +
        "on thin history they fall back to the undiversified column sum. " +
        "Weight, volatility, beta and liquidity are pulled from the same holdings payload. " +
        "Thinly-traded scrips with no beta/volatility show “—”.";
      if (d.snapshot_count) {
        base += " " + d.snapshot_count + " holding(s) are priced from your uploaded snapshot because they are not in the NEPSE EOD feed.";
      }
      var costNote = (cost.has_cost)
        ? " WACC and unrealised P/L use your imported “My WACC” report (" + cost.covered_count + " scrips matched)."
        : " Import your broker “My WACC” report to add WACC and unrealised P/L.";
      note.innerHTML = base + costNote;
    }
  }

  function summaryColgroup() {
    return "<colgroup>" +
      "<col class='pf-col-symbol'><col class='pf-col-kitta'><col class='pf-col-wacc'>" +
      "<col class='pf-col-investment'><col class='pf-col-ltp'><col class='pf-col-market'>" +
      "<col class='pf-col-day'><col class='pf-col-unrealized'>" +
      "<col class='pf-col-scenario'><col class='pf-col-maxloss'>" +
      "</colgroup>";
  }

  function summarySortHeader(label, key, title) {
    var active = state.summary.sort === key;
    var mark = active ? (state.summary.dir === "asc" ? "^" : "v") : "";
    return "<button type='button' class='pf-sort-head" + (active ? " active" : "") + "' " +
      "data-summary-sort='" + esc(key) + "' title='" + esc(title || ("Sort by " + label)) + "'>" +
      "<span>" + esc(label) + "</span><span class='pf-sort-mark'>" + mark + "</span></button>";
  }

  function setSummarySort(key) {
    if (state.summary.sort === key) {
      state.summary.dir = state.summary.dir === "asc" ? "desc" : "asc";
    } else {
      state.summary.sort = key;
      state.summary.dir = key === "symbol" ? "asc" : "desc";
    }
    syncSummaryFilterControls();
    recalcSummary();
  }

  function bindSummaryHeaderSort(table) {
    Array.prototype.forEach.call(table.querySelectorAll("[data-summary-sort]"), function (btn) {
      btn.addEventListener("click", function () {
        setSummarySort(btn.getAttribute("data-summary-sort"));
      });
    });
  }

  function renderSummaryTable(rows, changePct) {
    var t = el("pf-sum-table");
    if (!t) return;
    var colgroup = summaryColgroup();
    var head = "<thead>" +
      "<tr><th class='l'>" + summarySortHeader("Symbol", "symbol") + "</th>" +
        "<th>" + summarySortHeader("Quantity", "quantity") + "</th>" +
        "<th>" + summarySortHeader("Avg Cost", "wacc", "Sort by weighted average cost") + "</th>" +
        "<th>" + summarySortHeader("Investment Cost", "cost") + "<small>Cost weight</small></th>" +
        "<th>" + summarySortHeader("LTP", "price") + "<small>Change (% Chg)</small></th>" +
        "<th>" + summarySortHeader("Market Value", "value") + "<small>Market weight</small></th>" +
        "<th>" + summarySortHeader("Today's Gain/Loss", "day") + "<small>Gain/Loss %</small></th>" +
        "<th>" + summarySortHeader("Unrealized Gain/Loss", "unrealized") + "<small>Gain/Loss %</small></th>" +
        "<th>" + summarySortHeader("Scenario Value", "expValue") + "<small>Scenario gain/loss</small></th>" +
        "<th>Max Loss<small>VaR 1W / 1M</small></th></tr></thead>";

    if (!rows.length) {
      var emptyLabel = state.summary.tab === "gaining" ? "No gaining holdings today" :
        (state.summary.tab === "losing" ? "No losing holdings today" : "No matching holdings");
      t.innerHTML = colgroup + head + "<tbody><tr><td colspan='10' class='pf-muted'>" + emptyLabel + "</td></tr></tbody>";
      bindSummaryHeaderSort(t);
      return;
    }

    var totalBook = ((state.data || {}).cost || {}).book_value || 0;
    var body = rows.map(function (r) {
      var e = computeExp(r, changePct);
      var snap = r.price_source === "snapshot"
        ? " <span class='pf-snap' title='Priced from your uploaded snapshot'>snap</span>" : "";
      var tier = r.liq_tier || "untradeable";
      var costWeight = r.cost_value != null && totalBook ? 100 * r.cost_value / totalBook : null;
      var dayClass = r.day_pl == null ? "" : (r.day_pl >= 0 ? "num-pos" : "num-neg");
      var plClass = r.pl == null ? "" : (r.pl >= 0 ? "num-pos" : "num-neg");
      var scenarioClass = e.gain >= 0 ? "num-pos" : "num-neg";
      var unrealizedPct = r.cost_value ? 100 * r.pl / r.cost_value : null;
      var dayPlBase = r.cost_value || ((r.previous_close || 0) * (r.quantity || 0));
      var dayPlPct = r.day_pl == null || !dayPlBase ? null : 100 * r.day_pl / dayPlBase;
      var dayChange = r.day_change == null ? "—" : snf(r.day_change);
      var dayChangePct = r.day_change_pct == null ? "—" :
        (r.day_change_pct >= 0 ? "+" : "") + r.day_change_pct.toFixed(2) + "%";
      return "<tr>" +
        "<td class='l pf-tkr'>" + esc(r.symbol) +
          "<small>" + esc(r.sector || "Other") + "</small></td>" +
        "<td>" + nf(r.quantity) +
          "<small><span class='pf-liq-tier tier-" + esc(tier) + "'>" + dtlText(r.dtl) + "</span></small></td>" +
        "<td>" + (r.wacc == null ? "—" : nf(r.wacc)) + "</td>" +
        "<td>" + (r.cost_value == null ? "—" : nf(r.cost_value)) +
          "<small>" + (costWeight == null ? "—" : costWeight.toFixed(2) + "%") + "</small></td>" +
        "<td>" + nf(r.price) + snap +
          "<small class='" + dayClass + "'>" + dayChange + " (" + dayChangePct + ")</small></td>" +
        "<td>" + nf(r.value) + "<small>" + pct(r.weight) + "</small></td>" +
        "<td class='" + dayClass + "'>" + (r.day_pl == null ? "—" : snf(r.day_pl)) +
          "<small>" + (dayPlPct == null ? "—" : (dayPlPct >= 0 ? "+" : "") + dayPlPct.toFixed(2) + "%") + "</small></td>" +
        "<td class='" + plClass + "'>" + (r.pl == null ? "—" : snf(r.pl)) +
          "<small>" + (unrealizedPct == null ? "—" : (unrealizedPct >= 0 ? "+" : "") + unrealizedPct.toFixed(2) + "%") + "</small></td>" +
        "<td>" + nf(Math.round(e.expValue)) +
          "<small class='" + scenarioClass + "'>" + snf(e.gain) + "</small></td>" +
        "<td class='num-neg'>" + (r.loss_1w == null ? "—" : nf(Math.round(r.loss_1w))) +
          "<small>" + (r.loss_1m == null ? "—" : nf(Math.round(r.loss_1m))) + "</small></td>" +
        "</tr>";
    }).join("");
    t.innerHTML = colgroup + head + "<tbody>" + body + "</tbody>";
    bindSummaryHeaderSort(t);
  }

  /* Winners vs losers, and the best/worst holdings by percentage. Ranked on
     PERCENT rather than rupees — the rupee leader is normally just the largest
     position, which describes sizing rather than the holding. */
  function renderSnapshot(s, sectors) {
    var box = el("pf-snapshot");
    if (!box) return;
    if (!s || !s.ok) {
      box.innerHTML = "<div class='pf-muted'>" + esc((s && s.reason) || "Unrealised P/L unavailable.") + "</div>";
      return;
    }
    var arrow = function (v) { return v == null ? "" : (v >= 0 ? " ▲" : " ▼"); };
    var cls = function (v) { return v == null ? "" : (v >= 0 ? "num-pos" : "num-neg"); };
    var sPct = function (v) { return v == null ? "—" : (v >= 0 ? "+" : "") + v.toFixed(2) + "%"; };

    var side = function (label, o, tone) {
      return "<div class='pf-snap-side'>" +
        "<span class='pf-snap-ring " + tone + "'></span>" +
        "<div><div class='pf-snap-head'>" + o.count + " of " + s.total + " " + label + "</div>" +
        "<div class='pf-snap-sum'><span class='" + tone + "'>" + rsCompact(o.pl) + "</span>" +
        "<span class='" + tone + "'>" + sPct(o.pl_pct) + arrow(o.pl_pct) + "</span></div></div></div>";
    };

    var card = function (c) {
      return "<div class='pf-snap-card'>" +
        "<div class='pf-snap-r1'><b>" + esc(c.symbol) + "</b><span>" + nf(c.price) + "</span></div>" +
        "<div class='pf-snap-r2 " + cls(c.day_change) + "'>" +
          "<span>" + (c.day_change == null ? "—" : nf(c.day_change)) + "</span>" +
          "<span>" + sPct(c.day_change_pct) + arrow(c.day_change_pct) + "</span></div>" +
        "<div class='pf-snap-r3'>Gain/Loss</div>" +
        "<div class='pf-snap-r4 " + cls(c.pl) + "'>" +
          "<span>" + rsCompact(c.pl) + "</span>" +
          "<span>" + sPct(c.pl_pct) + arrow(c.pl_pct) + "</span></div></div>";
    };

    /* Diverging bars around a zero axis: profit grows right, loss grows left,
       scaled to the largest absolute sector P/L. Reads the shape of the book at
       a glance in a way four cropped cards never did. */
    var sectorBars = function (sectors) {
      var list = (sectors || []).filter(function (x) { return x.pl != null; });
      if (!list.length) return "<div class='pf-muted pf-snap-empty'>No sector P/L available.</div>";
      list.sort(function (a, b) { return b.pl - a.pl; });
      var max = list.reduce(function (m, x) { return Math.max(m, Math.abs(x.pl)); }, 0) || 1;
      return "<div class='pf-plbars'>" + list.map(function (x) {
        var w = (Math.abs(x.pl) / max) * 50;              // half-track = 50%
        var up = x.pl >= 0;
        var bar = "<i class='" + (up ? "pos" : "neg") + "' style='" +
          (up ? "left:50%;" : "right:50%;") + "width:" + w.toFixed(1) + "%'></i>";
        return "<div class='pf-plbar-row'>" +
          "<span class='pf-plbar-name' title='" + esc(x.sector) + "'>" + esc(x.sector) + "</span>" +
          "<span class='pf-plbar-track'>" + bar + "<b class='pf-plbar-zero'></b></span>" +
          "<span class='pf-plbar-val " + (up ? "num-pos" : "num-neg") + "'>" + rsCompact(x.pl) + "</span>" +
          "<span class='pf-plbar-pct " + (up ? "num-pos" : "num-neg") + "'>" + sPct(x.pl_pct) + "</span>" +
          "</div>";
      }).join("") + "</div>";
    };

    var gain = s.top_gainers || [], lose = s.top_losers || [];
    var best = gain.slice(0, 3).concat(lose.slice(0, 3));
    var holdings = best.length
      ? "<div class='pf-snap-cards'>" + best.map(card).join("") + "</div>"
      : "<div class='pf-muted pf-snap-empty'>No costed holdings.</div>";

    box.innerHTML =
      "<div class='pf-snap-tops'>" + side("in Profit", s.winners, "num-pos") +
        side("in Loss", s.losers, "num-neg") + "</div>" +
      "<div class='pf-snap-grid'>" +
        "<div class='pf-snap-col'><div class='pf-snap-col-h'>Profit / Loss by sector</div>" +
          sectorBars(sectors) + "</div>" +
        "<div class='pf-snap-col'><div class='pf-snap-col-h'>Best &amp; worst holdings (%)</div>" +
          holdings + "</div>" +
      "</div>" +
      "<div class='pf-var-note'>Unrealised P/L against your WACC cost basis, ranked by percentage. " +
      (s.uncosted ? "<b>" + s.uncosted + "</b> holding(s) have no cost basis and are excluded. " : "") +
      "Bonus and IPO lots carry a par cost of Rs 100 in the broker's WACC report, which can make " +
      "their percentage gain look extreme.</div>";
  }

  /* Shared with the Market Insights sector chart so a sector keeps the same
     colour across the app. */
  var SECTOR_COLORS = [
    "#12d39a", "#5cb3ff", "#ffc166", "#ff6e72", "#a78bfa", "#34d399", "#f472b6",
    "#60a5fa", "#fbbf24", "#fb7185", "#2dd4bf", "#c084fc", "#4ade80", "#f59e0b"
  ];
  function sectorColor(i) { return SECTOR_COLORS[i % SECTOR_COLORS.length]; }

  /* Donut drawn with stroke-dasharray on concentric circles rather than arc
     paths — no trigonometry to get wrong, and a single 100% slice still renders
     as a full ring instead of collapsing to a zero-length arc. */
  function donutSvg(items, centreVal, centreLabel) {
    var R = 54, SW = 20, CX = 70, C = 2 * Math.PI * R;
    var total = items.reduce(function (s, i) { return s + (i.value || 0); }, 0);
    if (total <= 0) return "";
    var offset = 0;
    var segs = items.map(function (it, i) {
      var len = C * ((it.value || 0) / total);
      var seg = "<circle cx='" + CX + "' cy='" + CX + "' r='" + R + "' fill='none'" +
        " stroke='" + sectorColor(i) + "' stroke-width='" + SW + "'" +
        " stroke-dasharray='" + len.toFixed(2) + " " + (C - len).toFixed(2) + "'" +
        " stroke-dashoffset='" + (-offset).toFixed(2) + "'>" +
        "<title>" + esc(it.label) + ": " + pct(it.weight) + "</title></circle>";
      offset += len;
      return seg;
    }).join("");
    return "<svg class='pf-donut-svg' viewBox='0 0 140 140' role='img' aria-label='Sector allocation'>" +
      "<g transform='rotate(-90 " + CX + " " + CX + ")'>" + segs + "</g>" +
      "<text class='pf-donut-v' x='" + CX + "' y='" + (CX - 2) + "' text-anchor='middle'>" +
        esc(centreVal) + "</text>" +
      "<text class='pf-donut-k' x='" + CX + "' y='" + (CX + 13) + "' text-anchor='middle'>" +
        esc(centreLabel) + "</text></svg>";
  }

  /* Left half of the card: where the CAPITAL sits. */
  function renderSectorAllocation(sectors, totalValue) {
    var box = el("pf-sector-donut");
    if (!box) return;
    if (!sectors || !sectors.length) {
      box.innerHTML = "<div class='pf-muted'>No sector data</div>";
      return;
    }
    var items = sectors.map(function (s) {
      return { label: s.sector, value: s.value || 0, weight: s.weight || 0 };
    });
    var legend = items.map(function (it, i) {
      return "<div class='pf-donut-li'>" +
        "<i style='background:" + sectorColor(i) + "'></i>" +
        "<span class='pf-donut-name' title='" + esc(it.label) + "'>" + esc(it.label) + "</span>" +
        "<span class='pf-donut-pct'>" + pct(it.weight) + "</span>" +
        "<span class='pf-donut-val'>" + rsCompact(it.value) + "</span></div>";
    }).join("");
    box.innerHTML = donutSvg(items, rsCompact(totalValue), "invested") +
      "<div class='pf-donut-legend'>" + legend + "</div>";
  }

  /* Right half: where the RISK sits. Same sectors, deliberately beside the
     donut — a sector can be a small share of capital and a large share of risk,
     and that gap is the whole point of the card. */
  function renderSectorRisk(f) {
    var box = el("pf-sector-risk");
    if (!box) return;
    if (!f || !f.ok || !(f.sectors || []).length) {
      box.innerHTML = "<div class='pf-muted'>Risk contribution unavailable.</div>";
      return;
    }
    var secs = f.sectors;
    var max = secs[0] && secs[0].pct ? secs[0].pct : 1;
    box.innerHTML = secs.map(function (s) {
      var w = (100 * s.pct / max).toFixed(1);
      return "<div class='pf-fac-srow'>" +
        "<span class='pf-sec-name' title='" + esc(s.sector) + "'>" + esc(s.sector) + "</span>" +
        "<span class='pf-sec-bar'><i style='width:" + w + "%'></i></span>" +
        "<span class='pf-sec-pct'>" + s.pct.toFixed(1) + "%</span></div>";
    }).join("");
  }

  function renderConc(d) {
    var box = el("pf-conc");
    if (!box) return;
    var c = d.concentration || {};
    var rows = c.top_holdings || [];
    var bandText = { low: "Well diversified", moderate: "Moderately concentrated", high: "Highly concentrated" };
    var top = "<div class='pf-mini-table-wrap'><table class='pf-mini-table pf-top10-table'>" +
      "<thead><tr><th>#</th><th>Holding</th><th>Weight</th><th>Return contrib.</th>" +
      "<th>Risk contrib.</th><th>Cumulative</th><th>Indicator</th></tr></thead><tbody>" +
      rows.map(function (r, i) {
        var ret = r.return_contribution_pct, risk = r.risk_contribution_pct;
        var retClass = ret == null ? "" : (ret >= 0 ? "num-pos" : "num-neg");
        return "<tr><td>" + (i + 1) + "</td><td><b>" + esc(r.symbol) + "</b></td>" +
          "<td>" + pct(r.weight) + "</td>" +
          "<td class='" + retClass + "'>" + (ret == null ? "—" : (ret >= 0 ? "+" : "") + ret.toFixed(2) + " pp") + "</td>" +
          "<td>" + (risk == null ? "—" : risk.toFixed(2) + "%") + "</td>" +
          "<td><span class='pf-cum-value'>" + pct(r.cumulative_weight) + "</span>" +
            "<span class='pf-cum-bar'><i style='width:" + Math.min(100, r.cumulative_weight) + "%'></i></span></td>" +
          "<td><span class='pf-risk-badge risk-" + esc(r.concentration_risk) + "'>" +
            esc(r.concentration_risk) + "</span></td></tr>";
      }).join("") + "</tbody></table></div>";
    box.innerHTML =
      "<div class='pf-conc-summary'>" +
        "<div><span class='pf-conc-num risk-" + (c.risk || "low") + "'>" + nf(c.hhi || 0) + "</span>" +
        "<span class='pf-conc-cap'>HHI — " + esc(bandText[c.risk] || "—") + "</span></div>" +
        "<div><span class='pf-conc-num'>" + (c.effective_holdings || 0).toFixed(1) + "</span>" +
        "<span class='pf-conc-cap'>effective holdings</span></div>" +
        "<div><span class='pf-conc-num'>" + pct(c.top10_weight || 0) + "</span>" +
        "<span class='pf-conc-cap'>Top-10 concentration</span></div>" +
      "</div>" +
      top +
      "<div class='pf-var-note'>Return contribution is one-year current-weight arithmetic attribution (" +
      (c.return_observations || 0) + " observed sessions), not transaction-adjusted performance. " +
      "Risk contribution is the holding's Euler contribution in the weekly NEPSE factor model.</div>";
  }

  // ── Data reliability ──────────────────────────────────────────────────
  // Grades the INPUTS behind each metric group. Deliberately worst-first: the
  // numbers you should trust least are the ones you should see first.
  function renderReliability(r, q) {
    var box = el("pf-reliability"), sum = el("pf-rel-summary");
    if (!box) return;
    if (!r || !r.items || !r.items.length) {
      box.innerHTML = "<div class='pf-muted'>Reliability grading unavailable.</div>";
      if (sum) sum.innerHTML = "";
      return;
    }
    var lab = r.labels || {};
    if (sum) {
      var c = r.counts || {};
      sum.innerHTML =
        "<span class='pf-rel-pill green'>" + (c.green || 0) + " reliable</span>" +
        "<span class='pf-rel-pill amber'>" + (c.amber || 0) + " estimate</span>" +
        "<span class='pf-rel-pill red'>" + (c.red || 0) + " uncertain</span>";
    }
    // Only genuinely-unreliable inputs get a banner. Everything else collapses
    // to chips: the full card ran ~730px tall, which pushed VaR, Compliance and
    // Stress below the fold — a caveat panel should not outrank the data.
    var stale = "";
    if (q && q.stale_names && q.stale_names.length) {
      stale = "<div class='pf-rel-stale'><b>" + q.stale_names.length +
        " holding" + (q.stale_names.length === 1 ? "" : "s") +
        " barely trade</b> (" + q.stale_names.map(esc).join(", ") + ") — " +
        "they carry their last close forward, which the return series reads as a 0% " +
        "move, so their volatility and VaR are understated. They are " +
        (q.stale_weight_pct || 0).toFixed(1) + "% of the book.</div>";
    }
    var chips = "<div class='pf-rel-chips'>" + r.items.map(function (i) {
      // title= keeps the full reasoning one hover away even when collapsed.
      return "<span class='pf-rel-chip " + esc(i.grade) + "' title='" +
        esc((lab[i.grade] || i.grade) + " — " + i.why) + "'>" +
        "<i></i>" + esc(i.label) + "</span>";
    }).join("") + "</div>";

    var details = "<div class='pf-rel-details' id='pf-rel-details' hidden>" +
      r.items.map(function (i) {
        return "<div class='pf-rel-row " + esc(i.grade) + "'>" +
          "<span class='pf-rel-dot'></span>" +
          "<div class='pf-rel-body'><div class='pf-rel-label'>" + esc(i.label) +
          "<span class='pf-rel-grade'>" + esc(lab[i.grade] || i.grade) + "</span></div>" +
          "<div class='pf-rel-why'>" + esc(i.why) + "</div></div></div>";
      }).join("") +
      (r.note ? "<div class='pf-rel-note'>" + esc(r.note) + "</div>" : "") +
      "</div>";

    box.innerHTML = stale + chips +
      "<button type='button' class='pf-rel-toggle' id='pf-rel-toggle' " +
      "aria-expanded='false'>Why these grades?</button>" + details;

    var btn = el("pf-rel-toggle"), det = el("pf-rel-details");
    if (btn && det) {
      btn.addEventListener("click", function () {
        var open = det.hidden;
        det.hidden = !open;
        btn.setAttribute("aria-expanded", String(open));
        btn.textContent = open ? "Hide detail" : "Why these grades?";
      });
    }
  }

  function renderCompliance(c) {
    var box = el("pf-compliance"), sum = el("pf-compliance-summary");
    if (!box) return;
    if (!c || !c.checks || !c.checks.length) {
      box.innerHTML = "<div class='pf-muted'>No limits evaluated.</div>";
      if (sum) sum.innerHTML = "";
      return;
    }
    var s = c.summary || {};
    if (sum) sum.innerHTML =
      (s.breach ? "<span class='pf-cpill breach'>" + s.breach + " breach</span>" : "") +
      (s.warn ? "<span class='pf-cpill warn'>" + s.warn + " watch</span>" : "") +
      "<span class='pf-cpill ok'>" + (s.ok || 0) + " ok</span>";
    var lbl = { ok: "OK", warn: "WATCH", breach: "BREACH" };
    box.innerHTML = c.checks.map(function (ch) {
      return "<div class='pf-chk pf-chk-" + ch.status + "'>" +
        "<span class='pf-chk-status " + ch.status + "'>" + (lbl[ch.status] || ch.status) + "</span>" +
        "<span class='pf-chk-label'>" + esc(ch.label) +
          (ch.detail ? "<span class='pf-chk-detail'>" + esc(ch.detail) + "</span>" : "") + "</span>" +
        "<span class='pf-chk-cur'>" + esc(ch.current) + "</span>" +
        "<span class='pf-chk-lim'>" + esc(ch.limit) + "</span></div>";
    }).join("");
  }

  function stat(label, val, sub) {
    return "<div class='pf-stat'><span class='pf-stat-label'>" + esc(label) + "</span>" +
      "<span class='pf-stat-val'>" + val + "</span>" +
      (sub ? "<span class='pf-stat-sub'>" + esc(sub) + "</span>" : "") + "</div>";
  }

  function renderRisk(risk, indexInfo) {
    var v = el("pf-var"), st = el("pf-stress");
    if (!risk || !risk.ok) {
      var msg = (risk && risk.reason) || "Risk metrics unavailable.";
      if (v) v.innerHTML = "<div class='pf-muted'>" + esc(msg) + "</div>";
      if (st) st.innerHTML = "<div class='pf-muted'>" + esc(msg) + "</div>";
      return;
    }
    var V = risk.var;
    var idxVal = indexInfo && indexInfo.value;
    function lossRow(label, rsv, big) {
      return "<div class='pf-var-row" + (big ? " big" : "") + "'>" +
        "<span class='pf-var-label'>" + esc(label) + "</span>" +
        "<span class='pf-var-val num-neg'>-" + rs(Math.abs(rsv)) + "</span></div>";
    }
    function varCell(value) {
      return value == null ? "—" : "-" + Math.abs(value).toFixed(2) + "%";
    }
    function shortScenarioLabel(label) {
      return String(label || "")
        .replace(/^Current NEPSE$/, "Current")
        .replace(/^NEPSE\s+/, "")
        .replace(/^at\s+/, "")
        .replace(/all-time high/i, "ATH");
    }
    function renderStressChart(rows, maxAbs) {
      var width = 720, height = 210, left = 42, right = 18, top = 22, bottom = 46;
      var plotW = width - left - right, plotH = height - top - bottom;
      maxAbs = maxAbs || 1;
      var zeroY = top + plotH / 2;
      var step = plotW / Math.max(rows.length, 1);
      var barW = Math.max(12, Math.min(46, step * 0.54));
      var parts = [
        "<div class='pf-stress-chart' role='img' aria-label='Portfolio beta stress gain/loss chart'>",
        "<svg class='pf-stress-svg' viewBox='0 0 " + width + " " + height + "' focusable='false'>",
        "<line class='pf-stress-grid' x1='" + left + "' x2='" + (width - right) + "' y1='" + top + "' y2='" + top + "'></line>",
        "<line class='pf-stress-grid' x1='" + left + "' x2='" + (width - right) + "' y1='" + (top + plotH) + "' y2='" + (top + plotH) + "'></line>",
        "<line class='pf-stress-axis' x1='" + left + "' x2='" + (width - right) + "' y1='" + zeroY + "' y2='" + zeroY + "'></line>",
        "<text class='pf-stress-axis-label' x='" + (left - 8) + "' y='" + (top + 4) + "' text-anchor='end'>+" + maxAbs.toFixed(1) + "%</text>",
        "<text class='pf-stress-axis-label' x='" + (left - 8) + "' y='" + (zeroY + 4) + "' text-anchor='end'>0%</text>",
        "<text class='pf-stress-axis-label' x='" + (left - 8) + "' y='" + (top + plotH + 4) + "' text-anchor='end'>-" + maxAbs.toFixed(1) + "%</text>"
      ];
      rows.forEach(function (s, i) {
        var pctv = s.gain_loss_pct || s.impact_pct || 0;
        var x = left + step * i + (step - barW) / 2;
        var y = zeroY - (pctv / maxAbs) * (plotH / 2);
        var barY = Math.min(y, zeroY);
        var barH = Math.max(2, Math.abs(y - zeroY));
        var cls = pctv < 0 ? "down" : (pctv > 0 ? "up" : "flat");
        parts.push(
          "<g class='pf-stress-bar-g'>",
          "<rect class='pf-stress-bar-rect " + cls + "' x='" + x.toFixed(1) + "' y='" + barY.toFixed(1) +
            "' width='" + barW.toFixed(1) + "' height='" + barH.toFixed(1) + "' rx='4'>",
          "<title>" + esc(s.label) + ": " + signedRs(s.impact_rs) + " (" +
            (pctv > 0 ? "+" : "") + pctv.toFixed(2) + "%)</title>",
          "</rect>",
          "<text class='pf-stress-value' x='" + (x + barW / 2).toFixed(1) + "' y='" +
            (pctv >= 0 ? Math.max(top + 12, barY - 6) : Math.min(top + plotH + 16, barY + barH + 13)).toFixed(1) +
            "' text-anchor='middle'>" + (pctv > 0 ? "+" : "") + pctv.toFixed(1) + "%</text>",
          "<text class='pf-stress-xlabel' x='" + (x + barW / 2).toFixed(1) + "' y='" + (height - 18) +
            "' text-anchor='middle'>" + esc(shortScenarioLabel(s.label)) + "</text>",
          "</g>"
        );
      });
      parts.push("</svg></div>");
      return parts.join("");
    }
    var matrix = "<div class='pf-mini-table-wrap'><table class='pf-mini-table pf-var-matrix'>" +
      "<thead><tr><th>Method</th><th>1D 95%</th><th>1D 99%</th><th>10D 95%</th><th>10D 99%</th></tr></thead><tbody>" +
      "<tr><td><b>Historical VaR</b></td><td>" + varCell(V.hist_95_1d_pct) + "</td><td>" +
        varCell(V.hist_99_1d_pct) + "</td><td>" + varCell(V.hist_95_10d_pct) + "</td><td>" +
        varCell(V.hist_99_10d_pct) + "</td></tr>" +
      "<tr><td><b>Parametric VaR</b></td><td>" + varCell(V.param_95_1d_pct) + "</td><td>" +
        varCell(V.param_99_1d_pct) + "</td><td>" + varCell(V.param_95_10d_pct) + "</td><td>" +
        varCell(V.param_99_10d_pct) + "</td></tr>" +
      "<tr><td><b>Expected Shortfall</b></td><td>" + varCell(V.cvar_95_1d_pct) + "</td><td>" +
        varCell(V.cvar_99_1d_pct) + "</td><td>" + varCell(V.cvar_95_10d_pct) + "</td><td>" +
        varCell(V.cvar_99_10d_pct) + "</td></tr></tbody></table></div>";
    if (v) v.innerHTML = matrix +
      lossRow("Historical VaR · 1D 95%", V.hist_95_1d_rs, true) +
      lossRow("Historical VaR · 10D 99%", V.hist_99_10d_rs) +
      lossRow("Expected Shortfall · 10D 99%", V.cvar_99_10d_rs) +
      "<div class='pf-stat-grid'>" +
        stat("Ann. volatility", risk.ann_vol_pct == null ? "—" : risk.ann_vol_pct.toFixed(1) + "%") +
        stat("Worst session", risk.worst_day ? risk.worst_day.pct.toFixed(2) + "%" : "—",
             risk.worst_day ? risk.worst_day.date : "") +
        stat("Max drawdown", risk.max_drawdown_pct.toFixed(2) + "%") +
        stat("Worst 5-session", risk.worst_5d_pct == null ? "—" : risk.worst_5d_pct.toFixed(2) + "%") +
      "</div>" +
      "<div class='pf-var-note'>Historical simulation over " + risk.sessions +
      " sessions; 10-day historical values use overlapping compounded windows. " +
      "Parametric values use normal quantiles and √time scaling for comparison. " +
      "Historical risk remains primary for fat-tailed, circuit-bounded NEPSE returns.</div>";

    if (st) {
      if (!risk.scenarios || !risk.scenarios.length) {
        st.innerHTML = "<div class='pf-muted'>" + esc(risk.stress_reason || "Beta scenarios unavailable.") + "</div>";
        return;
      }
      var maxAbs = risk.scenarios.reduce(function (m, s) {
        return Math.max(m, Math.abs(s.impact_pct || 0)); }, 0) || 1;
      st.innerHTML = renderStressChart(risk.scenarios, maxAbs) +
      "<div class='pf-mini-table-wrap'><table class='pf-mini-table pf-scenario-table'>" +
        "<thead><tr><th>Scenario</th><th>NEPSE</th><th>Move</th><th>Stressed Holdings</th><th>Gain/Loss</th></tr></thead><tbody>" +
        risk.scenarios.map(function (s) {
          var cls = s.impact_rs < 0 ? "num-neg" : "num-pos";
          return "<tr><td>" + esc(s.label) +
            (s.reference ? "<small class='pf-scenario-ref'>" + esc(s.reference) + "</small>" : "") +
            "</td><td>" +
            (s.target_index == null ? "—" : nf(Math.round(s.target_index))) + "</td><td>" +
            (s.shock > 0 ? "+" : "") + s.shock.toFixed(2) + "%</td><td>" +
            rsCompact(s.portfolio_value) + "</td><td class='" + cls + "'>" +
            signedRs(s.impact_rs) + " (" + (s.gain_loss_pct > 0 ? "+" : "") +
            s.gain_loss_pct.toFixed(2) + "%)</td></tr>";
        }).join("") + "</tbody></table></div>" +
      "<div class='pf-var-note'>Beta-propagated (β = " + risk.beta_used +
      "): NEPSE shocks show % and index-point move from " +
      (idxVal == null ? "the current index" : nf(Math.round(idxVal)) + " pts") +
      ". Stress scenarios cover invested holdings; Broker Cash is included in Total Equity but is not market-shocked. " +
      "Book impact flows via beta.</div>";
    }
  }

  function renderFactors(f) {
    var box = el("pf-factors");
    if (!box) return;
    if (!f || !f.ok) {
      box.innerHTML = "<div class='pf-muted'>" + esc((f && f.reason) || "Risk decomposition unavailable.") + "</div>";
      return;
    }
    var sys = f.systematic_pct, idio = f.idiosyncratic_pct;
    var split = "<div class='pf-split'><div class='pf-split-bar'>" +
      "<i class='sys' style='width:" + sys + "%'></i><i class='idio' style='width:" + idio + "%'></i></div>" +
      "<div class='pf-split-legend'>" +
        "<span><i class='sys'></i>Market " + sys.toFixed(1) + "%</span>" +
        "<span><i class='idio'></i>Stock-specific " + idio.toFixed(1) + "%</span></div></div>";
    var stats = "<div class='pf-stat-grid pf-stat-3'>" +
      stat("Total volatility", f.total_vol_pct.toFixed(1) + "%", "annualised") +
      stat("Market (systematic)", f.systematic_vol_pct.toFixed(1) + "%", "β = " + f.beta) +
      stat("Stock-specific", f.idiosyncratic_vol_pct.toFixed(1) + "%", "diversifiable") +
      "</div>";
    // Sector bars moved out to renderSectorRisk() so they can sit beside the
    // capital donut instead of below it; this block stays portfolio-level.
    box.innerHTML = split + stats +
      "<div class='pf-var-note'>Single-factor (NEPSE) model. <b>Market</b> risk moves with the index and can't be " +
      "diversified away; <b>stock-specific</b> risk can be cut by diversifying. Covers " +
      f.covered_weight_pct.toFixed(0) + "% of book value (names with price history).</div>";
  }

  /* Risk-adjusted performance. Every ratio here is weekly-based, matching the
     beta the desk already estimates — see the SOP. The header states plainly
     that this is today's weights run over past prices, not realised P/L. */
  function renderPerformance(p) {
    var box = el("pf-performance");
    if (!box) return;
    if (!p || !p.ok) {
      box.innerHTML = "<div class='pf-muted'>" + esc((p && p.reason) || "Performance unavailable.") + "</div>";
      return;
    }
    var sign = function (v) { return v == null ? "" : (v >= 0 ? "pos" : "neg"); };
    var n3 = function (v) { return v == null ? "—" : v.toFixed(2); };
    var p2 = function (v) { return v == null ? "—" : v.toFixed(2) + "%"; };

    var head = "<div class='pf-stat-grid pf-stat-3'>" +
      stat("Portfolio return", p2(p.portfolio_return_pct), "annualised, " + p.weeks + " weeks") +
      stat("NEPSE return", p2(p.benchmark_return_pct), "same window") +
      stat("Active return", p2(p.active_return_pct), "portfolio − index") +
      "</div>";

    var ratios = "<div class='pf-stat-grid pf-stat-3'>" +
      stat("Sharpe", n3(p.sharpe), "per unit of total risk") +
      stat("Treynor", n3(p.treynor), "per unit of beta") +
      stat("Information ratio", n3(p.information_ratio), "active return ÷ tracking error") +
      "</div>" +
      "<div class='pf-stat-grid pf-stat-3'>" +
      stat("Jensen's alpha", p2(p.jensen_alpha_pct), "vs CAPM required return") +
      stat("M² alpha", p2(p.m2_alpha_pct), "at NEPSE volatility") +
      stat("Tracking error", p2(p.tracking_error_pct), "annualised") +
      "</div>";

    // Sharpe/Treynor inverate when excess return is negative — a riskier book
    // scores a less-negative ratio. Say so rather than let it be misread.
    var warn = p.excess_negative
      ? "<div class='pf-var-note pf-warn-note'><b>Excess return is negative.</b> " +
        "Sharpe and Treynor rank unreliably below the risk-free rate — a riskier " +
        "portfolio produces a less negative ratio. Read alpha and active return instead.</div>"
      : "";

    box.innerHTML = head + ratios + warn +
      "<div class='pf-var-note'>Weekly returns vs NEPSE, risk-free " +
      p2(p.risk_free_pct) + ", β = " + (p.beta_used == null ? "—" : p.beta_used) + ". " +
      "<b>Sharpe / M²</b> use total risk — the right read if this book is your whole wealth. " +
      "<b>Treynor / alpha</b> use beta — the right read if it is one sleeve of a diversified pot. " +
      "Figures apply <b>current weights to past prices</b>, so they show how today's book would have " +
      "scored, not what was actually earned.</div>";
  }

  /* Correlation: effective-holdings counts names, this checks whether they move
     apart. Ten NEPSE banks score well on count and badly here. */
  function renderCorrelation(c) {
    var box = el("pf-correlation");
    if (!box) return;
    if (!c || !c.ok) {
      box.innerHTML = "<div class='pf-muted'>" + esc((c && c.reason) || "Correlation unavailable.") + "</div>";
      return;
    }
    var dr = c.diversification_ratio_pct;
    var stats = "<div class='pf-stat-grid pf-stat-3'>" +
      stat("Avg correlation", c.avg_correlation.toFixed(2), c.band + " — " + c.pairs_measured + " pairs") +
      stat("Diversification ratio", dr == null ? "—" : dr.toFixed(1) + "%", "lower is better") +
      stat("Portfolio vol", (c.portfolio_vol_pct == null ? "—" : c.portfolio_vol_pct.toFixed(1) + "%"),
           "avg holding " + (c.avg_holding_vol_pct == null ? "—" : c.avg_holding_vol_pct.toFixed(1) + "%")) +
      "</div>";

    var pairRow = function (p) {
      var w = Math.max(0, Math.min(100, p.corr * 100)).toFixed(0);
      return "<div class='pf-fac-srow'><span class='pf-sec-name'>" +
        esc(p.a) + " · " + esc(p.b) + "</span>" +
        "<span class='pf-sec-bar'><i style='width:" + w + "%'></i></span>" +
        "<span class='pf-sec-pct'>" + p.corr.toFixed(2) + "</span></div>";
    };
    var most = (c.most_correlated || []).map(pairRow).join("");
    var least = (c.least_correlated || []).map(pairRow).join("");

    box.innerHTML = stats +
      "<div class='pf-liq-label'>Most correlated pairs — least diversifying</div>" + most +
      "<div class='pf-liq-label'>Least correlated pairs — doing the real work</div>" + least +
      "<div class='pf-var-note'>Weekly returns. <b>Diversification ratio</b> = portfolio volatility ÷ " +
      "average single-holding volatility; 100% means holding several names bought you nothing. " +
      "Adding more holdings does not diversify — adding holdings that behave <i>differently</i> does.</div>";
  }

  function dtlText(dtl) {
    if (dtl == null) return "untradeable";
    if (dtl > 30) return ">30d";
    if (dtl < 0.1) return "<0.1d";
    return dtl.toFixed(1) + "d";
  }

  function renderLiquidity(L) {
    var box = el("pf-liq");
    if (!box) return;
    if (!L || !L.ok) { box.innerHTML = "<div class='pf-muted'>Liquidity data unavailable.</div>"; return; }
    var participation = el("pf-liq-participation");
    var target = el("pf-liq-target");
    var run = el("pf-liq-calculate");
    if (participation) participation.value = String(L.participation_pct || 20);
    if (target) target.value = String(L.custom_target_pct || 100);
    if (run && !run._pfBound) {
      run._pfBound = true;
      run.addEventListener("click", function () {
        var p = participation ? participation.value : 20;
        var t = target ? target.value : 100;
        run.disabled = true;
        loadPortfolio({ participation: p, liquidation_pct: t }).then(function () {
          run.disabled = false;
        });
      });
      if (target) target.addEventListener("keydown", function (event) {
        if (event.key === "Enter") run.click();
      });
    }
    var illiq = L.illiquid_count + (L.untradeable_count ? " (" + L.untradeable_count + " untradeable)" : "");
    var kpis = "<div class='pf-stat-grid pf-liq-kpis'>" +
      stat("Sellable in 1 day", (L.liquidatable_1d_pct || 0).toFixed(1) + "%", "of book value") +
      stat("Sellable in 5 days", (L.liquidatable_5d_pct || 0).toFixed(1) + "%", "of book value") +
      stat("Avg days to exit", L.wavg_days == null ? "—" : L.wavg_days.toFixed(1), "value-weighted") +
      stat("Illiquid positions", illiq, "> 5 days to exit") +
      "</div>";
    var scenarioRows = (L.liquidation_scenarios || {})[String(L.participation_pct)] || [];
    var milestones = "<div class='pf-liq-label'>Portfolio liquidation milestones</div>" +
      "<div class='pf-mini-table-wrap'><table class='pf-mini-table pf-liq-table'>" +
      "<thead><tr><th>Portfolio to sell</th><th>Days to liquidate</th><th>Liquidity risk</th></tr></thead><tbody>" +
      scenarioRows.map(function (row) {
        return "<tr" + (row.custom ? " class='is-custom'" : "") + "><td>" +
          row.target_pct.toFixed(1) + "%" + (row.custom ? " · custom" : "") + "</td><td>" +
          (row.days == null ? "Not achievable" : dtlText(row.days)) + "</td><td>" +
          "<span class='pf-liq-tier tier-" + esc(row.risk) + "'>" +
            esc(row.risk_label || liqRiskLabel(row.risk)) + "</span></td></tr>";
      }).join("") + "</tbody></table></div>";
    var list = "<div class='pf-liq-label'>Least liquid holdings</div><div class='pf-liq-list'>" +
      (L.least_liquid || []).map(function (r) {
        return "<div class='pf-liq-row'><span class='pf-tkr'>" + esc(r.symbol) + "</span>" +
          "<span class='pf-liq-tier tier-" + esc(r.tier) + "'>" +
            esc(r.risk_label || liqRiskLabel(r.tier)) + "</span>" +
          "<span class='pf-liq-adv'>ADV " + nf(r.adv_qty) + "</span>" +
          "<span class='pf-liq-dtl'>" + dtlText(r.dtl) + "</span></div>";
      }).join("") + "</div>";
    box.innerHTML = kpis + milestones + list +
      "<div class='pf-var-note'>Days-to-liquidate uses " + L.lookback_sessions +
      " market sessions from a one-year ADV window and caps each holding at " +
      L.participation_pct + "% of ADV per session. Positions sell simultaneously; " +
      "an unreachable target includes value with no observed volume.</div>";
  }

})();

/* Mutual Fund — NAV and discount to NAV.
 *
 * NAV and discount only. The allocation tabs that used to live here were a
 * second implementation of what the desk already computes, and the two
 * disagreed; they are now links, and this file no longer aggregates anything.
 */
(function () {
  "use strict";

  var CFG = window.MF_CONFIG || {};
  var el = function (id) { return document.getElementById(id); };

  function esc(s) {
    return String(s == null ? "" : s).replace(/[&<>"']/g, function (c) {
      return { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c];
    });
  }
  function num(v, dp) {
    if (v == null || isNaN(v)) return "—";
    return Number(v).toLocaleString("en-US",
      { minimumFractionDigits: dp == null ? 2 : dp, maximumFractionDigits: dp == null ? 2 : dp });
  }
  function pct(v, dp) {
    if (v == null || isNaN(v)) return "—";
    return (v > 0 ? "+" : "") + Number(v).toFixed(dp == null ? 2 : dp) + "%";
  }
  function getJSON(url) {
    return fetch(url, { headers: { "X-Requested-With": "XMLHttpRequest" } })
      .then(function (r) { return r.ok ? r.json() : Promise.reject(r.status); });
  }
  function empty(table, message) {
    table.innerHTML = '<tbody><tr><td class="mf-empty">' + message + "</td></tr></tbody>";
  }

  /* ---------- NAV & discount ---------- */
  /* No tab machinery: the page has one panel, and the other "tabs" are plain
     links to the desk screens. The old handler also bound to those links and
     blanked the page in the instant before navigation. */
  function navBar(discount) {
    /* A discount bar, drawn from the centre: left of centre is below NAV.
       Scaled to ±20%, which covers the observed range with room to spare. */
    if (discount == null) return "";
    var capped = Math.max(-20, Math.min(20, discount));
    var half = Math.abs(capped) / 20 * 50;
    var colour = discount < 0 ? "var(--down,#ef4444)" : "var(--up,#22c55e)";
    var style = discount < 0
      ? "right:50%;width:" + half + "%;background:" + colour
      : "left:50%;width:" + half + "%;background:" + colour;
    return '<div class="mf-bar"><span style="' + style + '"></span></div>';
  }

  function renderNav(d) {
    var t = el("mf-table");
    if (!d || !d.ok || !d.funds || !d.funds.length) {
      empty(t, "No NAV data yet. Run <b>Sync Fund NAV</b> in the Raw Inventory Manager.");
      return;
    }
    var head = "<thead><tr>" +
      "<th>Symbol</th><th>Fund</th><th class='num'>NAV</th><th class='num'>Price</th>" +
      "<th class='num'>Discount</th><th></th><th>NAV as of</th></tr></thead>";
    var body = d.funds.map(function (f) {
      var cls = f.discount_pct == null ? "" : (f.discount_pct < 0 ? "neg" : "pos");
      return "<tr>" +
        "<td><a href='/stock/" + encodeURIComponent(f.symbol) + "/'>" + esc(f.symbol) + "</a>" +
          (f.is_matured ? " <span class='mf-chip'>matured</span>" : "") + "</td>" +
        "<td>" + esc(f.name || "") + "</td>" +
        "<td class='num'>" + num(f.nav) + "</td>" +
        "<td class='num'>" + num(f.price) + "</td>" +
        "<td class='num mf-disc " + cls + "'>" + pct(f.discount_pct) + "</td>" +
        "<td>" + navBar(f.discount_pct) + "</td>" +
        "<td class='mf-sub'>" + esc(f.nav_when || "") + " · " + esc(f.nav_basis || "") + "</td>" +
        "</tr>";
    }).join("");
    t.innerHTML = head + "<tbody>" + body + "</tbody>";

    el("mf-sub").textContent = d.count + " funds · " + d.priced + " with a market price";

    var cov = CFG.coverage || {};
    el("mf-kpis").innerHTML = [
      kpi("Funds with NAV", d.count),
      kpi("Median discount", pct(d.median_discount_pct)),
      kpi("Active listed covered", (cov.withNav || 0) + " / " + (cov.listed || 0)),
      kpi("Trading below NAV", d.funds.filter(function (f) {
        return f.discount_pct != null && f.discount_pct < 0;
      }).length)
    ].join("");
  }
  function kpi(label, value) {
    /* Classes must be dsx-kpi-label / dsx-kpi-val — floorsheet.css styles those
       two and nothing else. (canslim.js emits dsx-kpi-value, which has no rule
       at all, so its KPI numbers render unstyled.) */
    return '<div class="dsx-kpi"><span class="dsx-kpi-label">' + esc(label) +
           '</span><span class="dsx-kpi-val">' + esc(value) + "</span></div>";
  }

  document.addEventListener("DOMContentLoaded", function () {
    getJSON(CFG.navUrl).then(renderNav).catch(function () {
      empty(el("mf-table"), "Could not load NAV data.");
    });
  });
})();

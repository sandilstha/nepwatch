/* fundamentals-desk.js — Fundamental Analysis desk.
 *
 * NAMESPACE: everything here is ia-* / IA_CONFIG, never fa-* / FA_CONFIG.
 * Those belong to Stock 360's fundamentals.js module. Keeping this desk off
 * that namespace means the two can never collide if they ever share a page —
 * they did once, silently: #fa-sector is a sector LABEL there but a sector
 * <select> here, and FA_CONFIG.matrixUrl is the per-company matrix, not this
 * desk's industry endpoint.
 *
 * Owns tab switching and the Industry Analysis matrix. The Smart Stock Score
 * pane is rendered by canslim.js, which is loaded alongside and binds to its own
 * element ids — the two never touch each other's DOM.
 */
(function () {
  "use strict";

  var CFG = window.IA_CONFIG || {
    matrixUrl: "/fundamentals/industry/",
    periodsUrl: "/fundamentals/industry/periods/"
  };
  var state = { stmt: "BS", loaded: false, last: null };

  function el(id) { return document.getElementById(id); }
  function esc(s) {
    return String(s == null ? "" : s).replace(/[&<>"']/g, function (c) {
      return { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c];
    });
  }

  /* Figures are shown EXACTLY as filed — in thousands of rupees, with comma
     separators. An earlier version abbreviated to B/M, which was easier to scan
     but silently restated the source: a filing of 420,134,125 (thousands) read
     as "420.13B", so the number on screen matched nothing in the report.

     Only money rows get thousands treatment. Ratios, percentages and per-share
     figures (Margin %, ROE, EPS, PE, Book Value) are NOT in Rs 000 and keep
     their decimals — rounding EPS of -17.4 to an integer would destroy it. */
  function fmtVal(v, unit) {
    if (v === null || v === undefined) {
      return "<span class='ia-blank' title='not reported by this company'>—</span>";
    }
    var n = Number(v);
    if (isNaN(n)) return "—";
    var money = unit && /000/.test(unit);
    /* Percent-unit lines are FILED AS FRACTIONS (ROE 0.09 = 9%). Displaying
       the raw fraction made every ratio read as ~0 — show them as percents. */
    var pctUnit = unit && /%/.test(unit) && !money;
    /* A handful of filed percentages are impossible — STC files 'Growth Period
       on Period' as 12,270,058, which the fraction convention turns into over a
       billion percent. Those are source artefacts, usually growth measured off
       a near-zero base. They are still shown, because silently hiding a filed
       number is worse, but they are marked so the reader blames the filing and
       not the page, and abbreviated so one bad cell cannot stretch the column.
       The cut-off is 1000%: real quarterly growth reaches a few hundred. */
    var absurd = pctUnit && Math.abs(n) > 10;
    var txt = money
      ? n.toLocaleString(undefined, { maximumFractionDigits: 0 })
      : absurd
      ? (n * 100).toExponential(1) + "%"
      : pctUnit
      ? (n * 100).toLocaleString(undefined, { minimumFractionDigits: 2, maximumFractionDigits: 2 }) + "%"
      : n.toLocaleString(undefined, {
          minimumFractionDigits: Math.abs(n) < 100 ? 2 : 0,
          maximumFractionDigits: Math.abs(n) < 100 ? 2 : 1
        });
    var cls = (n < 0 ? "num-neg" : "") + (absurd ? " ia-implausible" : "");
    var tip = absurd
      ? "Filed as " + n.toLocaleString(undefined, { maximumFractionDigits: 4 }) +
        " " + esc(unit) + ", i.e. " +
        (n * 100).toLocaleString(undefined, { maximumFractionDigits: 0 }) +
        "%. That is not a plausible percentage — the source figure looks wrong, " +
        "usually growth measured against a near-zero base."
      : n.toLocaleString(undefined, { maximumFractionDigits: 4 }) + (unit ? " " + esc(unit) : "");
    return "<span class='" + cls + "' title='" + tip + "'>" + txt + "</span>";
  }


  /* ── CSV export ───────────────────────────────────────────────────────
     Built from the payload the table was drawn from, so the file always
     matches what is on screen. Values are exported RAW, not formatted: money
     rows keep the Rs '000 figure exactly as filed, percent rows are written as
     percents (the feed sends them as fractions, the same conversion the table
     makes), and a line no company reported stays empty rather than becoming a
     zero. `reported_by` travels with each row so a partial line stays visible
     as partial in the file. */
  function csvCell(v) {
    var s = (v === null || v === undefined) ? "" : String(v);
    return /[",\r\n]/.test(s) ? '"' + s.replace(/"/g, '""') + '"' : s;
  }

  function csvRow(cells, width) {
    var out = cells.slice();
    while (out.length < width) out.push("");
    return out.map(csvCell).join(",");
  }

  function csvValue(v, unit) {
    if (v === null || v === undefined) return "";
    var n = Number(v);
    if (isNaN(n)) return "";
    var money = unit && /000/.test(unit);
    var pctUnit = unit && /%/.test(unit) && !money;
    // *100 on a float leaves noise (0.09 -> 9.000000000000002), so round it.
    return pctUnit ? String(Math.round(n * 1000000) / 10000) : String(n);
  }

  function toCsv(d) {
    var width = 6 + d.companies.length;
    var moneyRows = d.rows.filter(function (r) { return /000/.test(r.unit || ""); }).length;
    var lines = [
      csvRow(["Industry Analysis export"], width),
      csvRow(["Sector", d.sector], width),
      csvRow(["Statement", d.statement], width),
      csvRow(["Period", d.period_label], width),
      csvRow(["Companies", d.companies.length], width),
      csvRow(["Line items", d.line_items], width),
      csvRow(["Amounts", moneyRows ? "Money rows are in Rs '000 exactly as filed" : "No money rows in this statement"], width),
      csvRow(["Percent rows", "Written as percent, e.g. 9 means 9%"], width),
      csvRow(["Blank cell", "Line not reported by that company, not a zero"], width),
      csvRow(["Filed line", "Where a name is filed on two different lines, each is a separate row"], width),
      csvRow(["Share price", d.price_as_of
        ? "Market Value per Share and Reported PE use the close of " + d.price_as_of
        : "As filed with the statement"], width),
      csvRow(["Exported", new Date().toISOString()], width),
      csvRow(["Source", window.location.origin + "/fundamentals/ · Industry Analysis"], width)
    ];
    if (d.statement_note || d.note) {
      lines.push(csvRow(["Note", ((d.statement_note || "") + " " + (d.note || "")).trim()], width));
    }
    lines.push(csvRow([], width));
    lines.push(csvRow(
      ["Line item", "Filed line", "Unit", "Reported by", "Of companies", "Price basis"]
        .concat(d.companies), width));
    d.rows.forEach(function (r) {
      lines.push(csvRow(
        [r.item, r.line || "", r.unit || "", r.reported_by, d.companies.length,
         r.live ? "close of " + r.as_of : "as filed"].concat(
          r.values.map(function (v) { return csvValue(v, r.unit); })
        ), width));
    });
    return lines.join("\r\n");
  }

  function slug(s) {
    return String(s || "").replace(/[^A-Za-z0-9]+/g, "-").replace(/^-|-$/g, "") || "export";
  }

  function downloadCsv(d) {
    var name = "Industry_" + slug(d.sector) + "_" + slug(d.statement) + "_" + slug(d.period_label) + ".csv";
    // The BOM makes Excel read it as UTF-8 instead of the system codepage.
    var blob = new Blob(["\ufeff" + toCsv(d)], { type: "text/csv;charset=utf-8;" });
    var url = URL.createObjectURL(blob);
    var a = document.createElement("a");
    a.href = url;
    a.download = name;
    document.body.appendChild(a);
    a.click();
    document.body.removeChild(a);
    setTimeout(function () { URL.revokeObjectURL(url); }, 1000);
  }

  function drawMatrix(d) {
    var t = el("ia-table");
    if (!t) return;

    if (!d.ok) {
      /* A disabled statement is a DATA LIMIT, not a failure — say which. */
      var msg = d.unavailable
        ? "<b>" + esc(d.statement) + " is not available.</b><br>" + esc(d.reason)
        : esc(d.reason || "No data for this selection.");
      t.innerHTML = "<tbody><tr><td class='dsx-empty'>" + msg + "</td></tr></tbody>";
      var n0 = el("ia-note"); if (n0) n0.textContent = "";
      state.last = null;
      var b0 = el("ia-csv"); if (b0) b0.disabled = true;
      return;
    }

    state.last = d;
    var csvBtn = el("ia-csv");
    if (csvBtn) csvBtn.disabled = !(d.rows && d.rows.length);

    var head = "<thead><tr><th class='l ia-item'>Line item</th>" +
      d.companies.map(function (c) { return "<th class='ia-co'>" + esc(c) + "</th>"; }).join("") +
      "</tr></thead>";

    var body = d.rows.map(function (r) {
      /* A line only some companies report is not a sector-wide comparison —
         flag it rather than let the blanks read as zeros. */
      var partial = r.reported_by < d.companies.length
        ? " <span class='ia-partial' title='reported by " + r.reported_by + " of " +
          d.companies.length + " companies'>" + r.reported_by + "/" + d.companies.length + "</span>"
        : "";
      /* Share price and PE are re-stated at the latest close instead of the
         price filed months ago with the statement. Say so on the row, and say
         how many columns it covers, since the feed's median/average columns are
         not tradable and keep their filed figure. */
      var live = r.live
        ? " <span class='ia-live' title='Not the price filed with the statement. " +
          "This row uses the closing price of " + esc(r.as_of) + " for " +
          r.live_for + " of " + d.companies.length + " columns; any column without " +
          "a traded price keeps the filed figure.'>live " + esc(r.as_of) + "</span>"
        : "";
      return "<tr><td class='l ia-item'>" + esc(r.item) + partial + live +
        (r.unit ? " <span class='ia-unit'>" + esc(r.unit) + "</span>" : "") + "</td>" +
        r.values.map(function (v) {
          return "<td class='num'>" + fmtVal(v, r.unit) + "</td>";
        }).join("") + "</tr>";
    }).join("");

    t.innerHTML = head + "<tbody>" + body + "</tbody>";

    var title = el("ia-title");
    if (title) title.textContent = (d.statement || "").toUpperCase();
    var sub = el("ia-sub");
    if (sub) {
      var moneyRows = d.rows.filter(function (r) { return /000/.test(r.unit || ""); }).length;
      sub.textContent = d.sector + " · " + d.period_label + " · " +
        d.companies.length + " companies · " + d.line_items + " line items" +
        (moneyRows ? " · amounts in Rs '000 as filed" : "");
    }
    var note = el("ia-note");
    if (note) note.textContent = (d.statement_note || "") + " " + (d.note || "");
  }

  function fillPeriods(periods, keep) {
    var sel = el("ia-period");
    if (!sel) return;
    sel.innerHTML = (periods || []).map(function (p) {
      return "<option value='" + esc(p.fiscal_year) + "|" + p.quarter + "'>" +
        esc(p.label) + " (" + p.companies + ")</option>";
    }).join("");
    if (keep) sel.value = keep;
  }

  function load(usePeriod) {
    var t = el("ia-table");
    if (t) t.innerHTML = "<tbody><tr><td class='dsx-empty'>Loading…</td></tr></tbody>";
    var sector = (el("ia-sector") || {}).value || "";
    var q = "?sector=" + encodeURIComponent(sector) + "&fs_type=" + encodeURIComponent(state.stmt);
    if (usePeriod) {
      var parts = String(usePeriod).split("|");
      q += "&fiscal_year=" + encodeURIComponent(parts[0]) + "&quarter=" + encodeURIComponent(parts[1]);
    }
    fetch(CFG.matrixUrl + q, { headers: { "X-Requested-With": "XMLHttpRequest" } })
      .then(function (r) { return r.json(); })
      .then(function (d) {
        if (d.ok && d.periods) {
          fillPeriods(d.periods, d.fiscal_year + "|" + d.quarter);
        }
        drawMatrix(d);
      })
      .catch(function () {
        if (t) t.innerHTML = "<tbody><tr><td class='dsx-empty'>Could not load this statement.</td></tr></tbody>";
      });
  }

  function initTabs() {
    var tabs = document.querySelectorAll(".dsx-tabs .dsx-tab");
    [].forEach.call(tabs, function (btn) {
      btn.addEventListener("click", function () {
        [].forEach.call(tabs, function (b) { b.classList.remove("active"); });
        btn.classList.add("active");
        var want = btn.getAttribute("data-tab");
        // Any .dsx-panel with id "panel-<tab>" is a tab pane; no list to maintain.
        [].forEach.call(document.querySelectorAll(".dsx-panel[id^='panel-']"), function (pane) {
          pane.classList.toggle("active", pane.id === "panel-" + want);
        });
      });
    });
  }

  document.addEventListener("DOMContentLoaded", function () {
    initTabs();
    var sec = el("ia-sector"), per = el("ia-period"), btn = el("ia-refresh");
    if (sec) sec.addEventListener("change", function () { load(); });
    if (per) per.addEventListener("change", function () { load(per.value); });
    if (btn) btn.addEventListener("click", function () { load(per && per.value); });

    var csv = el("ia-csv");
    if (csv) csv.addEventListener("click", function () {
      if (state.last) downloadCsv(state.last);
    });

    /* Statement pills. The disabled-pill guard stays even though no pill is
       currently disabled: a disabled button still fires click events in some
       browsers, so any future greyed-out statement is safe by default. */
    var seg = document.querySelector("[data-group='ia-stmt']");
    if (seg) seg.addEventListener("click", function (e) {
      var pill = e.target.closest ? e.target.closest(".dsx-pill") : null;
      if (!pill || pill.disabled || pill.classList.contains("is-disabled")) return;
      [].forEach.call(seg.querySelectorAll(".dsx-pill"), function (p) {
        p.classList.remove("active");
      });
      pill.classList.add("active");
      state.stmt = pill.getAttribute("data-val");
      load();   // period list differs per statement, so re-resolve it
    });

    load();
  });
})();

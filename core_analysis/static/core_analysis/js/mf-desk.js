/* Shared helpers for the six mutual fund report screens.
 *
 * Each screen supplies a column spec and a row source; everything else —
 * rendering, sorting, CSV export, the empty state — lives here so the reports
 * behave identically. A reader who learns to sort one table has learned all six.
 */
(function () {
  "use strict";

  var MFD = (window.MFD = window.MFD || {});

  /* ---------- formatting ---------- */
  function esc(s) {
    return String(s == null ? "" : s).replace(/[&<>"']/g, function (c) {
      return { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c];
    });
  }
  function num(v, dp) {
    if (v == null || v === "" || isNaN(v)) return "—";
    dp = dp == null ? 2 : dp;
    return Number(v).toLocaleString("en-US",
      { minimumFractionDigits: dp, maximumFractionDigits: dp });
  }
  function pct(v, dp) {
    if (v == null || isNaN(v)) return "—";
    return Number(v).toFixed(dp == null ? 2 : dp) + "%";
  }
  function signed(v, dp) {
    if (v == null || isNaN(v)) return "—";
    return (v > 0 ? "+" : "") + Number(v).toFixed(dp == null ? 2 : dp) + "%";
  }
  /* Balance-sheet money is stored in rupees but the published tables print
     thousands. Screens that want to line up with them call this and say so in
     their footnote — the conversion never happens in storage. */
  function thousands(v, dp) {
    if (v == null || isNaN(v)) return "—";
    return num(Number(v) / 1000, dp == null ? 0 : dp);
  }
  function cls(v) { return v == null ? "" : (v < 0 ? "mfd-neg" : (v > 0 ? "mfd-pos" : "")); }

  function getJSON(url, params) {
    var qs = [];
    Object.keys(params || {}).forEach(function (k) {
      if (params[k] != null && params[k] !== "")
        qs.push(encodeURIComponent(k) + "=" + encodeURIComponent(params[k]));
    });
    return fetch(url + (qs.length ? "?" + qs.join("&") : ""),
                 { headers: { "X-Requested-With": "XMLHttpRequest" } })
      .then(function (r) { return r.ok ? r.json() : Promise.reject(r.status); });
  }

  /* ---------- table ----------
     cols: [{key, label, fmt(row)->html, raw(row)->sortable, sticky}]
     opts: {total: row, empty: html}
  */
  function Table(el, cols, opts) {
    this.el = el; this.cols = cols; this.opts = opts || {};
    this.rows = []; this.sortKey = null; this.sortAsc = false;
  }
  Table.prototype.setRows = function (rows, total) {
    this.rows = rows || []; this.total = total || null; this.render();
  };
  Table.prototype.render = function () {
    var self = this;
    if (!this.rows.length) {
      this.el.innerHTML = '<tbody><tr><td class="mfd-empty">' +
        (this.opts.empty || "Nothing to show.") + "</td></tr></tbody>";
      return;
    }
    var rows = this.rows.slice();
    if (this.sortKey) {
      var col = this.cols.filter(function (c) { return c.key === self.sortKey; })[0];
      if (col) {
        rows.sort(function (a, b) {
          var x = col.raw ? col.raw(a) : a[col.key];
          var y = col.raw ? col.raw(b) : b[col.key];
          // Missing values sink to the bottom in BOTH directions: a blank is
          // not "the smallest", it is "unknown", and sorting it to the top of
          // an ascending list buries the rows the reader asked for.
          if (x == null && y == null) return 0;
          if (x == null) return 1;
          if (y == null) return -1;
          if (typeof x === "string" || typeof y === "string")
            return self.sortAsc ? String(x).localeCompare(String(y))
                                : String(y).localeCompare(String(x));
          return self.sortAsc ? x - y : y - x;
        });
      }
    }
    var head = "<thead><tr>" + this.cols.map(function (c) {
      var k = c.key === self.sortKey ? (" sorted" + (self.sortAsc ? " asc" : "")) : "";
      return '<th class="' + k + '" data-key="' + esc(c.key) + '" title="' +
             esc(c.title || c.label) + '">' + esc(c.label) + "</th>";
    }).join("") + "</tr></thead>";

    function body(row, extra) {
      return "<tr" + (extra ? ' class="' + extra + '"' : "") + ">" +
        self.cols.map(function (c) {
          return "<td>" + (c.fmt ? c.fmt(row) : esc(row[c.key])) + "</td>";
        }).join("") + "</tr>";
    }
    var html = head + "<tbody>";
    if (this.total) html += body(this.total, "mfd-total");
    html += rows.map(function (r) { return body(r, null); }).join("") + "</tbody>";
    this.el.innerHTML = html;

    [].slice.call(this.el.querySelectorAll("thead th")).forEach(function (th) {
      th.addEventListener("click", function () {
        var key = th.getAttribute("data-key");
        if (self.sortKey === key) self.sortAsc = !self.sortAsc;
        else { self.sortKey = key; self.sortAsc = false; }
        self.render();
      });
    });
  };
  Table.prototype.csv = function (name) {
    var self = this;
    var lines = [this.cols.map(function (c) { return c.label; }).join(",")];
    function line(row) {
      return self.cols.map(function (c) {
        var v = c.raw ? c.raw(row) : row[c.key];
        if (v == null) return "";
        v = String(v);
        return /[",\n]/.test(v) ? '"' + v.replace(/"/g, '""') + '"' : v;
      }).join(",");
    }
    if (this.total) lines.push(line(this.total));
    this.rows.forEach(function (r) { lines.push(line(r)); });
    download(name, lines.join("\n"), "text/csv");
  };

  function download(name, text, mime) {
    var blob = new Blob([text], { type: mime + ";charset=utf-8;" });
    var a = document.createElement("a");
    a.href = URL.createObjectURL(blob);
    a.download = name;
    document.body.appendChild(a); a.click();
    setTimeout(function () { URL.revokeObjectURL(a.href); a.remove(); }, 0);
  }

  /* ---------- period picker ---------- */
  function fillPeriods(select, periods, selected) {
    if (!select) return;
    select.innerHTML = (periods || []).map(function (p) {
      return '<option value="' + esc(p) + '"' +
             (p === selected ? " selected" : "") + ">" + esc(p) + "</option>";
    }).join("") || '<option value="">— none —</option>';
  }

  /* Keep the current selection in the URL so a report can be linked to and a
     reload does not silently jump back to the newest month. */
  function syncUrl(params) {
    try {
      var u = new URL(window.location.href);
      Object.keys(params).forEach(function (k) {
        if (params[k]) u.searchParams.set(k, params[k]);
        else u.searchParams.delete(k);
      });
      window.history.replaceState(null, "", u.toString());
    } catch (e) {}
  }
  function urlParam(name) {
    try { return new URL(window.location.href).searchParams.get(name); }
    catch (e) { return null; }
  }

  MFD.esc = esc; MFD.num = num; MFD.pct = pct; MFD.signed = signed;
  MFD.thousands = thousands; MFD.cls = cls; MFD.getJSON = getJSON;
  MFD.Table = Table; MFD.fillPeriods = fillPeriods; MFD.download = download;
  MFD.syncUrl = syncUrl; MFD.urlParam = urlParam;
})();

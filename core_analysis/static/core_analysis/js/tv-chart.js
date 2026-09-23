/* ============================================================================
   TradingView Advanced Charts bootstrap (progressive enhancement).

   The Advanced Charts library is licensed and must be installed by hand (see
   TRADINGVIEW_SETUP.md). This script tries to load it; if present it replaces
   the NEPSE Index area chart with the full TradingView terminal wired to our
   UDF datafeed. If absent, it silently does nothing and the ApexCharts area
   chart rendered by insights.js stays in place.
   ========================================================================== */
(function () {
  "use strict";

  var cfg = window.MI_CONFIG || {};
  var udfBase = cfg.udfBase || "/insights/udf";
  var libRoot = cfg.tvLibraryPath || "/static/core_analysis/charting_library/";
  var LIB_JS = libRoot + "charting_library.standalone.js";
  var UDF_JS = libRoot + "datafeeds/udf/dist/bundle.js";

  function loadScript(src) {
    return new Promise(function (resolve, reject) {
      var s = document.createElement("script");
      s.src = src;
      s.async = true;
      s.onload = resolve;
      s.onerror = function () { reject(new Error("failed to load " + src)); };
      document.head.appendChild(s);
    });
  }

  function themeName() {
    return document.documentElement.getAttribute("data-theme") === "light" ? "light" : "dark";
  }

  function init() {
    if (typeof window.TradingView === "undefined" || !window.TradingView.widget || !window.Datafeeds) {
      return false;
    }
    var container = document.getElementById("mi-tv-container");
    if (!container) return false;

    // Hand the card over from Lightweight Charts to the full TradingView terminal.
    if (window.MIOHLC && window.MIOHLC.destroy) window.MIOHLC.destroy();
    window.__MI_TV_ACTIVE = true;
    container.style.display = "block";

    var hint = document.getElementById("ohlc-sessions");
    if (hint) hint.textContent = "TradingView · daily";

    window.__mi_tv_widget = new window.TradingView.widget({
      container: container,
      library_path: libRoot,
      datafeed: new window.Datafeeds.UDFCompatibleDatafeed(udfBase),
      symbol: cfg.tvSymbol || "NEPSE",
      interval: "1D",
      locale: "en",
      autosize: true,
      theme: themeName(),
      timezone: cfg.tvTimezone || "Asia/Kathmandu",
      disabled_features: ["use_localstorage_for_settings", "left_toolbar"],
      enabled_features: [],
      loading_screen: { backgroundColor: "transparent" }
    });
    return true;
  }

  // Expose a theme switcher so the dashboard's toggle can recolour the widget.
  window.MI_setTVTheme = function (theme) {
    var w = window.__mi_tv_widget;
    if (!w || !w.changeTheme) return;
    try {
      w.onChartReady(function () { w.changeTheme(theme === "light" ? "light" : "dark"); });
    } catch (e) { /* widget not ready */ }
  };

  loadScript(LIB_JS)
    .then(function () { return loadScript(UDF_JS); })
    .then(init)
    .catch(function () { /* library not installed — keep the ApexCharts fallback */ });
})();

/* Mobile table shim.
 *
 * DESKTOP SAFETY CONTRACT: every effect of this file is gated behind the same
 * 720px matchMedia used by responsive.css. Above that width it inserts nothing,
 * measures nothing and adds no class — the DOM is byte-identical to before.
 *
 * The site has ~40 data tables spread across templates that share no base, so
 * hand-wrapping each one in a scroll container would mean editing every page
 * and would miss every table rendered from JS. This wraps them at runtime
 * instead, including tables that appear later (the statement matrix, floorsheet
 * results and portfolio ledger all render after their fetch resolves).
 */
(function () {
  "use strict";

  var PHONE = "(max-width: 720px)";
  var mq = window.matchMedia ? window.matchMedia(PHONE) : null;
  if (!mq) return;                       // no matchMedia: leave the page alone

  function wrap(table) {
    var parent = table.parentNode;
    if (!parent || parent.classList.contains("rx-scroll")) return;
    // Respect containers that already solve this — floorsheet and the
    // fundamentals matrix ship their own scrolling wraps, and nesting a second
    // one would break their sticky headers.
    if (parent.classList.contains("fm-wrap") ||
        parent.classList.contains("gv-table-wrap") ||
        getComputedStyle(parent).overflowX === "auto") return;

    var box = document.createElement("div");
    box.className = "rx-scroll";
    parent.insertBefore(box, table);
    box.appendChild(table);
  }

  function unwrap(box) {
    var table = box.querySelector("table");
    if (!table) return;
    box.parentNode.insertBefore(table, box);
    box.parentNode.removeChild(box);
  }

  function sync() {
    if (mq.matches) {
      [].forEach.call(document.querySelectorAll("table"), wrap);
    } else {
      // Returning to desktop width restores the original DOM shape, so a
      // rotated tablet does not keep a stray wrapper the desktop CSS never
      // expected to style.
      [].forEach.call(document.querySelectorAll(".rx-scroll"), unwrap);
    }
  }

  function boot() {
    sync();
    // Tables that arrive with a fetch. Debounced, because the statement matrix
    // rebuilds its whole tbody row by row.
    var pending = null;
    new MutationObserver(function () {
      if (!mq.matches || pending) return;
      pending = requestAnimationFrame(function () { pending = null; sync(); });
    }).observe(document.body, { childList: true, subtree: true });
  }

  // addEventListener on a MediaQueryList is the modern form; addListener is the
  // Safari <14 fallback.
  if (mq.addEventListener) mq.addEventListener("change", sync);
  else if (mq.addListener) mq.addListener(sync);

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", boot);
  } else {
    boot();
  }
})();

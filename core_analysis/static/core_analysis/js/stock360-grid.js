/* stock360-grid.js — the Stock 360 "bento" layout on GridStack 10.3.1.
 *
 * Every card on the page is one tile on a 12-column grid: drag by the card
 * header to reorder, drag an edge/corner to resize, tiles reflow with no
 * overlap. The layout is saved to localStorage and restored on the next visit.
 *
 * GridStack is vendored (static/core_analysis/vendor/gridstack/), loaded from
 * this origin — no CDN, no jQuery. If the script fails to load or its CSS never
 * applied, the page falls back to a plain responsive CSS grid so nothing is
 * ever left as a raw, unpositioned stack.
 *
 * Tile content is filled in by stock360.js / fundamentals.js AFTER this runs,
 * so tile heights auto-fit their content as it arrives — until the user
 * resizes a tile by hand, after which that tile keeps the height they chose.
 */
(function () {
  "use strict";

  var CELL = 34;                          // px per grid row (cellHeight)
  var GAP = 6;                            // visual gutter, applied as tile-content padding (grid margin is 0)
  // Bump the suffix whenever the DEFAULT TILE SET changes (a tile added,
  // removed or renamed), so a stale saved layout cannot leave one missing or
  // duplicated. Position-only tweaks to the defaults do not need a bump.
  var STORE_KEY = "s360Layout.v1";

  // Default layout, by tile id. minW/minH stop a tile being shrunk to nothing;
  // fitH caps AUTO-fit only (a 24-quarter statement table would otherwise
  // stretch the page by thousands of px — past the cap it scrolls inside the
  // tile). The user can still drag a tile taller than fitH by hand.
  // Tiles absent from the page (e.g. fundnav on a non-fund symbol) are skipped.
  var DEFAULTS = {
    mkt:        { x: 0,  y: 0,  w: 4,  h: 8,  minW: 3, minH: 5, fitH: 14 },
    ratio:      { x: 4,  y: 0,  w: 4,  h: 8,  minW: 3, minH: 5, fitH: 14 },
    div:        { x: 8,  y: 0,  w: 4,  h: 8,  minW: 3, minH: 5, fitH: 14 },
    fundnav:    { x: 0,  y: 8,  w: 12, h: 8,  minW: 4, minH: 4, fitH: 12 },
    fund:       { x: 0,  y: 8,  w: 8,  h: 12, minW: 4, minH: 5, fitH: 18 },
    sop:        { x: 8,  y: 8,  w: 4,  h: 8,  minW: 3, minH: 4, fitH: 18 },
    tech:       { x: 8,  y: 16, w: 4,  h: 10, minW: 3, minH: 4, fitH: 16 },
    statements: { x: 0,  y: 20, w: 8,  h: 14, minW: 4, minH: 5, fitH: 18 },
    flow:       { x: 0,  y: 34, w: 12, h: 12, minW: 4, minH: 5, fitH: 18 },
    ai:         { x: 0,  y: 46, w: 12, h: 6,  minW: 4, minH: 3, fitH: 16 }
  };

  var host = document.getElementById("s360Grid");
  if (!host) return;
  var items = Array.prototype.slice.call(host.querySelectorAll(".grid-stack-item"));

  /* ---------------------------------------------------------------- fallback */
  function fallback(reason) {
    host.classList.add("s360-fallback");
    host.setAttribute("data-fallback", reason || "");
    items.forEach(function (el) { el.removeAttribute("style"); });
    var r = document.getElementById("s360LayoutReset");
    if (r) r.hidden = true;
  }

  if (typeof GridStack === "undefined") { fallback("script"); return; }

  /* ---------------------------------------------------------------- storage */
  function readSaved() {
    try {
      var raw = localStorage.getItem(STORE_KEY);
      if (!raw) return null;
      var parsed = JSON.parse(raw);
      return (parsed && Array.isArray(parsed.tiles)) ? parsed.tiles : null;
    } catch (e) { return null; }
  }
  function writeSaved(tiles) {
    try { localStorage.setItem(STORE_KEY, JSON.stringify({ v: 1, at: Date.now(), tiles: tiles })); } catch (e) {}
  }
  function clearSaved() { try { localStorage.removeItem(STORE_KEY); } catch (e) {} }

  /* ---------------------------------------------------------------- layout */
  // Ids present on this page, in DOM order.
  var ids = items.map(function (el) { return el.getAttribute("gs-id"); });

  function buildLayout(saved) {
    // Saved positions win for tiles that still exist; anything new falls back
    // to its default. A saved entry for a tile that no longer exists is dropped.
    var byId = {};
    (saved || []).forEach(function (t) { if (t && t.id) byId[t.id] = t; });
    return ids.map(function (id) {
      var d = DEFAULTS[id] || { x: 0, y: 0, w: 6, h: 6, minW: 3, minH: 3 };
      var s = byId[id];
      return {
        id: id,
        x: s && s.x != null ? s.x : d.x,
        y: s && s.y != null ? s.y : d.y,
        w: s && s.w != null ? Math.max(d.minW, s.w) : d.w,
        h: s && s.h != null ? Math.max(d.minH, s.h) : d.h,
        minW: d.minW, minH: d.minH,
        // remembered per tile: did the user set this height by hand?
        userH: !!(s && s.userH)
      };
    });
  }

  var saved = readSaved();
  var layout = buildLayout(saved);
  var userSized = {};
  var fitTimer = {};
  layout.forEach(function (t) { if (t.userH) userSized[t.id] = true; });

  var grid = GridStack.init({
    column: 12,
    cellHeight: CELL,
    margin: 0,
    float: false,
    animate: true,
    draggable: { handle: ".card-header" },  // header only — not the whole card
    resizable: { handles: "e,se,s,sw,w" },
    // Phones: one column, tiles in layout order (GridStack 10 responsive API).
    columnOpts: { breakpoints: [{ w: 760, c: 1 }], breakpointForWindow: true }
  }, host);

  // Tiles are server-rendered with gs-* attributes, so they are already
  // positioned by init(); load() then moves them to the saved/default layout
  // (matching by id) instead of creating widgets from scratch.
  grid.load(layout.map(function (t) {
    return { id: t.id, x: t.x, y: t.y, w: t.w, h: t.h, minW: t.minW, minH: t.minH };
  }), false);

  // CSS sanity: if GridStack's stylesheet never applied, items are not
  // absolutely positioned and would render as a raw stack — fall back.
  if (items.length && getComputedStyle(items[0]).position !== "absolute") {
    grid.destroy(false);
    fallback("css");
    return;
  }

  /* ---------------------------------------------------------------- persist */
  function snapshot() {
    return grid.save(false).map(function (n) {
      return { id: n.id, x: n.x, y: n.y, w: n.w, h: n.h, userH: !!userSized[n.id] };
    });
  }
  grid.on("change", function () { writeSaved(snapshot()); });
  grid.on("resizestart", function (ev, el) {
    var id = el.getAttribute("gs-id");
    if (id) { userSized[id] = true; clearTimeout(fitTimer[id]); }   // auto-fit stands down for this tile
  });
  grid.on("resizestop", function (ev, el) {
    var id = el.getAttribute("gs-id");
    if (id) userSized[id] = true;        // from now on this tile keeps its hand-set height
    writeSaved(snapshot());
  });

  /* ---------------------------------------------------------------- auto-fit */
  // Natural content height of a tile = the card's box with each scrolling
  // body expanded to its full scrollHeight. Applied only while the tile's
  // height has not been set by hand.
  function naturalHeight(el) {
    var card = el.querySelector(".grid-stack-item-content > .card");
    if (!card) return 0;
    // Measure with the card released from the tile's height (class toggles
    // height:auto and un-flexes the bodies), then restore. The tile is
    // absolutely positioned, so the momentary growth disturbs nothing else.
    card.classList.add("is-measuring");
    var h = card.offsetHeight;
    card.classList.remove("is-measuring");
    return h + GAP * 2;
  }
  function autoFit(el) {
    var id = el.getAttribute("gs-id");
    if (!id || userSized[id]) return;
    clearTimeout(fitTimer[id]);
    fitTimer[id] = setTimeout(function () {
      if (userSized[id]) return;         // re-check: a hand resize may have landed while this was pending
      var natural = naturalHeight(el);
      if (!natural) return;
      var d = DEFAULTS[id] || {};
      var h = Math.max(d.minH || 3, Math.min(d.fitH || 18, Math.ceil(natural / CELL)));
      var node = el.gridstackNode;
      if (node && node.h !== h) grid.update(el, { h: h });
    }, 120);
  }
  items.forEach(function (el) {
    autoFit(el);
    // Content arrives asynchronously (fetches, tab switches) — refit on change.
    var mo = new MutationObserver(function () { autoFit(el); });
    mo.observe(el, { childList: true, subtree: true, characterData: true });
    if (window.ResizeObserver) {
      var bodies = el.querySelectorAll(".grid-stack-item-content > .card > :not(.card-header)");
      var ro = new ResizeObserver(function () { autoFit(el); });
      bodies.forEach(function (b) { ro.observe(b); });
    }
  });

  /* ---------------------------------------------------------------- reset */
  // Forget the saved layout and reload: the page comes back on the defaults
  // with fresh auto-fit, which is simpler and more predictable than trying to
  // un-compact a live grid in place.
  var reset = document.getElementById("s360LayoutReset");
  if (reset) {
    reset.addEventListener("click", function () {
      clearSaved();
      grid.off("change");             // no re-save between clear and reload
      window.location.reload();
    });
  }

  window.S360_GRID = grid;
})();

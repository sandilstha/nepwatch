/**
 * workbench-ajax.js — turns each strategy tab's form into an in-place calc.
 *
 * Previously every "Execute" button did a full-page GET that re-ran the active
 * tab server-side AND re-rendered all 11 tabs. Here we intercept the submit,
 * POST the same query params to the calc endpoint (which returns ONLY that tab's
 * results partial), and swap the fragment into the tab's results container —
 * no full reload, the other tabs and the form's own widgets stay put.
 *
 * Safety: the forms keep method="GET" action="." so if this script is absent or
 * the fetch fails, the browser still performs the original full-page submit and
 * the user always gets their results.
 *
 * Ordering: dashboard.js binds its per-form validation/normalisation submit
 * listeners first (it is loaded first). This handler runs after and bails out if
 * a prior listener already called preventDefault (i.e. validation failed).
 */
(function () {
  'use strict';

  // Every strategy form that has a matching results container + calc tab.
  var FORM_IDS = [
    't3Form', 'emaForm', 'cciForm', 'rsiForm', 'sopForm', 'sopcForm', 'msvForm',
    'immForm', 'stageForm', 'supportResistanceForm', 'rrgForm', 'rrgIndicesForm',
  ];

  function calcUrl() {
    return (window.NEPSE_URLS && window.NEPSE_URLS.dashboardCalc) || '/workbench/calc/';
  }

  function resultsContainer(form) {
    // A pane can hold more than one independent desk (the RRG tab stacks the
    // company desk above the index desk), and querySelector would hand both
    // forms the FIRST container in the pane — so running the index RRG would
    // overwrite the company RRG's results. An explicit data-results wins.
    var explicit = form.getAttribute('data-results');
    if (explicit) return document.getElementById(explicit);
    var pane = form.closest('.tab-pane');
    return pane ? pane.querySelector('.tab-ajax-results') : null;
  }

  function nativeFallback(form, query) {
    // Reproduce the original full-page GET so results are never lost.
    var action = form.getAttribute('action');
    var base = (action && action !== '.') ? action : window.location.pathname;
    window.location.href = base + '?' + query;
  }

  function handleSubmit(event) {
    var form = event.currentTarget;
    // A prior listener (validation) already cancelled the submit — respect it.
    if (event.defaultPrevented) return;

    var container = resultsContainer(form);
    if (!container) return; // no AJAX target → let the native submit proceed

    var tabKey = container.getAttribute('data-tab-key');
    var query = new URLSearchParams(new FormData(form)).toString();

    event.preventDefault();

    container.setAttribute('aria-busy', 'true');
    container.classList.add('is-loading');
    container.innerHTML =
      '<div class="tab-ajax-loading text-muted text-center py-5">' +
      '<span class="spinner-border spinner-border-sm me-2" role="status" aria-hidden="true"></span>' +
      'Running calculation…</div>';

    fetch(calcUrl() + '?' + query, {
      method: 'GET',
      headers: { 'X-Requested-With': 'XMLHttpRequest' },
      credentials: 'same-origin',
    })
      .then(function (resp) {
        if (!resp.ok) throw new Error('HTTP ' + resp.status);
        return resp.text();
      })
      .then(function (html) {
        container.innerHTML = html;
        container.classList.remove('is-loading');
        container.removeAttribute('aria-busy');
        // Re-bind the dynamic widgets (charts, paginated tables, filters) that
        // dashboard.js wires up — they live in the freshly injected fragment.
        if (typeof window.WorkbenchReinit === 'function') {
          window.WorkbenchReinit(tabKey);
        }
        // Keep the URL shareable/bookmarkable without reloading.
        // Merge into the current URL rather than replace it: a pane can hold
        // two desks (RRG companies + RRG indices), and dropping the other
        // desk's params would reset it on reload/share.
        try {
          var merged = new URLSearchParams(window.location.search);
          var fresh = new URLSearchParams(query);
          var seen = {};
          fresh.forEach(function (value, key) {
            if (!seen[key]) { merged.delete(key); seen[key] = true; }
            merged.append(key, value);
          });
          window.history.replaceState(null, '', window.location.pathname + '?' + merged.toString());
        } catch (e) { /* history is non-critical */ }
      })
      .catch(function () {
        // Network / server error → fall back to a real navigation so the user
        // still gets their results the old way.
        container.classList.remove('is-loading');
        nativeFallback(form, query);
      });
  }

  function init() {
    FORM_IDS.forEach(function (id) {
      var form = document.getElementById(id);
      if (form) form.addEventListener('submit', handleSubmit);
    });
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', init, { once: true });
  } else {
    init();
  }
})();

/**
 * fundamentals-panel.js — collapsible workbench side panel that surfaces a
 * company's fundamentals for whichever desk (tab) is currently active.
 *
 * It never owns a symbol of its own: on open / tab-switch / symbol edit it reads
 * the active tab pane's `.autocomplete-input` and fetches that ticker's headline
 * metrics from `fundamental_data_api` (the same read-only FinancialStatement feed
 * the full /fundamentals/ desk uses). Collapsed by default; open state persists
 * in localStorage. Degrades silently if the panel markup is absent.
 */
(function () {
  'use strict';

  var panel = document.getElementById('fundaPanel');
  if (!panel) return;

  var handle    = document.getElementById('fundaHandle');
  var closeBtn  = document.getElementById('fundaClose');
  var refreshBtn = document.getElementById('fundaRefresh');
  var symbolEl  = document.getElementById('fundaSymbol');
  var nameEl    = document.getElementById('fundaName');
  var periodEl  = document.getElementById('fundaPeriod');
  var contentEl = document.getElementById('fundaContent');
  var moreLink  = document.getElementById('fundaMore');

  var API  = (window.NEPSE_URLS && window.NEPSE_URLS.fundamentalApi)  || '/fundamentals/api/';
  var PAGE = (window.NEPSE_URLS && window.NEPSE_URLS.fundamentalPage) || '/fundamentals/';

  // Headline rows, in display order: [label, canonical KS key, formatter].
  var METRICS = [
    ['Market Price',      'price',       'num'],
    ['EPS (annualized)',  'eps',         'num'],
    ['P/E',               'pe',          'num'],
    ['Book Value / Share', 'bvps',       'num'],
    ['ROE (TTM)',         'roe',         'pct'],
    ['ROA (TTM)',         'roa',         'pct'],
    ['Dividend / Share',  'dps',         'num'],
    ['Net Income',        'net_income',  'rs000'],
    ['Total Revenue',     'revenue',     'rs000']
  ];

  function fmtNum(v)  { return Number(v).toLocaleString(undefined, { maximumFractionDigits: 2 }); }
  function fmtPct(v)  { return (Number(v) * 100).toLocaleString(undefined, { maximumFractionDigits: 2 }) + '%'; }
  function fmtRs000(v) {           // amount stored in thousands of rupees
    var rs = Number(v) * 1000, a = Math.abs(rs);
    if (a >= 1e9) return 'Rs ' + (rs / 1e9).toFixed(2) + 'B';
    if (a >= 1e6) return 'Rs ' + (rs / 1e6).toFixed(2) + 'M';
    if (a >= 1e3) return 'Rs ' + (rs / 1e3).toFixed(1) + 'K';
    return 'Rs ' + fmtNum(rs);
  }
  function fmt(v, kind) {
    if (v === null || v === undefined) return '—';
    if (kind === 'pct')   return fmtPct(v);
    if (kind === 'rs000') return fmtRs000(v);
    return fmtNum(v);
  }

  var expanded  = false;
  var lastSymbol = null;   // symbol currently shown/targeted
  var lastFetched = null;  // symbol whose data is rendered (fetch de-dupe)
  var ctrl = null;         // in-flight fetch, so a fast tab-switch cancels it

  // The active desk's ticker: the visible autocomplete input in the shown pane.
  function activeSymbol() {
    var pane = document.querySelector('#workspaceTabsContent .tab-pane.show.active');
    if (!pane) return '';
    var input = pane.querySelector('.autocomplete-input');
    return input ? (input.value || '').trim().toUpperCase() : '';
  }

  function setMessage(msg) {
    // textContent: the message can echo the typed symbol, never trust it as HTML.
    contentEl.innerHTML = '<div class="funda-empty"></div>';
    contentEl.firstChild.textContent = msg;
  }

  function marginHtml(margin) {
    if (!margin) return '';
    if (margin.eligible) {
      var rate = (margin.rate !== null && margin.rate !== undefined)
        ? ' · up to ' + Number(margin.rate) + '% LTV' : '';
      var cat = margin.category ? ' · ' + margin.category : '';
      return '<div class="funda-margin is-eligible" title="Eligible for margin lending">' +
             '<span class="funda-margin-dot"></span>Margin eligible' + rate + cat + '</div>';
    }
    return '<div class="funda-margin is-not">' +
           '<span class="funda-margin-dot"></span>Not margin eligible</div>';
  }

  function render(data) {
    var ks = data.ks || {};
    symbolEl.textContent = data.symbol || '—';
    nameEl.textContent = (data.profile && data.profile.security_name) || '';
    periodEl.textContent = data.selected ? ('FY ' + data.selected.fy + ' · Q' + data.selected.quarter) : '';
    moreLink.setAttribute('href', PAGE + encodeURIComponent(data.symbol) + '/');

    var margin = marginHtml(data.margin);
    var rows = METRICS
      .filter(function (m) { return ks[m[1]] !== null && ks[m[1]] !== undefined; })
      .map(function (m) {
        return '<div class="funda-row"><span class="funda-k">' + m[0] +
               '</span><span class="funda-v">' + fmt(ks[m[1]], m[2]) + '</span></div>';
      }).join('');
    contentEl.innerHTML = margin + (rows || '<div class="funda-empty">No line items for this period.</div>');
  }

  function fetchFor(sym) {
    if (ctrl) ctrl.abort();
    ctrl = ('AbortController' in window) ? new AbortController() : null;
    contentEl.innerHTML = '<div class="funda-loading"><span class="spinner-border spinner-border-sm" role="status" aria-hidden="true"></span> Loading…</div>';
    fetch(API + '?symbol=' + encodeURIComponent(sym), {
      headers: { 'X-Requested-With': 'XMLHttpRequest' },
      credentials: 'same-origin',
      signal: ctrl ? ctrl.signal : undefined
    })
      .then(function (r) { return r.json().then(function (j) { return { ok: r.ok, body: j }; }); })
      .then(function (res) {
        if (res.ok && res.body && res.body.ok) {
          lastFetched = sym;
          render(res.body);
        } else {
          lastFetched = null;
          setMessage((res.body && res.body.error) || ('No fundamentals for ' + sym + '.'));
          symbolEl.textContent = sym;
          periodEl.textContent = '';
          moreLink.setAttribute('href', PAGE + encodeURIComponent(sym) + '/');
        }
      })
      .catch(function (err) {
        if (err && err.name === 'AbortError') return;
        lastFetched = null;
        setMessage('Could not load fundamentals.');
      });
  }

  // Reconcile the panel with the active desk's symbol. Only hits the network
  // when the panel is open (no point fetching behind a collapsed handle).
  function sync() {
    var sym = activeSymbol();
    lastSymbol = sym;
    if (!sym) {
      symbolEl.textContent = '—';
      nameEl.textContent = '';
      periodEl.textContent = '';
      moreLink.removeAttribute('href');
      setMessage('Pick a symbol in a desk to see its fundamentals.');
      lastFetched = null;
      return;
    }
    if (!expanded) return;
    if (sym === lastFetched) return;
    symbolEl.textContent = sym;
    fetchFor(sym);
  }

  function setExpanded(v) {
    expanded = v;
    panel.classList.toggle('is-collapsed', !v);
    handle.setAttribute('aria-expanded', v ? 'true' : 'false');
    try { localStorage.setItem('fundaPanelOpen', v ? '1' : '0'); } catch (e) { /* ignore */ }
    if (v) sync();
  }

  handle.addEventListener('click', function () { setExpanded(!expanded); });
  closeBtn.addEventListener('click', function () { setExpanded(false); });
  refreshBtn.addEventListener('click', function () { lastFetched = null; sync(); });

  // Re-sync on symbol edits and tab switches (debounced). The autocomplete sets
  // its input value programmatically on pick without always firing 'change', so
  // a light poll while open covers selections the events miss.
  var t = null;
  function debounced() { clearTimeout(t); t = setTimeout(sync, 250); }
  document.addEventListener('change', function (e) {
    if (e.target && e.target.classList && e.target.classList.contains('autocomplete-input')) debounced();
  }, true);
  document.querySelectorAll('#workspaceTabs [data-bs-target], #workspaceTabs [data-primary-section]')
    .forEach(function (b) { b.addEventListener('click', debounced); });

  var poll = null;
  function startPoll() {
    if (poll) return;
    poll = setInterval(function () {
      if (!expanded) return;
      var sym = activeSymbol();
      if (sym && sym !== lastFetched) sync();
    }, 1000);
  }

  // Restore persisted open state.
  var open = false;
  try { open = localStorage.getItem('fundaPanelOpen') === '1'; } catch (e) { /* ignore */ }
  setExpanded(open);
  startPoll();
})();

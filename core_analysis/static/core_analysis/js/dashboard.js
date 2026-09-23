  (function initWorkspaceNavigation() {
    const onReady = (callback) => {
      if (document.readyState === 'loading') {
        document.addEventListener('DOMContentLoaded', callback, { once: true });
      } else {
        callback();
      }
    };

    onReady(function() {
      const workspaceTabs = document.getElementById('workspaceTabs');
      if (!workspaceTabs || !window.bootstrap) return;

      const sectionDefaults = {
        inventory: '#inventory-pane',
        strategy: '#backtest-pane',
        advanced: '#msv-backtest-pane',
        rrg: '#rrg-backtest-pane',
      };

      const getActiveSectionTarget = (section) => {
        const activeChild = document.querySelector(`[data-section="${section}"].active[data-bs-target]`);
        return activeChild?.getAttribute('data-bs-target') || sectionDefaults[section];
      };

      const setPrimarySection = (section) => {
        workspaceTabs.classList.remove('nav-section-inventory', 'nav-section-strategy', 'nav-section-advanced', 'nav-section-rrg');
        workspaceTabs.classList.add(`nav-section-${section}`);

        document.querySelectorAll('[data-primary-section]').forEach((button) => {
          const isActive = button.dataset.primarySection === section;
          button.classList.toggle('active', isActive);
          if (!button.hasAttribute('data-bs-toggle')) {
            button.setAttribute('aria-selected', isActive ? 'true' : 'false');
          }
          button.setAttribute('aria-expanded', isActive ? 'true' : 'false');
        });
      };

      const showWorkspaceTab = (target) => {
        const button = document.querySelector(`[data-bs-target="${target}"]`);
        if (!button) return;
        const targetPane = document.querySelector(target);

        // Explicitly deactivate the current active pane & its button before
        // Bootstrap shows the new one. Bootstrap tracks "active" per-tablist
        // but when switching primary sections the outgoing pane may be in a
        // different CSS-hidden group, so Bootstrap won't deactivate it.
        const activePane = document.querySelector('#workspaceTabsContent .tab-pane.active');
        if (activePane && '#' + activePane.id !== target) {
          activePane.classList.remove('show', 'active');
          const oldBtn = document.querySelector('[data-bs-target="#' + activePane.id + '"]');
          if (oldBtn) {
            oldBtn.classList.remove('active');
            oldBtn.setAttribute('aria-selected', 'false');
          }
        }

        bootstrap.Tab.getOrCreateInstance(button).show();

        // Bootstrap's Tab.show() early-returns when the trigger already carries
        // the `active` class — which setPrimarySection() adds to the inventory
        // primary tab before this runs — leaving the pane hidden (blank screen
        // on re-click). Activate the target pane explicitly so it shows
        // regardless of Bootstrap's guard.
        if (targetPane && !targetPane.classList.contains('show')) {
          targetPane.classList.add('show', 'active');
          button.classList.add('active');
          button.setAttribute('aria-selected', 'true');
        }
      };

      // Apply custom styling classes for the new tab hierarchy.
      const primaryNavButtons = document.querySelectorAll('[data-primary-section]');
      if (primaryNavButtons.length > 0) {
        // Add container class to the parent element for proper spacing and border.
        primaryNavButtons[0].parentElement.classList.add('primary-nav-container');
      }
      document.querySelectorAll('[data-primary-section]').forEach((button) => {
        // Add the new class for main tab styling.
        button.classList.add('primary-nav-btn');

        button.addEventListener('click', () => {
          const section = button.dataset.primarySection;
          setPrimarySection(section);
          showWorkspaceTab(getActiveSectionTarget(section));
        });
      });

      document.querySelectorAll('[data-section]').forEach((button) => {
        button.addEventListener('click', () => {
          setPrimarySection(button.dataset.section);
        });
      });

      // Register the server-rendered active tab with Bootstrap so it correctly
      // tracks state for future tab switches (without re-triggering show/hide).
      const serverActiveBtn = document.querySelector(
        '#workspaceTabsContent .tab-pane.show.active'
      );
      if (serverActiveBtn) {
        const id = serverActiveBtn.id;
        const triggerBtn = document.querySelector('[data-bs-target="#' + id + '"]');
        if (triggerBtn) {
          bootstrap.Tab.getOrCreateInstance(triggerBtn);
        }
      }
    });
  })();

  /**
   * AUTOCOMPLETE SEARCH SYSTEM
   * Each strategy tab has its own autocomplete search input
   */
  class AutocompleteSearch {
    constructor(inputId, dropdownId, hiddenInputId, apiUrl, options = {}) {
      this.input = document.getElementById(inputId);
      this.dropdown = document.getElementById(dropdownId);
      this.hiddenInput = document.getElementById(hiddenInputId);
      this.apiUrl = apiUrl;
      this.debounceMs = options.debounceMs || 180;
      this.extraParams = options.extraParams || {};
      this.minChars = options.minChars === undefined ? 2 : options.minChars;
      this.emptySearch = options.emptySearch || false;
      this.showAllOnFocus = options.showAllOnFocus || false;
      this.hintText = options.hintText || 'Start typing to search for stocks and indices...';
      this.searchTimeout = null;
      this.abortController = null;
      this.resultCache = new Map();
      this.lastQuery = '';
      this.activeIndex = -1;
      this.results = [];
      this.metaEl = null;
      
      this.init();
    }
    
    init() {
      this.metaEl = document.createElement('div');
      this.metaEl.className = 'autocomplete-selected-meta';
      this.dropdown.parentNode.appendChild(this.metaEl);

      // Input events
      this.input.addEventListener('input', () => this.handleInput());
      this.input.addEventListener('focus', () => {
        if (this.showAllOnFocus && this.emptySearch) {
          clearTimeout(this.searchTimeout);
          this.searchTimeout = setTimeout(() => {
            this.performSearch('');
          }, this.debounceMs);
        } else {
          this.handleInput();
        }
      });
      this.input.addEventListener('keydown', (e) => this.handleKeydown(e));
      
      // Click outside to close
      document.addEventListener('click', (e) => {
        if (!this.input.contains(e.target) && !this.dropdown.contains(e.target)) {
          this.hideDropdown();
        }
      });
    }
    
    handleInput() {
      const query = this.input.value.trim();
      
      // Clear existing timeout
      clearTimeout(this.searchTimeout);
      
      // Keep results focused: require at least 2 characters.
      if (query.length === 0) {
        this.hiddenInput.value = '';
        if (this.emptySearch) {
          this.searchTimeout = setTimeout(() => {
            this.performSearch('');
          }, this.debounceMs);
        } else {
          this.showHint();
        }
        return;
      }
      this.hiddenInput.value = query.toUpperCase();
      this.metaEl.textContent = '';
      if (query.length < this.minChars) {
        this.showHint(`Type at least ${this.minChars} characters to search`);
        return;
      }
      
      // Debounce search
      this.searchTimeout = setTimeout(() => {
        this.performSearch(query);
      }, this.debounceMs);
    }
    
    async performSearch(query) {
      const normalizedQuery = query.trim();
      if (!normalizedQuery && !this.emptySearch) return;
      this.lastQuery = normalizedQuery;

      if (this.resultCache.has(normalizedQuery.toUpperCase())) {
        this.results = this.resultCache.get(normalizedQuery.toUpperCase());
        this.showResults();
        return;
      }

      try {
        this.showLoading();

        if (this.abortController) {
          this.abortController.abort();
        }
        this.abortController = new AbortController();
        
        const searchUrl = new URL(this.apiUrl, window.location.origin);
        searchUrl.searchParams.set('q', normalizedQuery);
        Object.entries(this.extraParams).forEach(([key, value]) => {
          searchUrl.searchParams.set(key, value);
        });

        const response = await fetch(searchUrl.toString(), {
          signal: this.abortController.signal,
        });
        const data = await response.json();
        if (this.lastQuery !== normalizedQuery) return;
        
        this.results = data.results || [];
        this.resultCache.set(normalizedQuery.toUpperCase(), this.results);
        this.showResults();
      } catch (error) {
        if (error.name === 'AbortError') return;
        console.error('Search error:', error);
        this.hideDropdown();
      }
    }
    
    showHint(message = this.hintText) {
      this.dropdown.innerHTML = `<div class="autocomplete-hint">${message}</div>`;
      this.dropdown.classList.add('show');
    }
    
    showLoading() {
      this.dropdown.innerHTML = '<div class="autocomplete-loading">Searching...</div>';
      this.dropdown.classList.add('show');
    }
    
    showResults() {
      if (this.results.length === 0) {
        this.dropdown.innerHTML = '<div class="autocomplete-hint">No matches found</div>';
        this.dropdown.classList.add('show');
        return;
      }

      const escapeHtml = (text) => String(text || '')
        .replace(/&/g, '&amp;')
        .replace(/</g, '&lt;')
        .replace(/>/g, '&gt;')
        .replace(/"/g, '&quot;')
        .replace(/'/g, '&#039;');
      const formatDisplayDate = (value) => {
        if (!value) return '';
        const text = String(value).slice(0, 10);
        return /^\d{4}-\d{2}-\d{2}$/.test(text) ? text : String(value);
      };

      this.dropdown.innerHTML = this.results.map((result, index) => {
        const isObject = result && typeof result === 'object';
        const value = isObject ? (result.value || '') : String(result || '');
        const label = isObject ? (result.label || value) : value;
        const type = isObject ? (result.type || '') : '';
        const latestClose = isObject ? result.latest_close : null;
        const latestDate = isObject ? result.latest_date : null;
        const margin = isObject ? result.margin : null;
        const mainText = escapeHtml(label);
        // Margin-eligibility pill (companies only). Shows the LTV rate if known.
        let marginBadge = '';
        if (margin && margin.eligible) {
          const rateText = (margin.rate !== null && margin.rate !== undefined)
            ? ` ${Number(margin.rate)}%` : ' ✓';
          const tip = 'Margin eligible' + ((margin.rate !== null && margin.rate !== undefined)
            ? ` · up to ${Number(margin.rate)}% loan-to-value` : '');
          marginBadge = `<span class="autocomplete-margin-badge" title="${tip}">Margin${rateText}</span>`;
        }
        const subParts = [];
        if (type) {
          subParts.push(type);
        }
        if (latestClose !== null && latestClose !== undefined) {
          const priceText = Number(latestClose).toFixed(2);
          const dateText = latestDate ? ` (${formatDisplayDate(latestDate)})` : '';
          subParts.push(`Latest: NPR ${priceText}${dateText}`);
        }
        const subText = subParts.length ? `<div class="autocomplete-item-sub">${escapeHtml(subParts.join(' | '))}</div>` : '';

        return `<div class="autocomplete-item" data-index="${index}">
                  <div class="autocomplete-item-main">${mainText}${marginBadge}</div>
                  ${subText}
                </div>`;
      }).join('');
      
      // Add click handlers
      this.dropdown.querySelectorAll('.autocomplete-item').forEach(item => {
        item.addEventListener('click', () => {
          this.selectItem(parseInt(item.dataset.index, 10));
        });
      });
      
      this.dropdown.classList.add('show');
      this.activeIndex = -1;
    }
    
    hideDropdown() {
      this.dropdown.classList.remove('show');
      this.activeIndex = -1;
    }
    
    selectItem(indexOrValue) {
      const selected = Number.isInteger(indexOrValue) ? this.results[indexOrValue] : null;
      const isObject = selected && typeof selected === 'object';
      const value = isObject ? (selected.value || '') : String(indexOrValue || '');
      const latestClose = isObject ? selected.latest_close : null;
      const latestDate = isObject ? selected.latest_date : null;
      const formatDisplayDate = (value) => {
        if (!value) return '';
        const text = String(value).slice(0, 10);
        return /^\d{4}-\d{2}-\d{2}$/.test(text) ? text : String(value);
      };

      this.input.value = value;
      this.hiddenInput.value = value;
      if (latestClose !== null && latestClose !== undefined) {
        const datePart = latestDate ? ` on ${formatDisplayDate(latestDate)}` : '';
        this.metaEl.textContent = `Latest close: NPR ${Number(latestClose).toFixed(2)}${datePart}`;
      } else {
        this.metaEl.textContent = '';
      }
      this.hideDropdown();
    }
    
    handleKeydown(e) {
      const items = this.dropdown.querySelectorAll('.autocomplete-item');
      
      if (e.key === 'ArrowDown') {
        e.preventDefault();
        this.activeIndex = Math.min(this.activeIndex + 1, items.length - 1);
        this.updateActiveItem(items);
      } else if (e.key === 'ArrowUp') {
        e.preventDefault();
        this.activeIndex = Math.max(this.activeIndex - 1, -1);
        this.updateActiveItem(items);
      } else if (e.key === 'Enter') {
        e.preventDefault();
        if (this.activeIndex >= 0 && items[this.activeIndex]) {
          this.selectItem(this.activeIndex);
        } else if (this.input.value.trim()) {
          // Use current input value
          this.hiddenInput.value = this.input.value.trim().toUpperCase();
          this.input.value = this.input.value.trim().toUpperCase();
          this.metaEl.textContent = '';
          this.hideDropdown();
        }
      } else if (e.key === 'Escape') {
        this.hideDropdown();
      }
    }
    
    updateActiveItem(items) {
      items.forEach((item, index) => {
        if (index === this.activeIndex) {
          item.classList.add('active');
          item.scrollIntoView({ block: 'nearest' });
        } else {
          item.classList.remove('active');
        }
      });
    }
  }
  
  // Initialize autocomplete for each tab
  document.addEventListener('DOMContentLoaded', function() {
    // T3MA tab
    new AutocompleteSearch(
      't3SearchInput',
      't3Dropdown',
      't3SymbolHidden',
      window.NEPSE_URLS.symbolAutocomplete
    );
    
    // EMA tab
    new AutocompleteSearch(
      'emaSearchInput',
      'emaDropdown',
      'emaSymbolHidden',
      window.NEPSE_URLS.symbolAutocomplete
    );
    
    // CCI tab
    new AutocompleteSearch(
      'cciSearchInput',
      'cciDropdown',
      'cciSymbolHidden',
      window.NEPSE_URLS.symbolAutocomplete
    );
    
    // RSI tab
    new AutocompleteSearch(
      'rsiSearchInput',
      'rsiDropdown',
      'rsiSymbolHidden',
      window.NEPSE_URLS.symbolAutocomplete
    );

    // SOP Strategy tab
    new AutocompleteSearch(
      'sopSearchInput',
      'sopDropdown',
      'sopSymbolHidden',
      window.NEPSE_URLS.symbolAutocomplete
    );

    // SOP Combined tab
    new AutocompleteSearch(
      'sopcSearchInput',
      'sopcDropdown',
      'sopcSymbolHidden',
      window.NEPSE_URLS.symbolAutocomplete
    );

    // MSV tab
    new AutocompleteSearch(
      'msvSearchInput',
      'msvDropdown',
      'msvSymbolHidden',
      window.NEPSE_URLS.symbolAutocomplete
    );

    // IMM tab
    new AutocompleteSearch(
      'immSearchInput',
      'immDropdown',
      'immSymbolHidden',
      window.NEPSE_URLS.symbolAutocomplete
    );

    // Stage Analysis tab
    new AutocompleteSearch(
      'stageSearchInput',
      'stageDropdown',
      'stageSymbolHidden',
      window.NEPSE_URLS.symbolAutocomplete
    );

    // Support & Resistance tab
    new AutocompleteSearch(
      'supportResistanceSearchInput',
      'supportResistanceDropdown',
      'supportResistanceSymbolHidden',
      window.NEPSE_URLS.symbolAutocomplete
    );

    // RRG tab
    new AutocompleteSearch(
      'rrgSearchInput',
      'rrgDropdown',
      'rrgSymbolHidden',
      window.NEPSE_URLS.symbolAutocomplete,
      { debounceMs: 120, extraParams: { fast: '1' } }
    );

    new AutocompleteSearch(
      'rrgIndicesBenchmarkSearchInput',
      'rrgIndicesBenchmarkDropdown',
      'rrgIndicesBenchmarkHidden',
      window.NEPSE_URLS.symbolAutocomplete,
      {
        debounceMs: 80,
        minChars: 0,
        emptySearch: true,
        showAllOnFocus: true,
        extraParams: { indices_only: '1', all: '1' },
        hintText: 'Search NEPSE indices only...'
      }
    );

    const rrgForm = document.getElementById('rrgForm');
    if (rrgForm) {
      rrgForm.addEventListener('submit', (e) => {
        const rrgSearchInput = document.getElementById('rrgSearchInput');
        const rrgSymbolHidden = document.getElementById('rrgSymbolHidden');
        if (rrgSearchInput && rrgSymbolHidden) {
          const typedSymbol = rrgSearchInput.value.trim().toUpperCase();
          rrgSearchInput.value = typedSymbol;
          rrgSymbolHidden.value = typedSymbol;
          if (!typedSymbol) {
            // Submitting with no symbol would swap in an empty results pane.
            e.preventDefault();
            rrgSearchInput.focus();
            rrgSearchInput.setCustomValidity('Pick a symbol first.');
            rrgSearchInput.reportValidity();
            rrgSearchInput.addEventListener('input', () => rrgSearchInput.setCustomValidity(''), { once: true });
          }
        }
      });
    }

    const setupRrgIndicesMultiSelect = () => {
      const grid = document.getElementById('rrgIndicesChoiceGrid');
      const countEl = document.getElementById('rrgIndicesSelectedCount');
      const selectAllBtn = document.getElementById('rrgIndicesSelectAllBtn');
      const clearBtn = document.getElementById('rrgIndicesClearBtn');
      if (!grid || !countEl || !selectAllBtn || !clearBtn) return;

      const checkboxes = Array.from(grid.querySelectorAll('.rrg-index-checkbox'));
      const refresh = () => {
        const selectedCount = checkboxes.filter((checkbox) => checkbox.checked).length;
        countEl.textContent = `${selectedCount} selected`;
        checkboxes.forEach((checkbox) => {
          const label = checkbox.closest('.rrg-index-choice');
          if (label) label.classList.toggle('is-selected', checkbox.checked);
        });
      };

      selectAllBtn.addEventListener('click', () => {
        checkboxes.forEach((checkbox) => { checkbox.checked = true; });
        refresh();
      });
      clearBtn.addEventListener('click', () => {
        checkboxes.forEach((checkbox) => { checkbox.checked = false; });
        refresh();
      });
      checkboxes.forEach((checkbox) => {
        checkbox.addEventListener('change', refresh);
      });
      refresh();
    };

    setupRrgIndicesMultiSelect();

    const rrgIndicesForm = document.getElementById('rrgIndicesForm');
    if (rrgIndicesForm) {
      rrgIndicesForm.addEventListener('submit', (e) => {
        const searchInput = document.getElementById('rrgIndicesBenchmarkSearchInput');
        const hiddenInput = document.getElementById('rrgIndicesBenchmarkHidden');
        if (searchInput && hiddenInput) {
          const typedIndex = searchInput.value.trim().toUpperCase();
          if (typedIndex) {
            searchInput.value = typedIndex;
            hiddenInput.value = typedIndex;
          }
        }
        const selectedIndices = rrgIndicesForm.querySelectorAll('.rrg-index-checkbox:checked');
        if (!selectedIndices.length) {
          e.preventDefault();
          alert('Select at least one NEPSE index to plot.');
        }
      });
    }

    const formatDate = (d) => {
      const year = d.getFullYear();
      const month = String(d.getMonth() + 1).padStart(2, '0');
      const day = String(d.getDate()).padStart(2, '0');
      return `${year}-${month}-${day}`;
    };
    const isTradingDay = (d) => d.getDay() !== 6 && d.getDay() !== 0; // NEPSE trades Mon-Fri; closed Sat (6) & Sun (0)
    const normalizeToTradingDay = (d, reverse = false) => {
      const cursor = new Date(d);
      while (!isTradingDay(cursor)) {
        cursor.setDate(cursor.getDate() + (reverse ? -1 : 1));
      }
      return cursor;
    };

    const setupFlatpickrRange = (fromId, toId, opts = {}) => {
      if (!window.flatpickr) return;
      const fromInput = document.getElementById(fromId);
      const toInput = document.getElementById(toId);
      if (!fromInput || !toInput) return;

      const today = normalizeToTradingDay(new Date(), true);
      const todayStr = formatDate(today);
      if (!toInput.value) toInput.value = todayStr;
      if (!fromInput.value) {
        if (opts.defaultMonthRange) {
          // Sync bars: default to the last month (From = one month before the
          // latest trading day) so a routine sync covers recent gaps without
          // pulling a whole year.
          const oneMonthAgo = new Date(today);
          oneMonthAgo.setMonth(oneMonthAgo.getMonth() - 1);
          fromInput.value = formatDate(normalizeToTradingDay(oneMonthAgo));
        } else {
          const oneYearAgo = new Date(today);
          oneYearAgo.setFullYear(oneYearAgo.getFullYear() - 1);
          fromInput.value = formatDate(normalizeToTradingDay(oneYearAgo));
        }
      }

      let fromPicker;
      let toPicker;
      const syncBounds = () => {
        if (toPicker) {
          toPicker.set('maxDate', todayStr);
          toPicker.set('minDate', fromInput.value || null);
        }
        if (fromPicker) {
          fromPicker.set('maxDate', toInput.value || todayStr);
        }
        if (fromInput.value && toInput.value && fromInput.value > toInput.value) {
          fromInput.value = toInput.value;
          if (fromPicker) fromPicker.setDate(fromInput.value, false);
        }
      };

      toPicker = flatpickr(toInput, {
        dateFormat: 'Y-m-d',
        defaultDate: toInput.value || todayStr,
        maxDate: todayStr,
        enable: [isTradingDay],
        allowInput: false,
        onChange: function(selectedDates) {
          if (!selectedDates.length) return;
          const selectedTo = formatDate(selectedDates[0]);
          if (fromPicker) {
            fromPicker.set('maxDate', selectedTo);
            if (fromInput.value && fromInput.value > selectedTo) {
              fromInput.value = selectedTo;
              fromPicker.setDate(selectedTo, false);
            }
          }
          syncBounds();
        }
      });

      fromPicker = flatpickr(fromInput, {
        dateFormat: 'Y-m-d',
        defaultDate: fromInput.value || undefined,
        maxDate: toInput.value || todayStr,
        enable: [isTradingDay],
        allowInput: false,
        onChange: function(selectedDates) {
          if (!selectedDates.length) return;
          const selectedFrom = formatDate(selectedDates[0]);
          if (toPicker) {
            toPicker.set('minDate', selectedFrom);
            if (toInput.value && toInput.value < selectedFrom) {
              toInput.value = selectedFrom;
              toPicker.setDate(selectedFrom, false);
            }
          }
          syncBounds();
        }
      });

      syncBounds();
    };

    const setupFlatpickrSingle = (inputId) => {
      if (!window.flatpickr) return;
      const input = document.getElementById(inputId);
      if (!input) return;
      const today = normalizeToTradingDay(new Date(), true);
      const todayStr = formatDate(today);
      if (!input.value) input.value = todayStr;
      flatpickr(input, {
        dateFormat: 'Y-m-d',
        defaultDate: input.value || todayStr,
        maxDate: todayStr,
        enable: [isTradingDay],
        allowInput: false,
      });
    };

    const setDateInputValue = (input, value) => {
      input.value = value;
      if (input._flatpickr) {
        input._flatpickr.setDate(value, false);
      }
      input.dispatchEvent(new Event('change', { bubbles: true }));
    };

    const setupGenericQuickRanges = () => {
      const today = normalizeToTradingDay(new Date(), true);
      const todayStr = formatDate(today);

      document.querySelectorAll('[data-date-range][data-from-id][data-to-id]').forEach((button) => {
        button.addEventListener('click', () => {
          const fromInput = document.getElementById(button.dataset.fromId);
          const toInput = document.getElementById(button.dataset.toId);
          if (!fromInput || !toInput) return;

          const start = new Date(today);
          const rangeKey = button.dataset.dateRange;
          if (rangeKey === '1m') {
            start.setMonth(start.getMonth() - 1);
          } else if (rangeKey === '3m') {
            start.setMonth(start.getMonth() - 3);
          } else if (rangeKey === '6m') {
            start.setMonth(start.getMonth() - 6);
          } else if (rangeKey === '1y') {
            start.setFullYear(start.getFullYear() - 1);
          } else if (rangeKey === '2y') {
            start.setFullYear(start.getFullYear() - 2);
          } else if (rangeKey === '3y') {
            start.setFullYear(start.getFullYear() - 3);
          } else if (rangeKey === 'ytd') {
            start.setFullYear(today.getFullYear(), 0, 1);
          }

          setDateInputValue(fromInput, formatDate(normalizeToTradingDay(start)));
          setDateInputValue(toInput, todayStr);
        });
      });
    };

    const getTableDataRows = (table) => {
      const tbody = table.querySelector('tbody');
      if (!tbody) return [];
      return Array.from(tbody.querySelectorAll('tr')).filter((row) => {
        const cells = Array.from(row.querySelectorAll('td'));
        return cells.length && !cells.some((cell) => cell.colSpan > 1);
      });
    };

    const setRowVisibility = (row) => {
      const hiddenByFilter = row.dataset.tableFilterHidden === '1';
      const hiddenByPage = row.dataset.tablePageHidden === '1';
      row.style.display = hiddenByFilter || hiddenByPage ? 'none' : '';
    };

    const applyTablePagination = (table) => {
      const state = table._paginationState;
      if (!state) return;

      const visibleRows = state.rows.filter((row) => row.dataset.tableFilterHidden !== '1');
      const totalPages = Math.max(1, Math.ceil(visibleRows.length / state.pageSize));
      state.currentPage = Math.min(Math.max(1, state.currentPage), totalPages);

      const start = (state.currentPage - 1) * state.pageSize;
      const end = start + state.pageSize;
      state.rows.forEach((row) => {
        const visibleIndex = visibleRows.indexOf(row);
        row.dataset.tablePageHidden = visibleIndex >= start && visibleIndex < end ? '0' : '1';
        setRowVisibility(row);
      });

      if (state.status) {
        const from = visibleRows.length ? start + 1 : 0;
        const to = Math.min(end, visibleRows.length);
        state.status.textContent = `${from}-${to} of ${visibleRows.length}`;
      }
      if (state.prevBtn) state.prevBtn.disabled = state.currentPage <= 1;
      if (state.nextBtn) state.nextBtn.disabled = state.currentPage >= totalPages;
    };
    window.applyTablePagination = applyTablePagination;

    const setupTablePagination = () => {
      document.querySelectorAll('table.minimal-table').forEach((table, index) => {
        if (table.dataset.paginationReady === '1') return;

        const rows = getTableDataRows(table);
        const defaultPageSize = parseInt(table.dataset.defaultPageSize || '5', 10) || 5;
        if (rows.length <= defaultPageSize) return;

        const toolbar = document.createElement('div');
        toolbar.className = 'table-page-toolbar';
        toolbar.innerHTML = `
          <label>Items Per Page
            <select class="table-page-size" aria-label="Items per page">
              <option value="5">5</option>
              <option value="10">10</option>
              <option value="20">20</option>
              <option value="50">50</option>
              <option value="200">200</option>
              <option value="300">300</option>
              <option value="500">500</option>
            </select>
          </label>
          <button type="button" class="table-page-filter">Filter</button>
          <button type="button" class="table-page-reset">Reset</button>
          <button type="button" class="table-page-nav" data-page-action="prev">Prev</button>
          <span class="table-page-status" aria-live="polite"></span>
          <button type="button" class="table-page-nav" data-page-action="next">Next</button>
        `;

        const tableWrap = table.closest('.imm-scroll-wrap') || table;
        tableWrap.parentNode.insertBefore(toolbar, tableWrap);

        const pageSizeSelect = toolbar.querySelector('.table-page-size');
        const filterBtn = toolbar.querySelector('.table-page-filter');
        const resetBtn = toolbar.querySelector('.table-page-reset');
        const prevBtn = toolbar.querySelector('[data-page-action="prev"]');
        const nextBtn = toolbar.querySelector('[data-page-action="next"]');
        const status = toolbar.querySelector('.table-page-status');

        table.dataset.paginationReady = '1';
        table.dataset.paginationIndex = String(index);
        table._paginationState = {
          rows,
          pageSize: defaultPageSize,
          currentPage: 1,
          pageSizeSelect,
          prevBtn,
          nextBtn,
          status,
        };
        pageSizeSelect.value = String(defaultPageSize);

        filterBtn.addEventListener('click', () => {
          table._paginationState.pageSize = parseInt(pageSizeSelect.value, 10) || defaultPageSize;
          table._paginationState.currentPage = 1;
          applyTablePagination(table);
        });
        resetBtn.addEventListener('click', () => {
          pageSizeSelect.value = String(defaultPageSize);
          table._paginationState.pageSize = defaultPageSize;
          table._paginationState.currentPage = 1;
          rows.forEach((row) => {
            row.dataset.tableFilterHidden = row.dataset.tableFilterHidden || '0';
          });
          applyTablePagination(table);
        });
        prevBtn.addEventListener('click', () => {
          table._paginationState.currentPage -= 1;
          applyTablePagination(table);
        });
        nextBtn.addEventListener('click', () => {
          table._paginationState.currentPage += 1;
          applyTablePagination(table);
        });

        applyTablePagination(table);
      });
    };

    const setupBottomTableFilters = (config) => {
      const table = document.getElementById(config.tableId);
      if (!table) return;
      const tbody = table.querySelector('tbody');
      if (!tbody) return;
      const rows = Array.from(tbody.querySelectorAll('tr')).filter((row) => row.querySelector('td'));
      if (!rows.length) return;

      const els = {
        dateFrom: document.getElementById(config.dateFromId),
        dateTo: document.getElementById(config.dateToId),
        closeMin: document.getElementById(config.closeMinId),
        closeMax: document.getElementById(config.closeMaxId),
        scoreMin: document.getElementById(config.scoreMinId),
        scoreMax: document.getElementById(config.scoreMaxId),
      };
      if (Object.values(els).some((el) => !el)) return;

      const parseNum = (v) => {
        const n = parseFloat(v);
        return Number.isFinite(n) ? n : null;
      };
      const normDate = (v) => (v || '').trim();

      const apply = () => {
        const dateFrom = normDate(els.dateFrom.value);
        const dateTo = normDate(els.dateTo.value);
        const closeMin = parseNum(els.closeMin.value);
        const closeMax = parseNum(els.closeMax.value);
        const scoreMin = parseNum(els.scoreMin.value);
        const scoreMax = parseNum(els.scoreMax.value);

        rows.forEach((row) => {
          const rowDate = row.dataset.date || '';
          const rowClose = parseFloat(row.dataset.close || 'NaN');
          const rowScore = parseFloat(row.dataset.score || 'NaN');

          let visible = true;
          if (dateFrom && rowDate < dateFrom) visible = false;
          if (dateTo && rowDate > dateTo) visible = false;
          if (closeMin !== null && !(rowClose >= closeMin)) visible = false;
          if (closeMax !== null && !(rowClose <= closeMax)) visible = false;
          if (scoreMin !== null && !(rowScore >= scoreMin)) visible = false;
          if (scoreMax !== null && !(rowScore <= scoreMax)) visible = false;

          row.dataset.tableFilterHidden = visible ? '0' : '1';
          row.dataset.tablePageHidden = row.dataset.tablePageHidden || '0';
          setRowVisibility(row);
        });
        if (table._paginationState) {
          table._paginationState.currentPage = 1;
          applyTablePagination(table);
        }
      };

      const bindEvent = (el) => {
        el.addEventListener('input', apply);
        el.addEventListener('change', apply);
      };
      Object.values(els).forEach(bindEvent);
      apply();
    };

    setupFlatpickrSingle('inventoryBusinessDate');
    // Sync Price defaults to a single latest trading day (both From and To set
    // to the current trading day), same as Sync Floorsheet below.
    setupFlatpickrRange('headerSyncFromDate', 'headerSyncToDate');
    (function () {
      const syncTo = document.getElementById('headerSyncToDate');
      const syncFrom = document.getElementById('headerSyncFromDate');
      if (syncTo && syncFrom && syncTo.value) setDateInputValue(syncFrom, syncTo.value);
    })();
    setupFlatpickrRange('headerCalcFromDate', 'headerCalcToDate', { defaultMonthRange: true });
    // Floorsheet defaults to a single latest trading day: both From and To are
    // set to the current (latest) trading day. The user can widen it as needed.
    setupFlatpickrRange('headerFloorFromDate', 'headerFloorToDate');
    (function () {
      const floorTo = document.getElementById('headerFloorToDate');
      const floorFrom = document.getElementById('headerFloorFromDate');
      if (floorTo && floorFrom && floorTo.value) setDateInputValue(floorFrom, floorTo.value);
    })();
    setupFlatpickrRange('t3FromDate', 't3ToDate', { defaultMonthRange: true });
    setupFlatpickrRange('emaFromDate', 'emaToDate', { defaultMonthRange: true });
    setupFlatpickrRange('cciFromDate', 'cciToDate', { defaultMonthRange: true });
    setupFlatpickrRange('rsiFromDate', 'rsiToDate', { defaultMonthRange: true });
    setupFlatpickrRange('msvFromDate', 'msvToDate', { defaultMonthRange: true });
    // SOP tabs need a long lookback (indicators want up to ~90 bars); default to
    // the 1-year range, not one month, so a fresh load isn't "insufficient data".
    setupFlatpickrRange('sopFromDate', 'sopToDate');
    setupFlatpickrRange('sopcFromDate', 'sopcToDate');
    setupFlatpickrRange('supportResistanceFromDate', 'supportResistanceToDate');
    setupFlatpickrRange('rrgFromDate', 'rrgToDate');
    setupFlatpickrRange('rrgIndicesFromDate', 'rrgIndicesToDate');
    setupGenericQuickRanges();
    setupTablePagination();
    // Named so the AJAX re-init hook (window.WorkbenchReinit) can rebind these
    // filters after a tab's results partial is swapped in without a full reload.
    const IMM_FILTER_CONFIG = {
      tableId: 'immScoringTable',
      dateFromId: 'immFilterDateFrom',
      dateToId: 'immFilterDateTo',
      closeMinId: 'immFilterCloseMin',
      closeMaxId: 'immFilterCloseMax',
      scoreMinId: 'immFilterScoreMin',
      scoreMaxId: 'immFilterScoreMax',
    };
    const STAGE_FILTER_CONFIG = {
      tableId: 'stageOutputTable',
      dateFromId: 'stageFilterDateFrom',
      dateToId: 'stageFilterDateTo',
      closeMinId: 'stageFilterCloseMin',
      closeMaxId: 'stageFilterCloseMax',
      scoreMinId: 'stageFilterScoreMin',
      scoreMaxId: 'stageFilterScoreMax',
    };
    setupBottomTableFilters(IMM_FILTER_CONFIG);
    setupBottomTableFilters(STAGE_FILTER_CONFIG);

    const msvForm = document.getElementById('msvForm');
    if (msvForm) {
      msvForm.addEventListener('submit', function(e) {
        const symbolInput = document.getElementById('msvSearchInput');
        const symbolHidden = document.getElementById('msvSymbolHidden');
        const symbol = ((symbolHidden && symbolHidden.value) || (symbolInput && symbolInput.value) || '').trim().toUpperCase();
        if (symbolInput) symbolInput.value = symbol;
        if (symbolHidden) symbolHidden.value = symbol;
        const getNum = (name) => parseFloat(msvForm.querySelector(`[name="${name}"]`).value || '0');
        const fast = getNum('msv_macd_fast');
        const slow = getNum('msv_macd_slow');
        const signal = getNum('msv_macd_signal');
        const atrLen = getNum('msv_atr_length');
        const atrMult = getNum('msv_atr_multiplier');
        const rvolPeriod = getNum('msv_rvol_period');
        const rvolTh = getNum('msv_rvol_threshold');
        const stLen = getNum('msv_supertrend_length');
        const stMult = getNum('msv_supertrend_multiplier');
        const fromDate = msvForm.querySelector('[name="msv_from_date"]').value;
        const toDate = msvForm.querySelector('[name="msv_to_date"]').value;

        const issues = [];
        if (!symbol) issues.push('Target asset is required.');
        if (!fromDate || !toDate) issues.push('From/To date is required.');
        if (fast >= slow) issues.push('MACD Fast must be smaller than MACD Slow.');
        if (signal < 1 || atrLen < 2 || rvolPeriod < 2 || stLen < 2) issues.push('Lookback periods are too small.');
        if (atrMult <= 0 || rvolTh <= 0 || stMult <= 0) issues.push('Multipliers and thresholds must be greater than zero.');

        if (issues.length) {
          e.preventDefault();
          alert('Validation warnings:\\n- ' + issues.join('\\n- '));
        }
      });
    }

    const immForm = document.getElementById('immForm');
    if (immForm) {
      const fromInput = document.getElementById('immFromDate');
      const toInput = document.getElementById('immToDate');

      const formatDate = (d) => {
        const year = d.getFullYear();
        const month = String(d.getMonth() + 1).padStart(2, '0');
        const day = String(d.getDate()).padStart(2, '0');
        return `${year}-${month}-${day}`;
      };
      const isTradingDay = (d) => d.getDay() !== 6 && d.getDay() !== 0; // NEPSE trades Mon-Fri; closed Sat (6) & Sun (0)
      const normalizeToTradingDay = (d, reverse = false) => {
        const cursor = new Date(d);
        while (!isTradingDay(cursor)) {
          cursor.setDate(cursor.getDate() + (reverse ? -1 : 1));
        }
        return cursor;
      };

      const today = new Date();
      const todayTrading = normalizeToTradingDay(today, true);
      const todayStr = formatDate(todayTrading);

      if (!toInput.value) {
        toInput.value = todayStr;
      }
      if (!fromInput.value) {
        const oneYearAgo = new Date(todayTrading);
        oneYearAgo.setFullYear(oneYearAgo.getFullYear() - 1);
        fromInput.value = formatDate(normalizeToTradingDay(oneYearAgo));
      }

      let immFromPicker;
      let immToPicker;

      const syncDateBounds = () => {
        if (immToPicker) {
          immToPicker.set('maxDate', todayStr);
          immToPicker.set('minDate', fromInput.value || null);
        }
        if (immFromPicker) {
          immFromPicker.set('maxDate', toInput.value || todayStr);
        }
        if (toInput.value && fromInput.value) {
          if (fromInput.value > toInput.value) {
            fromInput.value = toInput.value;
            if (immFromPicker) immFromPicker.setDate(fromInput.value, false);
          }
        }
      };
      
      if (window.flatpickr) {
        // Descending flow: user picks latest "To Date" first, then "From Date" constrained to <= To Date.
        immToPicker = flatpickr(toInput, {
          dateFormat: 'Y-m-d',
          defaultDate: toInput.value || todayStr,
          maxDate: todayStr,
          enable: [isTradingDay],
          allowInput: false,
          onChange: function(selectedDates) {
            if (!selectedDates.length) return;
            const selectedTo = formatDate(selectedDates[0]);
            if (immFromPicker) {
              immFromPicker.set('maxDate', selectedTo);
              if (fromInput.value && fromInput.value > selectedTo) {
                fromInput.value = selectedTo;
                immFromPicker.setDate(selectedTo, false);
              }
            }
            syncDateBounds();
          }
        });

        immFromPicker = flatpickr(fromInput, {
          dateFormat: 'Y-m-d',
          defaultDate: fromInput.value || undefined,
          maxDate: toInput.value || todayStr,
          enable: [isTradingDay],
          allowInput: false,
          onChange: function(selectedDates) {
            if (!selectedDates.length) return;
            const selectedFrom = formatDate(selectedDates[0]);
            if (immToPicker) {
              immToPicker.set('minDate', selectedFrom);
              if (toInput.value && toInput.value < selectedFrom) {
                toInput.value = selectedFrom;
                immToPicker.setDate(selectedFrom, false);
              }
            }
            syncDateBounds();
          }
        });
      } else {
        // Fallback when flatpickr is unavailable.
        toInput.max = todayStr;
        fromInput.max = toInput.value || todayStr;
        toInput.min = fromInput.value || '';
        fromInput.addEventListener('change', syncDateBounds);
        toInput.addEventListener('change', syncDateBounds);
      }

      const setImmRange = (rangeKey) => {
        const end = new Date(todayTrading);
        let start = new Date(todayTrading);

        if (rangeKey === 'latest') {
          start = new Date(todayTrading);
        } else if (rangeKey === '1m') {
          start.setMonth(start.getMonth() - 1);
        } else if (rangeKey === '3m') {
          start.setMonth(start.getMonth() - 3);
        } else if (rangeKey === '6m') {
          start.setMonth(start.getMonth() - 6);
        } else if (rangeKey === '1y') {
          start.setFullYear(start.getFullYear() - 1);
        } else if (rangeKey === 'ytd') {
          start = new Date(today.getFullYear(), 0, 1);
        }

        const normalizedStart = normalizeToTradingDay(start);
        const normalizedEnd = normalizeToTradingDay(end, true);
        fromInput.value = formatDate(normalizedStart);
        toInput.value = formatDate(normalizedEnd);
        if (immFromPicker) immFromPicker.setDate(fromInput.value, false);
        if (immToPicker) immToPicker.setDate(toInput.value, false);
        syncDateBounds();
      };

      immForm.querySelectorAll('[data-imm-range]').forEach((btn) => {
        btn.addEventListener('click', () => setImmRange(btn.dataset.immRange));
      });

      immForm.addEventListener('submit', function(e) {
        const getNum = (name) => parseFloat(immForm.querySelector(`[name="${name}"]`).value || '0');
        const fast = getNum('imm_macd_fast');
        const slow = getNum('imm_macd_slow');
        const rsLookback = getNum('imm_rs_lookback');
        const atrLen = getNum('imm_atr_length');
        const rsiLen = getNum('imm_rsi_length');
        const stLen = getNum('imm_supertrend_length');
        const stMult = getNum('imm_supertrend_multiplier');
        const fromDate = immForm.querySelector('[name="imm_from_date"]').value;
        const toDate = immForm.querySelector('[name="imm_to_date"]').value;

        const issues = [];
        if (!fromDate || !toDate) issues.push('From/To date is required.');
        if (fast >= slow) issues.push('MACD Fast must be smaller than MACD Slow.');
        if (rsLookback < 2 || atrLen < 2 || rsiLen < 2 || stLen < 2) issues.push('Lookback periods are too small.');
        if (stMult <= 0) issues.push('Supertrend multiplier must be greater than zero.');

        if (issues.length) {
          e.preventDefault();
          alert('Validation warnings:\\n- ' + issues.join('\\n- '));
        }
      });
    }
    
    // Stage Analysis date pickers — IMM-quality: trading-day filter + quick ranges
    const stageFromInput = document.getElementById('stageFromDate');
    const stageToInput   = document.getElementById('stageToDate');
    const stageForm      = document.getElementById('stageForm');
    if (stageFromInput && stageToInput && stageForm) {
      const fmtDate = (d) => {
        const y = d.getFullYear();
        const m = String(d.getMonth() + 1).padStart(2, '0');
        const day = String(d.getDate()).padStart(2, '0');
        return `${y}-${m}-${day}`;
      };
      const isTradingDay = (d) => d.getDay() !== 6 && d.getDay() !== 0; // NEPSE trades Mon-Fri; closed Sat (6) & Sun (0)
      const toTradingDay = (d, rev = false) => {
        const c = new Date(d);
        while (!isTradingDay(c)) c.setDate(c.getDate() + (rev ? -1 : 1));
        return c;
      };

      const stageToday = toTradingDay(new Date(), true);
      const stageTodayStr = fmtDate(stageToday);

      if (!stageToInput.value)   stageToInput.value = stageTodayStr;
      if (!stageFromInput.value) {
        const y1 = new Date(stageToday);
        y1.setFullYear(y1.getFullYear() - 1);
        stageFromInput.value = fmtDate(toTradingDay(y1));
      }

      let stageToPicker, stageFromPicker;

      const syncStageBounds = () => {
        if (stageToPicker)   stageToPicker.set('minDate', stageFromInput.value || null);
        if (stageFromPicker) stageFromPicker.set('maxDate', stageToInput.value || stageTodayStr);
        if (stageFromInput.value && stageToInput.value && stageFromInput.value > stageToInput.value) {
          stageFromInput.value = stageToInput.value;
          if (stageFromPicker) stageFromPicker.setDate(stageFromInput.value, false);
        }
      };

      if (window.flatpickr) {
        stageToPicker = flatpickr(stageToInput, {
          dateFormat: 'Y-m-d',
          defaultDate: stageToInput.value || stageTodayStr,
          maxDate: stageTodayStr,
          enable: [isTradingDay],
          allowInput: false,
          onChange(selectedDates) {
            if (!selectedDates.length) return;
            const sel = fmtDate(selectedDates[0]);
            if (stageFromPicker) {
              stageFromPicker.set('maxDate', sel);
              if (stageFromInput.value && stageFromInput.value > sel) {
                stageFromInput.value = sel;
                stageFromPicker.setDate(sel, false);
              }
            }
            syncStageBounds();
          }
        });

        stageFromPicker = flatpickr(stageFromInput, {
          dateFormat: 'Y-m-d',
          defaultDate: stageFromInput.value || undefined,
          maxDate: stageToInput.value || stageTodayStr,
          enable: [isTradingDay],
          allowInput: false,
          onChange(selectedDates) {
            if (!selectedDates.length) return;
            const sel = fmtDate(selectedDates[0]);
            if (stageToPicker) {
              stageToPicker.set('minDate', sel);
              if (stageToInput.value && stageToInput.value < sel) {
                stageToInput.value = sel;
                stageToPicker.setDate(sel, false);
              }
            }
            syncStageBounds();
          }
        });
      }

      // Quick-range shortcut buttons (Stage needs ≥150 rows — minimum 8 months recommended)
      const setStageRange = (rangeKey) => {
        const end = new Date(stageToday);
        let start = new Date(stageToday);
        if      (rangeKey === '6m')  start.setMonth(start.getMonth() - 6);
        else if (rangeKey === '1y')  start.setFullYear(start.getFullYear() - 1);
        else if (rangeKey === '2y')  start.setFullYear(start.getFullYear() - 2);
        else if (rangeKey === '3y')  start.setFullYear(start.getFullYear() - 3);
        else if (rangeKey === 'ytd') start = new Date(stageToday.getFullYear(), 0, 1);
        const ns = fmtDate(toTradingDay(start));
        const ne = fmtDate(toTradingDay(end, true));
        stageFromInput.value = ns;
        stageToInput.value   = ne;
        if (stageFromPicker) stageFromPicker.setDate(ns, false);
        if (stageToPicker)   stageToPicker.setDate(ne, false);
        syncStageBounds();
      };

      stageForm.querySelectorAll('[data-stage-range]').forEach((btn) => {
        btn.addEventListener('click', () => setStageRange(btn.dataset.stageRange));
      });

      stageForm.addEventListener('submit', function(e) {
        const getNum = (name) => parseFloat(stageForm.querySelector(`[name="${name}"]`)?.value || '0');
        const volMult = getNum('stage_volume_multiplier');
        const resLookback = getNum('stage_resistance_lookback');
        const volLookback = getNum('stage_volume_lookback');
        const momentumPeriod = getNum('stage_momentum_period');
        const rsiLen = getNum('stage_rsi_length');
        const rsiMin = getNum('stage_rsi_threshold');
        const adxLen = getNum('stage_adx_length');
        const adxMin = getNum('stage_adx_threshold');

        const issues = [];
        if (!stageFromInput.value || !stageToInput.value) issues.push('From/To date is required.');
        if (volMult <= 0) issues.push('Volume ratio minimum must be greater than zero.');
        if (resLookback < 2 || volLookback < 2 || momentumPeriod < 2 || rsiLen < 2 || adxLen < 2) {
          issues.push('Lookback/length values must be at least 2.');
        }
        if (rsiMin < 0 || rsiMin > 100) issues.push('RSI minimum should be between 0 and 100.');
        if (adxMin < 0 || adxMin > 100) issues.push('ADX minimum should be between 0 and 100.');

        if (issues.length) {
          e.preventDefault();
          alert('Validation warnings:\\n- ' + issues.join('\\n- '));
        }
      });
    }

    // ── RRG charts (company desk + index desk) ──────────────────────────────
    // Both desks share one toolbar binder and one palette. All colours are CSS
    // custom properties set on the chart wrapper (dashboard.css), so the SVG
    // follows the light/dark theme instead of painting a white slab.
    const RRG_QUADRANT_COLOR = {
      Leading: 'var(--rrg-leading)',
      Weakening: 'var(--rrg-weakening)',
      Lagging: 'var(--rrg-lagging)',
      Improving: 'var(--rrg-improving)',
    };
    const rrgColorFor = (quadrant) => RRG_QUADRANT_COLOR[quadrant] || 'var(--rrg-ink-2)';
    const escapeSvg = (value) => {
      const entityMap = { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' };
      return String(value == null ? '' : value).replace(/[&<>"']/g, (ch) => entityMap[ch]);
    };
    const readJsonScript = (id) => {
      const el = document.getElementById(id);
      if (!el) return [];
      try {
        return JSON.parse(el.textContent || '[]');
      } catch (e) {
        return [];
      }
    };
    const finiteXY = (row) => Number.isFinite(Number(row.RS_Ratio)) && Number.isFinite(Number(row.RS_Momentum));
    // Four arrowheads (one per quadrant colour) so an arrow reads as belonging
    // to its own trail rather than to no series.
    const rrgArrowDefs = (prefix) => `<defs>${Object.keys(RRG_QUADRANT_COLOR).map((q) =>
      `<marker id="${prefix}-${q}" viewBox="0 0 10 10" refX="8" refY="5" markerWidth="6" markerHeight="6" orient="auto-start-reverse"><path d="M 0 0 L 10 5 L 0 10 z" fill="${RRG_QUADRANT_COLOR[q]}"></path></marker>`).join('')}</defs>`;
    // Axis domain shared by both charts: Lock pins it, Fit hugs the visible
    // points, Center keeps 100 in the middle with a symmetric spread.
    const rrgDomain = (state, rows) => {
      let min;
      let max;
      if (state.lockedDomain) {
        ({ min, max } = state.lockedDomain);
      } else if (state.scaleMode === 'fit') {
        const values = rows.flatMap((row) => [Number(row.RS_Ratio), Number(row.RS_Momentum)]).concat([100]);
        min = Math.floor(Math.min(...values)) - 1;
        max = Math.ceil(Math.max(...values)) + 1;
      } else {
        const spread = Math.max(2, Math.ceil(Math.max(0, ...rows.flatMap((row) => [
          Math.abs(Number(row.RS_Ratio) - 100),
          Math.abs(Number(row.RS_Momentum) - 100),
        ]))) + 1);
        min = 100 - spread;
        max = 100 + spread;
      }
      if (!Number.isFinite(min) || !Number.isFinite(max) || min >= max) {
        min = 98;
        max = 102;
      }
      state.lastDomain = { min, max };
      return { min, max };
    };

    // Per-desk view state. Module-scope on purpose: it must outlive an AJAX
    // swap of the results partial so Fit/Lock survive a recalculation.
    const rrgState = { scaleMode: 'center', lockedDomain: null, lastDomain: null, timer: null, frame: null };
    const rrgIndicesState = { scaleMode: 'center', lockedDomain: null, lastDomain: null, timer: null, frame: null };

    const readRrgPoints = () => readJsonScript('rrg-chart-data')
      .filter(finiteXY)
      .sort((a, b) => String(a.business_date || '').localeCompare(String(b.business_date || '')));
    const getRrgMaxTail = () => Math.max(1, readRrgPoints().length);

    const syncTailControls = (sliderId, numberId, value, maxTail) => {
      const normalized = Math.max(1, Math.min(Number(value) || maxTail, maxTail));
      const slider = document.getElementById(sliderId);
      const number = document.getElementById(numberId);
      [slider, number].forEach((el) => {
        if (!el) return;
        el.max = String(maxTail);
        el.value = String(normalized);
        el.setAttribute('aria-valuetext', `${normalized} of ${maxTail} bars`);
      });
      return normalized;
    };
    const syncRrgTailControls = (value) => syncTailControls('rrgTailSlider', 'rrgTailNumber', value, getRrgMaxTail());

    const drawRrgChart = () => {
      const container = document.getElementById('rrgChart');
      if (!container) return;
      const allPoints = readRrgPoints();
      if (!allPoints.length) return;
      // Tail Length: show only the last N points of the trail (1 = latest only).
      const tailLength = syncRrgTailControls(document.getElementById('rrgTailNumber')?.value || allPoints.length);
      const arrowMode = document.getElementById('rrgArrowMode')?.checked || false;
      const isAnimatingTrail = Number.isInteger(rrgState.frame);
      const animationFrame = Math.max(1, Math.min(rrgState.frame || 1, allPoints.length));
      const points = isAnimatingTrail ? allPoints.slice(0, animationFrame) : allPoints.slice(-tailLength);

      const width = 900;
      const height = 390;
      const pad = 46;
      const { min, max } = rrgDomain(rrgState, isAnimatingTrail ? allPoints : points);
      const scaleX = (value) => pad + ((value - min) / (max - min)) * (width - pad * 2);
      const scaleY = (value) => height - pad - ((value - min) / (max - min)) * (height - pad * 2);
      const fmtDate = (value) => escapeSvg(String(value || '').slice(0, 10));

      // Quadrant cross is anchored at value 100, clamped into the plot box so
      // it stays visible even when Fit/Lock push 100 toward an edge.
      const cx = Math.max(pad, Math.min(width - pad, scaleX(100)));
      const cy = Math.max(pad, Math.min(height - pad, scaleY(100)));

      const latest = points[points.length - 1];
      const latestX = scaleX(Number(latest.RS_Ratio));
      const latestY = scaleY(Number(latest.RS_Momentum));
      const latestColor = rrgColorFor(latest.Quadrant);
      const showLatestState = !isAnimatingTrail || animationFrame >= allPoints.length;

      // Trail along the visible tail (needs >= 2 points). Arrow Mode adds a
      // direction arrowhead at the leading (latest) end.
      const arrowAttr = arrowMode ? ` marker-end="url(#rrgArrow-${escapeSvg(latest.Quadrant)})"` : '';
      const path = points.length > 1
        ? `<path d="${points
            .map((row, index) => `${index === 0 ? 'M' : 'L'} ${scaleX(Number(row.RS_Ratio)).toFixed(2)} ${scaleY(Number(row.RS_Momentum)).toFixed(2)}`)
            .join(' ')}" fill="none" stroke="${latestColor}" stroke-width="2" stroke-linejoin="round" stroke-linecap="round" opacity="0.75"${arrowAttr}></path>`
        : '';

      // Historical dots (all but the latest), fading toward the start of the tail.
      const trailDots = points.slice(0, -1).map((row, index) => {
        const opacity = 0.25 + (index / Math.max(1, points.length - 1)) * 0.45;
        return `<circle cx="${scaleX(Number(row.RS_Ratio)).toFixed(2)}" cy="${scaleY(Number(row.RS_Momentum)).toFixed(2)}" r="3.5" fill="${rrgColorFor(row.Quadrant)}" opacity="${opacity.toFixed(2)}"><title>${fmtDate(row.business_date)}: ${Number(row.RS_Ratio).toFixed(2)}, ${Number(row.RS_Momentum).toFixed(2)} (${escapeSvg(row.Quadrant)})</title></circle>`;
      }).join('');

      const latestDot = `<circle cx="${latestX.toFixed(2)}" cy="${latestY.toFixed(2)}" r="6" fill="${latestColor}"><title>${fmtDate(latest.business_date)}: ${Number(latest.RS_Ratio).toFixed(2)}, ${Number(latest.RS_Momentum).toFixed(2)} (${escapeSvg(latest.Quadrant)})</title></circle>`;
      // Pulsing halo around the latest point (CSS animation in dashboard.css).
      const blink = showLatestState
        ? `<circle class="rrg-blink" cx="${latestX.toFixed(2)}" cy="${latestY.toFixed(2)}" r="6" fill="none" stroke="${latestColor}" stroke-width="2.5"></circle>`
        : '';
      const pointLabel = showLatestState ? 'Latest' : fmtDate(latest.business_date);
      // Keep the label inside the plot: flip to the left of the dot near the right edge.
      const labelRight = latestX + 10 + 60 <= width - pad;
      const labelX = labelRight ? latestX + 10 : latestX - 10;
      const labelY = Math.max(pad + 12, latestY - 12);
      const aria = `RRG for the selected symbol: latest RS-Ratio ${Number(latest.RS_Ratio).toFixed(2)}, RS-Momentum ${Number(latest.RS_Momentum).toFixed(2)}, ${escapeSvg(latest.Quadrant)} quadrant`;

      container.innerHTML = `
        <svg class="rrg-chart-svg" viewBox="0 0 ${width} ${height}" role="img" aria-label="${aria}">
          ${arrowMode ? rrgArrowDefs('rrgArrow') : ''}
          <rect x="${pad}" y="${pad}" width="${(cx - pad).toFixed(2)}" height="${(cy - pad).toFixed(2)}" fill="var(--rrg-improving)" opacity="0.1"></rect>
          <rect x="${cx.toFixed(2)}" y="${pad}" width="${(width - pad - cx).toFixed(2)}" height="${(cy - pad).toFixed(2)}" fill="var(--rrg-leading)" opacity="0.1"></rect>
          <rect x="${pad}" y="${cy.toFixed(2)}" width="${(cx - pad).toFixed(2)}" height="${(height - pad - cy).toFixed(2)}" fill="var(--rrg-lagging)" opacity="0.1"></rect>
          <rect x="${cx.toFixed(2)}" y="${cy.toFixed(2)}" width="${(width - pad - cx).toFixed(2)}" height="${(height - pad - cy).toFixed(2)}" fill="var(--rrg-weakening)" opacity="0.1"></rect>
          <line x1="${pad}" y1="${cy.toFixed(2)}" x2="${width - pad}" y2="${cy.toFixed(2)}" stroke="var(--rrg-axis)" stroke-width="1.5"></line>
          <line x1="${cx.toFixed(2)}" y1="${pad}" x2="${cx.toFixed(2)}" y2="${height - pad}" stroke="var(--rrg-axis)" stroke-width="1.5"></line>
          <rect x="${pad}" y="${pad}" width="${width - pad * 2}" height="${height - pad * 2}" fill="none" stroke="var(--rrg-grid)"></rect>
          <text x="${width - pad - 70}" y="${pad + 22}" fill="var(--rrg-leading)" font-size="13" font-weight="700">Leading</text>
          <text x="${width - pad - 88}" y="${height - pad - 12}" fill="var(--rrg-weakening)" font-size="13" font-weight="700">Weakening</text>
          <text x="${pad + 14}" y="${height - pad - 12}" fill="var(--rrg-lagging)" font-size="13" font-weight="700">Lagging</text>
          <text x="${pad + 14}" y="${pad + 22}" fill="var(--rrg-improving)" font-size="13" font-weight="700">Improving</text>
          <text x="${width / 2}" y="${height - 12}" fill="var(--rrg-ink-2)" font-size="12" text-anchor="middle">RS-Ratio</text>
          <text x="15" y="${height / 2}" fill="var(--rrg-ink-2)" font-size="12" text-anchor="middle" transform="rotate(-90 15 ${height / 2})">RS-Momentum</text>
          <text x="${(cx + 6).toFixed(2)}" y="${(cy - 7).toFixed(2)}" fill="var(--rrg-ink-2)" font-size="11">100</text>
          ${path}
          ${trailDots}
          ${latestDot}
          ${blink}
          <text x="${labelX.toFixed(2)}" y="${labelY.toFixed(2)}" fill="var(--rrg-ink)" font-size="12" font-weight="700" text-anchor="${labelRight ? 'start' : 'end'}">${pointLabel}</text>
        </svg>`;
    };

    // One binder for both desks. `ids` maps the logical control to its DOM id;
    // `state` is that desk's view state; `draw` re-renders; `maxTail` is how
    // many bars the animation walks through.
    const bindRrgToolbar = ({ chartCard, ids, state, draw, maxTail, syncTail, maxClass }) => {
      const el = (key) => document.getElementById(ids[key]);
      const animateBtn = el('animate');
      const fitBtn = el('fit');
      const maxBtn = el('max');
      const centerBtn = el('center');
      const lockBtn = el('lock');
      const slider = el('slider');
      const number = el('number');
      const arrowMode = el('arrow');
      const card = document.querySelector(chartCard);
      if (!card || !animateBtn || !fitBtn || !maxBtn || !centerBtn || !lockBtn || !slider || !number || !arrowMode) return;

      // Fresh nodes (first load or AJAX swap): kill any animation still
      // ticking against the old DOM, and drop a locked domain — it belonged to
      // the previous dataset and would draw the new points against a stale axis.
      if (state.timer) clearInterval(state.timer);
      state.timer = null;
      state.frame = null;
      state.lockedDomain = null;

      // Default to the latest dot only. The full trail is dozens of crossing
      // lines, which reads as a tangle; the slider and Animate reveal the path.
      syncTail(1);

      const setPressed = (btn, on) => {
        btn.classList.toggle('active', on);
        btn.setAttribute('aria-pressed', on ? 'true' : 'false');
      };
      const refreshScaleButtons = () => {
        setPressed(fitBtn, state.scaleMode === 'fit');
        setPressed(centerBtn, state.scaleMode === 'center');
      };
      const refreshLockButton = () => {
        const locked = Boolean(state.lockedDomain);
        setPressed(lockBtn, locked);
        lockBtn.innerHTML = locked ? '&#128274;' : '&#128275;';
        lockBtn.setAttribute('aria-label', locked ? 'Unlock scale' : 'Lock scale');
        lockBtn.title = locked ? 'Unlock scale' : 'Lock scale';
      };
      const stopAnimation = () => {
        if (state.timer) clearInterval(state.timer);
        state.timer = null;
        state.frame = null;
        animateBtn.innerHTML = '&#9658; Animate';
        setPressed(animateBtn, false);
      };
      const setMax = (on) => {
        card.classList.toggle(maxClass, on);
        setPressed(maxBtn, on);
        document.body.classList.toggle('rrg-max-open', Boolean(document.querySelector('.rrg-max-view, .rrg-indices-max-view')));
        draw();
      };
      // Button state always mirrors the (surviving) view state.
      refreshScaleButtons();
      refreshLockButton();
      setPressed(maxBtn, card.classList.contains(maxClass));

      slider.addEventListener('input', () => { stopAnimation(); syncTail(slider.value); draw(); });
      number.addEventListener('input', () => { stopAnimation(); syncTail(number.value); draw(); });
      arrowMode.addEventListener('change', draw);
      fitBtn.addEventListener('click', () => {
        state.scaleMode = 'fit';
        state.lockedDomain = null;
        refreshScaleButtons();
        refreshLockButton();
        draw();
      });
      centerBtn.addEventListener('click', () => {
        state.scaleMode = 'center';
        state.lockedDomain = null;
        refreshScaleButtons();
        refreshLockButton();
        draw();
      });
      maxBtn.addEventListener('click', () => setMax(!card.classList.contains(maxClass)));
      card.addEventListener('keydown', (e) => {
        if (e.key === 'Escape' && card.classList.contains(maxClass)) { setMax(false); maxBtn.focus(); }
      });
      lockBtn.addEventListener('click', () => {
        state.lockedDomain = state.lockedDomain ? null : (state.lastDomain ? { ...state.lastDomain } : null);
        refreshLockButton();
        draw();
      });
      animateBtn.addEventListener('click', () => {
        if (state.timer) { stopAnimation(); draw(); return; }
        const total = maxTail();
        let frame = 1;
        animateBtn.textContent = 'Animating';
        setPressed(animateBtn, true);
        state.frame = frame;                // head starts at the initial date…
        draw();
        state.timer = setInterval(() => {
          frame += 1;                       // …and advances toward the last date
          state.frame = frame;
          draw();
          if (frame >= total) { stopAnimation(); draw(); }   // back to the user's tail length
        }, 180);
      });
    };

    const setupRrgToolbar = () => bindRrgToolbar({
      chartCard: '.rrg-chart-card',
      maxClass: 'rrg-max-view',
      ids: { animate: 'rrgAnimateBtn', fit: 'rrgFitBtn', max: 'rrgMaxBtn', center: 'rrgCenterBtn', lock: 'rrgLockBtn', slider: 'rrgTailSlider', number: 'rrgTailNumber', arrow: 'rrgArrowMode' },
      state: rrgState,
      draw: drawRrgChart,
      maxTail: getRrgMaxTail,
      syncTail: syncRrgTailControls,
    });

    // ── Index desk ──
    const readRrgIndicesTrails = () => {
      const bySymbol = new Map();
      readJsonScript('rrg-indices-trails-data').filter(finiteXY).forEach((row) => {
        if (!bySymbol.has(row.symbol)) bySymbol.set(row.symbol, []);
        bySymbol.get(row.symbol).push(row);
      });
      bySymbol.forEach((rows) => rows.sort((a, b) => Number(a.step) - Number(b.step)));
      return bySymbol;
    };
    const getRrgIndicesMaxTail = () => Math.max(1, ...Array.from(readRrgIndicesTrails().values()).map((rows) => rows.length));
    const syncRrgIndicesTailControls = (value) =>
      // The form's hidden tail field is deliberately NOT written here. It is how
      // many trail bars the server SENDS; this toolbar only shortens what has
      // already arrived.
      syncTailControls('rrgIndicesTailSlider', 'rrgIndicesTailNumber', value, getRrgIndicesMaxTail());

    const setupRrgIndicesToolbar = () => bindRrgToolbar({
      chartCard: '.rrg-indices-chart-card',
      maxClass: 'rrg-indices-max-view',
      ids: { animate: 'rrgIndicesAnimateBtn', fit: 'rrgIndicesFitBtn', max: 'rrgIndicesMaxBtn', center: 'rrgIndicesCenterBtn', lock: 'rrgIndicesLockBtn', slider: 'rrgIndicesTailSlider', number: 'rrgIndicesTailNumber', arrow: 'rrgIndicesArrowMode' },
      state: rrgIndicesState,
      draw: drawRrgIndicesChart,
      maxTail: getRrgIndicesMaxTail,
      syncTail: syncRrgIndicesTailControls,
    });

    const drawRrgIndicesChart = () => {
      const container = document.getElementById('rrgIndicesChart');
      if (!container) return;

      const points = readJsonScript('rrg-indices-points-data').filter(finiteXY);
      if (!points.length) return;
      const allTrailsBySymbol = readRrgIndicesTrails();
      const maxTail = Math.max(1, ...Array.from(allTrailsBySymbol.values()).map((rows) => rows.length));
      const tailLength = syncTailControls('rrgIndicesTailSlider', 'rrgIndicesTailNumber',
        document.getElementById('rrgIndicesTailNumber')?.value || maxTail, maxTail);
      const arrowMode = document.getElementById('rrgIndicesArrowMode')?.checked || false;
      // When animating, accumulate each index's trail up to the current frame and
      // put its head at that frame — so the head moves from the initial date to
      // the last date. Otherwise show the last `tailLength` bars with the head at
      // the latest date.
      const isAnimating = Number.isInteger(rrgIndicesState.frame);
      const headBySymbol = new Map();
      const trailsBySymbol = new Map();
      allTrailsBySymbol.forEach((rows, symbol) => {
        const windowRows = isAnimating
          ? rows.slice(0, Math.max(1, Math.min(rrgIndicesState.frame, rows.length)))
          : rows.slice(-tailLength);
        if (windowRows.length) headBySymbol.set(symbol, windowRows[windowRows.length - 1]);
        trailsBySymbol.set(symbol, windowRows);
      });
      const headPoints = points.map((p) => {
        const h = headBySymbol.get(p.symbol);
        return (isAnimating && h)
          ? { ...p, RS_Ratio: h.RS_Ratio, RS_Momentum: h.RS_Momentum, Quadrant: h.Quadrant, business_date: h.business_date }
          : p;
      });
      // Current animation date (indices share one trading calendar) for the header.
      const animDate = isAnimating
        ? Array.from(headBySymbol.values()).reduce((d, h) => (h && h.business_date > d ? h.business_date : d), '')
        : '';
      const benchmark = readJsonScript('rrg-indices-benchmark-data').filter((row) => Number.isFinite(Number(row.close)));

      const width = 1080;
      const height = 620;
      const plotLeft = 74;
      const plotRight = width - 38;
      const plotTop = 148;
      const plotBottom = height - 58;
      const plotWidth = plotRight - plotLeft;
      const plotHeight = plotBottom - plotTop;
      // Fit to the FULL extent during animation so the axes stay fixed and the
      // dots visibly move within a stable frame (instead of rescaling each tick).
      const coords = isAnimating
        ? points.concat(Array.from(allTrailsBySymbol.values()).flat())
        : headPoints.concat(Array.from(trailsBySymbol.values()).flat());
      const { min, max } = rrgDomain(rrgIndicesState, coords);
      const scaleX = (value) => plotLeft + ((value - min) / (max - min)) * plotWidth;
      const scaleY = (value) => plotBottom - ((value - min) / (max - min)) * plotHeight;
      const centerX = scaleX(100);
      const centerY = scaleY(100);

      const tickStep = Math.max(1, Math.ceil((max - min) / 8));
      const ticks = [];
      for (let value = Math.floor(min / tickStep) * tickStep; value <= max + 0.001; value += tickStep) {
        if (value >= min - 0.001) ticks.push(value);
      }
      if (!ticks.some((value) => Math.abs(value - 100) < 0.001)) ticks.push(100);
      ticks.sort((a, b) => a - b);

      const gridNodes = ticks.map((value) => {
        const x = scaleX(value);
        const y = scaleY(value);
        const strong = Math.abs(value - 100) < 0.001;
        const stroke = strong ? 'var(--rrg-axis)' : 'var(--rrg-grid)';
        return `
          <line x1="${x.toFixed(2)}" y1="${plotTop}" x2="${x.toFixed(2)}" y2="${plotBottom}" stroke="${stroke}" stroke-width="${strong ? 2 : 1}"></line>
          <line x1="${plotLeft}" y1="${y.toFixed(2)}" x2="${plotRight}" y2="${y.toFixed(2)}" stroke="${stroke}" stroke-width="${strong ? 2 : 1}"></line>
          <text x="${x.toFixed(2)}" y="${plotBottom + 25}" fill="var(--rrg-ink-2)" font-size="14" text-anchor="middle">${value}</text>
          <text x="${plotLeft - 12}" y="${(y + 5).toFixed(2)}" fill="var(--rrg-ink-2)" font-size="14" text-anchor="end">${value}</text>
        `;
      }).join('');

      let sparkNodes = '';
      if (benchmark.length > 1) {
        const sparkTop = 24;
        const sparkBottom = 116;
        const closes = benchmark.map((row) => Number(row.close));
        const minClose = Math.min(...closes);
        const maxClose = Math.max(...closes);
        const closeRange = Math.max(1, maxClose - minClose);
        const sparkX = (index) => plotLeft + (index / (benchmark.length - 1)) * plotWidth;
        const sparkY = (close) => sparkBottom - ((close - minClose) / closeRange) * (sparkBottom - sparkTop - 12) - 6;
        const linePath = benchmark
          .map((row, index) => `${index === 0 ? 'M' : 'L'} ${sparkX(index).toFixed(2)} ${sparkY(Number(row.close)).toFixed(2)}`)
          .join(' ');
        const lastX = sparkX(benchmark.length - 1);
        const lastY = sparkY(closes[closes.length - 1]);
        const areaPath = `${linePath} L ${lastX.toFixed(2)} ${sparkBottom} L ${plotLeft} ${sparkBottom} Z`;
        const latest = benchmark[benchmark.length - 1];
        sparkNodes = `
          <text x="${plotLeft}" y="19" fill="var(--rrg-ink)" font-size="19" font-weight="700">NEPSE RRG Indices</text>
          <text x="${plotLeft + 198}" y="19" fill="var(--rrg-ink-2)" font-size="13">${escapeSvg((isAnimating && animDate) ? animDate : (latest.business_date || ''))}</text>
          <rect x="${plotLeft}" y="${sparkTop}" width="${plotWidth}" height="${sparkBottom - sparkTop}" fill="var(--rrg-ink)" opacity="0.05"></rect>
          <path d="${areaPath}" fill="var(--rrg-ink)" opacity="0.08"></path>
          <path d="${linePath}" fill="none" stroke="var(--rrg-ink-2)" stroke-width="2.5" stroke-linejoin="round"></path>
          <line x1="${plotLeft}" y1="${lastY.toFixed(2)}" x2="${plotRight}" y2="${lastY.toFixed(2)}" stroke="var(--rrg-ink)" stroke-width="1.5" opacity="0.7"></line>
          <line x1="${lastX.toFixed(2)}" y1="${sparkTop}" x2="${lastX.toFixed(2)}" y2="${sparkBottom}" stroke="var(--rrg-ink)" stroke-width="1.5" opacity="0.7"></line>
          <text x="${plotLeft - 10}" y="${(lastY + 5).toFixed(2)}" fill="var(--rrg-ink)" font-size="15" font-weight="700" text-anchor="end">${Number(latest.close).toFixed(2)}</text>
        `;
      }

      const trailNodes = Array.from(trailsBySymbol.entries()).map(([symbol, rows]) => {
        if (rows.length < 2) return '';
        const head = headBySymbol.get(symbol) || rows[rows.length - 1];
        const path = rows
          .map((row, index) => `${index === 0 ? 'M' : 'L'} ${scaleX(Number(row.RS_Ratio)).toFixed(2)} ${scaleY(Number(row.RS_Momentum)).toFixed(2)}`)
          .join(' ');
        const trailDots = rows.slice(0, -1).map((row, index) => {
          const opacity = 0.18 + (index / Math.max(1, rows.length - 1)) * 0.35;
          return `<circle cx="${scaleX(Number(row.RS_Ratio)).toFixed(2)}" cy="${scaleY(Number(row.RS_Momentum)).toFixed(2)}" r="3" fill="${rrgColorFor(row.Quadrant)}" opacity="${opacity.toFixed(2)}"></circle>`;
        }).join('');
        const arrowAttr = arrowMode ? ` marker-end="url(#rrgIndicesArrow-${escapeSvg(head.Quadrant)})"` : '';
        return `<path d="${path}" fill="none" stroke="${rrgColorFor(head.Quadrant)}" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" opacity="0.5"${arrowAttr}></path>${trailDots}`;
      }).join('');

      // Labels: anchored to the side of the dot away from the centre line, then
      // nudged apart per side so clustered indices near 100 don't overprint.
      const LABEL_H = 15;
      const labels = headPoints.map((point) => {
        const x = scaleX(Number(point.RS_Ratio));
        const y = scaleY(Number(point.RS_Momentum));
        const rightSide = Number(point.RS_Ratio) >= 100;
        return { point, x, y, rightSide, labelY: y + 5 };
      });
      ['left', 'right'].forEach((side) => {
        const group = labels.filter((l) => (side === 'right') === l.rightSide).sort((a, b) => a.labelY - b.labelY);
        for (let i = 1; i < group.length; i += 1) {
          if (group[i].labelY - group[i - 1].labelY < LABEL_H) group[i].labelY = group[i - 1].labelY + LABEL_H;
        }
        // If the stack ran off the bottom, shift the whole group back up.
        const overflow = group.length ? group[group.length - 1].labelY - (plotBottom - 4) : 0;
        if (overflow > 0) group.forEach((l) => { l.labelY -= overflow; });
      });
      const pointNodes = labels.map(({ point, x, y, rightSide, labelY }) => {
        const color = rrgColorFor(point.Quadrant);
        return `
          <circle cx="${x.toFixed(2)}" cy="${y.toFixed(2)}" r="7" fill="${color}" stroke="var(--rrg-paper)" stroke-width="2">
            <title>${escapeSvg(point.symbol)}: ${Number(point.RS_Ratio).toFixed(2)}, ${Number(point.RS_Momentum).toFixed(2)} (${escapeSvg(point.Quadrant)})</title>
          </circle>
          <text x="${(x + (rightSide ? 10 : -10)).toFixed(2)}" y="${labelY.toFixed(2)}" fill="var(--rrg-ink)" font-size="13" font-weight="700" text-anchor="${rightSide ? 'start' : 'end'}">${escapeSvg(point.label)}</text>
        `;
      }).join('');

      const aria = `NEPSE indices relative rotation graph: ${headPoints.map((p) => `${p.label} ${p.Quadrant}`).join(', ')}`;
      container.innerHTML = `
        <svg class="rrg-indices-chart-svg" viewBox="0 0 ${width} ${height}" role="img" aria-label="${escapeSvg(aria)}">
          ${arrowMode ? rrgArrowDefs('rrgIndicesArrow') : ''}
          <rect x="0" y="0" width="${width}" height="${height}" fill="var(--rrg-paper)"></rect>
          ${sparkNodes}
          <rect x="${plotLeft}" y="${plotTop}" width="${centerX - plotLeft}" height="${centerY - plotTop}" fill="var(--rrg-improving)" opacity="0.14"></rect>
          <rect x="${centerX}" y="${plotTop}" width="${plotRight - centerX}" height="${centerY - plotTop}" fill="var(--rrg-leading)" opacity="0.14"></rect>
          <rect x="${plotLeft}" y="${centerY}" width="${centerX - plotLeft}" height="${plotBottom - centerY}" fill="var(--rrg-lagging)" opacity="0.14"></rect>
          <rect x="${centerX}" y="${centerY}" width="${plotRight - centerX}" height="${plotBottom - centerY}" fill="var(--rrg-weakening)" opacity="0.14"></rect>
          ${gridNodes}
          <rect x="${plotLeft}" y="${plotTop}" width="${plotWidth}" height="${plotHeight}" fill="none" stroke="var(--rrg-grid)" stroke-width="1"></rect>
          <text x="${plotLeft + 18}" y="${plotTop + 30}" fill="var(--rrg-improving)" font-size="21" font-weight="800">Improving</text>
          <text x="${plotRight - 22}" y="${plotTop + 30}" fill="var(--rrg-leading)" font-size="21" font-weight="800" text-anchor="end">Leading</text>
          <text x="${plotLeft + 18}" y="${plotBottom - 22}" fill="var(--rrg-lagging)" font-size="21" font-weight="800">Lagging</text>
          <text x="${plotRight - 22}" y="${plotBottom - 22}" fill="var(--rrg-weakening)" font-size="21" font-weight="800" text-anchor="end">Weakening</text>
          <text x="${(plotLeft + plotRight) / 2}" y="${height - 16}" fill="var(--rrg-ink)" font-size="16" font-weight="700" text-anchor="middle">RS-Ratio</text>
          <text x="24" y="${(plotTop + plotBottom) / 2}" fill="var(--rrg-ink)" font-size="16" font-weight="700" text-anchor="middle" transform="rotate(-90 24 ${(plotTop + plotBottom) / 2})">RS-Momentum</text>
          <text x="${(plotLeft + plotRight) / 2}" y="${plotBottom - 16}" fill="var(--rrg-ink-2)" font-size="22" font-weight="800" opacity="0.35" text-anchor="middle">NEPSE / RRG</text>
          ${trailNodes}
          ${pointNodes}
        </svg>`;
    };

    const drawAdvancedMarketStructureChart = () => {
      const chartContainer = document.getElementById('advancedMarketStructureChart');
      const dataEl = document.getElementById('advanced-market-structure-data');
      if (!chartContainer || !dataEl) return;

      // Ensure LightweightCharts library is loaded
      if (typeof LightweightCharts === 'undefined') {
        chartContainer.innerHTML = '<div class="alert alert-warning m-4"><strong>Lightweight Charts library not loaded.</strong> Please check the script tag.</div>';
        return;
      }

      let chartPayload;
      try {
        // The json_script should contain the 'chart' object from the backend metrics
        chartPayload = JSON.parse(dataEl.textContent || '{}');
      } catch (e) {
        chartContainer.innerHTML = "<p class='text-muted p-4'>Failed to parse chart data.</p>";
        return;
      }

      if (!chartPayload || !chartPayload.candles || chartPayload.candles.length === 0) {
        chartContainer.innerHTML = "<p class='text-muted p-4'>Not enough chart data available.</p>";
        return;
      }

      chartContainer.innerHTML = '';
      chartContainer.style.height = '600px';
      chartContainer.style.backgroundColor = '#ffffff';

      const chart = LightweightCharts.createChart(chartContainer, {
        width: chartContainer.clientWidth,
        height: 600,
        layout: { background: { color: '#ffffff' }, textColor: '#333' },
        grid: { vertLines: { color: '#f0f3fa' }, horzLines: { color: '#f0f3fa' } },
        crosshair: { mode: LightweightCharts.CrosshairMode.Normal },
        rightPriceScale: { borderColor: '#cccccc' },
        timeScale: { borderColor: '#cccccc', timeVisible: false, secondsVisible: false },
      });

      const candleSeries = chart.addCandlestickSeries({
        upColor: '#26a69a', downColor: '#ef5350', borderVisible: false,
        wickUpColor: '#26a69a', wickDownColor: '#ef5350',
      });

      const candleData = chartPayload.candles.map(c => ({
        time: c.date, open: c.open, high: c.high, low: c.low, close: c.close
      }));
      candleSeries.setData(candleData);

      if (chartPayload.candles.some(c => c.volume !== undefined && c.volume !== null)) {
        const volumeSeries = chart.addHistogramSeries({
          priceFormat: { type: 'volume' },
          priceScaleId: '',
          scaleMargins: { top: 0.8, bottom: 0 },
        });
        const volumeData = chartPayload.candles.map(c => ({
          time: c.date, value: c.volume,
          color: c.close >= c.open ? 'rgba(38, 166, 154, 0.4)' : 'rgba(239, 83, 80, 0.4)'
        }));
        volumeSeries.setData(volumeData);
      }

      if (chartPayload.baselines && chartPayload.baselines.vwap) {
        const vwapSeries = chart.addLineSeries({ color: '#2962FF', lineWidth: 2, title: 'VWAP' });
        const vwapData = chartPayload.baselines.vwap.map(p => ({ time: p.date, value: p.value })).filter(p => p.value !== null);
        if (vwapData.length > 0) vwapSeries.setData(vwapData);
      }

      // Markers carry NO text — with many pivots/sweeps the labels overlap into
      // an unreadable pile. The arrow direction + colour encodes the meaning and
      // the legend (added below) explains the shapes.
      let markers = [];
      if (chartPayload.pivots) {
        chartPayload.pivots.forEach(p => {
          markers.push({
            time: p.date,
            position: p.pivot_type === 'swing_high' ? 'aboveBar' : 'belowBar',
            color: p.pivot_type === 'swing_high' ? '#ef5350' : '#26a69a',
            shape: p.pivot_type === 'swing_high' ? 'arrowDown' : 'arrowUp',
          });
        });
      }
      if (chartPayload.sweeps) {
        chartPayload.sweeps.forEach(s => {
          markers.push({
            time: s.date,
            position: s.type.includes('Buy-side') ? 'aboveBar' : 'belowBar',
            color: '#ff9800',
            shape: 'circle',
          });
        });
      }
      if (markers.length > 0) {
        markers.sort((a, b) => new Date(a.time).getTime() - new Date(b.time).getTime());
        candleSeries.setMarkers(markers);
      }

      if (chartPayload.zones) {
        chartPayload.zones.forEach(zone => {
          candleSeries.createPriceLine({
            price: zone.center,
            color: zone.type.includes('Supply') || zone.type.includes('Resistance') ? 'rgba(239, 83, 80, 0.7)' : 'rgba(38, 166, 154, 0.7)',
            lineWidth: 2,
            lineStyle: LightweightCharts.LineStyle.Solid,
            axisLabelVisible: true,
            title: zone.type,
          });
        });
      }

      if (chartPayload.trendlines) {
        chartPayload.trendlines.forEach(line => {
          const tlSeries = chart.addLineSeries({
            color: line.direction === 'Ascending' ? '#26a69a' : '#ef5350',
            lineWidth: 2,
            lineStyle: LightweightCharts.LineStyle.Dotted,
            title: line.label,
          });
          const lineData = [{ time: line.start_date, value: line.start_price }, { time: line.end_date, value: line.end_price }].filter(p => p.value !== null);
          if (lineData.length === 2) tlSeries.setData(lineData);
        });
      }

      // Spread the bars across the full width (fixes the large empty gap on the
      // left where bars were packed against the right edge at default spacing).
      chart.timeScale().fitContent();

      // Compact legend so the markers can stay text-free.
      chartContainer.style.position = 'relative';
      const legend = document.createElement('div');
      legend.className = 'ams-chart-legend';
      legend.innerHTML =
        '<span><i style="color:#ef5350">&#9660;</i> Swing High</span>' +
        '<span><i style="color:#26a69a">&#9650;</i> Swing Low</span>' +
        '<span><i style="color:#ff9800">&#9679;</i> Liquidity sweep</span>' +
        '<span><i style="color:#2962FF">&#9472;</i> VWAP</span>';
      chartContainer.appendChild(legend);

      const fitAndResize = () => {
        if (chartContainer.clientWidth > 0) {
          chart.resize(chartContainer.clientWidth, 600);
          chart.timeScale().fitContent();
        }
      };
      window.addEventListener('resize', fitAndResize);

      // Re-fit whenever the tab becomes visible (it may render at 0 width while hidden).
      const tabButton = document.querySelector('[data-bs-target="#support-resistance-pane"]');
      if (tabButton) {
        tabButton.addEventListener('shown.bs.tab', () => {
          setTimeout(fitAndResize, 50);
        });
      }
    };

    // ── Gemini AI narrative (Support & Resistance tab) ─────────────────────
    // The S/R partial renders an #sr-ai-analysis panel with a loading state and
    // a data-ai-url. We auto-fire one fetch per render, reusing the tab's own
    // query params (the URL is kept in sync by workbench-ajax.js after a swap,
    // and is the real query string on a full-page load). The heavy LLM call is
    // off the tab's main render path so the rest of the tab stays instant.
    function loadSrAiAnalysis() {
      const panel = document.getElementById('sr-ai-analysis');
      if (!panel) return;
      const body = panel.querySelector('[data-ai-body]');
      if (!body || panel.dataset.aiLoaded === '1') return;
      panel.dataset.aiLoaded = '1';

      const renderNote = function (text, withRetry) {
        const note = document.createElement('div');
        note.className = 'sr-ai-note text-muted small';
        note.textContent = text;
        body.innerHTML = '';
        body.appendChild(note);
        if (withRetry) {
          const retry = document.createElement('a');
          retry.href = '#';
          retry.className = 'ms-2';
          retry.textContent = 'Retry';
          retry.addEventListener('click', function (e) {
            e.preventDefault();
            panel.dataset.aiLoaded = '';
            body.innerHTML =
              '<div class="text-muted small d-flex align-items-center gap-2" data-ai-loading>' +
              '<span class="spinner-border spinner-border-sm" role="status" aria-hidden="true"></span>' +
              'Generating AI analysis…</div>';
            loadSrAiAnalysis();
          });
          note.appendChild(retry);
        }
      };

      const url = panel.getAttribute('data-ai-url') || '/workbench/ai-analysis/';
      // Read the tab's params straight from its form so we get the exact symbol,
      // dates and filters that produced these results. (window.location.search is
      // unreliable here: workbench-ajax.js calls WorkbenchReinit BEFORE it updates
      // the URL via replaceState, so on the first run the URL is still empty.)
      const srForm = document.getElementById('supportResistanceForm');
      const params = srForm
        ? new URLSearchParams(new FormData(srForm))
        : new URLSearchParams(window.location.search);
      params.set('active_tab', 'support_resistance');

      fetch(url + '?' + params.toString(), {
        method: 'GET',
        headers: { 'X-Requested-With': 'XMLHttpRequest' },
        credentials: 'same-origin',
      })
        .then(function (resp) {
          if (!resp.ok) throw new Error('HTTP ' + resp.status);
          return resp.json();
        })
        .then(function (data) {
          if (data.error) { renderNote(data.error, false); return; }
          const meta = panel.querySelector('[data-ai-meta]');
          if (meta && data.model) {
            meta.textContent = (data.provider ? data.provider + ' · ' : '') + data.model;
          }
          body.innerHTML = '<div class="sr-ai-content">' + (data.analysis_html || '') + '</div>';
        })
        .catch(function () {
          renderNote('AI analysis is temporarily unavailable.', true);
        });
    }

    // ── SOP equity curve (ApexCharts; regime-shaded) — shared by the single
    //    and combined SOP tabs, keyed by their host + json element ids. ───────
    var sopEquityCharts = {};
    function drawSopEquityChartInto(hostId, dataId) {
      var host = document.getElementById(hostId);
      var dataEl = document.getElementById(dataId);
      if (!host || !dataEl || typeof ApexCharts === 'undefined') return;
      var pts;
      try { pts = JSON.parse(dataEl.textContent); } catch (e) { return; }
      if (!pts || !pts.length) return;
      if (sopEquityCharts[hostId]) { try { sopEquityCharts[hostId].destroy(); } catch (e) {} sopEquityCharts[hostId] = null; }
      var isDark = document.documentElement.getAttribute('data-theme') !== 'light';
      var equity = pts.map(function (p) { return [p.x, p.equity]; });
      var buyhold = pts.map(function (p) { return [p.x, p.buyhold]; });
      // Contiguous bear-regime runs → shaded x-axis bands.
      var bands = [], cur = null;
      pts.forEach(function (p) {
        if (p.regime === 'Bear') {
          if (cur) { cur.x2 = p.x; } else { cur = { x: p.x, x2: p.x, fillColor: '#dc2626', opacity: isDark ? 0.10 : 0.08 }; }
        } else if (cur) { bands.push(cur); cur = null; }
      });
      if (cur) bands.push(cur);
      var opts = {
        chart: { type: 'line', height: 360, fontFamily: 'inherit', animations: { enabled: false },
          toolbar: { show: true, tools: { download: false, pan: true, zoom: true, zoomin: true, zoomout: true, reset: true, selection: true } },
          background: 'transparent' },
        theme: { mode: isDark ? 'dark' : 'light' },
        series: [{ name: 'Strategy equity', data: equity }, { name: 'Buy & Hold', data: buyhold }],
        colors: ['#4c8dff', '#94a3b8'],
        stroke: { width: [2.2, 1.4], curve: 'straight', dashArray: [0, 4] },
        dataLabels: { enabled: false },
        markers: { size: 0 },
        legend: { show: true, position: 'top', horizontalAlign: 'left', fontSize: '11px' },
        grid: { borderColor: isDark ? 'rgba(148,163,184,.14)' : 'rgba(15,23,42,.08)', strokeDashArray: 3 },
        xaxis: { type: 'datetime', labels: { datetimeUTC: true } },
        yaxis: { labels: { formatter: function (v) { return v == null ? '' : Math.round(v).toLocaleString(); } }, tickAmount: 5 },
        tooltip: { shared: true, x: { format: 'dd MMM yyyy' },
          y: { formatter: function (v) { return v == null ? '—' : 'NPR ' + Math.round(v).toLocaleString(); } } },
        annotations: { xaxis: bands }
      };
      try { sopEquityCharts[hostId] = new ApexCharts(host, opts); sopEquityCharts[hostId].render(); }
      catch (e) { sopEquityCharts[hostId] = null; }
    }
    function drawSopEquityChart() { drawSopEquityChartInto('sopEquityChart', 'sop-equity-data'); }
    function drawSopcEquityChart() { drawSopEquityChartInto('sopcEquityChart', 'sopc-equity-data'); }

    setupRrgToolbar();
    drawRrgChart();
    drawAdvancedMarketStructureChart();
    setupRrgIndicesToolbar();
    drawRrgIndicesChart();
    drawSopEquityChart();
    drawSopcEquityChart();
    loadSrAiAnalysis();

    // ── AJAX re-init hook ──────────────────────────────────────────────────
    // workbench-ajax.js swaps a single tab's results partial into the DOM after
    // the calc endpoint responds; the dynamic widgets in that partial (paginated
    // tables, bottom-table filters, SVG/Lightweight charts + their toolbars) are
    // bound here on first load, so they must be re-bound against the fresh nodes.
    // Each piece is idempotent / guarded, so calling this repeatedly is safe.
    window.WorkbenchReinit = function (tabKey) {
      try { setupTablePagination(); } catch (e) { /* never block the swap */ }
      try {
        if (tabKey === 'imm_backtest') {
          setupBottomTableFilters(IMM_FILTER_CONFIG);
        } else if (tabKey === 'stage_backtest') {
          setupBottomTableFilters(STAGE_FILTER_CONFIG);
        } else if (tabKey === 'rrg_backtest') {
          setupRrgToolbar();
          drawRrgChart();
        } else if (tabKey === 'rrg_indices') {
          setupRrgIndicesToolbar();
          drawRrgIndicesChart();
        } else if (tabKey === 'support_resistance') {
          drawAdvancedMarketStructureChart();
          loadSrAiAnalysis();
        } else if (tabKey === 'sop_backtest') {
          drawSopEquityChart();
        } else if (tabKey === 'sop_combined') {
          drawSopcEquityChart();
        }
      } catch (e) { /* a chart failure must not break the rest of the page */ }
    };
  });

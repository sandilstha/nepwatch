# MetaStock 11.0 — Standard Operating Procedure (SOP)

> Day-to-day operating guide for the MetaStock 11.0 (End-of-Day) suite installed
> at `C:\Program Files (x86)\Equis\`. Companion to `metastock-blueprint.md`
> (features) and `metastock-engineering-blueprint.md` (how it's built).
>
> Modules on this machine: **MetaStock** (`MsWin.exe`), **The DownLoader**
> (`Dlwin.exe`), **QuoteCenter** (`QCenter.exe`), **OptionScope** (`Oscope.exe`).

---

## 0. One-time setup (first run only)

1. **Activate the license** — `ControlActivation.exe` runs on first launch; enter
   your customer number / activation key (online activation via `Connection.exe`).
2. **Choose data mode** — End-of-Day (DownLoader) vs Real-time (QuoteCenter).
   This install is wired for both; EoD is the default workflow below.
3. **Set the data folder** — decide where security/price files live (e.g.
   `C:\MetaStock Data\`). The DownLoader manages this folder.
4. **Verify** — open MetaStock, open any bundled sample security to confirm
   charts render.

---

## 1. Daily routine (the core SOP loop)

```
 (A) Update data  →  (B) Open chart  →  (C) Apply indicators/experts
        →  (D) Run an Exploration (screen)  →  (E) Review results
        →  (F) (optional) Back-test in System Tester  →  (G) Save layout
```

### A. Update market data — **The DownLoader** (`Dlwin.exe`)
1. Launch The DownLoader.
2. `Online ▸ Collect` (or schedule) to pull the latest end-of-day prices, **or**
   `Tools ▸ Convert` / `File ▸ Import` to load CSV/ASCII data (e.g. NEPSE EoD).
3. Confirm the securities updated (last date column).
4. Close — MetaStock reads the same data folder.

> CSV import field order is typically: `Symbol, Date, Open, High, Low, Close, Volume, OpenInterest`.

### B. Open a chart — **MetaStock** (`MsWin.exe`)
1. `File ▸ Open` → pick the security (or use a **smart chart / template**).
2. Set periodicity: Daily / Weekly / Monthly (toolbar).
3. Choose chart type: Bar, Candlestick, Line, Point & Figure, Kagi, Renko.

### C. Apply indicators & Experts
1. **Indicator:** drag from the **Indicator QuickList** onto the chart (or
   `Insert ▸ Indicator`). Set parameters in the dialog (e.g. `Mov(C,200,E)`).
2. **Custom formula:** `Tools ▸ Indicator Builder ▸ New`, write MFL, `OK`, then
   drag it on like any built-in indicator.
3. **Expert Advisor:** `Tools ▸ Expert Advisor ▸ Attach` → pick an Expert
   (e.g. *Equis – Support & Resistance*). It adds commentary, buy/sell symbols,
   alerts, and trend coloring to the chart.

### D. Screen the market — **The Explorer**
1. `Tools ▸ The Explorer`.
2. Select a pre-built exploration (e.g. *DT Trendline Breakouts*,
   *Deel – New Highs*) **or** `New` to build one.
3. In a new exploration, fill the tabs:
   - **Filter** — boolean MFL that a security must pass, e.g.
     `Cross(C, Mov(C,200,E)) AND V > Ref(V,-1)*1.5`
   - **Columns A–F** — values to display/rank, e.g. `Col A: RSI(14)`,
     `Col B: ROC(C,5,%)`.
4. Pick the security list (whole DB or a watchlist) → `Explore`.
5. Results table appears; sort by any column to rank candidates.

### E. Review results
- Double-click a result row → opens that security's chart for confirmation.
- Reports/results are cached under `MetaStock\Results\…`.

### F. (Optional) Back-test — **System Tester** (`Simulation.dll`)
1. `Tools ▸ The System Tester`.
2. `New System` → define rules on the tabs:
   - **Buy / Sell / Sell Short / Buy to Cover** — MFL entry/exit conditions.
   - **Stops** — max loss, profit target, trailing, breakeven, inactivity.
   - **Trade Execution / Broker** — commissions, slippage, position sizing.
3. Run against a security or list → review the **report**: net profit, win %,
   max drawdown, equity curve, trade list.
4. **Optimize** — mark inputs with `Opt1..OptN` to sweep parameters and find
   the best-performing set.

### G. Save your work
- `File ▸ Save` the chart, or save as a **Template**/**Layout** so the same
  indicators+experts reapply to any security next session.

---

## 2. Option analysis — **OptionScope** (`Oscope.exe`)
1. Launch OptionScope.
2. Enter the underlying + option chain inputs (strike, expiry, rate, volatility).
3. Review greeks (Delta/Gamma/Theta/Vega) and strategy payoff diagrams.
   (Inside MetaStock, the `Option/Delta/Gamma/Theta/Vega` MFL functions give the
   same math on a chart.)

---

## 3. Real-time mode — **QuoteCenter** (`QCenter.exe`)
1. Launch QuoteCenter and log in to the live feed.
2. MetaStock switches to intraday periodicities; charts/Explorer update live.
3. Use the same A–G loop, but data refreshes in real time instead of after close.

---

## 4. Quick reference

| Task | Menu path | Module |
|---|---|---|
| Update / import data | `Online ▸ Collect` / `File ▸ Import` | The DownLoader |
| Open chart | `File ▸ Open` | MetaStock |
| Add indicator | Drag QuickList / `Insert ▸ Indicator` | MetaStock |
| Write custom formula | `Tools ▸ Indicator Builder` | MetaStock |
| Attach commentary | `Tools ▸ Expert Advisor` | MetaStock |
| Screen the universe | `Tools ▸ The Explorer` | MetaStock |
| Back-test a strategy | `Tools ▸ The System Tester` | MetaStock |
| Options greeks/strategies | (standalone) | OptionScope |
| Live quotes | (standalone) | QuoteCenter |
| Share/organize formulas | (standalone) | Formula Organizer |

---

## 5. Reference manuals (on disk)
- `GettingStartedManual.pdf` — first-run walkthrough
- `MetaStockUserManual.pdf` (18 MB) — full operating reference
- `MasteringMetastockManual.pdf` — advanced techniques
- `Formula Help.doc`, `mswin.chm` — MFL function help

---
*Operating procedure compiled from the installed MetaStock 11.0 modules on
2026-06-16. Exact menu labels may vary slightly by build/edition; the
`GettingStartedManual.pdf` is authoritative for this machine.*

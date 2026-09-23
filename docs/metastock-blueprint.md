# MetaStock 11.0 — Feature & Design Blueprint

> Reverse-engineered from the installed product at `C:\Program Files (x86)\Equis\`
> (MetaStock 11.0, Equis International). Purpose: reference for replicating
> equivalent functionality in the NEPSE Analytics Platform.

## 1. What MetaStock is

A desktop technical-analysis and charting suite for stocks/futures/forex/options.
The core workflow is: **load price data → chart it → overlay indicators →
screen the whole universe → get an automated read-out ("Expert") → back-test
trading systems.** A built-in formula language (MFL) drives everything.

## 2. Product architecture (the modules installed)

| Module | Executable | Role |
|---|---|---|
| **MetaStock** (main app) | `MsWin.exe` (v11.0) | Charting, indicators, Explorer, Expert Advisor, System Tester, formula editor |
| **The DownLoader** | `Dlwin.exe` (v11.0) | Local end-of-day data manager: import/convert/organize price history |
| **QuoteCenter** | `QCenter.exe` (v11.0) | Real-time streaming quotes feed |
| **OptionScope** | `Oscope.exe` (v2.1) | Options analysis (greeks, strategies) |
| Formula Organizer | `FormOrg.exe` | Manage/share formula files |
| Activation/Connection | `ControlActivation.exe`, `Connection.exe` | Licensing & online services |
| QuoteScope Tutor | `qstutor.exe` | Tutorial |

**External calculation engines (DLLs)** — pluggable indicator packs:
- `DynamicTradingTools.dll` — Gann/Fibonacci/Andrews-style drawing tools
- `PointFig.dll` — Point & Figure charting
- `ppEval.dll` — formula/expression evaluator (the MFL runtime)
- `Simulation.dll` — System Tester / back-testing engine

## 3. Core subsystems (the "blueprint" to replicate)

### 3.1 Charting engine
- Bar, candlestick, line, point-and-figure, Kagi, Renko, etc.
- Multiple panes per window, overlays, multi-security comparison.
- Drawing tools: trendlines, Fibonacci, Gann, Andrews pitchfork, channels
  (the "Dynamic Trading Tools" / `DynamicTradingTools.dll`).

### 3.2 MetaStock Formula Language (MFL) — the heart of the product
A spreadsheet-like expression language. ~250 built-in functions spanning:
- **Price/volume primitives:** `C`, `O`, `H`, `L`, `V`, `OI`, `Typical()`, `WC()` (weighted close)
- **Math/array ops:** `Abs Add Sub Mul Div Mod Power Sqrt Log Exp Sin Cos Atan
  Floor Ceiling Round Int Frac Neg Max Min Sum Cum`
- **Reference/time-series:** `Ref` (look back/forward), `HHV/LLV` (highest/lowest value),
  `HHVBars/LLVBars`, `BarsSince`, `ValueWhen`, `Highest/Lowest`, `Peak/Trough`,
  `HighestSince/LowestSince`, `Cross`, `Alert`, `When`
- **Logic & inputs:** `If`, `IsDefined/IsUndefined`, `Input()` (user prompt),
  `Fml()/FmlVar()` (call other formulas), `Security()` (pull another symbol)
- **Date/calendar:** `Year Month DayOfWeek DayOfMonth Hour Minute Tick`

See §4 for the full indicator catalog.

### 3.3 The Explorer (screening engine)
Runs a formula across the entire database/watchlist and returns a ranked table
of matches with up to ~6 calculated columns (A–F) plus a Filter condition.
Output is stored under `MetaStock\Results\<n>\<n>\...` (a sharded result cache).
Ships with a large library of pre-built screens (see §5).

### 3.4 The Expert Advisor (automated commentary)
"Experts" attach to a chart and produce: **Commentary** (plain-English read of
current conditions), **Alerts**, **Symbols** (buy/sell arrows), **Trends** (color
the price), and **Highlights**. Stored as `EXPERTS\EXPT00xx.DTA` (70 bundled
experts, e.g. "Bill Williams – Profitunity", "Equis – Support & Resistance").

### 3.5 The System Tester (back-testing)
`Simulation.dll`. Define buy/sell/short/cover rules in MFL, run against history,
get equity curve + trade statistics (net profit, win %, drawdown, etc.).

### 3.6 Data layer
- Native security format = classic MetaStock binary (`.dop`/`.mwd` master files;
  managed via The DownLoader). Demo/sample data ships as `.dta`.
- `ST_DATA.MDB` — a Microsoft Jet (Access) database for symbol/security metadata.
- Templates `.MWT`, layouts `.mwl`, custom menus `Ms10CustMenu.txt`.

## 4. Built-in indicator / function catalog (~250)

**Trend / moving averages:** `Mov` (Simple/Exponential/Triangular/Weighted/
Variable/Time-series), `Dema`, `Tema`, `Wilders`, `VariableMA511`,
`LinearReg`, `LinRegSlope`, `TSF` (time-series forecast), `ForecastOsc`.

**Momentum / oscillators:** `RSI`, `MACD`, `Stoch`, `StochMomentum` (SMI),
`CCI`/`CCIE`, `CMO`, `MFI`, `WillR`, `WillA`, `Mo` (momentum), `ROC`, `TRIX`,
`Ult` (Ultimate Osc), `DPO`, `RMI`, `IMI`, `Qstick`, `Inertia`, `RVI`,
`PFE` (polarized fractal efficiency), `RangeIndicator`.

**Directional / trend strength:** `ADX`, `ADXR`, `DX`, `PDI`, `MDI`, `DMI`,
`CSI`, `RWIH`/`RWIL` (random walk index), `VHF` (vertical horizontal filter),
`Mass` (mass index).

**Volatility / bands / channels:** `ATR`, `Std`/`Stdev`, `BBandTop`/`BBandBot`
(Bollinger), `STEBandTop`/`STEBandBot` (standard-error bands),
`ProjBandTop`/`ProjBandBot`, `PriceChannelHigh`/`PriceChannelLow`, `SAR` (parabolic).

**Volume / money flow:** `OBV`, `AD` (accum/dist), `CO` (Chaikin osc),
`CMF` (Chaikin money flow), `PVT`, `NVI`, `PVI`, `VolO`, `MFI`, `EMV` (ease of
movement), `KVO` (Klinger), `TVI` (trade volume index), `HPI` (herrick payoff),
`DI` (demand index), `MarketFacIndex` (Bill Williams MFI).

**Cycles / advanced math:** `FFT`, `MESASineWave`, `MESALeadSine`,
`Correl`/`Corr`, `RSquared`, `Var`, `STE`, `Divergence`.

**Ichimoku:** `TenkanSen`, `KijunSen`, `SenkouSpanA`, `SenkouSpanB`, `ChikouSpan`.

**Aroon / swing / Zig-Zag:** `AroonUp`, `AroonDown`, `Swing`, `ASwing`, `Zig`,
`Peak`/`Trough` (+ `*Bars` variants).

**Candlestick patterns (recognizers):** `Doji`, `DojiStar`, `GravestoneDoji`,
`LongLeggedDoji`, `Hammer`, `HangingMan`, `InvHammer`, `InvBlackHammer`,
`ShootingStar`, `SpinningTop`, `EngulfingBull`/`EngulfingBear`,
`BullHarami`/`BearHarami` (+ Cross), `Bull3Formation`/`Bear3Formation`,
`MorningStar`/`EveningStar` (+ DojiStar), `DarkCloud`, `PiercingLine`,
`OnNeckLine`, `SeparatingLines`, `TweezerTops`/`TweezerBottoms`,
`RisingWindow`/`FallingWindow`, `GapUp`/`GapDown`, `BigWhite`/`BigBlack`,
`ShavenHead`/`ShavenBottom`, `LongUpperShadow`/`LongLowerShadow`,
`Black`/`White`, `Inside`/`Outside`, `Rally`/`Reaction` (+ WithVol).

**Options pricing (greeks):** `Option`, `Delta`, `Gamma`, `Theta`, `Vega`,
`Life`, `OptionExp` (European/American puts & calls).

**Pivot / market profile:** `MP` (market profile), `BuyP`/`SellP` (buying/selling
pressure), `Typ`.

## 5. Bundled Explorations (pre-built screens — examples)
- **Bill Williams – Profitunity** (Market Facilitation Index windows)
- **Robert Deel** set: Momentum Filter, New Highs, Oversold Reversal
- **DT Support & Resistance** (longer/shorter), **DT Trendline Breakouts**
- **Don Fishback – ODDS Option Analyst**
- **Equis volume family:** 1/5/90-Day Average Volume, High-Volume-Price,
  Surge Volume, Binary Waves, etc.

## 6. Documentation shipped (PDF/CHM, for deeper detail)
`MetaStockUserManual.pdf` (18 MB), `MasteringMetastockManual.pdf`,
`GettingStartedManual.pdf`, `Broad Market Indicators.pdf`,
`Dynamic Trading Tools.pdf`, `Point and Figure Toolbox.pdf`,
`Formula Help.doc`, `mswin.chm`, `activation.chm`.

## 7. Mapping to a modern NEPSE platform (recommended equivalents)

| MetaStock subsystem | Modern equivalent for your platform |
|---|---|
| Binary `.dop`/MDB data layer | Postgres/TimescaleDB OHLCV tables |
| MFL formula engine (`ppEval.dll`) | An expression evaluator over pandas/`pandas-ta`/`TA-Lib` |
| Indicator library (§4) | `TA-Lib` / `pandas-ta` cover ~90% of these directly |
| The Explorer | A screener service: run formula over all symbols → ranked table |
| Expert Advisor | Rule-based "commentary"/signal generator per symbol |
| System Tester | A back-testing engine (e.g. `backtrader`, `vectorbt`) |
| Charting | TradingView Lightweight Charts / Plotly / ECharts |

### Practical takeaway
The highest-leverage thing to copy is the **three-layer model**:
1. **Indicators** (pure functions over OHLCV) — start from TA-Lib so you inherit
   most of §4 for free.
2. **Explorer** (apply a boolean filter + ranking columns across the universe).
3. **Expert/Signals** (turn indicator states into human-readable buy/sell/neutral
   commentary). This is what makes MetaStock feel "smart" and is straightforward
   to reproduce for NEPSE symbols.
```
```
*Generated from the on-disk MetaStock 11.0 install on 2026-06-16.*

# MetaStock 11.0 — Engineering Blueprint (How It's Built)

> Reverse-engineered from the binaries at `C:\Program Files (x86)\Equis\`.
> This documents the **implementation** — language, toolchain, runtime
> dependencies, and internal code architecture — as opposed to the feature
> list (see `metastock-blueprint.md`).

## 1. Language & toolchain

| Aspect | Finding | Evidence |
|---|---|---|
| **Core language** | **C++** (native, unmanaged) | PE32 headers, `.cpp`/`.h` source strings in PDB data |
| **Architecture** | **32-bit x86** (every core binary) | PE machine = 0x14C across all `.exe`/`.dll` |
| **UI framework** | **MFC** (Microsoft Foundation Classes) | `MFC42` import, `comctl32`, `RICHED`, dialog/wizard classes |
| **C runtime** | **Visual C++ 6.0-era CRT** | `msvcrt.dll`, `msvcp60.dll` (VC6 C++ stdlib) |
| **Build config** | Release build from a fixed source root | PDB path `C:\MS11.0\Source\EQUIS APPS\Mswin\Release\Mswin.pdb` |
| **COM/ATL** | Heavy COM use, ATL helpers | `atl.dll`, COM apartment plumbing, Interop shims |
| **.NET** | Only **two thin shims** | `ControlActivation.exe` (.NET 1.0 licensing UI) + `Interop.MSFrameworkRawLib.dll` (COM→.NET interop) |

**Takeaway:** MetaStock is a long-lived native C++/MFC desktop application. The
code predates modern .NET; .NET appears only at the edges (activation/licensing).

## 2. Runtime dependency map (what `MsWin.exe` links against)

Grouped by purpose — this is effectively the platform's "stack":

**Windows core / UI**
`kernel32 user32 gdi32 advapi32 shell32 comdlg32 comctl32 oledlg uxtheme version`

**Charting / graphics**
`gdiplus` (2D vector), `ddraw ddrawex dciman32 d3dim700` (DirectDraw / legacy
Direct3D immediate-mode — hardware-accelerated chart rendering),
`dtgraphics.dll` (Equis chart drawing), `msvfw32 winmm` (video/multimedia).

**Embedded browser / HTML reports**
`mshtml mshtmled shdocvw shdoclc urlmon wininet vgx msls31 mlang` — the full
Internet Explorer / Trident engine is embedded to render Expert commentary,
help, and HTML/VML reports. Backed by `MSBrowser.dll` + `MSWebResources.dll`.

**Embedded scripting engines**
`vbscript jscript vbajet32 expsrv` — VBScript/JScript + VBA expression service.
Used for scriptable reports/automation alongside the proprietary formula engine.

**Data access**
`msado15 msadrh15 msdart oledb32 oledb32r` (ADO / OLE DB),
`msjetoledb40 msjtes40` + `JETCOMP.exe` (Microsoft **Jet 4.0 / Access** engine —
this is what reads `ST_DATA.MDB`; JETCOMP compacts it),
`msxml4` (XML parsing for `.xml` reports/config),
**`msfl11.dll`** = the proprietary **MetaStock File Library** (native binary
price-history format reader/writer).

**Imaging codecs** — **LEADTOOLS** (3rd-party): `ltkrn61n lfpng61n lfcmp61n`
(kernel, PNG, compression) for chart image export/import.

**Networking / data feed**
`wininet wsock32 rasapi32 sensapi` (HTTP/sockets/dial-up/connectivity),
`eqnotify.dll` (Equis push notifications), `olvi11.dll`, `mtapi.dll`,
`InterbankFX.dll`, `Preferred.dll` (broker/quote-vendor feed adapters).

## 3. Internal code architecture

### 3.1 Process/module decomposition
The product is split into cooperating native binaries (each its own `.exe`/`.dll`):

```
MsWin.exe ........... main app shell (MFC), charts, menus, document model
 ├─ msfl11.dll ...... MetaStock File Library (native price-data format)
 ├─ MSFramework.dll . shared app framework / services
 ├─ MSBrowser.dll ... embedded IE host for HTML commentary/reports
 ├─ dtgraphics.dll .. chart rendering primitives
 └─ External Function DLLs/   (pluggable calculation engines)
     ├─ ppEval.dll .............. MFL formula compiler + evaluator
     ├─ Simulation.dll .......... System Tester / back-test engine
     ├─ DynamicTradingTools.dll . Gann/Fib/Andrews drawing math
     └─ PointFig.dll ............ Point & Figure chart math
```

Companion processes: `Dlwin.exe` (The DownLoader, data management),
`QCenter.exe` (QuoteCenter realtime feed), `Oscope.exe` (OptionScope),
`FormOrg.exe` (formula organizer), `ControlActivation.exe` (.NET licensing).

### 3.2 The `ST_Library` — a textbook layered (MVC) design
The System Tester library (shared between `MsWin.exe` and `Simulation.dll`) is
organized into four clean layers — the clearest window into their engineering style:

```
EQUIS LIBRARIES\ST_Library\
├─ Common\          cross-cutting core
│   ├─ ST_Core.cpp
│   └─ ST_SystemCompiler.cpp      ← compiles trading-system rules (MFL → executable)
├─ Data\            persistence / iteration layer
│   ├─ ST_Database.cpp  ST_DatabaseCompactor.cpp  ST_DataManager.cpp
│   ├─ ST_DBUtility.cpp  ST_RecordsetIterator.cpp  ST_SystemIterator.cpp
├─ Model\           domain objects (the business logic)
│   ├─ ST_Account.cpp     ST_Broker.cpp      ST_Exchange.cpp
│   ├─ ST_Order.cpp       ST_OrderManager.cpp
│   ├─ ST_Position.cpp    ST_PositionStop.cpp
│   ├─ ST_Trader.cpp      ST_Analyst.cpp     ST_Analysis.cpp / ST_AnalysisData.cpp
│   ├─ ST_SimulationSystem.cpp   ST_SimulationContext.cpp
│   ├─ ST_SimulationOptions.cpp  ST_SimulationInspector.cpp
│   ├─ ST_OptimizationVariables.cpp   ST_Dataset.cpp
└─ Presentation\    UI (MFC wizards/sheets/reports)
    ├─ ST_TradingAnalysisWizard.cpp  ST_SystemEditorSheet.cpp
    ├─ ST_GeneralPage.cpp  ST_StopsPage.cpp  ST_BrokerPage.cpp
    ├─ ST_TradeExecutionPage.cpp  ST_MoreTestingOptionsPage.cpp
    ├─ ST_SystemListCtrl.cpp  ST_CleanupWizard.cpp
    └─ ST_StandardReports.cpp  ST_AnalysisReports.cpp
        ST_XMLReport.cpp  ST_XMLReportView.cpp   ← reports emitted as XML→HTML
```

**Design patterns visible from the class names:**
- **Layered / MVC separation:** Data (persistence) ↔ Model (domain) ↔ Presentation (MFC UI). `Common` holds the compiler/core.
- **Domain model of a broker simulation:** `Account`, `Broker`, `Exchange`,
  `Order` + `OrderManager`, `Position` + `PositionStop`, `Trader` — a realistic
  order/position lifecycle, not just "signal on a chart."
- **Iterator pattern** for streaming data: `RecordsetIterator`, `SystemIterator`.
- **Compiler pattern:** `ST_SystemCompiler` turns MFL trading rules into something
  the simulation engine executes bar-by-bar (`SimulationContext`/`SimulationSystem`).
- **Optimizer:** `OptimizationVariables` → parameter sweeps over a system.
- **Reports as XML + an HTML view** (`ST_XMLReport` → rendered through the
  embedded IE engine), decoupling report data from presentation.

### 3.3 The formula engine (`ppEval.dll`)
A standalone native evaluator: parses/compiles MetaStock Formula Language
expressions and evaluates them against in-memory OHLCV arrays. It's a separate
DLL so the same engine powers indicators, the Explorer, Experts, and the
System Tester consistently.

## 4. How it fits together (request flow)

```
 Price data (.dat via msfl11.dll  |  ST_DATA.MDB via Jet/OLEDB)
        │
        ▼
 In-memory OHLCV arrays  ──►  ppEval.dll  (compile + evaluate MFL)
        │                          │
        ▼                          ├─► Charting (dtgraphics + GDI+/DirectDraw)
 ST_Library / Simulation.dll       ├─► Explorer (formula across universe → table)
 (compile system → simulate        └─► Expert Advisor (state → HTML commentary
  orders/positions → XML report)         rendered by embedded IE / MSBrowser)
```

## 5. What this means for the NEPSE platform (a modern re-implementation)

You are (rightly) building the modern equivalent in **Python** — the opened file
`core_analysis/services/support_resistance.py` is exactly the analogue of one
Equis "indicator/analysis" module. Translating MetaStock's engineering choices:

| MetaStock (2010, C++/MFC) | Your stack (modern) |
|---|---|
| Native C++ monolith + plugin DLLs | Python services / modules (`core_analysis/services/*`) |
| `ppEval.dll` MFL engine | pandas + TA-Lib/pandas-ta; optionally a small DSL evaluator |
| `ST_Library` layered MVC | Keep the same split: **Data** (DB/repositories) → **Model/Services** (analysis) → **Presentation** (API/UI) |
| `Simulation.dll` broker model | A back-test engine with explicit `Order`/`Position`/`Account` objects (don't shortcut to "signal arrays" — their domain model is worth copying) |
| Embedded IE for HTML reports | Server-rendered HTML / React + a JSON report schema (their XML-report → view split maps cleanly to JSON-API → frontend) |
| Jet/Access `.MDB` | PostgreSQL / TimescaleDB |
| Per-symbol iterators | Vectorized pandas / batched DB queries |

**The single most reusable idea:** their **layered separation + a rich broker
domain model** (`Account/Order/Position/Trader`) behind a single shared
formula/evaluation engine. Mirror that structure in `core_analysis/` and the
platform will scale the same way MetaStock did, without the 32-bit/MFC baggage.

---
*Reverse-engineered from on-disk MetaStock 11.0 binaries on 2026-06-16. Findings
are inferred from PE headers, import tables, and embedded source-path/PDB strings;
no source code was available.*

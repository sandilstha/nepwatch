# MetaStock Indicators → pandas_ta Mapping (NEPSE platform)

> Full parity checklist for reproducing MetaStock 11.0's ~250 MFL functions in
> this platform. Verified against **pandas_ta 0.4.71b0** (already installed).
> Convention: OHLCV come from `df["open_price_adj"]`, `high_price_adj`,
> `low_price_adj`, `close_price_adj`, `volume`.

## Status legend
- ✅ **pandas_ta** — one call, already available here.
- 🟦 **pandas** — trivial pandas/numpy one-liner (math & utility functions).
- 🟨 **custom** — must hand-code (small): exotic indicators, candlesticks, greeks.
- ⚠️ **needs TA-Lib** — available via `ta.cdl_pattern(...)` only if TA-Lib is installed (it is NOT, currently).

---

## 1. Moving averages & trend
| MetaStock (MFL) | pandas_ta | Status |
|---|---|---|
| `Mov(C,n,S)` simple | `ta.sma(close, length=n)` | ✅ |
| `Mov(C,n,E)` exp | `ta.ema(close, length=n)` | ✅ |
| `Mov(C,n,W)` weighted | `ta.wma(close, length=n)` | ✅ |
| `Mov(C,n,TRI)` triangular | `ta.trima(close, length=n)` | ✅ |
| `Mov(C,n,VAR)` / `VariableMA511` | `ta.vidya(close, length=n)` | ✅ |
| `Dema` | `ta.dema(close, length=n)` | ✅ |
| `Tema` | `ta.tema(close, length=n)` | ✅ |
| `Wilders(C,n)` | `ta.rma(close, length=n)` | ✅ |
| `LinearReg` | `ta.linreg(close, length=n)` | ✅ |
| `LinRegSlope` | `ta.linreg(close, length=n, slope=True)` | ✅ |
| `TSF` (time-series forecast) | `ta.linreg(close, length=n, tsf=True)` | ✅ |
| `ForecastOsc` | from `linreg`: `100*(close-tsf)/close` | 🟦 |

## 2. Momentum / oscillators
| MetaStock | pandas_ta | Status |
|---|---|---|
| `RSI(C,n)` | `ta.rsi(close, length=n)` | ✅ |
| `MACD()` | `ta.macd(close, fast, slow, signal)` | ✅ (already in macd.py) |
| `Stoch(%k,slow)` | `ta.stoch(high, low, close)` | ✅ |
| `StochMomentum` (SMI) | `ta.smi(close)` | ✅ |
| `CCI`/`CCIE` | `ta.cci(high, low, close, length=n)` | ✅ (replace CCI.py hand-loop) |
| `CMO` | `ta.cmo(close, length=n)` | ✅ |
| `MFI(n)` | `ta.mfi(high, low, close, volume, length=n)` | ✅ |
| `WillR(n)` | `ta.willr(high, low, close, length=n)` | ✅ |
| `Mo(C,n)` momentum | `ta.mom(close, length=n)` | ✅ |
| `ROC(C,n,%)` | `ta.roc(close, length=n)` | ✅ |
| `TRIX(C,n)` | `ta.trix(close, length=n)` | ✅ |
| `DPO(n)` | `ta.dpo(close, length=n)` | ✅ |
| `Ult(c1,c2,c3)` Ultimate Osc | `ta.uo(high, low, close)` | ✅ |
| `Qstick(n)` | `ta.qstick(open, close, length=n)` | ✅ |
| `Inertia` | `ta.inertia(close, high, low)` | ✅ |
| `RVI(n)` | `ta.rvi(close, length=n)` | ✅ |
| `PFE` | `ta.pfe(close, length=n)` | ✅ |
| `RMI` | `ta.rsx`/`ta.rsi` w/ momentum param | 🟨 (thin wrapper) |
| `IMI` (intraday momentum) | from O/C: ups/(ups+downs) over n | 🟦 |
| `WillA` (accum/dist) | `ta.willr` family / custom | 🟨 |

## 3. Directional / trend strength
| MetaStock | pandas_ta | Status |
|---|---|---|
| `ADX(n)` / `ADXR` | `ta.adx(high, low, close, length=n)` → `ADX_n`,`DMP_n`,`DMN_n` | ✅ (already in CCI.py) |
| `PDI`/`MDI`/`DX`/`DMI` | columns of `ta.adx(...)` | ✅ |
| `Aroon` Up/Down | `ta.aroon(high, low, length=n)` | ✅ |
| `VHF` | `ta.vhf(close, length=n)` | ✅ |
| `Mass` (mass index) | `ta.massi(high, low)` | ✅ |
| `CSI` (commodity selection) | from ADX+ATR | 🟨 |
| `RWIH`/`RWIL` (random walk) | — | 🟨 (custom, simple) |

## 4. Volatility / bands / channels
| MetaStock | pandas_ta | Status |
|---|---|---|
| `ATR(n)` | `ta.atr(high, low, close, length=n)` | ✅ |
| `Std`/`Stdev(C,n)` | `ta.stdev(close, length=n)` or `close.rolling(n).std()` | ✅/🟦 |
| `BBandTop`/`BBandBot` | `ta.bbands(close, length=n, std=k)` | ✅ |
| `PriceChannelHigh/Low` | `ta.donchian(high, low, ...)` | ✅ |
| `SAR(step,max)` parabolic | `ta.psar(high, low, close)` | ✅ |
| `STEBandTop/Bot` (std-error) | from `linreg` + std-error | 🟨 |
| `ProjBandTop/Bot`, `ProjOsc` | linear-projection bands | 🟨 |

## 5. Volume / money flow
| MetaStock | pandas_ta | Status |
|---|---|---|
| `OBV` | `ta.obv(close, volume)` | ✅ |
| `AD` (accum/dist) | `ta.ad(high, low, close, volume)` | ✅ |
| `CO` (Chaikin osc) | `ta.adosc(high, low, close, volume)` | ✅ |
| `CMF(n)` | `ta.cmf(high, low, close, volume, length=n)` | ✅ |
| `PVT` | `ta.pvt(close, volume)` | ✅ |
| `NVI` / `PVI` | `ta.nvi(...)` / `ta.pvi(...)` | ✅ |
| `EMV` (ease of movement) | `ta.eom(high, low, close, volume)` | ✅ |
| `KVO` (Klinger) | `ta.kvo(high, low, close, volume)` | ✅ |
| `VolO` (volume osc) | `ta.pvo(volume)` | ✅ |
| `MarketFacIndex` (Bill Williams) | `(high-low)/volume` | 🟦 |
| `TVI`, `HPI`, `DI` (demand index) | — | 🟨 (custom, niche) |

## 6. Ichimoku / swing / zigzag
| MetaStock | pandas_ta | Status |
|---|---|---|
| `TenkanSen`,`KijunSen`,`SenkouSpanA/B`,`ChikouSpan` | `ta.ichimoku(high, low, close)` (returns all 5 lines) | ✅ |
| `Zig(C,chg,%)` | `ta.zigzag(high, low, close)` | ✅ |
| `Peak`/`Trough` (+Bars) | derive from `ta.zigzag` pivots | 🟦 |
| `Swing`/`ASwing` (Wilder swing index) | — | 🟨 (custom) |

## 7. Cycle / statistical / math
| MetaStock | pandas_ta / pandas | Status |
|---|---|---|
| `Correl`/`Corr` | `close.rolling(n).corr(other)` | 🟦 |
| `RSquared` | `corr**2` over rolling window | 🟦 |
| `Var(C,n)` | `close.rolling(n).var()` | 🟦 |
| `FFT` | `numpy.fft` | 🟦 |
| `MESASineWave`/`MESALeadSine` | Ehlers MESA — `ta.ebsw` (approx) or custom | 🟨 |
| `Divergence` | custom (compare two series' pivots) | 🟨 |

## 8. Reference / time-series / logic (the MFL "glue")
All of these are **pure pandas** — they're how MetaStock formulas are wired, and
map to vectorized pandas idioms (no library needed):
| MFL | pandas | 
|---|---|
| `Ref(x,-n)` / `Ref(x,n)` | `x.shift(n)` / `x.shift(-n)` |
| `HHV(x,n)` / `LLV(x,n)` | `x.rolling(n).max()` / `.min()` |
| `HHVBars`/`LLVBars` | `n-1 - x.rolling(n).apply(np.argmax/argmin)` |
| `Sum(x,n)` | `x.rolling(n).sum()` |
| `Cum(x)` | `x.cumsum()` |
| `BarsSince(cond)` | groupby-cumcount since last True |
| `ValueWhen(nth,cond,x)` | `x.where(cond).ffill()` (nth via shift) |
| `Cross(a,b)` | `(a>b) & (a.shift()<=b.shift())` |
| `If(cond,a,b)` | `np.where(cond,a,b)` |
| `Highest/Lowest(x)` | `x.cummax()` / `x.cummin()` |
| `Alert(cond,n)` | `cond.rolling(n).max().astype(bool)` |
| `Abs/Log/Exp/Sqrt/Round/Int/...` | numpy ufuncs |
| `Year/Month/DayOfWeek/...` | `df.business_date.dt.*` |

## 9. Candlestick patterns — ⚠️ the one real gap
MetaStock's ~45 recognizers (`Doji`, `Hammer`, `HangingMan`, `EngulfingBull/Bear`,
`Harami`, `MorningStar`/`EveningStar`, `DarkCloud`, `PiercingLine`, `ShootingStar`,
`SpinningTop`, `Tweezer*`, `Big/Shaven*`, `Inside`/`Outside`, etc.) map to
`ta.cdl_pattern(open, high, low, close, name=...)` — **but that delegates to
TA-Lib, which is not installed here.**

Two options:
- **Install TA-Lib** (Windows: `pip install TA-Lib` needs the prebuilt wheel /
  `ta-lib` binaries) → unlocks all 60+ candlestick recognizers at once.
- **Hand-code the dozen you actually use** — each is a 1–3 line boolean on
  O/H/L/C (e.g. `Doji = abs(close-open) <= (high-low)*0.1`). Lowest-friction if
  you only need the common ones.

## 10. Options greeks — 🟨 custom
`Option`, `Delta`, `Gamma`, `Theta`, `Vega`, `Life`, `OptionExp` (Black-Scholes).
Not in pandas_ta. Use a small `scipy.stats.norm`-based Black-Scholes module, or
`py_vollib`/`mibian` if you want a library. Only relevant if you add options.

---

## Coverage summary
| Bucket | Count (approx) | How |
|---|---|---|
| Direct `pandas_ta` call | ~45 core indicators | ✅ available now |
| Pure pandas one-liners | ~40 (math + MFL glue) | 🟦 no dep |
| Custom (exotic indicators) | ~12 | 🟨 small effort |
| Candlestick patterns | ~45 | ⚠️ TA-Lib or hand-code |
| Options greeks | ~7 | 🟨 Black-Scholes module |

**Bottom line:** ~85% of MetaStock's indicator surface is one `pandas_ta` call or
one pandas line — and pandas_ta is already installed. The only meaningful add-on
decision is whether to install **TA-Lib** for the candlestick library.

## Recommended cleanup of existing services
- `CCI.py` — replace the hand-written typical-price/MAD loop with `ta.cci(...)`
  (identical math, vectorized, much faster).
- `RSI_SMA.py` — `_compute_rsi` can become `ta.rsi(close, length)`; keep the
  SMA-of-RSI + crossover logic.
- `moving_average.py` — back all MA types with `ta.sma/ema/wma/trima`.
- Keep `support_resistance.py`, `strategy_tester.py`, `advanced_market_structure.py`
  as-is — those are geometric/structural, not standard indicators.

---
*Verified against pandas_ta 0.4.71b0 on 2026-06-16. MetaStock function list
extracted from `MS11sntx.dta`.*

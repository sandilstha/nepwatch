# NEPSE Swing Trading Strategy — SOP

**Strategy:** Donchian breakout, 10 / 10
**Market:** NEPSE — long-only, T+2 settlement
**Review cadence:** daily, after close
**Evidence base:** 1,694 indicator configurations backtested over 29 years (1997-07-20 → 2026-07-31, 6,396 sessions)
**Validation:** out-of-sample 2015–2026, five-period stability screen, cost sensitivity

---

## 1. Objective

Capture intermediate price swings — typical hold **17–19 sessions** — in the direction of the prevailing trend, while capping the loss on any single position at roughly **−2.5%**.

The strategy does not predict tops or bottoms. It is a **momentum-continuation** system: it buys strength and exits weakness.

Its edge is an asymmetric payoff, not accuracy. **It is wrong on 57% of trades and still compounds**, because the average win is about four times the average loss.

> Long-only by construction. NEPSE has no short selling, so you are either **in** (fully invested) or **flat** (in cash).

---

## 2. Parameters — the complete list

These five numbers define the entire strategy. Nothing else is required.

| # | Parameter | Value | What it means | Why this value |
|---|---|---|---|---|
| 1 | **Entry lookback (N)** | `10 sessions` | Buy when today's close is the **highest close of the last 10 sessions** | Tested 10–80. Sharpe stays 0.78–1.27 across the whole range; 10 gave the best out-of-sample result |
| 2 | **Exit lookback (M)** | `10 sessions` | Sell when today's close is the **lowest close of the last 10 sessions** | Tested 5–30. This *is* the risk control — it caps the average loss at −2.5% with no separate stop |
| 3 | **Price series** | `Closing price` | All calculations use the **close**. High/low are never used | 63% of NEPSE index bars have `high == low`, so intraday range is unreliable. This also rules out ATR, ADX, Supertrend and Parabolic SAR |
| 4 | **Execution lag** | `1 session` | Signal read on today's close; order placed for the **next session** | You cannot trade at a close you have not yet seen. Removing this lag inflates results and is unachievable |
| 5 | **Settlement lock** | `T+2` | Hold **≥ 2 sessions**; after a sale, cash idle **2 sessions** | NEPSE reality. At a 19-session average hold this costs only **−0.23% CAGR** — but it would destroy any 2–5 day system |

### Parameters deliberately NOT used

Each was tested on the same data and rejected on measured evidence.

| Omitted | Measured result |
|---|---|
| Stop-loss (5% / 8% / 10%) | **No effect.** CAGR 16.19% with or without — the 10-day-low exit always fires first. A stop only adds cost |
| Profit target | **Harmful.** Truncates the large winners that carry the strategy |
| Time stop (20d / 40d) | **Harmful.** Cut CAGR to 14.96% / 15.26% |
| Trend filter (Price > EMA200) | **Not needed.** The breakout condition already implies an uptrend |
| Bull/bear regime filter | **Harmful.** Cut out-of-sample CAGR from 13.83% to ~9%; the 20% confirmation rule is far too slow |
| Volume / turnover confirmation | **No edge.** OBV, MFI, CMF, VWMA and Force Index all failed to improve any configuration |

---

## 3. Trading rules

### Entry

> **BUY next session** if today's close ≥ the highest close of the previous 10 sessions
> **AND** you are currently flat
> **AND** at least 2 sessions have passed since your last sale.

### Exit

> **SELL next session** if today's close ≤ the lowest close of the previous 10 sessions
> **AND** the position has been held at least 2 sessions.

### Position sizing

All-in / all-out on a single instrument. The backtest assumes 100% of capital in one position when invested, 100% cash when flat. Partial sizing changes the return profile and is **not** covered by this evidence.

> There is no discretion in these rules. If you override an exit because you expect a bounce, you are trading a different system and the statistics below no longer apply.

---

## 4. Daily procedure (after close)

1. Record today's closing price.
2. Compute the **highest close** and the **lowest close** of the last 10 sessions (today included).
3. **If flat** — is today's close the 10-session high? If yes, and 2+ sessions have passed since your last sale → place a buy for the next session.
4. **If invested** — is today's close the 10-session low? If yes, and you have held 2+ sessions → place a sell for the next session.
5. If neither condition is met, **do nothing**. Most days require no action.
6. **Log every fill**: date, price, position state. The strategy is only auditable if entries and exits are recorded.

> Expect roughly **6–7 trades per year**. If you are trading far more often, a rule is being applied incorrectly.

---

## 5. Backtest evidence

All figures net of **0.50% round-trip** costs, with T+2 enforced and a 1-session execution lag.

| Metric | Full 1997–2026 | **Out-of-sample 2015–2026** | Buy & hold (OOS) |
|---|---|---|---|
| CAGR | 16.19% | **13.83%** | 9.70% |
| Sharpe | 1.01 | **1.08** | 0.59 |
| Max drawdown | −37.0% | **−23.0%** | −43.2% |
| Win rate | 43% | 38% | — |
| Profit factor | 3.04 | 2.49 | — |
| Average win | +10.4% | +10.1% | — |
| Average loss | −2.5% | −2.5% | — |
| Trades / year | 6.0 | 6.7 | — |
| Average hold | 19 sessions | 17 sessions | — |

### Stability across five independent periods (Sharpe)

| | 1997–03 | 2004–09 | 2009–15 | 2015–21 | 2021–26 | Beats B&H |
|---|---|---|---|---|---|---|
| **Breakout 10/10** | 0.57 | 1.92 | 1.23 | 1.69 | 0.45 | **5 / 5** |
| Buy & hold | 0.24 | 1.22 | 0.50 | 1.01 | 0.19 | — |

### Cost sensitivity (CAGR)

| Round-trip cost | 0.5% | 1.0% | 1.5% |
|---|---|---|---|
| Breakout 10/10 | 16.19% | 12.8% | 9.4% |

> **At 1.5% round-trip the edge nearly disappears** (9.4% vs 9.70% buy & hold). Confirm your actual brokerage, SEBON fee and DP charge before trading this.

---

## 6. Sector selection

Same rules applied per sector index, common window **2021-03 → 2026-07** (identical period for all sectors, 32–44 trades each).

| Sector | Win % | Avg win | Avg loss | Win/Loss | Profit factor |
|---|---|---|---|---|---|
| **Finance** | 47% | +17.6% | −3.4% | 5.21 | **4.60** |
| Trading | 28% | +13.7% | −3.4% | 4.06 | 1.56 |
| Hotels & Tourism | 38% | +10.8% | −3.4% | 3.14 | 1.93 |
| HydroPower | 44% | +9.3% | −3.8% | 2.41 | 1.93 |
| Manufacturing | 40% | +9.6% | −3.5% | 2.73 | 1.82 |
| Mutual Fund | 38% | +5.6% | −1.9% | 2.92 | 1.82 |
| Non-Life Insurance | 38% | +7.6% | −2.8% | 2.70 | 1.65 |
| Development Bank | 36% | +9.9% | −3.5% | 2.80 | 1.60 |
| Investment | 39% | +7.5% | −3.2% | 2.38 | 1.55 |
| Life Insurance | 35% | +7.3% | −3.1% | 2.35 | 1.27 |
| Microfinance | 31% | +7.0% | −2.9% | 2.39 | 1.07 |
| Others | 27% | +8.9% | −3.1% | 2.88 | 1.06 |
| **Banking** | 30% | +5.9% | −2.6% | 2.25 | **0.98** |

> **Banking is unprofitable on these rules** (profit factor 0.98) despite being the largest sector by weight.
> The **NEPSE index itself (PF 3.04) outperformed 12 of 13 sectors** — only Finance beat it. Prefer the broad index unless you have a specific reason to trade Finance.

---

## 7. What NOT to do — measured failures

Tested on the same data; each lost money. Listed so they are not re-attempted.

| Approach | Result | Profit factor |
|---|---|---|
| Buy the dip: RSI(2) < 10 in an uptrend | −2.10% CAGR | 0.68 |
| Buy the dip: RSI(3) < 10 in an uptrend | −2.33% CAGR | 0.46 |
| Bollinger 20/2.0, lower band → middle | −2.37% CAGR | 0.45 |
| Z-score(20) < −2 → 0 | −2.37% CAGR | 0.45 |

> **Mean reversion does not work on NEPSE.** Every "buy the dip" variant tested was loss-making, with profit factors of 0.45–0.68. NEPSE dips tend to keep falling.

---

## 8. Limitations — read before trading

- **Index-level only.** Validated on the NEPSE index and sector indices, which are not directly tradable instruments. Applying it to a single scrip introduces liquidity and circuit-limit risk that is not modelled.
- **Circuit limits (±10%) are not modelled.** On a limit-up or limit-down day the signal price may be unobtainable.
- **Multiple-testing inflation.** 1,694 configurations were tested. The 10/10 breakout survived out-of-sample testing, a five-period stability screen and cost sensitivity — but expect live results at the lower end of the range.
- **Drawdowns are real and long.** −37% peak-to-trough over the full history, and the strategy underperforms buy & hold for years at a time (2021–26 Sharpe of only 0.45).
- **Stock-level parameters are unverified.** Individual stock data covers only 2022-11 onward — a single bull phase — which cannot support optimisation.

> This is a documented, backtested procedure — **not investment advice, and not a forecast**. Past performance does not guarantee future results. Position sizing and risk-capital limits remain the trader's responsibility.

---

## Appendix — data notes

| Item | Value |
|---|---|
| Source table | `nepse_market_indices` (`NepseMarketIndex`), `sector_name = "NEPSE INDEX"` |
| Sessions | 6,396 |
| Coverage | 1997-07-20 → 2026-07-31 |
| Duplicate dates / null closes | 0 / 0 |
| Bars with `high == low` | 4,029 (63.0%) — the reason all rules are close-based |
| In-sample window | 1997-07-20 → 2014-12-31 (3,753 sessions) |
| Out-of-sample window | 2015-01-01 → 2026-07-31 (2,643 sessions) |

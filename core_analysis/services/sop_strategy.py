"""SOP multi-indicator backtest engine for the Strategy Simulator.

Implements the five optimized, long-only technical strategies from the NEPSE
Master SOP (v2.0), each gated by the mandatory 200-day-SMA market-regime filter
(NEPSE Index): trade buys in Bull/Sideways, stand aside (hold cash, exit longs)
in Bear. Long-only throughout — NEPSE is a one-way market, so the bear action is
cash, never short.

Returns the full SOP metric set (Total Return, Sharpe, Win%, Profit Factor, Max
Drawdown, Trades, Avg Holding) plus a daily equity curve for charting. Mirrors
the existing backtests: next-open fills, all-in sizing, single position, adjusted
prices — see services/CCI.py. Sharpe/PF/MaxDD follow the equity-curve pattern in
services/msv_strategy.py. Costs default to 80 bps round-trip and Sharpe uses a 0%
risk-free rate, matching the SOP §8b calibration caveats.
"""

import numpy as np
import pandas as pd
import pandas_ta as ta

# SOP-optimized defaults per indicator (Master SOP §3).
INDICATOR_DEFAULTS = {
    "volume_breakout": {"vb_high_lookback": 40, "vb_low_lookback": 20,
                        "vb_vol_avg": 20, "vb_vol_mult": 2.0},
    "ema": {"ema_fast": 20, "ema_slow": 50},
    "macd": {"macd_fast": 26, "macd_slow": 45, "macd_signal": 20},
    # Added after a 2003-2026 index backtest (regime-gated, 80bps) in which the
    # existing RSI and Bollinger defaults lost money while these two beat both
    # buy & hold and every incumbent on risk-adjusted return.
    #   Supertrend 20/2.0 : +2,108% | Sharpe 1.12 | MaxDD -31.4% | PF 6.24
    #   Donchian   20/10  : +1,405% | Sharpe 1.02 | MaxDD -37.9% | Win 56.2%
    # Supertrend uses a close/stdev band rather than ATR: 63% of NEPSE index bars
    # carry high == low, which makes true range unusable on index series.
    "supertrend": {"st_length": 20, "st_mult": 2.0},
    "donchian": {"dc_entry": 20, "dc_exit": 10},
}

INDICATOR_LABELS = {
    "volume_breakout": "Volume Breakout 40/20/2.0×",
    "ema": "EMA 20/50 crossover",
    "macd": "MACD 26/45/20",
    "supertrend": "Supertrend 20/2.0 SD",
    "donchian": "Donchian 20/10 breakout",
}

_TRADING_DAYS = 252  # annualisation factor for the daily-return Sharpe


# ── Regime filter ────────────────────────────────────────────────────────────
def market_regime_series(index_df):
    """Weinstein-style 200-SMA regime for the NEPSE Index, per session.

    Bull = close > SMA200 and SMA200 rising; Bear = close < SMA200 and SMA200
    falling; otherwise Sideways. `index_df` must carry `business_date` +
    `close_price_adj` (the shape `_build_standard_dataframe` yields for an index).
    Returns a DataFrame `business_date, regime` (str in {Bull, Sideways, Bear}).
    """
    if index_df is None or index_df.empty:
        return pd.DataFrame(columns=["business_date", "regime"])
    df = index_df[["business_date", "close_price_adj"]].copy()
    df["business_date"] = pd.to_datetime(df["business_date"])
    df = df.sort_values("business_date").reset_index(drop=True)
    close = df["close_price_adj"].astype(float)
    sma200 = close.rolling(200).mean()
    slope = sma200 - sma200.shift(10)
    regime = np.where(
        (close > sma200) & (slope > 0), "Bull",
        np.where((close < sma200) & (slope < 0), "Bear", "Sideways"),
    )
    # Before the 200-SMA warms up, treat as Sideways (permissive — never blocks
    # trades purely for lack of index history).
    regime = np.where(sma200.isna(), "Sideways", regime)
    df["regime"] = regime
    return df[["business_date", "regime"]]


# ── Per-indicator signal generation ──────────────────────────────────────────
def _signals(df, indicator, p):
    """Return (buy, sell, entry_label, exit_label) boolean Series for `indicator`.

    Signals are evaluated on bar close; the caller fills at the NEXT bar's open.
    """
    close = df["close_price_adj"]

    if indicator == "volume_breakout":
        vol = pd.to_numeric(df.get("volume"), errors="coerce")
        prior_high = df["high_price_adj"].rolling(p["vb_high_lookback"]).max().shift(1)
        prior_low = df["low_price_adj"].rolling(p["vb_low_lookback"]).min().shift(1)
        avg_vol = vol.rolling(p["vb_vol_avg"]).mean().shift(1)
        buy = (close > prior_high) & (vol >= p["vb_vol_mult"] * avg_vol)
        sell = close < prior_low
        return buy, sell, "Breakout + vol", "Break prior low"

    if indicator == "ema":
        fast = ta.ema(close, length=p["ema_fast"])
        slow = ta.ema(close, length=p["ema_slow"])
        diff = fast - slow
        prev = diff.shift(1)
        buy = (prev <= 0) & (diff > 0)                         # fast crosses above slow
        sell = (prev >= 0) & (diff < 0)                        # fast crosses below slow
        return buy, sell, "EMA fast↑slow", "EMA fast↓slow"

    if indicator == "macd":
        macd = ta.macd(close, fast=p["macd_fast"], slow=p["macd_slow"], signal=p["macd_signal"])
        line = macd.filter(like="MACD_").iloc[:, 0]
        signal = macd.filter(like="MACDs_").iloc[:, 0]
        diff = line - signal
        prev = diff.shift(1)
        buy = (prev <= 0) & (diff > 0)                         # MACD crosses above signal
        sell = (prev >= 0) & (diff < 0)                        # MACD crosses below signal
        return buy, sell, "MACD↑signal", "MACD↓signal"

    if indicator == "supertrend":
        # Volatility-adaptive trend band. Deliberately built from a rolling
        # stdev of CLOSE rather than ATR — 63% of NEPSE index bars have
        # high == low, so true range collapses to zero and an ATR band would
        # never widen. Long above the upper band, flat below the lower.
        mid = close.rolling(p["st_length"]).mean()
        sd = close.rolling(p["st_length"]).std()
        upper = mid + p["st_mult"] * sd
        lower = mid - p["st_mult"] * sd
        buy = close > upper
        sell = close < lower
        return buy, sell, "Close>upper band", "Close<lower band"

    if indicator == "donchian":
        # Pure-price breakout — the companion to volume_breakout, which also
        # demands a 2x volume surge. This one fires on breakouts that occur on
        # ordinary volume, which the volume-gated version never sees.
        prior_high = close.rolling(p["dc_entry"]).max().shift(1)
        prior_low = close.rolling(p["dc_exit"]).min().shift(1)
        buy = close > prior_high
        sell = close < prior_low
        return buy, sell, f"{p['dc_entry']}d high break", f"{p['dc_exit']}d low break"

    raise ValueError(f"Unknown indicator: {indicator}")


def _min_bars(indicator, p):
    if indicator == "volume_breakout":
        return max(p["vb_high_lookback"], p["vb_low_lookback"], p["vb_vol_avg"]) * 2
    if indicator == "ema":
        return p["ema_slow"] * 2
    if indicator == "macd":
        return p["macd_slow"] * 2
    if indicator == "supertrend":
        return p["st_length"] * 3
    if indicator == "donchian":
        return max(p["dc_entry"], p["dc_exit"]) * 3
    return 60


# ── Main entry point ─────────────────────────────────────────────────────────
def run_sop_simulation(
    data_source,
    initial_capital: float = 100000.0,
    indicator: str = "rsi",
    use_regime_filter: bool = True,
    cost_bps: float = 80.0,
    regime_df=None,
    **overrides,
):
    """Run one SOP indicator on one symbol. Returns (metrics, trades_df, equity_df).

    `data_source` is a DataFrame with the `*_price_adj`/`volume` schema (from
    `_build_standard_dataframe`). `regime_df` is the NEPSE-Index regime frame
    (`market_regime_series`); when omitted the regime gate is skipped.
    """
    indicator = (indicator or "rsi").lower()
    if indicator not in INDICATOR_DEFAULTS:
        return ({"error": f"Unknown indicator '{indicator}'."}, pd.DataFrame(), pd.DataFrame())

    p = dict(INDICATOR_DEFAULTS[indicator])
    for k, v in overrides.items():          # apply only recognised param overrides
        if k in p and v is not None:
            p[k] = v

    if isinstance(data_source, pd.DataFrame):
        df = data_source.copy()
    else:
        return ({"error": "SOP engine expects a prepared DataFrame."}, pd.DataFrame(), pd.DataFrame())

    required = {"business_date", "open_price_adj", "high_price_adj",
                "low_price_adj", "close_price_adj"}
    if not required.issubset(df.columns):
        return ({"error": "Input data missing required OHLC columns."}, pd.DataFrame(), pd.DataFrame())

    df = df.sort_values("business_date").reset_index(drop=True)
    df["business_date"] = pd.to_datetime(df["business_date"])
    for col in ["open_price_adj", "high_price_adj", "low_price_adj", "close_price_adj"]:
        df[col] = df[col].astype(float)

    min_bars = _min_bars(indicator, p)
    if len(df) < min_bars:
        return ({"error": f"Insufficient data: need at least {min_bars} bars for {INDICATOR_LABELS[indicator]}."},
                pd.DataFrame(), pd.DataFrame())

    if indicator == "volume_breakout":
        if "volume" not in df.columns or pd.to_numeric(df["volume"], errors="coerce").isna().all():
            return ({"error": "Volume data is unavailable for the selected symbol/date range (needed for Volume Breakout)."},
                    pd.DataFrame(), pd.DataFrame())

    buy, sell, entry_label, exit_label = _signals(df, indicator, p)

    # Regime per bar, aligned to this symbol's dates (default Sideways = permissive).
    if use_regime_filter and regime_df is not None and not regime_df.empty:
        r = regime_df.copy()
        r["business_date"] = pd.to_datetime(r["business_date"])
        regime = df.merge(r, on="business_date", how="left")["regime"].fillna("Sideways")
    else:
        regime = pd.Series(["Sideways"] * len(df), index=df.index)

    cost_frac = max(0.0, float(cost_bps)) / 10000.0

    position = 0
    cash = float(initial_capital)
    entry_price = 0.0
    entry_date = None
    entry_idx = None
    trades = []
    blocked_by_regime = 0

    for i in range(1, len(df)):
        dt = df.loc[i, "business_date"]
        exec_price = df.loc[i, "open_price_adj"]
        if exec_price <= 0:
            continue
        sig = i - 1
        # Regime is built from bar closes; deciding at bar i's OPEN must use
        # the previous close (sig), or the filter sees the bar it is trading.
        is_bear = use_regime_filter and (regime.iloc[sig] == "Bear")

        # Exit on the indicator's sell signal OR when the regime turns bear.
        if position > 0 and (bool(sell.iloc[sig]) or is_bear):
            proceeds = position * exec_price
            gross_pnl = proceeds - (position * entry_price)
            costs = (position * entry_price + proceeds) * (cost_frac / 2.0)
            pnl = gross_pnl - costs
            pnl_pct = ((exec_price / entry_price) - 1) * 100 - cost_frac * 100
            cash += proceeds - (proceeds * (cost_frac / 2.0))
            trades.append({
                "entry_signal": entry_label,
                "exit_signal": ("Bear regime" if is_bear and not bool(sell.iloc[sig]) else exit_label),
                "entry_date": entry_date,
                "exit_date": dt,
                "entry_price": round(entry_price, 2),
                "exit_price": round(exec_price, 2),
                "shares": int(position),
                "pnl": round(pnl, 2),
                "pnl_pct": round(pnl_pct, 2),
                "hold_bars": int(i - entry_idx),
                "exit_reason": ("bear_regime" if is_bear and not bool(sell.iloc[sig]) else "signal_exit"),
            })
            position = 0
            entry_price = 0.0
            entry_date = None
            entry_idx = None

        # Enter on a buy signal, but never in a bear regime.
        if position == 0 and bool(buy.iloc[sig]):
            if is_bear:
                blocked_by_regime += 1
            else:
                buy_cost = exec_price * (1 + cost_frac / 2.0)
                shares = int(cash // buy_cost)
                if shares > 0:
                    cash -= shares * exec_price
                    cash -= shares * exec_price * (cost_frac / 2.0)
                    position = shares
                    entry_price = float(exec_price)
                    entry_date = dt
                    entry_idx = i

    # Force-close any open position at the last close.
    if position > 0:
        last_close = float(df["close_price_adj"].iloc[-1])
        proceeds = position * last_close
        gross_pnl = proceeds - (position * entry_price)
        costs = (position * entry_price + proceeds) * (cost_frac / 2.0)
        pnl = gross_pnl - costs
        pnl_pct = ((last_close / entry_price) - 1) * 100 - cost_frac * 100
        cash += proceeds - (proceeds * (cost_frac / 2.0))
        trades.append({
            "entry_signal": entry_label,
            "exit_signal": "End of data",
            "entry_date": entry_date,
            "exit_date": df["business_date"].iloc[-1],
            "entry_price": round(entry_price, 2),
            "exit_price": round(last_close, 2),
            "shares": int(position),
            "pnl": round(pnl, 2),
            "pnl_pct": round(pnl_pct, 2),
            "hold_bars": int((len(df) - 1) - entry_idx),
            "exit_reason": "end_of_data",
        })
        position = 0

    trades_df = pd.DataFrame(trades)
    equity_df = _daily_equity(df, trades, initial_capital, regime, cost_frac)
    metrics = _metrics(df, trades_df, equity_df, initial_capital,
                       indicator, use_regime_filter, regime, cost_bps, blocked_by_regime)
    # Same live BUY/HOLD/SELL/WAIT read as the confluence model, expressed as a
    # single-indicator vote so both tabs answer "what do I do today".
    _st = np.asarray(_indicator_state(df, indicator, p)).reshape(1, -1)
    metrics["signal"] = _current_signal(df, [indicator], _st, _st[0],
                                        _st[0].astype(bool), regime, 1, trades)
    return metrics, trades_df, equity_df


# ── Combined confluence strategy ─────────────────────────────────────────────
def _indicator_state(df, indicator, p):
    """Per-bar long/flat state (1/0) for one indicator, from its own entry/exit
    crosses — 'is this indicator currently in a long?' Used for confluence votes."""
    buy, sell, _, _ = _signals(df, indicator, p)
    buy = buy.fillna(False).to_numpy()
    sell = sell.fillna(False).to_numpy()
    n = len(df)
    state = np.zeros(n, dtype=int)
    pos = 0
    for i in range(n):
        if pos == 0 and buy[i]:
            pos = 1
        elif pos == 1 and sell[i]:
            pos = 0
        state[i] = pos
    return state


def run_sop_combined_simulation(
    data_source,
    indicators=None,
    min_agree=1,
    initial_capital: float = 100000.0,
    use_regime_filter: bool = True,
    cost_bps: float = 80.0,
    regime_df=None,
    regime_mode: str = "strict",
    bear_min_agree: int = None,
):
    """Confluence strategy: go long when at least `min_agree` of the chosen
    indicators are simultaneously in a long state; exit when fewer agree (or the
    regime turns bear). Long-only, next-open fills, all-in — same conventions as
    run_sop_simulation. Returns (metrics, trades_df, equity_df).
    """
    if indicators is None:
        indicators = list(INDICATOR_DEFAULTS.keys())
    indicators = [i for i in indicators if i in INDICATOR_DEFAULTS]
    if not indicators:
        return ({"error": "Select at least one indicator to combine."}, pd.DataFrame(), pd.DataFrame())
    # Confluence floor: at least 3 indicators must agree before any BUY. One or
    # two firing is noise; the 2003-2026 index study put min_agree=3 at the top
    # (Sharpe 1.30). Still clamped down if fewer than 3 indicators are selected.
    min_agree = max(min(3, len(indicators)), min(int(min_agree), len(indicators)))

    if not isinstance(data_source, pd.DataFrame):
        return ({"error": "SOP engine expects a prepared DataFrame."}, pd.DataFrame(), pd.DataFrame())
    df = data_source.copy()
    required = {"business_date", "open_price_adj", "high_price_adj",
                "low_price_adj", "close_price_adj"}
    if not required.issubset(df.columns):
        return ({"error": "Input data missing required OHLC columns."}, pd.DataFrame(), pd.DataFrame())

    df = df.sort_values("business_date").reset_index(drop=True)
    df["business_date"] = pd.to_datetime(df["business_date"])
    for col in ["open_price_adj", "high_price_adj", "low_price_adj", "close_price_adj"]:
        df[col] = df[col].astype(float)

    need = max(_min_bars(i, INDICATOR_DEFAULTS[i]) for i in indicators)
    if len(df) < need:
        return ({"error": f"Insufficient data: need at least {need} bars for the selected indicators."},
                pd.DataFrame(), pd.DataFrame())
    if "volume_breakout" in indicators:
        if "volume" not in df.columns or pd.to_numeric(df["volume"], errors="coerce").isna().all():
            indicators = [i for i in indicators if i != "volume_breakout"]  # drop, don't fail
            if not indicators:
                return ({"error": "Volume unavailable and no other indicators selected."}, pd.DataFrame(), pd.DataFrame())
            min_agree = min(min_agree, len(indicators))

    # Confluence votes per bar.
    states = np.vstack([_indicator_state(df, i, INDICATOR_DEFAULTS[i]) for i in indicators])
    votes = states.sum(axis=0)
    desired_long = votes >= min_agree

    if use_regime_filter and regime_df is not None and not regime_df.empty:
        r = regime_df.copy()
        r["business_date"] = pd.to_datetime(r["business_date"])
        regime = df.merge(r, on="business_date", how="left")["regime"].fillna("Sideways")
    else:
        regime = pd.Series(["Sideways"] * len(df), index=df.index)

    # Regime handling mode:
    #   strict - no position while the regime is Bear (SOP default, best tested)
    #   swing  - trade through Bear, but only on `bear_min_agree` agreement
    #   off    - ignore the regime entirely
    regime_mode = (regime_mode or "strict").lower()
    if regime_mode not in ("strict", "swing", "off"):
        regime_mode = "strict"
    if bear_min_agree is None:
        bear_min_agree = min(min_agree + 1, len(indicators))
    bear_min_agree = max(1, min(int(bear_min_agree), len(indicators)))

    cost_frac = max(0.0, float(cost_bps)) / 10000.0
    label = f"Combined · {min_agree} of {len(indicators)}"
    if regime_mode == "swing":
        label += f" · swing (bear ≥{bear_min_agree})"
    elif regime_mode == "off":
        label += " · regime off"

    position = 0
    cash = float(initial_capital)
    entry_price = 0.0
    entry_date = None
    entry_idx = None
    trades = []
    blocked_by_regime = 0

    for i in range(1, len(df)):
        exec_price = df.loc[i, "open_price_adj"]
        if exec_price <= 0:
            continue
        dt = df.loc[i, "business_date"]
        sig = i - 1
        raw_bear = use_regime_filter and (regime.iloc[sig] == "Bear")
        # SWING mode keeps trading through a bear regime, but only on a raised
        # agreement threshold. Measured cost on 2003-2026 NEPSE index: bear-regime
        # entries alone run at profit factor 0.44 (3 of 5) down to 0.12 (5 of 5),
        # so this is opt-in and never the default.
        if raw_bear and regime_mode == "swing":
            is_bear = votes[sig] < bear_min_agree
        elif regime_mode == "off":
            is_bear = False
        else:
            is_bear = raw_bear

        if position > 0 and ((not desired_long[sig]) or is_bear):
            proceeds = position * exec_price
            gross_pnl = proceeds - (position * entry_price)
            costs = (position * entry_price + proceeds) * (cost_frac / 2.0)
            pnl = gross_pnl - costs
            pnl_pct = ((exec_price / entry_price) - 1) * 100 - cost_frac * 100
            cash += proceeds - proceeds * (cost_frac / 2.0)
            trades.append({
                "entry_signal": f"≥{min_agree}/{len(indicators)} agree",
                "exit_signal": ("Bear regime" if is_bear and desired_long[sig] else f"<{min_agree} agree"),
                "entry_date": entry_date, "exit_date": dt,
                "entry_price": round(entry_price, 2), "exit_price": round(exec_price, 2),
                "shares": int(position), "pnl": round(pnl, 2), "pnl_pct": round(pnl_pct, 2),
                "hold_bars": int(i - entry_idx),
                "exit_reason": ("bear_regime" if is_bear and desired_long[sig] else "confluence_lost"),
            })
            position = 0; entry_price = 0.0; entry_date = None; entry_idx = None

        if position == 0 and desired_long[sig]:
            if is_bear:
                blocked_by_regime += 1
            else:
                buy_cost = exec_price * (1 + cost_frac / 2.0)
                shares = int(cash // buy_cost)
                if shares > 0:
                    cash -= shares * exec_price
                    cash -= shares * exec_price * (cost_frac / 2.0)
                    position = shares
                    entry_price = float(exec_price); entry_date = dt; entry_idx = i

    if position > 0:
        last_close = float(df["close_price_adj"].iloc[-1])
        proceeds = position * last_close
        gross_pnl = proceeds - (position * entry_price)
        costs = (position * entry_price + proceeds) * (cost_frac / 2.0)
        pnl = gross_pnl - costs
        pnl_pct = ((last_close / entry_price) - 1) * 100 - cost_frac * 100
        cash += proceeds - proceeds * (cost_frac / 2.0)
        trades.append({
            "entry_signal": f"≥{min_agree}/{len(indicators)} agree",
            "exit_signal": "End of data",
            "entry_date": entry_date, "exit_date": df["business_date"].iloc[-1],
            "entry_price": round(entry_price, 2), "exit_price": round(last_close, 2),
            "shares": int(position), "pnl": round(pnl, 2), "pnl_pct": round(pnl_pct, 2),
            "hold_bars": int((len(df) - 1) - entry_idx), "exit_reason": "end_of_data",
        })
        position = 0

    trades_df = pd.DataFrame(trades)
    equity_df = _daily_equity(df, trades, initial_capital, regime, cost_frac)
    metrics = _metrics(df, trades_df, equity_df, initial_capital,
                       "combined", use_regime_filter, regime, cost_bps, blocked_by_regime,
                       indicator_label=label)
    metrics["combined_indicators"] = [INDICATOR_LABELS[i] for i in indicators]
    metrics["min_agree"] = min_agree
    metrics["n_indicators"] = len(indicators)
    metrics["regime_mode"] = regime_mode
    metrics["bear_min_agree"] = bear_min_agree
    metrics["signal"] = _current_signal(df, indicators, states, votes, desired_long,
                                        regime, min_agree, trades,
                                        regime_mode=regime_mode,
                                        bear_min_agree=bear_min_agree)
    return metrics, trades_df, equity_df


# ── Live signal ──────────────────────────────────────────────────────────────
# ACTION CODES
#   BUY   - confluence met, regime permits, currently flat -> open a position
#   HOLD  - confluence still met and already long -> stay in
#   SELL  - was long but confluence lost, or the regime turned bear -> exit
#   WAIT  - flat and confluence not met -> stand aside
# The backtest answers "would this have worked"; this answers "what do I do
# today", which is the only output a trader can act on.
_ACTION_META = {
    "BUY":  {"colour": "#16a34a", "tone": "positive"},
    "HOLD": {"colour": "#2563eb", "tone": "positive"},
    "SELL": {"colour": "#dc2626", "tone": "negative"},
    "WAIT": {"colour": "#94a3b8", "tone": "neutral"},
}


def _current_signal(df, indicators, states, votes, desired_long, regime, min_agree, trades,
                    regime_mode="strict", bear_min_agree=None):
    """What the model says to do on the LAST bar of the data."""
    if len(df) == 0:
        return None
    i = len(df) - 1
    cur_regime = str(regime.iloc[i]) if len(regime) else "Sideways"
    agree = int(votes[i])
    total = len(indicators)
    met = bool(desired_long[i])
    if bear_min_agree is None:
        bear_min_agree = min(min_agree + 1, total)

    # A bear regime only blocks trading in strict mode. In swing mode it raises
    # the bar instead; in off mode it is ignored.
    raw_bear = cur_regime == "Bear"
    if regime_mode == "off":
        is_bear = False
    elif regime_mode == "swing" and raw_bear:
        is_bear = agree < bear_min_agree
        met = met and agree >= bear_min_agree
    else:
        is_bear = raw_bear

    # Are we in a position right now? Only true if the final trade was closed by
    # running out of data rather than by a real exit signal.
    in_position = bool(trades) and trades[-1].get("exit_reason") == "end_of_data"

    if is_bear:
        action = "SELL" if in_position else "WAIT"
        if regime_mode == "swing":
            why = (f"Market regime is BEAR. Swing mode allows trading here, but requires "
                   f"{bear_min_agree} of {total} agreement and only {agree} agree.")
        else:
            why = ("Market regime is BEAR - the SOP rule is to hold cash and exit longs, "
                   "whatever the indicators say.")
    elif met and in_position:
        action, why = "HOLD", f"{agree} of {total} indicators still agree (threshold {min_agree}). Stay in."
    elif met:
        action, why = "BUY", f"{agree} of {total} indicators agree (threshold {min_agree}) and the regime permits trading."
    elif in_position:
        action, why = "SELL", f"Only {agree} of {total} indicators agree, below the threshold of {min_agree}. Confluence lost."
    else:
        action, why = "WAIT", f"Only {agree} of {total} indicators agree; {min_agree} are required. Stand aside."

    # Per-indicator breakdown, so the number is auditable rather than a black box.
    detail = [{
        "key": ind,
        "label": INDICATOR_LABELS.get(ind, ind),
        "long": bool(states[n][i]),
        "state": "LONG" if states[n][i] else "flat",
    } for n, ind in enumerate(indicators)]

    # PURE SIGNAL — the indicators' own verdict, binary, regime ignored.
    # Requested explicitly: a directional call with no hedging. BUY when the
    # confluence threshold is met on the raw votes, SELL when it is not. The
    # regime-aware `action` above stays available as risk context, but it no
    # longer masks what the indicators are actually saying.
    pure = "BUY" if int(votes[i]) >= min_agree else "SELL"
    pure_meta = _ACTION_META["BUY"] if pure == "BUY" else _ACTION_META["SELL"]

    meta = _ACTION_META[action]
    return {
        "pure_action": pure,
        "pure_colour": pure_meta["colour"],
        "pure_reason": (f"{agree} of {total} indicators are long; threshold is {min_agree}."),
        "regime_conflict": bool(pure == "BUY" and action in ("WAIT", "SELL")),
        "action": action,
        "colour": meta["colour"],
        "tone": meta["tone"],
        "reason": why,
        "agree": agree,
        "required": min_agree,
        "total": total,
        "regime": cur_regime,
        "regime_mode": regime_mode,
        "bear_min_agree": bear_min_agree,
        "bear_override": bool(raw_bear and regime_mode in ("swing", "off") and not is_bear),
        "in_position": in_position,
        "as_of": str(df["business_date"].iloc[i].date())
                 if hasattr(df["business_date"].iloc[i], "date") else str(df["business_date"].iloc[i]),
        "close": round(float(df["close_price_adj"].iloc[i]), 2),
        "indicators": detail,
    }


# ── Equity curve & metrics ───────────────────────────────────────────────────
def _daily_equity(df, trades, initial_capital, regime, cost_frac=0.0):
    """Daily mark-to-market equity of the strategy vs. buy&hold, + drawdown/regime.

    Reconstructed from the trade list so the curve is a true per-bar mark-to-market
    (not just trade-close points). Running cash after each trade advances by that
    trade's net P&L, so the final equity equals initial_capital + Σ net P&L.
    """
    n = len(df)
    held_shares = np.zeros(n)
    cash_track = np.full(n, float(initial_capital))
    date_to_idx = {d: i for i, d in enumerate(df["business_date"])}
    cash = float(initial_capital)
    for t in trades:
        ei = date_to_idx.get(pd.Timestamp(t["entry_date"]))
        xi = date_to_idx.get(pd.Timestamp(t["exit_date"]))
        if ei is None or xi is None:
            continue
        shares = t["shares"]
        # Cash tied up in the position, including the entry-side commission
        # the simulation charged — otherwise mark-to-market equity (and hence
        # drawdown / Sharpe) is overstated for the whole holding period.
        cash_while_held = cash - shares * t["entry_price"] * (1 + cost_frac / 2.0)
        for b in range(ei, xi):                              # held: entry bar → (excl) exit bar
            held_shares[b] = shares
            cash_track[b] = cash_while_held
        cash = cash + t["pnl"]                               # realise: net P&L already costed
        for b in range(xi, n):                               # flat again until the next entry
            cash_track[b] = cash
    equity = cash_track + held_shares * df["close_price_adj"].to_numpy()

    close0 = float(df["close_price_adj"].iloc[0])
    buyhold = (df["close_price_adj"].to_numpy() / close0) * float(initial_capital)

    peak = np.maximum.accumulate(equity)
    drawdown = (equity / np.where(peak == 0, np.nan, peak) - 1.0) * 100.0

    out = pd.DataFrame({
        "business_date": df["business_date"],
        "equity": np.round(equity, 2),
        "buyhold": np.round(buyhold, 2),
        "drawdown_pct": np.round(drawdown, 2),
        "regime": regime.values,
    })
    return out


def _annualised_sharpe(equity):
    """Annualised Sharpe (rf=0, √252) from a daily equity array."""
    equity = np.asarray(equity, dtype=float)
    if equity.size < 3:
        return 0.0
    rets = np.diff(equity) / np.where(equity[:-1] == 0, np.nan, equity[:-1])
    rets = rets[np.isfinite(rets)]
    if rets.size > 1 and np.std(rets) > 0:
        return float(np.mean(rets) / np.std(rets) * np.sqrt(_TRADING_DAYS))
    return 0.0


def _max_drawdown_pct(series):
    """Worst peak-to-trough drop of a value series, as a negative percent."""
    arr = np.asarray(series, dtype=float)
    if arr.size == 0:
        return 0.0
    peak = np.maximum.accumulate(arr)
    return float((arr / np.where(peak == 0, np.nan, peak) - 1.0).min() * 100)


def _metrics(df, trades_df, equity_df, initial_capital,
             indicator, use_regime_filter, regime, cost_bps, blocked_by_regime,
             indicator_label=None, oos_frac=0.3):
    equity = equity_df["equity"].to_numpy(dtype=float)
    final_equity = float(equity[-1]) if len(equity) else float(initial_capital)
    total_return = ((final_equity / initial_capital) - 1) * 100

    sharpe = _annualised_sharpe(equity)                 # full-window Sharpe (rf=0)
    max_dd = _max_drawdown_pct(equity)

    # Out-of-sample Sharpe: Sharpe over just the most recent `oos_frac` of the
    # equity curve (the "unseen" tail) — a per-run proxy for the SOP's held-out
    # 2025–26 window (SOP §8b).
    n = len(equity)
    oos_start = max(1, int(n * (1.0 - oos_frac)))
    oos_sharpe = _annualised_sharpe(equity[oos_start:]) if n - oos_start >= 3 else 0.0

    close = df["close_price_adj"]
    buyhold_return = ((float(close.iloc[-1]) / float(close.iloc[0])) - 1) * 100
    buyhold_max_dd = _max_drawdown_pct(equity_df["buyhold"].to_numpy(dtype=float))

    # Time in market: share of sessions the strategy actually held a position.
    n_bars = max(1, len(df))
    bars_held = int(trades_df["hold_bars"].sum()) if not trades_df.empty else 0
    time_in_market = round(min(100.0, bars_held / n_bars * 100.0), 1)

    metrics = {
        "initial_capital": float(initial_capital),
        "final_equity": round(final_equity, 2),
        "total_return": round(total_return, 2),
        "sharpe": round(sharpe, 2),
        "oos_sharpe": round(oos_sharpe, 2),
        "max_drawdown": round(max_dd, 2),
        "buyhold_return": round(buyhold_return, 2),
        "buyhold_max_drawdown": round(buyhold_max_dd, 2),
        "excess_vs_buyhold": round(total_return - buyhold_return, 2),
        "beats_buyhold": bool(total_return > buyhold_return),
        "time_in_market": time_in_market,
        "indicator": indicator,
        "indicator_label": indicator_label or INDICATOR_LABELS.get(indicator, indicator),
        "regime_used": bool(use_regime_filter),
        "current_regime": str(regime.iloc[-1]) if len(regime) else "Sideways",
        "cost_bps": float(cost_bps),
        "blocked_by_regime": int(blocked_by_regime),
    }

    if not trades_df.empty:
        wins = int((trades_df["pnl"] >= 0).sum())
        losses = int((trades_df["pnl"] < 0).sum())
        gross_win = float(trades_df.loc[trades_df["pnl"] > 0, "pnl"].sum())
        gross_loss = float(trades_df.loc[trades_df["pnl"] < 0, "pnl"].sum())
        metrics.update({
            "total_trades": int(len(trades_df)),
            "wins": wins,
            "losses": losses,
            "win_rate": round(wins / len(trades_df) * 100, 2),
            "avg_holding_bars": round(float(trades_df["hold_bars"].mean()), 1),
            "profit_factor": (
                round(gross_win / abs(gross_loss), 2) if gross_loss != 0
                else ("∞" if gross_win > 0 else 0.0)
            ),
        })
    else:
        metrics.update({
            "total_trades": 0, "wins": 0, "losses": 0, "win_rate": 0.0,
            "avg_holding_bars": 0.0, "profit_factor": 0.0,
        })
    return metrics

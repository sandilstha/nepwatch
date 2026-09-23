import logging
from typing import Dict, List, Optional, Tuple

import numpy as np
import pandas as pd

try:
    import pandas_ta as ta
except Exception:
    ta = None


logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Score thresholds — max theoretical raw score breakdown:
#   trend_score    : 0–40   (15+10+10+5)
#   momentum_score : -10–15
#   volume_score   : -12–20 (10+10; or -12 on down-volume)
#   greed_score    : -15–15
#   rs_score       : -10–10
#   vol_score      : -8–5
#   ─────────────────────────
#   raw max        : ~105   → clipped to 100
#   raw min        : ~-45   → clipped to 0
#
# Thresholds are intentionally conservative: a "Strong Buy" (≥80) requires
# all four trend pillars PLUS strong momentum, volume, and RS alignment.
# ---------------------------------------------------------------------------

_SCORE_MAX_RAW = 105  # documented ceiling before clip


# ── Utilities ───────────────────────────────────────────────────────────────

def _safe_divide(numerator: pd.Series, denominator: pd.Series) -> pd.Series:
    """Divide two series, replacing zeros in the denominator with NaN."""
    denominator = denominator.replace(0, np.nan)
    return numerator / denominator


# ── Score classification ─────────────────────────────────────────────────

def classify_score(score: pd.Series) -> pd.Series:
    """Map a 0–100 technical score to a human-readable classification label."""
    conditions = [
        score >= 80,
        (score >= 65) & (score < 80),
        (score >= 50) & (score < 65),
        (score >= 35) & (score < 50),
        score < 35,
    ]
    labels = [
        "Strong Buy",
        "Buy / Accumulate",
        "Neutral Watchlist",
        "Weak",
        "Avoid",
    ]
    return pd.Series(
        np.select(conditions, labels, default="Neutral Watchlist"),
        index=score.index,
    )


# ── Relative Strength vs NEPSE ──────────────────────────────────────────

def calculate_relative_strength(
    stock_df: pd.DataFrame,
    nepse_df: pd.DataFrame,
    lookback: int = 20,
) -> pd.Series:
    """
    Relative strength as the ratio of the stock's price-relative to NEPSE's
    price-relative over ``lookback`` bars:  (1 + stock_return) / (1 + nepse_return).
    Values > 1.0 mean the stock outperformed; values < 1.0 mean it underperformed.

    This is deliberately NOT a raw ratio of signed returns (stock_return /
    nepse_return): that form flips sign when the index falls — a stock that
    rose while NEPSE dropped would score negative (worst) instead of >1 (best) —
    and explodes when the index return is near zero (very common on flat index
    bars).  Using price-relatives keeps the metric monotonic and bounded:
        stock +5%, index -2%  ->  1.05 / 0.98 = 1.07  (outperform, correct)
        stock +5%, index  0%  ->  1.05 / 1.00 = 1.05  (stable, no blow-up)

    Returns a NaN series (aligned to stock_df.index) when benchmark data
    is unavailable — callers should emit a warning in that case.
    """
    if nepse_df is None or nepse_df.empty:
        return pd.Series(np.nan, index=stock_df.index)

    nepse = nepse_df[["business_date", "close_price_adj"]].copy()
    nepse = nepse.rename(columns={"close_price_adj": "nepse_close"})
    merged = stock_df[["business_date", "close_price_adj"]].merge(
        nepse, on="business_date", how="left"
    )

    stock_return = merged["close_price_adj"].pct_change(lookback)
    nepse_return = merged["nepse_close"].pct_change(lookback)
    rs = _safe_divide(1.0 + stock_return, 1.0 + nepse_return)
    return rs.reindex(stock_df.index)


# ── Signal generation ────────────────────────────────────────────────────

def generate_buy_signal(df: pd.DataFrame) -> pd.Series:
    """
    Raw buy-condition series (True on every bar where ALL conditions hold).
    This is NOT the final event signal — pass through ``_build_position_signals``
    to get entry-only events.

    Conditions (all must be true simultaneously):
    - Price above SMA 50 and SMA 200 (uptrend structure)
    - SMA 20 above SMA 50 (short-term momentum aligned)
    - RSI between 50–70 (momentum zone, not overbought)
    - Volume above 20-bar average (institutional participation)
    - Relative strength > 1.0 (outperforming NEPSE)
    - Supertrend bullish
    - MACD line crossing above signal line this bar
    - Close above VWAP (intraday demand)

    NOTE: If ``relative_strength`` is entirely NaN (NEPSE data unavailable),
    the condition ``relative_strength > 1.0`` evaluates to False for every
    bar and NO buy signals will fire.  A warning is raised in the main runner.
    """
    prev_macd = df["MACD_line"].shift(1)
    prev_signal = df["MACD_signal"].shift(1)
    macd_cross_up = (prev_macd <= prev_signal) & (df["MACD_line"] > df["MACD_signal"])

    vwap = pd.to_numeric(df["VWAP"], errors="coerce")
    close = pd.to_numeric(df["close_price_adj"], errors="coerce")

    return (
        (df["close_price_adj"] > df["SMA_50"])
        & (df["close_price_adj"] > df["SMA_200"])
        & (df["SMA_20"] > df["SMA_50"])
        & (df["RSI_14"].between(50, 70, inclusive="both"))
        & (df["volume"] > df["VOL_SMA_20"])
        & (df["relative_strength"] > 1.0)
        & (df["supertrend_bullish"])
        & macd_cross_up
        & (close > vwap)
    )


def _sell_condition_count(
    df: pd.DataFrame,
    atr_stop: Optional[pd.Series] = None,
) -> pd.Series:
    """Return the number of active sell conditions for each bar."""
    if atr_stop is None:
        atr_stop = (
            df["atr_trailing_stop"]
            if "atr_trailing_stop" in df.columns
            else pd.Series(np.nan, index=df.index)
        )

    prev_macd = df["MACD_line"].shift(1)
    prev_signal = df["MACD_signal"].shift(1)
    macd_cross_down = (prev_macd >= prev_signal) & (df["MACD_line"] < df["MACD_signal"])

    close = pd.to_numeric(df["close_price_adj"], errors="coerce")
    sma_20 = pd.to_numeric(df["SMA_20"], errors="coerce")
    atr_stop = pd.to_numeric(atr_stop, errors="coerce")
    supertrend_bullish = df["supertrend_bullish"].fillna(False).astype(bool)

    c1 = (df["RSI_14"] > 80).fillna(False)
    c2 = macd_cross_down.fillna(False)
    c3 = (close < sma_20).fillna(False)
    c4 = (~supertrend_bullish).fillna(False)
    c5 = (close < atr_stop).fillna(False)

    return (
        c1.astype(int)
        + c2.astype(int)
        + c3.astype(int)
        + c4.astype(int)
        + c5.astype(int)
    )


def generate_sell_signal(df: pd.DataFrame) -> pd.Series:
    """
    Raw sell-condition series.  Exit is triggered when ANY TWO OR MORE of
    the five sub-conditions are true simultaneously.  This "2-of-5" gate
    prevents a single noisy indicator (e.g., a brief RSI spike above 80)
    from prematurely exiting a healthy trend.

    Sub-conditions:
    1. RSI > 80 (extreme overbought)
    2. MACD line crosses below signal line
    3. Close drops below SMA 20 (short-term trend break)
    4. Supertrend flips bearish
    5. Close falls below the per-position ATR trailing stop

    Design note: returning a plain boolean Series keeps the interface
    identical to the original; the 2-of-5 threshold is applied here so
    ``_build_position_signals`` remains generic.
    """
    return _sell_condition_count(df) >= 2


# ── Position state machine ───────────────────────────────────────────────

def _build_position_signals(
    raw_buy: pd.Series,
    raw_sell: pd.Series,
) -> Tuple[pd.Series, pd.Series]:
    """
    Convert per-bar condition booleans into discrete entry/exit events.

    - ``buy_signal``  : True ONLY on the bar we enter a position (flat → long)
    - ``sell_signal`` : True ONLY on the bar we exit a position (long → flat)

    Uses ``iloc``-based integer indexing to avoid ambiguity on datetime or
    non-contiguous integer indices and to guarantee O(n) performance.
    """
    n = len(raw_buy)
    buy_evt = pd.Series(False, index=raw_buy.index, dtype=bool)
    sell_evt = pd.Series(False, index=raw_sell.index, dtype=bool)
    in_position = False

    for i in range(n):
        if not in_position and bool(raw_buy.iloc[i]):
            buy_evt.iloc[i] = True
            in_position = True
        elif in_position and bool(raw_sell.iloc[i]):
            sell_evt.iloc[i] = True
            in_position = False

    return buy_evt, sell_evt


# ── Per-position ATR trailing stop ───────────────────────────────────────

def _build_position_signals_with_atr_stop(
    df: pd.DataFrame,
    raw_buy: pd.Series,
    multiplier: float = 2.0,
) -> Tuple[pd.Series, pd.Series, pd.Series]:
    """
    Build entry/exit events while maintaining the live ATR trailing stop.

    The ATR stop is position state, so it must be calculated in the same
    forward pass that decides exits. Computing raw sells before the stop exists
    creates a circular dependency and can raise a missing-column error.
    """
    buy_evt = pd.Series(False, index=raw_buy.index, dtype=bool)
    sell_evt = pd.Series(False, index=raw_buy.index, dtype=bool)
    stop = pd.Series(np.nan, index=raw_buy.index)

    base_sell_count = _sell_condition_count(
        df,
        atr_stop=pd.Series(np.nan, index=df.index),
    )
    close = pd.to_numeric(df["close_price_adj"], errors="coerce")
    atr = pd.to_numeric(df["ATR"], errors="coerce")

    in_position = False
    running_high = np.nan

    for i in range(len(df)):
        close_value = close.iloc[i]
        atr_value = atr.iloc[i]
        entered = False

        if not in_position and bool(raw_buy.iloc[i]):
            buy_evt.iloc[i] = True
            in_position = True
            entered = True
            running_high = float(close_value) if pd.notna(close_value) else np.nan

        if not in_position:
            continue

        if pd.notna(close_value) and (pd.isna(running_high) or close_value > running_high):
            running_high = float(close_value)

        current_stop = np.nan
        if pd.notna(running_high) and pd.notna(atr_value):
            current_stop = running_high - multiplier * float(atr_value)
            stop.iloc[i] = current_stop

        stop_hit = (
            pd.notna(current_stop)
            and pd.notna(close_value)
            and float(close_value) < current_stop
        )
        sell_count = int(base_sell_count.iloc[i]) + int(stop_hit)

        if not entered and sell_count >= 2:
            sell_evt.iloc[i] = True
            in_position = False
            running_high = np.nan

    return buy_evt, sell_evt, stop


def _calculate_atr_trailing_stop(
    close: pd.Series,
    atr: pd.Series,
    buy_events: pd.Series,
    sell_events: Optional[pd.Series] = None,
    multiplier: float = 2.0,
) -> pd.Series:
    """
    Compute a per-position ATR trailing stop that RESETS each time a new
    entry signal fires.

    Algorithm (bar by bar):
    - When a buy event fires, start tracking the running high from that bar.
    - While in a position, update the running high if close makes a new high.
    - Stop = running_high − (multiplier × ATR).
    - When no position is active, stop is NaN.

    FIX vs original: the original used ``close.cummax()`` over the entire
    series, meaning the stop was anchored to the all-time high of the full
    date range, never resetting after exits.  This version resets on each
    new entry, giving a stop that actually trails the current position.
    """
    stop = pd.Series(np.nan, index=close.index)
    if sell_events is None:
        sell_events = pd.Series(False, index=close.index)
    else:
        sell_events = sell_events.reindex(close.index).fillna(False)

    in_pos = False
    running_high = np.nan

    for i in range(len(close)):
        c = close.iloc[i]
        a = atr.iloc[i]
        is_entry = bool(buy_events.iloc[i])

        if is_entry:
            in_pos = True
            running_high = float(c) if pd.notna(c) else np.nan

        if in_pos:
            if pd.notna(c) and c > running_high:
                running_high = float(c)
            if pd.notna(running_high) and pd.notna(a):
                stop.iloc[i] = running_high - multiplier * float(a)
            if bool(sell_events.iloc[i]):
                in_pos = False
                running_high = np.nan

    return stop


# ── Volume Greed/Fear Meter ──────────────────────────────────────────────

def calculate_volume_greed(df: pd.DataFrame) -> pd.Series:
    """
    Classify each bar's volume-price relationship as a market sentiment label.

    Requires columns: ``price_change_pct``, ``volume_ratio``
    (both must be present in ``df`` before calling this function).

    Labels (priority order — first matching condition wins):
    - Extreme Greed : price up  + volume ≥ 2× average
    - Greed         : price up  + volume ≥ 1.2× average
    - Extreme Fear  : price down + volume ≥ 2× average
    - Fear          : price down + volume ≥ 1.2× average
    - Neutral       : everything else (low volume or flat price)
    """
    if "price_change_pct" not in df.columns or "volume_ratio" not in df.columns:
        raise KeyError(
            "calculate_volume_greed requires 'price_change_pct' and 'volume_ratio' columns. "
            "Ensure these are computed before calling this function."
        )

    conditions = [
        (df["price_change_pct"] > 0) & (df["volume_ratio"] >= 2.0),
        (df["price_change_pct"] > 0) & (df["volume_ratio"] >= 1.2),
        (df["price_change_pct"] < 0) & (df["volume_ratio"] >= 2.0),
        (df["price_change_pct"] < 0) & (df["volume_ratio"] >= 1.2),
    ]
    labels = ["Extreme Greed", "Greed", "Extreme Fear", "Fear"]
    return pd.Series(np.select(conditions, labels, default="Neutral"), index=df.index)


# ── Technical Score ──────────────────────────────────────────────────────

def calculate_technical_score(df: pd.DataFrame) -> pd.Series:
    """
    Composite 0–100 technical score.  Higher = stronger bullish alignment.

    Component breakdown (max raw values):
    ┌─────────────────┬─────────────┬───────────────────────────────────────┐
    │ Component       │  Max / Min  │ What it measures                      │
    ├─────────────────┼─────────────┼───────────────────────────────────────┤
    │ trend_score     │  40 /  0    │ SMA structure (20/50/200 alignment)   │
    │ momentum_score  │  15 / -10   │ RSI zone quality                      │
    │ volume_score    │  20 / -12   │ Volume vs average + price-vol confirm │
    │ greed_score     │  15 / -15   │ Volume Greed/Fear meter               │
    │ rs_score        │  10 / -10   │ Relative strength vs NEPSE            │
    │ vol_score       │   5 /  -8   │ ATR% (volatility penalty/reward)      │
    └─────────────────┴─────────────┴───────────────────────────────────────┘
    Raw max ≈ 105, raw min ≈ -45; clipped to [0, 100].
    A score of 80+ requires near-perfect alignment across all components.
    """
    trend_score = (
        np.where(df["close_price_adj"] > df["SMA_200"], 15, 0)
        + np.where(df["close_price_adj"] > df["SMA_50"], 10, 0)
        + np.where(df["SMA_20"] > df["SMA_50"], 10, 0)
        + np.where(df["SMA_50"] > df["SMA_200"], 5, 0)
    )

    momentum_score = np.select(
        [
            df["RSI_14"].between(55, 65, inclusive="both"),   # sweet spot
            df["RSI_14"].between(45, 55, inclusive="left"),   # acceptable
            df["RSI_14"].between(65, 70, inclusive="right"),  # slightly hot
            df["RSI_14"].between(70, 80, inclusive="right"),  # overbought risk
            df["RSI_14"] > 80,                                # extreme overbought
            df["RSI_14"] < 40,                                # weak momentum
        ],
        [15, 10, 8, 3, -10, -8],
        default=0,
    )

    volume_score = (
        np.where(df["volume"] > df["VOL_SMA_20"], 10, 0)
        + np.where((df["price_change_pct"] > 0) & (df["volume_change_pct"] > 0), 10, 0)
        + np.where((df["price_change_pct"] < 0) & (df["volume_change_pct"] > 0), -12, 0)
    )

    greed_score = np.select(
        [
            df["volume_greed"] == "Extreme Greed",
            df["volume_greed"] == "Greed",
            df["volume_greed"] == "Extreme Fear",
            df["volume_greed"] == "Fear",
        ],
        [15, 5, -15, -5],
        default=0,
    )

    rs_score = np.select(
        [
            df["relative_strength"] > 1.0,
            df["relative_strength"].between(0.9, 1.0, inclusive="both"),
            df["relative_strength"] < 0.9,
        ],
        [10, 3, -10],
        default=0,
    )

    vol_score = np.select(
        [
            df["atr_percent"] < 3,
            df["atr_percent"].between(3, 6, inclusive="both"),
            df["atr_percent"] > 8,
        ],
        [5, 2, -8],
        default=0,
    )

    raw_score = trend_score + momentum_score + volume_score + greed_score + rs_score + vol_score
    return pd.Series(raw_score, index=df.index).clip(0, 100)


# ── Main entry point ─────────────────────────────────────────────────────

def run_imm_scoring_system(
    stock_df: pd.DataFrame,
    nepse_index_df: pd.DataFrame,
    rs_lookback: int = 20,
    atr_length: int = 14,
    rsi_length: int = 14,
    macd_fast: int = 12,
    macd_slow: int = 26,
    macd_signal: int = 9,
    supertrend_length: int = 10,
    supertrend_multiplier: float = 3.0,
    atr_stop_multiplier: float = 2.0,
) -> Tuple[Dict, pd.DataFrame]:
    """
    Run the full Institutional Momentum Model (IMM) scoring pipeline.

    Parameters
    ----------
    stock_df : DataFrame
        OHLCV data for the target stock.  Required columns:
        business_date, open_price_adj, high_price_adj, low_price_adj,
        close_price_adj, volume.
    nepse_index_df : DataFrame
        NEPSE index OHLCV data used to compute relative strength.
        If empty or None, RS will be NaN and buy signals will not fire.
    rs_lookback : int
        Lookback window (bars) for relative-strength return comparison.
    atr_length : int
        ATR period for volatility and trailing stop calculation.
    rsi_length : int
        RSI period.
    macd_fast, macd_slow, macd_signal : int
        MACD parameters.
    supertrend_length : int
        Supertrend ATR period.
    supertrend_multiplier : float
        Supertrend ATR multiplier.
    atr_stop_multiplier : float
        Multiplier applied to ATR for the per-position trailing stop.
        Default 2.0 means stop = position_high − 2 × ATR.

    Returns
    -------
    metrics : dict
        Summary statistics and latest-bar signal state.
        Contains an "error" key (and empty DataFrame) on failure.
    out : DataFrame
        Full indicator + signal table, one row per input bar.
    """
    warnings: List[str] = []

    # ── Guard: pandas_ta availability ───────────────────────────────────
    if ta is None:
        return (
            {
                "error": "pandas_ta is unavailable.",
                "warnings": ["Install pandas_ta to run IMM scoring."],
            },
            pd.DataFrame(),
        )

    # ── Guard: input data ────────────────────────────────────────────────
    if stock_df is None or stock_df.empty:
        return (
            {
                "error": "Stock dataframe is empty.",
                "warnings": ["No stock data available for selected range."],
            },
            pd.DataFrame(),
        )

    required = {
        "business_date",
        "open_price_adj",
        "high_price_adj",
        "low_price_adj",
        "close_price_adj",
        "volume",
    }
    missing = sorted(required.difference(stock_df.columns))
    if missing:
        return (
            {"error": f"Missing required fields: {', '.join(missing)}", "warnings": []},
            pd.DataFrame(),
        )

    # ── Guard: parameter sanity ──────────────────────────────────────────
    if rs_lookback < 2 or atr_length < 2 or rsi_length < 2 or macd_fast < 1 or macd_slow < 2:
        return (
            {
                "error": "Invalid indicator settings.",
                "warnings": ["Lookback settings are too small."],
            },
            pd.DataFrame(),
        )
    if macd_fast >= macd_slow:
        return (
            {
                "error": "Invalid MACD settings.",
                "warnings": ["MACD fast must be smaller than MACD slow."],
            },
            pd.DataFrame(),
        )

    # ── Guard: minimum bar count ─────────────────────────────────────────
    if supertrend_multiplier <= 0 or atr_stop_multiplier <= 0:
        return (
            {
                "error": "Invalid indicator settings.",
                "warnings": ["Multiplier settings must be greater than zero."],
            },
            pd.DataFrame(),
        )

    min_bars = max(
        200,
        rs_lookback + 5,
        macd_slow + macd_signal + 5,
        atr_length + 5,
        supertrend_length + 5,
    )
    if len(stock_df) < min_bars:
        return (
            {
                "error": f"Insufficient historical data: need at least {min_bars} rows.",
                "warnings": ["Extend date range to stabilize long-term indicators like SMA 200."],
            },
            pd.DataFrame(),
        )

    # ── Warn when NEPSE benchmark is missing ─────────────────────────────
    # Buy signals require relative_strength > 1.0; if RS is all-NaN no
    # entries will ever fire.  Surface this to the user immediately.
    if nepse_index_df is None or (
        isinstance(nepse_index_df, pd.DataFrame) and nepse_index_df.empty
    ):
        warnings.append(
            "NEPSE INDEX data is unavailable. Relative strength will be NaN "
            "and all buy signals will be suppressed for the entire date range."
        )

    # ── Prepare working dataframe ────────────────────────────────────────
    df = stock_df.sort_values("business_date").reset_index(drop=True).copy()
    for c in ["open_price_adj", "high_price_adj", "low_price_adj", "close_price_adj", "volume"]:
        df[c] = pd.to_numeric(df[c], errors="coerce")

    # ── Guard: flat-bar share (index series / ultra-illiquid scrips) ──────
    # ~63% of NEPSE *index* bars have high == low, which degenerates every
    # range-based input here (ATR%, Supertrend, ATR trailing stop) and thus the
    # volatility and stop legs of the composite. Warn loudly instead of scoring
    # garbage — IMM is a per-stock model.
    flat_share = float((df["high_price_adj"] == df["low_price_adj"]).mean())
    if flat_share > 0.30:
        warnings.append(
            f"{flat_share:.0%} of bars have High == Low (typical of NEPSE index series or "
            "very thin scrips). ATR-based volatility scores, Supertrend and trailing stops "
            "are unreliable here — run IMM on individual stocks, not indices."
        )

    # ── Moving averages ──────────────────────────────────────────────────
    df["SMA_20"] = ta.sma(df["close_price_adj"], length=20)
    df["SMA_50"] = ta.sma(df["close_price_adj"], length=50)
    df["SMA_200"] = ta.sma(df["close_price_adj"], length=200)
    df["RSI_14"] = ta.rsi(df["close_price_adj"], length=rsi_length)
    df["VOL_SMA_20"] = ta.sma(df["volume"], length=20)

    # ── MACD ─────────────────────────────────────────────────────────────
    macd_df = ta.macd(df["close_price_adj"], fast=macd_fast, slow=macd_slow, signal=macd_signal)
    if macd_df is None or macd_df.empty:
        return {"error": "MACD calculation failed.", "warnings": []}, pd.DataFrame()
    df["MACD_line"] = macd_df.iloc[:, 0]
    df["MACD_histogram"] = macd_df.iloc[:, 1]
    df["MACD_signal"] = macd_df.iloc[:, 2]

    # ── Supertrend ───────────────────────────────────────────────────────
    st = ta.supertrend(
        df["high_price_adj"],
        df["low_price_adj"],
        df["close_price_adj"],
        length=supertrend_length,
        multiplier=supertrend_multiplier,
    )
    if st is None or st.empty:
        return {"error": "Supertrend calculation failed.", "warnings": []}, pd.DataFrame()
    st_col = next((c for c in st.columns if c.startswith("SUPERT_")), None)
    dir_col = next((c for c in st.columns if c.startswith("SUPERTd_")), None)
    if st_col is None or dir_col is None:
        return {"error": "Unexpected Supertrend output format.", "warnings": []}, pd.DataFrame()
    df["Supertrend"] = st[st_col]
    df["supertrend_bullish"] = st[dir_col] > 0

    # ── VWAP & ATR ───────────────────────────────────────────────────────
    # Rolling 20-bar VWAP. ta.vwap anchors daily, so on daily bars it collapses
    # to the same bar's (H+L+C)/3 (see msv_strategy for the full note) — and it
    # needs a DatetimeIndex, which this frame does not have.
    _tp = (df["high_price_adj"] + df["low_price_adj"] + df["close_price_adj"]) / 3.0
    _vol = pd.to_numeric(df["volume"], errors="coerce")
    df["VWAP"] = (_tp * _vol).rolling(window=20).sum() / _vol.rolling(window=20).sum().replace(0, np.nan)
    df["ATR"] = ta.atr(
        df["high_price_adj"], df["low_price_adj"], df["close_price_adj"], length=atr_length
    )

    # Normalise indicator dtypes to avoid float-vs-NoneType comparison errors
    for col in [
        "SMA_20", "SMA_50", "SMA_200", "RSI_14",
        "MACD_line", "MACD_signal", "MACD_histogram",
        "Supertrend", "VWAP", "ATR",
    ]:
        df[col] = pd.to_numeric(df[col], errors="coerce")

    df["atr_percent"] = _safe_divide(df["ATR"], df["close_price_adj"]) * 100.0

    # ── Price & volume momentum ──────────────────────────────────────────
    df["price_change_pct"] = df["close_price_adj"].pct_change() * 100.0
    df["volume_change_pct"] = df["volume"].pct_change() * 100.0

    # ── Volume ratio & greed meter ───────────────────────────────────────
    # volume_ratio and price_change_pct must exist BEFORE calling
    # calculate_volume_greed — the function now raises KeyError if they don't.
    df["volume_ratio"] = _safe_divide(df["volume"], df["VOL_SMA_20"])
    df["volume_breakout"] = df["volume_ratio"] >= 2.0
    df["volume_breakout_status"] = np.where(df["volume_breakout"], "BREAKOUT", "NORMAL")
    df["volume_greed"] = calculate_volume_greed(df)

    # ── Relative strength ────────────────────────────────────────────────
    df["relative_strength"] = calculate_relative_strength(
        df, nepse_index_df, lookback=rs_lookback
    )

    # ── Technical score ──────────────────────────────────────────────────
    df["technical_score"] = calculate_technical_score(df)
    df["score_classification"] = classify_score(df["technical_score"])

    # ── Trend & momentum alignment flags ────────────────────────────────
    df["trend_alignment"] = (
        (df["close_price_adj"] > df["SMA_50"])
        & (df["close_price_adj"] > df["SMA_200"])
        & (df["SMA_20"] > df["SMA_50"])
        & (df["SMA_50"] > df["SMA_200"])
    )
    df["momentum_alignment"] = df["RSI_14"].between(50, 70, inclusive="both")

    # ── Buy / sell raw conditions → position events ───────────────────────
    raw_buy = generate_buy_signal(df)
    df["buy_signal"], df["sell_signal"], df["atr_trailing_stop"] = (
        _build_position_signals_with_atr_stop(
            df=df,
            raw_buy=raw_buy,
            multiplier=atr_stop_multiplier,
        )
    )

    # ── ATR trailing stop (per-position, resets on each entry) ───────────
    # The single-pass builder computes the live stop used by sell decisions.

    # ── NaN warmup warning ───────────────────────────────────────────────
    indicator_cols = [
        "technical_score", "relative_strength", "atr_percent",
        "SMA_20", "SMA_50", "SMA_200", "RSI_14",
        "MACD_line", "MACD_signal", "MACD_histogram",
        "Supertrend", "VWAP", "ATR",
    ]
    nan_count = int(df[indicator_cols].isna().sum().sum())
    if nan_count > 0:
        warnings.append(
            "NaN values detected in warmup periods; early rows may not be signal-eligible."
        )

    # ── Build output ─────────────────────────────────────────────────────
    output_cols = [
        "business_date",
        "close_price_adj",
        "technical_score",
        "score_classification",
        "buy_signal",
        "sell_signal",
        "relative_strength",
        "atr_percent",
        "atr_trailing_stop",
        "volume_ratio",
        "volume_breakout",
        "volume_breakout_status",
        "volume_greed",
        "trend_alignment",
        "momentum_alignment",
        "MACD_line",
        "MACD_signal",
        "MACD_histogram",
        "Supertrend",
        "VWAP",
        "ATR",
    ]
    out = df[output_cols].copy()
    latest = out.iloc[-1]

    metrics: Dict = {
        "warnings": warnings,
        "latest_data_date": latest["business_date"],
        "latest_score": float(latest["technical_score"]) if pd.notna(latest["technical_score"]) else None,
        "latest_classification": str(latest["score_classification"]),
        "latest_volume_greed": str(latest["volume_greed"]),
        "latest_volume_ratio": float(latest["volume_ratio"]) if pd.notna(latest["volume_ratio"]) else None,
        "latest_buy_signal": bool(latest["buy_signal"]) if pd.notna(latest["buy_signal"]) else False,
        "latest_sell_signal": bool(latest["sell_signal"]) if pd.notna(latest["sell_signal"]) else False,
        "latest_relative_strength": (
            float(latest["relative_strength"]) if pd.notna(latest["relative_strength"]) else None
        ),
        "latest_atr_percent": float(latest["atr_percent"]) if pd.notna(latest["atr_percent"]) else None,
        "latest_atr_trailing_stop": (
            float(latest["atr_trailing_stop"]) if pd.notna(latest["atr_trailing_stop"]) else None
        ),
        "buy_count": int(out["buy_signal"].fillna(False).sum()),
        "sell_count": int(out["sell_signal"].fillna(False).sum()),
    }

    logger.info(
        "IMM scoring completed | rows=%s latest_score=%s class=%s buy=%s sell=%s",
        len(out),
        metrics["latest_score"],
        metrics["latest_classification"],
        metrics["buy_count"],
        metrics["sell_count"],
    )

    return metrics, out

"""
support_resistance.py  –  v4: Genuine multi-framework institutional reading
============================================================================

The level engine (run_support_resistance_analysis) computes classic S/R levels
(pivots, Fibonacci, std-dev bands, MAs, stochastics, RSI targets, Bollinger
headline cards) and is unchanged from v3.

v4 rewrites the *Institutional Multi-Framework Analysis* table. Previously the
nine rows shared canned prose and their Bullish/Bearish label came from scraping
keywords out of that prose (which double-counted the words "support"/
"resistance" present in almost every row, so the signals cancelled to noise),
and an unused "Random-Forest vote" confidence that never reached the template.

Now each of the nine frameworks reads the DATA FACET that actually represents
its methodology and returns its own (logic, sentiment, status, signal,
confidence):

  - SMC / ICT      : market structure (BOS=continuation, CHoCH=reversal),
                     liquidity sweeps, and premium/discount within the dealing
                     range (Bollinger %B). ICT bias = buy discount / sell premium.
  - RTM / QM       : fresh vs tested supply/demand zones; a CHoCH at a zone is a
                     Quasimodo reversal. Trades the first tap of a fresh imbalance.
  - BTMM           : market-maker cycle (accumulation → manipulation/stop-hunt →
                     mark-up/down) from the EMA/HMA/VWAP stack + the latest sweep.
  - Malaysian SNR  : fresh walls hold, repeatedly-tested walls break; broken
                     levels flip role (SNR flip).
  - Wyckoff Phase  : Spring/Upthrust (Phase C), SOS/SOW (Phase D), cause built at
                     the volume node, POC as the auction control point.
  - Elliott Wave   : impulse vs corrective posture from trend + RSI (wave-3
                     expansion vs wave-5 divergence). Wave count is approximate.
  - Volume Flow    : VPVR — POC magnet, value-area acceptance/rejection (VAH/VAL).
  - Candle Range   : last-candle anatomy (rejection wick / displacement / absorption).
  - Structural S/R : floor-trader pivot bias + R/R + RSI/Stochastic confluence.

The "Institutional Consensus" row is a genuine confidence-weighted vote of those
nine independent reads — not a separate black box.
"""

import math
from typing import Any

import numpy as np
import pandas as pd


DEFAULT_LEVEL_FAMILIES = (
    "pivots",
    "stochastics",
    "fibonacci",
    "moving_averages",
    "highs_lows",
    "rsi",
    "hlc",
    "standard_deviation",
)

# Percentage of latest price within which a level is treated as "at latest price".
_LATEST_ZONE_PCT = 0.0005  # 0.05 %


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _normalize_level_families(enabled_families):
    allowed = set(DEFAULT_LEVEL_FAMILIES)
    if enabled_families is None:
        return allowed
    return {
        str(family).strip().lower()
        for family in enabled_families
        if str(family).strip().lower() in allowed
    }


def _round_price(value):
    if value is None:
        return None
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(parsed) or parsed <= 0:
        return None
    return round(parsed, 2)


def _format_date(value) -> str:
    """Return a YYYY-MM-DD string, or '' for missing/NaT values."""
    # pd.isna raises on array-like input; use a try/except for safety.
    try:
        if pd.isna(value):
            return ""
    except (TypeError, ValueError):
        return ""
    return pd.to_datetime(value).strftime("%Y-%m-%d")


def _format_price(value) -> str:
    rounded = _round_price(value)
    return f"{rounded:.2f}" if rounded is not None else "N/A"


def _format_price_range(low_value, high_value) -> str:
    low_price = _round_price(low_value)
    high_price = _round_price(high_value)
    if low_price is None or high_price is None:
        return "N/A"
    return f"{low_price:.2f} - {high_price:.2f}"


def _coerce_int(value, default, minimum=1):
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return default
    return parsed if parsed >= minimum else default


def _series_rsi(close: pd.Series, length: int) -> pd.Series:
    delta = close.diff()
    gain = delta.clip(lower=0)
    loss = -delta.clip(upper=0)
    avg_gain = gain.ewm(alpha=1 / length, min_periods=length, adjust=False).mean()
    avg_loss = loss.ewm(alpha=1 / length, min_periods=length, adjust=False).mean()
    # Replace zero avg_loss with NaN to avoid ZeroDivisionError in the RS
    # calculation. The edge-case masks below restore the correct RSI values
    # (100 when only gains, 0 when only losses, 50 when flat) afterwards.
    rs = avg_gain / avg_loss.replace(0, np.nan)
    rsi = 100 - (100 / (1 + rs))
    rsi = rsi.mask((avg_loss == 0) & (avg_gain > 0), 100)
    rsi = rsi.mask((avg_gain == 0) & (avg_loss > 0), 0)
    rsi = rsi.mask((avg_gain == 0) & (avg_loss == 0), 50)
    return rsi


def _latest_simple_rsi_target_prices(
    close: pd.Series, length: int, thresholds
) -> dict:
    """
    Estimate the price at which the *simple-average* RSI would reach each
    threshold level.

    NOTE: this uses a plain arithmetic-mean approximation of RSI (not the EWM
    method used by _series_rsi).  The two will agree closely on long series but
    may diverge on short ones.  The results are therefore indicative only.
    """
    if len(close) < length + 1:
        return {}

    delta = close.diff().dropna().tail(length)
    if len(delta) < length:
        return {}

    avg_gain = float(delta.clip(lower=0).mean())
    avg_loss = float((-delta.clip(upper=0)).mean())
    latest_price = float(close.iloc[-1])
    targets: dict = {}

    if avg_loss == 0 and avg_gain == 0:
        return targets

    current_rs = np.inf if avg_loss == 0 else avg_gain / avg_loss
    current_rsi = 100 - (100 / (1 + current_rs))

    for threshold in thresholds:
        if threshold <= 0 or threshold >= 100:
            continue
        target_rs = threshold / (100 - threshold)
        if threshold >= current_rsi:
            # FIX: multiplier is `length`, not `(length - 1)`
            required_gain = (target_rs * avg_loss - avg_gain) * length
            if required_gain >= 0:
                targets[threshold] = latest_price + required_gain
        else:
            if target_rs == 0:
                continue
            # FIX: multiplier is `length`, not `(length - 1)`
            required_loss = (avg_gain / target_rs - avg_loss) * length
            if required_loss >= 0:
                targets[threshold] = latest_price - required_loss

    return targets


def _next_sma_cross_price(
    close: pd.Series, fast_length: int, slow_length: int
) -> float | None:
    """Return the price at which the fast SMA would cross the slow SMA.

    Returns None if the result is non-positive (i.e. mathematically invalid
    as a price level).
    """
    if fast_length >= slow_length or len(close) < slow_length - 1:
        return None

    fast_tail = close.tail(fast_length - 1)
    slow_tail = close.tail(slow_length - 1)
    if len(fast_tail) < fast_length - 1 or len(slow_tail) < slow_length - 1:
        return None

    denominator = slow_length - fast_length  # always > 0 given the guard above
    numerator = (fast_length * float(slow_tail.sum())) - (slow_length * float(fast_tail.sum()))
    cross_price = numerator / denominator

    # A cross price that is zero or negative is not a valid price level.
    return cross_price if cross_price > 0 else None


def _add_level(levels: list, price, label="", key_point="", kind="neutral") -> None:
    price = _round_price(price)
    if price is None:
        return
    levels.append({
        "price": price,
        "label": label,
        "key_point": key_point,
        "kind": kind,
    })


def _add_window_extremes(levels: list, df: pd.DataFrame, window: int, label: str) -> None:
    if df.empty:
        return
    window_df = df.tail(min(window, len(df)))
    _add_level(levels, window_df["high_price_adj"].max(), f"{label} High", kind="resistance")
    _add_level(levels, window_df["low_price_adj"].min(), f"{label} Low", kind="support")


def _add_retracements(levels: list, df: pd.DataFrame, window: int, label: str) -> None:
    if df.empty:
        return
    window_df = df.tail(min(window, len(df)))
    high = float(window_df["high_price_adj"].max())
    low = float(window_df["low_price_adj"].min())
    price_range = high - low
    if price_range <= 0:
        return

    for ratio, ratio_label in ((0.382, "38.2%"), (0.5, "50%"), (0.618, "61.8%")):
        _add_level(
            levels,
            high - (price_range * ratio),
            key_point=f"{ratio_label} Retracement From {label} High",
            kind="turning",
        )
        _add_level(
            levels,
            low + (price_range * ratio),
            key_point=f"{ratio_label} Retracement From {label} Low",
            kind="turning",
        )


def _build_fibonacci_level_rows(work_df: pd.DataFrame, latest_price: float) -> list[dict[str, Any]]:
    fibonacci_levels = []
    _add_retracements(fibonacci_levels, work_df, 252, "52 Week")
    _add_retracements(fibonacci_levels, work_df, 65, "13 Week")
    _add_retracements(fibonacci_levels, work_df, 20, "4 Week")
    rows = _merge_levels(fibonacci_levels, latest_price)
    for row in rows:
        row["basis"] = "Fibonacci"
    return rows


def _build_bollinger_headline_levels(close: pd.Series, latest_price: float, period: int) -> tuple[dict[str, Any] | None, dict[str, Any] | None, dict[str, Any]]:
    window = close.tail(min(period, len(close)))
    if window.empty:
        return None, None, {}

    middle_band = float(window.mean())
    std = float(window.std(ddof=0))
    if not math.isfinite(middle_band) or not math.isfinite(std):
        return None, None, {}

    upper_band = middle_band + (2 * std)
    lower_band = middle_band - (2 * std)
    band_width = upper_band - lower_band
    percent_b = ((latest_price - lower_band) / band_width) if band_width > 0 else None

    metadata = {
        "period": int(period),
        "middle_band": round(middle_band, 2),
        "upper_band": round(upper_band, 2),
        "lower_band": round(lower_band, 2),
        "band_width": round(band_width, 2),
        "percent_b": round(float(percent_b), 4) if percent_b is not None and math.isfinite(percent_b) else None,
    }

    # Degenerate (flat) series: zero band width makes upper == lower, so the two
    # "levels" merge into one row that would be forced to both zones and served
    # as headline support AND resistance at the same price. Keep the metadata
    # (callers use middle band / %B) but emit no headline rows.
    if band_width <= 0:
        return None, None, metadata

    levels = []
    _add_level(
        levels,
        upper_band,
        "Bollinger Upper Band Resistance",
        f"{period} Day Bollinger Upper Band (+2σ)",
        kind="resistance",
    )
    _add_level(
        levels,
        lower_band,
        "Bollinger Lower Band Support",
        f"{period} Day Bollinger Lower Band (-2σ)",
        kind="support",
    )
    rows = _merge_levels(levels, latest_price)
    for row in rows:
        row["basis"] = "Bollinger Band"

    upper_row = next((row for row in rows if "Upper Band" in " ".join(row["level_names"])), None)
    lower_row = next((row for row in rows if "Lower Band" in " ".join(row["level_names"])), None)
    if upper_row:
        _force_level_zone(upper_row, "resistance")
    if lower_row:
        _force_level_zone(lower_row, "support")
    return upper_row, lower_row, metadata


def _force_level_zone(row: dict[str, Any], zone: str) -> None:
    row["zone"] = zone
    row["level_class"] = f"sr-level-{zone}"
    row["key_class"] = f"sr-key-{zone}"
    row["price_class"] = f"sr-price-{zone}"


def _merge_levels(levels: list, latest_price: float) -> list:
    """
    Deduplicate levels by price bucket and annotate each with distance metrics.

    FIX: The "latest" zone threshold is now percentage-based (_LATEST_ZONE_PCT)
    rather than the hardcoded absolute value of 0.005, which was too tight for
    high-priced instruments.
    """
    latest_threshold = latest_price * _LATEST_ZONE_PCT
    merged: dict = {}
    for level in levels:
        price = level["price"]
        bucket = merged.setdefault(price, {
            "price": price,
            "level_names": [],
            "key_points": [],
            "distance": round(price - latest_price, 2),
            "pct_distance": round(((price - latest_price) / latest_price) * 100, 2) if latest_price else 0,
            "zone": (
                "latest" if abs(price - latest_price) <= latest_threshold
                else ("resistance" if price > latest_price else "support")
            ),
        })
        if level["label"] and level["label"] not in bucket["level_names"]:
            bucket["level_names"].append(level["label"])
        if level["key_point"] and level["key_point"] not in bucket["key_points"]:
            bucket["key_points"].append(level["key_point"])

    for bucket in merged.values():
        bucket["level_class"] = f"sr-level-{bucket['zone']}"
        bucket["key_class"] = f"sr-key-{bucket['zone']}"
        bucket["price_class"] = f"sr-price-{bucket['zone']}"

    return sorted(merged.values(), key=lambda row: row["price"], reverse=True)


def _historical_window(work_df: pd.DataFrame, window: int) -> pd.DataFrame:
    historical = work_df.iloc[:-1]
    if historical.empty:
        historical = work_df
    return historical.tail(min(window, len(historical)))


def _round_number_levels(latest_price: float) -> str:
    if latest_price < 1:
        step = 0.1
    elif latest_price < 100:
        step = 5
    elif latest_price < 1000:
        step = 10
    elif latest_price < 5000:
        step = 50
    else:
        step = 100

    lower = math.floor(latest_price / step) * step
    upper = math.ceil(latest_price / step) * step
    if lower == upper:
        return f"{lower:.2f}"
    return f"{lower:.2f} / {upper:.2f}"


def _latest_gap_zone(work_df: pd.DataFrame) -> dict | None:
    """
    Return the most recent price gap (up or down) as a dict, or None.

    FIX: Replaced the slow Python for-loop with vectorised pandas operations.
    """
    if len(work_df) < 2:
        return None

    prev = work_df.iloc[:-1].reset_index(drop=True)
    curr = work_df.iloc[1:].reset_index(drop=True)

    gap_up_mask = curr["low_price_adj"] > prev["high_price_adj"]
    gap_down_mask = curr["high_price_adj"] < prev["low_price_adj"]

    # FIX: derive integer index positions from the boolean mask to guarantee
    # alignment when indexing both `curr` and `prev` DataFrames.
    gap_up_idx = gap_up_mask[gap_up_mask].index
    gap_down_idx = gap_down_mask[gap_down_mask].index

    gap_up_df = curr.loc[gap_up_idx].copy()
    gap_up_df["gap_type"] = "Gap Up"
    gap_up_df["gap_low"] = prev.loc[gap_up_idx, "high_price_adj"].values
    gap_up_df["gap_high"] = curr.loc[gap_up_idx, "low_price_adj"].values

    gap_down_df = curr.loc[gap_down_idx].copy()
    gap_down_df["gap_type"] = "Gap Down"
    gap_down_df["gap_low"] = curr.loc[gap_down_idx, "high_price_adj"].values
    gap_down_df["gap_high"] = prev.loc[gap_down_idx, "low_price_adj"].values

    all_gaps = pd.concat([gap_up_df, gap_down_df], ignore_index=True)
    if all_gaps.empty:
        return None

    latest = all_gaps.sort_values("business_date").iloc[-1]
    return {
        "type": latest["gap_type"],
        "date": latest["business_date"],
        "low": latest["gap_low"],
        "high": latest["gap_high"],
    }


def _high_volume_zone(work_df: pd.DataFrame) -> dict | None:
    """
    Return the high-volume bar's price zone over the last 120 bars.

    FIX: Use direct column access instead of Series.get() which is unreliable
    on pandas Series objects.
    """
    if "volume" not in work_df.columns or work_df["volume"].isna().all():
        return None

    volume_df = work_df.dropna(subset=["volume"]).tail(min(120, len(work_df)))
    if volume_df.empty:
        return None

    high_volume_row = volume_df.loc[volume_df["volume"].idxmax()]
    close_price = float(high_volume_row["close_price_adj"])

    if "open_price_adj" in volume_df.columns and pd.notna(high_volume_row["open_price_adj"]):
        open_price = float(high_volume_row["open_price_adj"])
        zone_type = "Accumulation" if close_price >= open_price else "Distribution"
    elif "previous_close_adj" in volume_df.columns and pd.notna(high_volume_row["previous_close_adj"]):
        previous_close = float(high_volume_row["previous_close_adj"])
        zone_type = "Accumulation" if close_price >= previous_close else "Distribution"
    else:
        zone_type = "High Volume"

    return {
        "type": zone_type,
        "date": high_volume_row["business_date"],
        "low": high_volume_row["low_price_adj"],
        "high": high_volume_row["high_price_adj"],
        "volume": high_volume_row["volume"],
    }


def _build_simple_level_rows(
    work_df: pd.DataFrame,
    latest_price: float,
    latest_high: float,
    latest_low: float,
) -> list:
    """
    Build a ranked list of simple, human-readable support/resistance rows.

    FIX: Fibonacci window now also excludes the latest bar (uses
    _historical_window) to be consistent with the swing-high/low windows.
    """
    rows = []
    previous_65 = _historical_window(work_df, 65)
    previous_20 = _historical_window(work_df, 20)
    previous_5 = _historical_window(work_df, 5)
    # FIX: was work_df.tail(252) — now excludes the latest bar for consistency.
    range_252 = _historical_window(work_df, 252)

    swing_high = previous_65["high_price_adj"].max() if not previous_65.empty else np.nan
    swing_low = previous_65["low_price_adj"].min() if not previous_65.empty else np.nan
    rows.append({
        "rank": 1,
        "basis": "Previous major swing high / low",
        "level": f"H {_format_price(swing_high)} / L {_format_price(swing_low)}",
        "note": "Uses the prior 13-week high/low as the major swing map.",
    })

    retest_high = previous_20["high_price_adj"].max() if not previous_20.empty else np.nan
    retest_low = previous_20["low_price_adj"].min() if not previous_20.empty else np.nan
    if pd.notna(retest_high) and latest_price > retest_high:
        retest_note = "Breakout retest support."
        retest_level = _format_price(retest_high)
    elif pd.notna(retest_low) and latest_price < retest_low:
        retest_note = "Breakdown retest resistance."
        retest_level = _format_price(retest_low)
    else:
        retest_note = "Watch prior range boundary for retest."
        retest_level = f"{_format_price(retest_low)} / {_format_price(retest_high)}"
    rows.append({
        "rank": 2,
        "basis": "Breakout or breakdown retest level",
        "level": retest_level,
        "note": retest_note,
    })

    week_high = previous_5["high_price_adj"].max() if not previous_5.empty else np.nan
    week_low = previous_5["low_price_adj"].min() if not previous_5.empty else np.nan
    month_high = previous_20["high_price_adj"].max() if not previous_20.empty else np.nan
    month_low = previous_20["low_price_adj"].min() if not previous_20.empty else np.nan
    rows.append({
        "rank": 3,
        "basis": "Previous week/month high or low",
        "level": f"W {_format_price(week_high)}/{_format_price(week_low)} | M {_format_price(month_high)}/{_format_price(month_low)}",
        "note": "Uses prior 5 and 20 trading days, excluding the latest bar.",
    })

    gap = _latest_gap_zone(work_df)
    rows.append({
        "rank": 4,
        "basis": "Gap zone",
        "level": _format_price_range(gap["low"], gap["high"]) if gap else "N/A",
        "note": f"{gap['type']} on {_format_date(gap['date'])}." if gap else "No recent daily gap detected in selected data.",
    })

    high_volume = _high_volume_zone(work_df)
    rows.append({
        "rank": 5,
        "basis": "High-volume accumulation/distribution zone",
        "level": _format_price_range(high_volume["low"], high_volume["high"]) if high_volume else "N/A",
        "note": (
            f"{high_volume['type']} zone from {_format_date(high_volume['date'])}; volume {int(high_volume['volume']):,}."
            if high_volume else
            "Volume data unavailable for this range."
        ),
    })

    rows.append({
        "rank": 6,
        "basis": "Round psychological number",
        "level": _round_number_levels(latest_price),
        "note": "Nearest round support/resistance magnets around latest price.",
    })

    # FIX: guard against empty range_252 before calling .max()/.min()
    if not range_252.empty:
        fib_high = float(range_252["high_price_adj"].max())
        fib_low = float(range_252["low_price_adj"].min())
    else:
        fib_high = fib_low = np.nan
    fib_range = fib_high - fib_low if (not np.isnan(fib_high) and not np.isnan(fib_low)) else 0
    fib_50 = fib_high - (fib_range * 0.5) if fib_range > 0 else np.nan
    fib_618 = fib_high - (fib_range * 0.618) if fib_range > 0 else np.nan
    rows.append({
        "rank": 7,
        "basis": "Fibonacci 50% or 61.8%",
        "level": f"50% {_format_price(fib_50)} | 61.8% {_format_price(fib_618)}",
        "note": "Uses the prior 252 trading rows, excluding the latest bar.",
    })

    pivot = (latest_high + latest_low + latest_price) / 3
    r1 = (2 * pivot) - latest_low
    r2 = pivot + (latest_high - latest_low)
    s1 = (2 * pivot) - latest_high
    s2 = pivot - (latest_high - latest_low)
    rows.append({
        "rank": 8,
        "basis": "Pivot S1/S2/R1/R2",
        "level": f"S1 {_format_price(s1)} / S2 {_format_price(s2)} | R1 {_format_price(r1)} / R2 {_format_price(r2)}",
        "note": "Classic daily pivot levels from latest H/L/C.",
    })

    return rows


# ---------------------------------------------------------------------------
# Confluence-based nearest support / resistance
# ---------------------------------------------------------------------------
#
# Bollinger bands are a volatility envelope, not real support/resistance. The
# headline cards instead pick the nearest CONFLUENCE zone: the actual reaction
# levels the sheet already computes (pivots, swing highs/lows, Fibonacci, MAs,
# std-dev, RSI/stochastic targets, prior H/L) plus the dominant volume node,
# clustered into zones and ranked by how many independent methods agree.

def _classify_level_methods(labels: list[str]) -> dict[str, float]:
    """Map a merged level's labels to the method families it represents.

    Methods where price actually reacted (swings, volume nodes) outweigh derived
    targets (RSI / stochastic / std-dev). Returns {method_name: weight}.
    """
    joined = " ".join(str(label) for label in labels).lower()
    label_set = {str(label).strip().lower() for label in labels}
    methods: dict[str, float] = {}
    if any(k in joined for k in ("week high", "week low", "month high", "month low", "swing")):
        methods["Swing"] = 3.0
    if "pivot" in joined:
        methods["Pivot"] = 2.0
    if "retracement" in joined or "fibonacci" in joined:
        methods["Fibonacci"] = 2.0
    if "moving average" in joined:
        methods["Moving Average"] = 1.5
    if "stochastic" in joined:
        methods["Stochastic"] = 1.0
    if "rsi" in joined:
        methods["RSI"] = 1.0
    if "standard deviation" in joined:
        methods["Std Dev"] = 1.0
    if "bollinger" in joined:
        methods["Bollinger"] = 1.0
    if label_set & {"high", "low", "previous close", "latest"}:
        methods["HLC"] = 1.5
    return methods


def _find_swings(work_df: pd.DataFrame, window: int = 5):
    """Detect fractal swing highs / lows: a bar that is the UNIQUE extreme within
    +/- `window` bars (so the most recent `window` bars are never pivots, which is
    correct — a swing is only confirmed once price moves away from it).

    Returns (swing_highs, swing_lows), each oldest -> newest as {price, date}.
    """
    n = len(work_df)
    if n < (2 * window + 1):
        return [], []
    highs = work_df["high_price_adj"].to_numpy(dtype=float)
    lows = work_df["low_price_adj"].to_numpy(dtype=float)
    dates = work_df["business_date"].to_numpy()
    swing_highs, swing_lows = [], []
    for i in range(window, n - window):
        hw = highs[i - window:i + window + 1]
        lw = lows[i - window:i + window + 1]
        if highs[i] >= hw.max() and (hw == highs[i]).sum() == 1:
            swing_highs.append({"price": round(float(highs[i]), 2), "date": _format_date(dates[i])})
        if lows[i] <= lw.min() and (lw == lows[i]).sum() == 1:
            swing_lows.append({"price": round(float(lows[i]), 2), "date": _format_date(dates[i])})
    return swing_highs, swing_lows


def _build_confluence_levels(
    rows: list[dict[str, Any]],
    latest_price: float,
    work_df: pd.DataFrame,
    density_zones: list[dict[str, Any]] | None = None,
    swing_levels: list[dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    """Cluster the engine's reaction levels (plus the dominant volume node, the
    DBSCAN volume-density zones from the advanced layer, and the latest fractal
    swing highs/lows) into confluence zones, ranked by how many independent
    methods agree."""
    if not rows or latest_price <= 0:
        return []
    tolerance = max(latest_price * 0.006, 0.01)  # ~0.6 % price band

    candidates = []
    for row in rows:
        if row.get("zone") == "latest":
            continue
        labels = list(row.get("level_names") or []) + list(row.get("key_points") or [])
        methods = _classify_level_methods(labels)
        if not methods:
            methods = {"Level": 0.5}
        candidates.append({
            "price": float(row["price"]),
            "methods": methods,
            "labels": [label for label in (row.get("level_names") or row.get("key_points") or []) if label],
        })

    volume_node = _high_volume_zone(work_df)
    if volume_node:
        center = (float(volume_node["low"]) + float(volume_node["high"])) / 2.0
        candidates.append({
            "price": round(center, 2),
            "methods": {"Volume": 3.0},
            "labels": [f"{volume_node['type']} volume node"],
        })

    # Fold in the full DBSCAN volume-density zones (strongest institutional S/R).
    for zone in (density_zones or []):
        center = _safe_float(zone.get("center"))
        if center is None or center <= 0:
            continue
        detail = []
        touches = _safe_float(zone.get("touches"))
        density = _safe_float(zone.get("volume_density"))
        if touches is not None:
            detail.append(f"{int(touches)} touches")
        if density is not None:
            detail.append(f"{density:.0f}% vol")
        label = "Volume profile zone" + (f" ({', '.join(detail)})" if detail else "")
        candidates.append({
            "price": round(float(center), 2),
            "methods": {"Volume Profile": 3.0},
            "labels": [label],
        })

    # Fold in the latest fractal swing highs / lows — real reaction points that
    # anchor a meaningful structural range (rather than the derived levels that
    # cluster tightly around price).
    for swing in (swing_levels or []):
        price = _safe_float(swing.get("price"))
        if price is None or price <= 0:
            continue
        candidates.append({
            "price": round(float(price), 2),
            "methods": {"Swing": 3.0},
            "labels": [swing.get("label") or "Swing level"],
        })

    if not candidates:
        return []

    candidates.sort(key=lambda c: c["price"])
    clusters: list[list[dict[str, Any]]] = []
    current: list[dict[str, Any]] = []
    for cand in candidates:
        if not current:
            current = [cand]
            continue
        # Complete-linkage: bound the cluster's total SPAN (first→candidate,
        # candidates are price-sorted) by the tolerance. Comparing to the
        # running MEAN lets near-equally-spaced levels daisy-chain into a zone
        # wider than the tolerance — wide enough to straddle the latest price,
        # at which point the ladder drops it from BOTH sides and a genuine
        # nearby level silently disappears.
        if cand["price"] - current[0]["price"] <= tolerance:
            current.append(cand)
        else:
            clusters.append(current)
            current = [cand]
    if current:
        clusters.append(current)

    zones = []
    for cluster in clusters:
        method_weights: dict[str, float] = {}
        for c in cluster:
            for name, weight in c["methods"].items():
                method_weights[name] = max(method_weights.get(name, 0.0), weight)
        prices = [c["price"] for c in cluster]
        cand_weights = [sum(c["methods"].values()) or 0.5 for c in cluster]
        weighted_center = sum(p * w for p, w in zip(prices, cand_weights)) / (sum(cand_weights) or 1.0)
        labels = []
        for c in cluster:
            for label in c["labels"]:
                if label not in labels:
                    labels.append(label)
        real_methods = [m for m in method_weights if m != "Level"]
        total_weight = sum(method_weights[m] for m in real_methods) or 0.5
        zones.append({
            "low": round(min(prices), 2),
            "high": round(max(prices), 2),
            "center": round(weighted_center, 2),
            "methods": sorted(real_methods) or ["Level"],
            "method_count": len(real_methods),
            "score": round(total_weight * (1 + 0.15 * max(len(real_methods) - 1, 0)), 2),
            "labels": labels[:6],
        })
    zones.sort(key=lambda z: z["center"])
    return zones


def _ladder_row(zone: dict[str, Any], latest_price: float, label: str) -> dict[str, Any]:
    pct = round(((zone["center"] - latest_price) / latest_price) * 100, 2) if latest_price else 0.0
    return {
        "label": label,
        "price": zone["center"],
        "low": zone["low"],
        "high": zone["high"],
        "basis": "Confluence",
        "pct_distance": pct,
        "method_count": zone["method_count"],
        "methods": zone["methods"],
        "key_points": zone["labels"] or zone["methods"],
        "score": zone["score"],
    }


def _build_confluence_ladder(zones, latest_price, max_each: int = 5):
    """Return (resistances, supports): ranked confluence zones above/below price.

    Each entry is the *average* (conviction-weighted) of the levels that agree
    at that price. Resistances are labelled R1.. (nearest first), supports S1..
    """
    def qualified(zone):
        return (
            zone["method_count"] >= 2
            or "Swing" in zone["methods"]
            or "Volume" in zone["methods"]
            or "Volume Profile" in zone["methods"]
        )

    # Use the zone's NEAR EDGE, not its centre: a zone whose band straddles the
    # latest price (low < price < high) is the zone price is trading *inside*, so
    # it is neither resistance nor support. Resistance must sit wholly above the
    # price (low >= price); support wholly below (high <= price).
    # Rank by the same NEAR EDGE used to qualify: ordering by centre can put a
    # wide zone whose edge is closest to price behind a narrow farther one, so
    # R1/S1 would not be the truly nearest level.
    # The far edge must sit strictly beyond the price: a zero-width zone pinned
    # exactly at the latest price (low == high == price) would otherwise pass
    # BOTH filters and list as R1 and S1 at 0.00% distance. It is the zone price
    # is trading inside — _detect_price_zone surfaces it instead.
    res_zones = sorted(
        [z for z in zones if z["low"] >= latest_price and z["high"] > latest_price and qualified(z)],
        key=lambda z: z["low"],
    )
    sup_zones = sorted(
        [z for z in zones if z["high"] <= latest_price and z["low"] < latest_price and qualified(z)],
        key=lambda z: z["high"],
        reverse=True,
    )
    resistances = [_ladder_row(z, latest_price, f"R{i}") for i, z in enumerate(res_zones[:max_each], start=1)]
    supports = [_ladder_row(z, latest_price, f"S{i}") for i, z in enumerate(sup_zones[:max_each], start=1)]
    return resistances, supports


def _detect_price_zone(zones, latest_price):
    """Return the strongest confluence zone the latest price is trading inside,
    or None. Surfaced so the UI can explain why support/resistance sit so close.
    """
    inside = [
        z for z in zones
        if z["low"] <= latest_price <= z["high"]
        and (
            z["method_count"] >= 2
            or "Swing" in z["methods"]
            or "Volume" in z["methods"]
            or "Volume Profile" in z["methods"]
        )
    ]
    if not inside:
        return None
    z = max(inside, key=lambda z: z.get("score", 0))
    return {
        "low": z["low"],
        "high": z["high"],
        "center": z["center"],
        "method_count": z["method_count"],
        "methods": z["methods"],
        "key_points": (z.get("labels") or z["methods"])[:4],
    }


def _bollinger_headline(boll_row: dict[str, Any] | None, band_name: str) -> dict[str, Any] | None:
    if not boll_row:
        return None
    labels = boll_row.get("level_names") or boll_row.get("key_points") or [band_name]
    return {
        "price": boll_row["price"],
        "low": boll_row["price"],
        "high": boll_row["price"],
        "basis": "Bollinger Band",
        "method_count": 1,
        "methods": ["Bollinger"],
        "key_points": labels,
        "score": 1.0,
    }


# ---------------------------------------------------------------------------
# Pivot-average support / resistance (Classic + Camarilla + Woodie + Fibonacci),
# plus 52-week Fibonacci retracement. Averages each tier across the four methods
# to give a wider, more robust S/R map than the confluence engine.
# ---------------------------------------------------------------------------

def _round2(value):
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    # round(nan)/round(inf) return nan/inf rather than raising — block them
    # here so a poisoned input can't surface as "nan" in the rendered table.
    return round(parsed, 2) if math.isfinite(parsed) else None


def _classic_pivot(h, l, c):
    p = (h + l + c) / 3.0
    rng = h - l
    return {
        "name": "Classic", "pivot": p,
        "r1": 2 * p - l, "r2": p + rng, "r3": h + 2 * (p - l),
        "s1": 2 * p - h, "s2": p - rng, "s3": l - 2 * (h - p),
    }


def _woodie_pivot(h, l, c):
    p = (h + l + 2 * c) / 4.0
    rng = h - l
    return {
        "name": "Woodie", "pivot": p,
        "r1": 2 * p - l, "r2": p + rng, "r3": h + 2 * (p - l),
        "s1": 2 * p - h, "s2": p - rng, "s3": l - 2 * (h - p),
    }


def _fibonacci_pivot(h, l, c):
    p = (h + l + c) / 3.0
    rng = h - l
    return {
        "name": "Fibonacci", "pivot": p,
        "r1": p + 0.382 * rng, "r2": p + 0.618 * rng, "r3": p + rng,
        "s1": p - 0.382 * rng, "s2": p - 0.618 * rng, "s3": p - rng,
    }


def _camarilla_pivot(h, l, c):
    rng = h - l
    return {
        "name": "Camarilla", "pivot": (h + l + c) / 3.0,
        "r1": c + rng * 1.1 / 12, "r2": c + rng * 1.1 / 6, "r3": c + rng * 1.1 / 4,
        "s1": c - rng * 1.1 / 12, "s2": c - rng * 1.1 / 6, "s3": c - rng * 1.1 / 4,
    }


def compute_pivot_average(high, low, close, wk52_high=None, wk52_low=None, latest_price=None):
    """Average of the four pivot methods per tier + 52-week Fibonacci levels."""
    try:
        h, l, c = float(high), float(low), float(close)
    except (TypeError, ValueError):
        return None
    if not (h >= l) or h <= 0:
        return None

    methods = [_classic_pivot(h, l, c), _camarilla_pivot(h, l, c),
               _woodie_pivot(h, l, c), _fibonacci_pivot(h, l, c)]

    keys = ("pivot", "r1", "r2", "r3", "s1", "s2", "s3")
    average = {k: _round2(sum(m[k] for m in methods) / len(methods)) for k in keys}

    method_rows = [{k: _round2(m[k]) for k in keys} | {"name": m["name"]} for m in methods]

    # 52-week Fibonacci retracement (absolute price levels off the real range).
    price = _safe_float(latest_price)
    fib_52 = []
    hi52, lo52 = _safe_float(wk52_high), _safe_float(wk52_low)
    if hi52 and lo52 and hi52 > lo52:
        wk_rng = hi52 - lo52
        for ratio in (0.236, 0.382, 0.5, 0.618, 0.786):
            level = _round2(hi52 - ratio * wk_rng)
            kind = "neutral"
            if price is not None and level is not None:
                kind = "resistance" if level > price else "support"
            fib_52.append({"label": f"{ratio * 100:.1f}%", "price": level, "kind": kind})

    return {
        "input": {
            "high": _round2(h), "low": _round2(l), "close": _round2(c),
            "wk52_high": _round2(hi52), "wk52_low": _round2(lo52),
        },
        "methods": method_rows,
        "average": average,
        "resistances": [average["r1"], average["r2"], average["r3"]],
        "supports": [average["s1"], average["s2"], average["s3"]],
        "fib_52week": fib_52,
    }


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def run_support_resistance_analysis(
    df: pd.DataFrame,
    symbol: str = "",
    std_period: int = 20,
    rsi_length: int = 14,
    stochastic_length: int = 14,
    enabled_families=None,
    density_zones=None,
    fractal_window: int = 5,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """
    Compute support and resistance levels for a price series.

    Parameters
    ----------
    df : pd.DataFrame
        OHLC data.  Required columns: business_date, high_price_adj,
        low_price_adj, close_price_adj.  Optional: open_price_adj, volume.
    symbol : str
        Ticker symbol for display purposes only.
    std_period : int
        Look-back period for standard-deviation bands (default 20).
    rsi_length : int
        RSI period (default 14).
    stochastic_length : int
        Stochastic %K period (default 14).
    enabled_families : iterable or None
        Subset of DEFAULT_LEVEL_FAMILIES to compute.  None = all families.
    fractal_window : int
        +/- bars a swing high/low must dominate to count as a fractal pivot
        in the confluence engine (default 5).

    Returns
    -------
    metrics : dict
        Summary statistics and nearest levels.
    rows : list[dict]
        All merged S/R levels sorted by price descending.
    """
    std_period = _coerce_int(std_period, 20, minimum=2)
    rsi_length = _coerce_int(rsi_length, 14, minimum=2)
    stochastic_length = _coerce_int(stochastic_length, 14, minimum=2)
    fractal_window = _coerce_int(fractal_window, 5, minimum=2)
    enabled_families = _normalize_level_families(enabled_families)

    if df is None or df.empty:
        return {"error": "No price data was supplied."}, []

    required_columns = {"business_date", "high_price_adj", "low_price_adj", "close_price_adj"}
    if not required_columns.issubset(df.columns):
        return {"error": "Price data is missing required OHLC columns."}, []

    work_df = df.copy()
    work_df["business_date"] = pd.to_datetime(work_df["business_date"])
    for column in ["open_price_adj", "high_price_adj", "low_price_adj", "close_price_adj", "volume"]:
        if column not in work_df.columns:
            continue
        work_df[column] = pd.to_numeric(work_df[column], errors="coerce")
    work_df = (
        work_df.dropna(subset=["business_date", "high_price_adj", "low_price_adj", "close_price_adj"])
        .sort_values("business_date")
        .reset_index(drop=True)
    )

    if len(work_df) < 2:
        return {"error": "At least two price rows are required for support/resistance analysis."}, []

    close = work_df["close_price_adj"]
    work_df["previous_close_adj"] = close.shift(1)
    latest_row = work_df.iloc[-1]
    previous_row = work_df.iloc[-2]
    latest_price = float(latest_row["close_price_adj"])
    latest_high = float(latest_row["high_price_adj"])
    latest_low = float(latest_row["low_price_adj"])
    previous_close = float(previous_row["close_price_adj"])
    levels = []

    # Pivot-average S/R (Classic + Camarilla + Woodie + Fibonacci) + 52-week Fib,
    # using the latest session's H/L/C and the 52-week high/low from the data.
    _wk = work_df.tail(min(252, len(work_df)))
    pivot_average = compute_pivot_average(
        latest_high, latest_low, latest_price,
        wk52_high=float(_wk["high_price_adj"].max()),
        wk52_low=float(_wk["low_price_adj"].min()),
        latest_price=latest_price,
    )

    pivot = (latest_high + latest_low + latest_price) / 3
    price_range = latest_high - latest_low

    if "hlc" in enabled_families:
        _add_level(levels, latest_high, "High", "High", "resistance")
        _add_level(levels, latest_low, "Low", "Low", "support")
        _add_level(levels, previous_close, "Previous Close", "Previous Close", "neutral")
        _add_level(levels, latest_price, "Latest", "Latest", "latest")

    if "pivots" in enabled_families:
        _add_level(levels, pivot, "Pivot Point", "Pivot Point", "neutral")
        _add_level(levels, (2 * pivot) - latest_low, "Pivot Point 1st Resistance Point", kind="resistance")
        _add_level(levels, pivot + price_range, "Pivot Point 2nd Level Resistance", kind="resistance")
        _add_level(levels, latest_high + (2 * (pivot - latest_low)), "Pivot Point 3rd Level Resistance", kind="resistance")
        _add_level(levels, (2 * pivot) - latest_high, "Pivot Point 1st Support Point", kind="support")
        _add_level(levels, pivot - price_range, "Pivot Point 2nd Support Point", kind="support")
        _add_level(levels, latest_low - (2 * (latest_high - pivot)), "Pivot Point 3rd Support Point", kind="support")

    if "highs_lows" in enabled_families:
        _add_window_extremes(levels, work_df, 252, "52-Week")
        _add_window_extremes(levels, work_df, 65, "13-Week")
        _add_window_extremes(levels, work_df, 20, "1-Month")

    if "fibonacci" in enabled_families:
        _add_retracements(levels, work_df, 252, "52 Week")
        _add_retracements(levels, work_df, 65, "13 Week")
        _add_retracements(levels, work_df, 20, "4 Week")

    if "standard_deviation" in enabled_families:
        std_window = close.tail(min(std_period, len(close)))
        rolling_mean = float(std_window.mean())
        rolling_std = float(std_window.std(ddof=0))
        if rolling_std > 0:
            for deviation in (1, 2, 3):
                label = "Standard Deviation" if deviation == 1 else "Standard Deviations"
                _add_level(levels, rolling_mean + (rolling_std * deviation), f"Price {deviation} {label} Resistance", kind="resistance")
                _add_level(levels, rolling_mean - (rolling_std * deviation), f"Price {deviation} {label} Support", kind="support")

    if "moving_averages" in enabled_families:
        for length in (9, 18, 40, 50, 90, 200):
            if len(close) >= length:
                _add_level(levels, close.rolling(length).mean().iloc[-1], key_point=f"Price Crosses {length} Day Moving Average", kind="turning")

        for fast_length, slow_length in ((3, 10), (9, 18), (18, 40), (50, 200)):
            cross_price = _next_sma_cross_price(close, fast_length, slow_length)
            if cross_price is not None:
                _add_level(levels, cross_price, key_point=f"Price Crosses {fast_length}-{slow_length} Day Moving Average", kind="turning")

    # FIX: stochastic_k computation moved inside the enabled-families guard to
    # avoid wasted work when stochastics are disabled.
    stochastic_k = None
    if "stochastics" in enabled_families:
        stochastic_df = work_df.tail(min(stochastic_length, len(work_df)))
        stoch_high = float(stochastic_df["high_price_adj"].max())
        stoch_low = float(stochastic_df["low_price_adj"].min())
        stoch_range = stoch_high - stoch_low
        if stoch_range > 0:
            stochastic_k = ((latest_price - stoch_low) / stoch_range) * 100
            for threshold in (20, 30, 50, 70, 80):
                _add_level(
                    levels,
                    stoch_low + (stoch_range * threshold / 100),
                    key_point=f"{stochastic_length}-Day Raw Stochastic %K at {threshold}%",
                    kind="turning",
                )

    rsi_series = _series_rsi(close, rsi_length)
    latest_rsi = rsi_series.iloc[-1] if not rsi_series.empty else np.nan
    if "rsi" in enabled_families:
        for threshold, target_price in _latest_simple_rsi_target_prices(close, rsi_length, (20, 30, 50, 70, 80)).items():
            _add_level(levels, target_price, f"{rsi_length} Day RSI at {threshold}%", kind="turning")

    rows = _merge_levels(levels, latest_price)
    resistance_rows = [row for row in rows if row["price"] > latest_price]
    support_rows = [row for row in rows if row["price"] < latest_price]

    # Bollinger bands are kept only as a volatility reference (and to drive the
    # institutional premium/discount read). The headline nearest support /
    # resistance now use the CONFLUENCE engine: real reaction levels clustered
    # into zones and ranked by how many independent methods agree. Bollinger is
    # used only as a fallback when too few levels exist to form a zone.
    boll_upper, boll_lower, bollinger_bands = _build_bollinger_headline_levels(
        close,
        latest_price,
        std_period,
    )
    swing_highs, swing_lows = _find_swings(work_df, window=fractal_window)
    swing_levels = (
        [{"price": s["price"], "label": f"Swing high {s['date']}"} for s in swing_highs[-12:]]
        + [{"price": s["price"], "label": f"Swing low {s['date']}"} for s in swing_lows[-12:]]
    )
    # Latest swing pivot above / below the current price (the structural range).
    latest_swing_high = next((s for s in reversed(swing_highs) if s["price"] >= latest_price), None)
    latest_swing_low = next((s for s in reversed(swing_lows) if s["price"] <= latest_price), None)

    confluence_zones = _build_confluence_levels(
        rows, latest_price, work_df, density_zones=density_zones, swing_levels=swing_levels
    )
    confluence_resistances, confluence_supports = _build_confluence_ladder(confluence_zones, latest_price)
    price_zone = _detect_price_zone(confluence_zones, latest_price)
    # Bollinger fallback must stay on the correct side of the price: after a
    # close OUTSIDE the bands (%B > 1 or < 0 — new range extremes, exactly when
    # no confluence zone qualifies on that side) the band would otherwise be
    # served as a headline "support" above price / "resistance" below it,
    # inverting the R/R math and the SNR wall reads downstream.
    nearest_resistance = confluence_resistances[0] if confluence_resistances else None
    if nearest_resistance is None:
        boll_res = _bollinger_headline(boll_upper, "Bollinger Upper Band Resistance")
        nearest_resistance = boll_res if boll_res and boll_res["price"] > latest_price else None
    nearest_support = confluence_supports[0] if confluence_supports else None
    if nearest_support is None:
        boll_sup = _bollinger_headline(boll_lower, "Bollinger Lower Band Support")
        nearest_support = boll_sup if boll_sup and boll_sup["price"] < latest_price else None
    nearest_level_basis = (nearest_resistance or nearest_support or {}).get("basis", "Confluence")

    support_distance = abs(latest_price - nearest_support["price"]) if nearest_support else None
    resistance_distance = abs(nearest_resistance["price"] - latest_price) if nearest_resistance else None
    risk_reward_ratio = None
    if support_distance and support_distance > 0 and resistance_distance is not None:
        risk_reward_ratio = resistance_distance / support_distance

    sma_50 = close.rolling(50).mean().iloc[-1] if len(close) >= 50 else np.nan
    sma_200 = close.rolling(200).mean().iloc[-1] if len(close) >= 200 else np.nan
    if pd.notna(sma_50) and pd.notna(sma_200):
        if latest_price > sma_50 > sma_200:
            trend_bias = "Bullish"
        elif latest_price < sma_50 < sma_200:
            trend_bias = "Bearish"
        else:
            trend_bias = "Mixed"
    elif latest_price > pivot:
        trend_bias = "Above Pivot"
    else:
        trend_bias = "Below Pivot"

    warnings = []
    if not enabled_families:
        warnings.append("No level families selected; enable at least one checkbox to generate support/resistance rows.")
    if ("highs_lows" in enabled_families or "fibonacci" in enabled_families) and len(work_df) < 252:
        warnings.append("52-week levels use all available rows because the selected range has fewer than 252 trading rows.")
    if ("highs_lows" in enabled_families or "fibonacci" in enabled_families) and len(work_df) < 65:
        warnings.append("13-week levels use all available rows because the selected range has fewer than 65 trading rows.")

    metrics: dict[str, Any] = {
        "symbol": symbol,
        "latest_data_date": _format_date(latest_row["business_date"]),
        "latest_price": round(latest_price, 2),
        "latest_price_source": latest_row.get("price_source", "Adjusted") if hasattr(latest_row, "get") else "Adjusted",
        "previous_close": round(previous_close, 2),
        "latest_high": round(latest_high, 2),
        "latest_low": round(latest_low, 2),
        "pivot": round(pivot, 2),
        "rows_used": int(len(work_df)),
        "levels_count": int(len(rows)),
        "resistance_count": int(len(resistance_rows)),
        "support_count": int(len(support_rows)),
        "nearest_resistance": nearest_resistance,
        "nearest_support": nearest_support,
        "nearest_level_basis": nearest_level_basis,
        "confluence_resistances": confluence_resistances,
        "confluence_supports": confluence_supports,
        "price_zone": price_zone,
        "latest_swing_high": latest_swing_high,
        "latest_swing_low": latest_swing_low,
        "pivot_average": pivot_average,
        "bollinger_bands": bollinger_bands,
        "resistance_distance_pct": round((resistance_distance / latest_price) * 100, 2) if resistance_distance is not None else None,
        "support_distance_pct": round((support_distance / latest_price) * 100, 2) if support_distance is not None else None,
        "risk_reward_ratio": round(risk_reward_ratio, 2) if risk_reward_ratio is not None else None,
        "latest_rsi": round(float(latest_rsi), 2) if pd.notna(latest_rsi) else None,
        "latest_stochastic_k": round(float(stochastic_k), 2) if stochastic_k is not None and math.isfinite(stochastic_k) else None,
        "trend_bias": trend_bias,
        "warnings": warnings,
        "std_period": std_period,
        "rsi_length": rsi_length,
        "stochastic_length": stochastic_length,
        "enabled_families": sorted(enabled_families),
        "simple_level_rows": _build_simple_level_rows(work_df, latest_price, latest_high, latest_low),
    }

    return metrics, rows


# ---------------------------------------------------------------------------
# Institutional Multi-Framework Reading Engine
# ---------------------------------------------------------------------------
#
# Shared formatting helpers
# ---------------------------------------------------------------------------

def _safe_float(value) -> float | None:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    return parsed if math.isfinite(parsed) else None


def _price_text(value) -> str:
    parsed = _safe_float(value)
    return f"{parsed:.2f}" if parsed is not None else "N/A"


def _zone_text(zone: dict[str, Any]) -> str:
    if not zone:
        return "N/A"
    return f"{_price_text(zone.get('low'))}-{_price_text(zone.get('high'))}"


def _zone_touches(zone: dict[str, Any] | None) -> int:
    value = _safe_float((zone or {}).get("touches"))
    return int(value) if value is not None else 0


def _dir_from_text(text: Any) -> str:
    lowered = str(text or "").strip().lower()
    if lowered.startswith("bull"):
        return "Bullish"
    if lowered.startswith("bear"):
        return "Bearish"
    return "Neutral"


def _premium_discount(percent_b, trend_bias) -> tuple[str, float | None]:
    """Where price sits inside its dealing range (Bollinger %B).

    >0.55 = premium (institutional sell zone), <0.45 = discount (buy zone),
    else equilibrium. Falls back to the SMA trend bias when %B is unavailable.
    """
    pb = _safe_float(percent_b)
    if pb is None:
        bias = str(trend_bias or "")
        if "Bull" in bias or "Above" in bias:
            return "Premium", None
        if "Bear" in bias or "Below" in bias:
            return "Discount", None
        return "Equilibrium", None
    if pb > 0.55:
        return "Premium", pb
    if pb < 0.45:
        return "Discount", pb
    return "Equilibrium", pb


def _pack(logic: str, sentiment: str, status: str, signal: str, confidence: float, **extra) -> dict[str, Any]:
    row = {
        "institutional_logic": logic,
        "price_sentiment": sentiment,
        "status": status,
        "signal": signal if signal in {"Bullish", "Bearish", "Neutral"} else "Neutral",
        "confidence": int(max(0, min(100, round(confidence)))),
    }
    row.update(extra)
    return row


def _build_institutional_context(support_metrics: dict[str, Any], advanced: dict[str, Any]) -> dict[str, Any]:
    """Pre-compute every data facet the nine framework reads draw from."""
    boll = support_metrics.get("bollinger_bands") or {}
    price = _safe_float(support_metrics.get("latest_price"))
    pd_zone, pb = _premium_discount(boll.get("percent_b"), support_metrics.get("trend_bias"))

    nearest_res = support_metrics.get("nearest_resistance") or {}
    nearest_sup = support_metrics.get("nearest_support") or {}

    zones = advanced.get("density_zones") or []
    supply_zone = demand_zone = None
    if price is not None:
        supplies = [z for z in zones if _safe_float(z.get("center")) is not None and _safe_float(z.get("center")) > price]
        demands = [z for z in zones if _safe_float(z.get("center")) is not None and _safe_float(z.get("center")) < price]
        if supplies:
            supply_zone = min(supplies, key=lambda z: _safe_float(z.get("center")))
        if demands:
            demand_zone = max(demands, key=lambda z: _safe_float(z.get("center")))

    structure_events = advanced.get("structure_events") or []
    sweeps = advanced.get("liquidity_sweeps") or []
    profile = advanced.get("profile") or {}
    baselines = advanced.get("baselines") or {}
    chart = advanced.get("chart") or {}
    candles = chart.get("candles") or []
    zones_sorted = sorted(zones, key=lambda z: _safe_float(z.get("strength")) or 0.0, reverse=True)

    last_event = structure_events[-1] if structure_events else {}
    # A CHoCH is a change-of-character CALL — "reversal expected soon" — and that
    # expectation decays. Without an age check the latest CHoCH, however old,
    # keeps announcing an imminent reversal at full confidence every day (the
    # audit measured ZERO Neutral votes from SMC/RTM/Wyckoff across 36 symbols,
    # because there is always a "latest event"). A BOS is different: broken
    # structure is a persistent state and stays honoured at any age. So expose
    # whether the event printed within the last 15 sessions and let the readers
    # decay a stale CHoCH instead of repeating it.
    event_recent = bool(
        last_event
        and last_event.get("date") in {c.get("date") for c in candles[-15:] if c.get("date")}
    ) if candles else bool(last_event)
    # A liquidity sweep is an EVENT whose expected reaction plays out within a
    # few sessions — unlike structure (BOS/CHoCH), it is not a persistent state.
    # Without a recency gate, a sweep from months ago keeps driving the SMC/ICT,
    # BTMM and Wyckoff reads ("Spring — mark-up expected") every day after.
    # Only honour a sweep that printed within the last 10 chart sessions; the
    # dates share the same YYYY-MM-DD formatter, so set membership is exact.
    last_sweep = sweeps[-1] if sweeps else {}
    recent_dates = {c.get("date") for c in candles[-10:] if c.get("date")}
    if last_sweep and recent_dates and last_sweep.get("date") not in recent_dates:
        last_sweep = {}
    sweep_type = str(last_sweep.get("type") or "")
    if "Sell-side" in sweep_type:
        sweep_side = "Sell-side"
    elif "Buy-side" in sweep_type:
        sweep_side = "Buy-side"
    elif sweep_type:
        sweep_side = "Trendline"
    else:
        sweep_side = None

    return {
        "price": price,
        "pivot": _safe_float(support_metrics.get("pivot")),
        "trend_bias": str(support_metrics.get("trend_bias") or "Mixed"),
        "rsi": _safe_float(support_metrics.get("latest_rsi")),
        "stoch": _safe_float(support_metrics.get("latest_stochastic_k")),
        "rr": _safe_float(support_metrics.get("risk_reward_ratio")),
        "support": _safe_float(nearest_sup.get("price")),
        "resistance": _safe_float(nearest_res.get("price")),
        "support_pct": _safe_float(support_metrics.get("support_distance_pct")),
        "resistance_pct": _safe_float(support_metrics.get("resistance_distance_pct")),
        "percent_b": pb,
        "pd_zone": pd_zone,
        "vwap": _safe_float(baselines.get("latest_vwap")),
        "hma": _safe_float(baselines.get("latest_hma")),
        "poc": _safe_float(profile.get("poc")),
        "vah": _safe_float(profile.get("vah")),
        "val": _safe_float(profile.get("val")),
        "last_event": last_event,
        "event_type": str(last_event.get("event") or ""),
        "event_dir": _dir_from_text(last_event.get("direction")),
        "event_recent": event_recent,
        "last_sweep": last_sweep,
        "sweep_side": sweep_side,
        "sweep_level": _safe_float(last_sweep.get("level")),
        "supply_zone": supply_zone,
        "demand_zone": demand_zone,
        "strongest_zone": zones_sorted[0] if zones_sorted else {},
        "candle": candles[-1] if candles else {},
    }


# ---------------------------------------------------------------------------
# 1. SMC / ICT — structure + liquidity + premium/discount
# ---------------------------------------------------------------------------

def _read_smc_ict(ctx: dict[str, Any]) -> dict[str, Any]:
    et, ed = ctx["event_type"], ctx["event_dir"]
    side, pd_zone, pb = ctx["sweep_side"], ctx["pd_zone"], ctx["percent_b"]
    pb_txt = f"%B {pb:.2f}" if pb is not None else "%B n/a"

    if et and ed != "Neutral":
        kind = "reversal (CHoCH)" if et == "CHoCH" else "continuation (BOS)"
        struct_txt = f"{ed} {et} at {_price_text(ctx['last_event'].get('level'))} → {kind}"
        # A fresh CHoCH is the strongest read here (70); a stale one (>15
        # sessions) has had its reversal window pass, so it decays to below
        # BOS-continuation weight instead of repeating "reversal" forever.
        if et == "CHoCH":
            confidence = 70 if ctx.get("event_recent") else 55
            if not ctx.get("event_recent"):
                struct_txt += " [aging — printed >15 sessions ago]"
        else:
            confidence = 62
        signal = ed
    else:
        struct_txt = "no confirmed BOS/CHoCH on the visible range"
        signal, confidence = "Neutral", 45

    if side == "Sell-side":
        sweep_txt = f"sell-side liquidity taken at {_price_text(ctx['sweep_level'])} (longs’ stops run → bullish reaction expected)"
        if signal != "Bearish":
            signal, confidence = "Bullish", max(confidence, 66)
    elif side == "Buy-side":
        sweep_txt = f"buy-side liquidity taken at {_price_text(ctx['sweep_level'])} (breakout buyers trapped → bearish reaction expected)"
        if signal != "Bullish":
            signal, confidence = "Bearish", max(confidence, 66)
    else:
        sweep_txt = "no fresh liquidity sweep"

    # ICT premium/discount confluence: buy in discount, sell in premium.
    if signal == "Bullish" and pd_zone == "Discount":
        confidence += 12
    elif signal == "Bearish" and pd_zone == "Premium":
        confidence += 12
    elif signal == "Bullish" and pd_zone == "Premium":
        confidence -= 12
    elif signal == "Bearish" and pd_zone == "Discount":
        confidence -= 12

    logic = (
        f"Structure: {struct_txt}. Liquidity: {sweep_txt}. "
        f"Dealing range: {pd_zone} ({pb_txt}); ICT bias favours longs from discount / shorts from premium."
    )
    if signal == "Bullish":
        sentiment = "Smart money accumulating; the swept lows are the trap, not the trend."
    elif signal == "Bearish":
        sentiment = "Smart money distributing; chased highs are being offloaded into breakout buyers."
    else:
        sentiment = "Indecision; price is mid-range and engineering liquidity before its next move."
    status = (
        f"{ed} {et} ({'reversal' if et == 'CHoCH' else 'continuation'})" if et and ed != "Neutral"
        else ("Liquidity grab — reversal watch" if side else "Awaiting structural confirmation")
    )
    return _pack(logic, sentiment, status, signal, confidence)


# ---------------------------------------------------------------------------
# 2. RTM / QM — fresh supply/demand zones & Quasimodo reversals
# ---------------------------------------------------------------------------

def _read_rtm_qm(ctx: dict[str, Any]) -> dict[str, Any]:
    price = ctx["price"]
    supply, demand = ctx["supply_zone"], ctx["demand_zone"]
    et, ed = ctx["event_type"], ctx["event_dir"]
    # A QM reversal sequence is a live trade setup, not a state — only a FRESH
    # change of character qualifies. Without the recency test this row echoed
    # SMC's stale CHoCH (83% signal agreement measured across 36 symbols).
    qm_active = et == "CHoCH" and ed != "Neutral" and bool(ctx.get("event_recent"))

    target, signal, confidence = None, "Neutral", 45
    if qm_active:
        signal, confidence = ed, 66
        target = demand if ed == "Bullish" else supply
        status = f"{ed} QM reversal sequence active"
    else:
        if demand and supply and price is not None:
            d_dist = abs(price - _safe_float(demand.get("center")))
            s_dist = abs(_safe_float(supply.get("center")) - price)
            if d_dist <= s_dist:
                target, signal, confidence = demand, "Bullish", 56
            else:
                target, signal, confidence = supply, "Bearish", 56
        elif demand:
            target, signal, confidence = demand, "Bullish", 54
        elif supply:
            target, signal, confidence = supply, "Bearish", 54
        status = "Fresh-zone reaction pending" if target else "No qualified supply/demand zone"

    if target:
        touches = _zone_touches(target)
        freshness = "fresh/untested" if touches <= 1 else (f"{touches}× tested" if touches == 2 else f"{touches}× tested (weak)")
        if touches >= 3:
            confidence -= 12
        zone_txt = f"{target.get('type', 'zone')} {_zone_text(target)} [{freshness}]"
    else:
        zone_txt = "no engine zone in range"

    qm_prefix = "QM (Quasimodo) reversal: prior swing broken, reaction expected. " if qm_active else ""
    logic = (
        f"{qm_prefix}Working engine zone: {zone_txt}. "
        "RTM trades the first tap of a fresh imbalance; repeatedly-tested zones lose their edge."
    )
    sentiment = (
        "Suspicion; chase entries are exposed to a QM shoulder trap — wait for the zone tap."
        if qm_active else
        "Patience; only the first retest of a fresh zone carries an institutional edge."
    )
    return _pack(logic, sentiment, status, signal, confidence)


# ---------------------------------------------------------------------------
# 3. BTMM — market-maker cycle
# ---------------------------------------------------------------------------

# This panel mirrors what _read_btmm ACTUALLY computes on daily data. The
# classic BTMM playbook (15-minute session boxes, EMA 5/13/50/200/800 crosses)
# needs intraday candles, and this platform is EOD-only — an earlier version of
# this panel documented that intraday system anyway, promising rules the code
# never ran on data that does not exist here.
_BTMM_DETAIL_SECTIONS = [
    {
        "title": "Inputs (daily adaptation — this desk is EOD-only)",
        "items": [
            "Most recent liquidity sweep within the last 10 sessions (buy-side / sell-side).",
            "Dealing-range zone from Bollinger %B: premium / equilibrium / discount.",
            "Trend stack from the SMA bias; price vs HMA and vs VWAP as value references.",
        ],
    },
    {
        "title": "Market-Maker Cycle Read",
        "items": [
            "Sell-side sweep in discount/equilibrium: manipulation low set, mark-up expected → Bullish 64.",
            "Buy-side sweep in premium/equilibrium: manipulation high set, mark-down expected → Bearish 64.",
            "No fresh sweep, stack up and price above VWAP: mark-up leg → Bullish 58.",
            "No fresh sweep, stack down and price below a KNOWN VWAP: mark-down leg → Bearish 58.",
            "Anything else (including missing VWAP): accumulation / consolidation → Neutral 48.",
        ],
    },
    {
        "title": "Not Implemented Here",
        "items": [
            "The intraday BTMM entry (15m opening-range false break + EMA 13/50 cross) "
            "requires intraday data NEPSE does not publish to this desk — the daily "
            "cycle read above is the honest substitute, not a replacement for it.",
        ],
    },
]

def _read_btmm(ctx: dict[str, Any]) -> dict[str, Any]:
    price, hma, vwap = ctx["price"], ctx["hma"], ctx["vwap"]
    side, pd_zone, trend = ctx["sweep_side"], ctx["pd_zone"], ctx["trend_bias"]
    above_hma = price is not None and hma is not None and price > hma
    above_vwap = price is not None and vwap is not None and price > vwap
    stack = "up" if ("Bull" in trend or "Above" in trend) else "down" if ("Bear" in trend or "Below" in trend) else "flat"

    signal, confidence = "Neutral", 48
    if side == "Sell-side" and pd_zone in ("Discount", "Equilibrium"):
        phase = "Manipulation low set — stops run below, mark-up (Level 1→2) expected"
        signal, confidence = "Bullish", 64
    elif side == "Buy-side" and pd_zone in ("Premium", "Equilibrium"):
        phase = "Manipulation high set — stops run above, mark-down expected"
        signal, confidence = "Bearish", 64
    elif stack == "up" and above_vwap:
        phase = "Mark-up / distribution leg — MM trending up above value"
        signal, confidence = "Bullish", 58
    elif stack == "down" and vwap is not None and not above_vwap:
        # `vwap is not None` matters: with VWAP missing, `not above_vwap` is
        # vacuously true, so a down-stack earned Bearish 58 from ABSENT data
        # while the mirror-image up-stack (which requires a real VWAP) could
        # not go Bullish. Missing data must read Neutral in both directions.
        phase = "Mark-down leg — MM trending down below value"
        signal, confidence = "Bearish", 58
    else:
        phase = "Accumulation / consolidation — awaiting the stop-hunt trigger"

    logic = (
        f"MM cycle: {phase}. EMA-stack bias {stack}; price {'above' if above_hma else 'below'} HMA, "
        f"{'above' if above_vwap else 'below'} VWAP; {pd_zone} zone. "
        "Trigger = false break of the session range, then EMA 13/50 cross."
    )
    sentiment = (
        "Deception; the stop-hunt is the entry, not the exit — fade the trapped side."
        if side else
        "Boredom is the setup; the range is engineering the next liquidity run."
    )
    status = "Stop-Hunt Reversal" if side else "Range / pre-manipulation"
    return _pack(logic, sentiment, status, signal, confidence)


# ---------------------------------------------------------------------------
# 4. Malaysian SNR — fresh vs tested walls, SNR flip
# ---------------------------------------------------------------------------

def _read_malaysian_snr(ctx: dict[str, Any]) -> dict[str, Any]:
    price, sup, res, pivot = ctx["price"], ctx["support"], ctx["resistance"], ctx["pivot"]
    et, ed = ctx["event_type"], ctx["event_dir"]

    flip = ""
    if et == "BOS" and ed == "Bullish":
        flip = " Broken resistance flips to support (SNR flip)."
    elif et == "BOS" and ed == "Bearish":
        flip = " Broken support flips to resistance (SNR flip)."

    at_support = sup is not None and ctx["support_pct"] is not None and ctx["support_pct"] <= 1.5
    at_resistance = res is not None and ctx["resistance_pct"] is not None and ctx["resistance_pct"] <= 1.5
    wall = ctx["demand_zone"] if at_support else ctx["supply_zone"] if at_resistance else None

    # The wall comes from the S/R engine; the touch count comes from a DENSITY
    # ZONE — a different system. Only trust the count when a zone actually sits
    # ON the wall (within 2%). Previously a missing zone meant touches=0, which
    # read as "fresh/strong": the strongest possible claim, made from no data,
    # about a level the zone might not even be near.
    wall_level = sup if at_support else res if at_resistance else None
    zone_center = _safe_float((wall or {}).get("center"))
    tracked = (
        wall is not None and wall_level is not None and zone_center is not None
        and abs(zone_center - wall_level) / wall_level <= 0.02
    )
    touches = _zone_touches(wall) if tracked else None
    if tracked:
        strength = "fresh/strong" if touches <= 1 else ("holding" if touches == 2 else f"weak ({touches}× tested → break risk)")
    else:
        strength = "strength untracked (no volume zone at this level)"

    signal, confidence = "Neutral", 48
    if at_support:
        if tracked:
            signal = "Bullish" if touches <= 2 else "Bearish"
            confidence = 60 if touches <= 2 else 56
            status = "At support wall — bounce expected" if touches <= 2 else "Support wall fatigued — breakdown risk"
        else:
            # Level is real, freshness unverified — lean with it, but without
            # the confidence a measured fresh wall earns.
            signal, confidence = "Bullish", 54
            status = "At support wall — strength untracked"
    elif at_resistance:
        if tracked:
            signal = "Bearish" if touches <= 2 else "Bullish"
            confidence = 60 if touches <= 2 else 56
            status = "At resistance wall — rejection expected" if touches <= 2 else "Resistance wall fatigued — breakout risk"
        else:
            signal, confidence = "Bearish", 54
            status = "At resistance wall — strength untracked"
    else:
        status = "Mid-range between walls"
        if price is not None and pivot is not None:
            signal, confidence = ("Bullish" if price > pivot else "Bearish"), 50

    logic = (
        f"Walls: support {_price_text(sup)}, resistance {_price_text(res)}; working wall is {strength}.{flip} "
        "SNR principle: fresh walls hold, repeatedly-tested walls break."
    )
    if touches is None:
        sentiment = "Caution; the level is real but its freshness is unmeasured — size accordingly."
    elif touches <= 1:
        sentiment = "Confidence; an untested wall should reject cleanly on the first tap."
    else:
        sentiment = "Doubt; every retest drains the wall and late defenders get run."
    return _pack(logic, sentiment, status, signal, confidence)


# ---------------------------------------------------------------------------
# 5. Wyckoff Phase — Spring/Upthrust, SOS/SOW, cause & POC
# ---------------------------------------------------------------------------

def _read_wyckoff(ctx: dict[str, Any]) -> dict[str, Any]:
    et, ed, side = ctx["event_type"], ctx["event_dir"], ctx["sweep_side"]
    pd_zone, poc, price = ctx["pd_zone"], ctx["poc"], ctx["price"]
    zone = ctx["strongest_zone"]
    density = _safe_float(zone.get("volume_density")) if zone else None
    cause = f"cause built at {_zone_text(zone)} (vol {density:.0f}% of profile)" if zone and density is not None else "cause still forming"

    signal, confidence = "Neutral", 48
    if side == "Sell-side":
        phase, signal, confidence = "Phase C Spring — sell-side test absorbed", "Bullish", 66
    elif side == "Buy-side":
        phase, signal, confidence = "Phase C Upthrust / UTAD — buy-side test rejected", "Bearish", 66
    elif et == "BOS" and ed == "Bullish":
        phase, signal, confidence = "Phase D SOS — mark-up confirmed", "Bullish", 62
    elif et == "BOS" and ed == "Bearish":
        phase, signal, confidence = "Phase D SOW — mark-down confirmed", "Bearish", 62
    elif et == "CHoCH" and ed != "Neutral" and ctx.get("event_recent"):
        # Recency-gated for the same reason as RTM's QM read: an old character
        # change is not an active Phase C/D transition, and honouring it forever
        # made this row a third copy of SMC's vote.
        phase, signal, confidence = f"Phase C/D character change ({ed.lower()})", ed, 58
    else:
        if pd_zone == "Discount":
            phase, signal, confidence = "Phase B accumulation test", "Bullish", 50
        elif pd_zone == "Premium":
            phase, signal, confidence = "Phase B distribution test", "Bearish", 50
        else:
            phase = "Phase B range — cause building"

    poc_txt = ""
    if poc is not None and price is not None:
        poc_txt = f" POC {_price_text(poc)} is the auction control point — price {'accepted above' if price > poc else 'capped below'} it."
    logic = f"{phase}; {cause}.{poc_txt}"
    sentiment = (
        "Churning; weak hands read the test as direction while the composite operator positions."
        if "Phase B" in phase else
        "Resolution; the composite operator’s hand is now visible in the structure."
    )
    return _pack(logic, sentiment, phase, signal, confidence)


# ---------------------------------------------------------------------------
# 6. Elliott Wave — impulse vs corrective, RSI confirmation/divergence
# ---------------------------------------------------------------------------

def _read_elliott_wave(ctx: dict[str, Any]) -> dict[str, Any]:
    trend, rsi = ctx["trend_bias"], ctx["rsi"]
    res, sup = ctx["resistance"], ctx["support"]
    up = "Bull" in trend or "Above" in trend
    down = "Bear" in trend or "Below" in trend

    signal, confidence = "Neutral", 46
    if up:
        # Wave-5 divergence: price pressing into the nearest resistance (within
        # 1%) while momentum lags. (`price >= res` could never trigger — the
        # nearest resistance is by construction a zone above the price.)
        at_resistance = _safe_float(ctx.get("resistance_pct")) is not None and ctx["resistance_pct"] <= 1.0
        if rsi is not None and rsi >= 60:
            posture, signal, confidence = "Impulse (likely wave 3) — momentum expanding", "Bullish", 64
        elif rsi is not None and at_resistance and rsi < 60:
            posture, signal, confidence = "Possible wave 5 — price extends but momentum lags (bearish divergence risk)", "Neutral", 50
        else:
            posture, signal, confidence = "Impulsive up-trend, mid-wave", "Bullish", 56
    elif down:
        if rsi is not None and rsi <= 40:
            posture, signal, confidence = "Impulse down (likely wave 3) — momentum expanding", "Bearish", 64
        else:
            posture, signal, confidence = "Corrective ABC or impulsive down-leg", "Bearish", 54
    else:
        posture = "Corrective / sideways — wave count unresolved"

    rsi_txt = f"{rsi:.0f}" if rsi is not None else "n/a"
    confirms = (up and rsi is not None and rsi >= 55) or (down and rsi is not None and rsi <= 45)
    logic = (
        f"{posture}. Impulse target above {_price_text(res)}, ABC support into {_price_text(sup)}. "
        f"RSI {rsi_txt} {'confirms the wave direction' if confirms else 'is non-confirming'}."
    )
    sentiment = (
        "Conviction; momentum and structure agree on the wave direction."
        if confidence >= 60 else
        "Impatience; the count is ambiguous mid-wave — wait for the confirmed break."
    )
    return _pack(logic, sentiment, posture, signal, confidence)


# ---------------------------------------------------------------------------
# 7. Volume Flow — VPVR (POC / value area)
# ---------------------------------------------------------------------------

def _read_volume_flow(ctx: dict[str, Any]) -> dict[str, Any]:
    price, poc, vah, val = ctx["price"], ctx["poc"], ctx["vah"], ctx["val"]
    zone = ctx["strongest_zone"]
    density = _safe_float(zone.get("volume_density")) if zone else None

    if poc is None or price is None:
        return _pack(
            "Volume profile unavailable for this range.",
            "Unclear; no auction reference to read order flow.",
            "Profile unavailable", "Neutral", 35,
        )

    above_poc = price > poc
    if vah is not None and val is not None:
        if price > vah:
            va, signal, confidence = f"accepted ABOVE value (VAH {_price_text(vah)}) — breakout / excess", "Bullish", 64
        elif price < val:
            va, signal, confidence = f"rejected BELOW value (VAL {_price_text(val)}) — distribution / new value", "Bearish", 62
        else:
            va = f"inside value ({_price_text(val)}–{_price_text(vah)}) — balanced auction"
            signal, confidence = ("Bullish" if above_poc else "Bearish"), 52
    else:
        va, signal, confidence = "value area undefined", ("Bullish" if above_poc else "Bearish"), 50

    dens_txt = f" Controlling node {_zone_text(zone)} ({density:.0f}% of profile)." if zone and density is not None else ""
    logic = f"POC {_price_text(poc)} — price {'above' if above_poc else 'below'} the control price; {va}.{dens_txt}"
    sentiment = (
        "Constructive; buyers are accepting higher value above the control price."
        if above_poc else
        "Defensive; sellers are dictating value below the control price."
    )
    status = "Acceptance above POC" if above_poc else "Below POC / auction imbalance"
    return _pack(logic, sentiment, status, signal, confidence)


# ---------------------------------------------------------------------------
# 8. Candle Range — last-candle anatomy
# ---------------------------------------------------------------------------

def _read_candle_range(ctx: dict[str, Any]) -> dict[str, Any]:
    candle = ctx["candle"]
    o = _safe_float(candle.get("open"))
    h = _safe_float(candle.get("high"))
    l = _safe_float(candle.get("low"))
    c = _safe_float(candle.get("close"))
    if None in (o, h, l, c):
        return _pack(
            "Latest candle anatomy unavailable.",
            "Unclear; the tape gives no commitment.",
            "Candle context unavailable", "Neutral", 35,
        )

    rng = max(h - l, 0.01)
    body = abs(c - o)
    upper_wick = h - max(o, c)
    lower_wick = min(o, c) - l
    body_pct = body / rng

    if lower_wick > body * 1.5 and c > o:
        logic = f"Lower rejection wick with {body_pct:.0%} body — sell-side raid absorbed (bullish pin)."
        sentiment, status, signal, confidence = "Deception; shorts are trapped by the close.", "Bullish rejection candle", "Bullish", 64
    elif upper_wick > body * 1.5 and c < o:
        logic = f"Upper rejection wick with {body_pct:.0%} body — buy-side raid failed (bearish pin)."
        sentiment, status, signal, confidence = "Frustration; breakout buyers are trapped.", "Bearish rejection candle", "Bearish", 64
    elif body_pct >= 0.65 and c > o:
        logic = f"Wide bullish body ({body_pct:.0%}) — displacement up."
        sentiment, status, signal, confidence = "Urgency; sidelined buyers feel forced to chase.", "Bullish displacement", "Bullish", 62
    elif body_pct >= 0.65 and c < o:
        logic = f"Wide bearish body ({body_pct:.0%}) — mark-down displacement."
        sentiment, status, signal, confidence = "Fear; bids are stepping away.", "Bearish displacement", "Bearish", 62
    else:
        logic = f"Compressed body ({body_pct:.0%}) — absorption / indecision."
        sentiment, status, signal, confidence = "Suspicion; neither side has clean control.", "Absorption / indecision", "Neutral", 44
    return _pack(logic, sentiment, status, signal, confidence)


# ---------------------------------------------------------------------------
# 9. Structural S/R / Pivot Matrix — pivot bias + R/R + momentum confluence
# ---------------------------------------------------------------------------

def _read_structural_sr(ctx: dict[str, Any]) -> dict[str, Any]:
    price, pivot, sup, res = ctx["price"], ctx["pivot"], ctx["support"], ctx["resistance"]
    rr, rsi, stoch = ctx["rr"], ctx["rsi"], ctx["stoch"]
    above_pivot = price is not None and pivot is not None and price > pivot
    below_pivot = price is not None and pivot is not None and price < pivot
    mom_up = (rsi is not None and rsi >= 55) or (stoch is not None and stoch >= 60)
    mom_dn = (rsi is not None and rsi <= 45) or (stoch is not None and stoch <= 40)

    signal, confidence = "Neutral", 48
    if above_pivot and not mom_dn:
        signal, confidence, status = "Bullish", 58 + (8 if mom_up else 0), "Above-pivot constructive bias"
    elif below_pivot and not mom_up:
        signal, confidence, status = "Bearish", 58 + (8 if mom_dn else 0), "Below-pivot defensive bias"
    else:
        status = "Pivot straddle — mixed momentum"

    # rr = resistance_distance / support_distance (reward-up vs risk-down).
    # For a long, a high ratio is favourable; for a short the INVERSE applies
    # (more room below than above), so adjust both directions symmetrically.
    if rr is not None:
        if signal == "Bullish":
            if rr >= 2:
                confidence += 8
            elif rr < 1:
                confidence -= 8
        elif signal == "Bearish":
            if rr <= 0.5:
                confidence += 8
            elif rr > 1:
                confidence -= 8

    logic = (
        f"Pivot {_price_text(pivot)}; support {_price_text(sup)}, resistance {_price_text(res)}; "
        f"R/R {rr if rr is not None else 'N/A'}. "
        f"RSI {f'{rsi:.0f}' if rsi is not None else 'N/A'}, Stoch {f'{stoch:.0f}' if stoch is not None else 'N/A'}."
    )
    if signal == "Bullish":
        sentiment = "Constructive; price is working the mechanical levels from the strong side."
    elif signal == "Bearish":
        sentiment = "Defensive; the mechanical levels are capping price from above."
    else:
        sentiment = "Tense; price is balanced on the pivot with no momentum edge."
    return _pack(logic, sentiment, status, signal, confidence)


# ---------------------------------------------------------------------------
# Consensus & contradiction
# ---------------------------------------------------------------------------

def _contradiction_alert(rows: list[dict]) -> str | None:
    """Warn when the frameworks are split with no clear majority."""
    signals = [r.get("signal", "Neutral") for r in rows if r.get("system") != "Institutional Consensus"]
    bullish = signals.count("Bullish")
    bearish = signals.count("Bearish")
    total = bullish + bearish
    if total >= 4 and abs(bullish - bearish) <= 1:
        return (
            f"Conflicting signals: {bullish} bullish vs {bearish} bearish frameworks. "
            "Wait for structural confirmation before committing."
        )
    return None


# Per-framework reliability prior for the consensus vote. The frameworks are
# NOT equally trustworthy: objective / multi-factor reads (volume auction,
# structure + liquidity) earn more weight than subjective or single-bar reads.
# A framework's vote weight = its self-reported confidence × this prior.
# Heuristic starting point — calibrate against realized hit-rate over time.
_FRAMEWORK_RELIABILITY: dict[str, float] = {
    "SMC / ICT": 1.00,                    # structure + liquidity + premium/discount
    "Wyckoff Phase": 1.00,                # composite structure + volume cause
    "Volume Flow": 0.95,                  # objective auction (POC / value area)
    "RTM / QM": 0.90,                     # fresh supply/demand zones
    "Structural S/R / Pivot Matrix": 0.90,  # mechanical levels + momentum
    "Malaysian SNR": 0.85,               # wall strength / test count
    "BTMM": 0.70,                         # tuned for intraday sessions; weaker on daily
    "Elliott Wave": 0.70,                 # subjective wave counts, often ambiguous
    "Candle Range": 0.60,                # single-candle, noisy, flat-bar exposed
}
_DEFAULT_RELIABILITY = 0.80


def _framework_weight(row: dict) -> float:
    """Consensus vote weight = self-reported confidence × reliability prior."""
    reliability = _FRAMEWORK_RELIABILITY.get(row.get("system", ""), _DEFAULT_RELIABILITY)
    return row.get("confidence", 0) * reliability


# The nine frameworks are NOT independent witnesses: several read the same data
# facet, so a naive nine-way vote counts one piece of evidence several times.
# Measured over 36 liquid symbols, SMC↔RTM agreed on 83% of signals (+33pp above
# what their marginals predict under independence), SMC↔Wyckoff 78% (+25pp) and
# BTMM↔Wyckoff +24pp — all four are driven by the same BOS/CHoCH event, the same
# sweep, and the same %B zone. Grouping by SHARED INPUT (the causal mechanism,
# not the empirical clustering, which trends contaminate) and giving each group
# ONE vote removes the double-count while all nine rows stay on screen.
_EVIDENCE_CLUSTERS: dict[str, tuple[str, ...]] = {
    "Structure & liquidity": ("SMC / ICT", "RTM / QM", "Wyckoff Phase", "BTMM"),
    "Auction & levels": ("Volume Flow", "Malaysian SNR", "Structural S/R / Pivot Matrix"),
    "Momentum & wave": ("Elliott Wave",),
    "Tape": ("Candle Range",),
}


def _cluster_votes(rows: list[dict]) -> list[dict[str, Any]]:
    """One (signal, weight) vote per evidence cluster.

    Within a cluster the members' signed weights are netted, but the cluster's
    vote weight is CAPPED at its strongest member — three correlated frameworks
    repeating one CHoCH must not outvote one framework reading something else.
    A cluster whose members cancel (net under 20% of its gross) votes Neutral.
    """
    by_name = {r.get("system"): r for r in rows}
    votes = []
    for cluster, members in _EVIDENCE_CLUSTERS.items():
        present = [by_name[m] for m in members if m in by_name]
        if not present:
            continue
        signed = sum(
            _framework_weight(r) * (1 if r["signal"] == "Bullish" else -1)
            for r in present if r["signal"] != "Neutral"
        )
        gross = sum(_framework_weight(r) for r in present)
        cap = max(_framework_weight(r) for r in present)
        if gross > 0 and abs(signed) >= 0.2 * gross:
            direction = "Bullish" if signed > 0 else "Bearish"
            weight = min(abs(signed), cap)
        else:
            direction, weight = "Neutral", cap
        # `cap` is carried separately: the consensus denominator must be the
        # cluster's FULL capacity, not its netted vote. Otherwise four clusters
        # that each barely clear the 20% bar read as 100% conviction — weak
        # internal agreement has to show up as weak conviction.
        votes.append({"cluster": cluster, "signal": direction, "weight": weight,
                      "cap": cap, "members": [r["system"] for r in present]})
    return votes


def _institutional_consensus(rows: list[dict]) -> dict[str, Any]:
    """Cluster-weighted vote: nine frameworks, one vote per evidence group."""
    votes = _cluster_votes(rows)
    bull_v = [v for v in votes if v["signal"] == "Bullish"]
    bear_v = [v for v in votes if v["signal"] == "Bearish"]
    bull_w = sum(v["weight"] for v in bull_v)
    bear_w = sum(v["weight"] for v in bear_v)
    total_w = bull_w + bear_w
    # Denominator = every cluster's full capacity. A cluster whose members half
    # cancel contributes its small net to the numerator but its full cap here,
    # so internal disagreement reads as reduced conviction rather than vanishing.
    panel_w = sum(v["cap"] for v in votes)

    bull = [r for r in rows if r["signal"] == "Bullish"]
    bear = [r for r in rows if r["signal"] == "Bearish"]
    neutral = [r for r in rows if r["signal"] == "Neutral"]

    if total_w == 0:
        signal, confidence, lean = "Neutral", 0, 0.0
    else:
        lean = (bull_w - bear_w) / total_w
        # Conviction = |lean| scaled by how much of the panel actually voted a
        # direction. Without the participation factor, a single directional
        # cluster among neutrals reads as "100% conviction".
        participation = (total_w / panel_w) if panel_w > 0 else 0.0
        confidence = int(round(abs(lean) * participation * 100))
        if lean > 0.15:
            signal = "Bullish"
        elif lean < -0.15:
            signal = "Bearish"
        else:
            signal = "Neutral"

    aligned_v = bull_v if signal == "Bullish" else bear_v if signal == "Bearish" else []
    names = ", ".join(v["cluster"] for v in sorted(aligned_v, key=lambda v: v["weight"], reverse=True))
    names = names or "no directional alignment"
    logic = (
        f"Vote of {len(votes)} evidence groups (9 frameworks; correlated frameworks share "
        f"one vote) → net lean {lean:+.2f}. Frameworks: {len(bull)} bullish / {len(bear)} "
        f"bearish / {len(neutral)} neutral. Aligned groups: {names}."
    )
    sentiment = (
        f"Ensemble reads {signal.lower()} at {confidence}% conviction."
        if signal != "Neutral" else
        "Ensemble is balanced — no committed edge; stand aside until alignment improves."
    )
    return {
        "system": "Institutional Consensus",
        "institutional_logic": logic,
        "price_sentiment": sentiment,
        "status": signal if signal != "Neutral" else "Balanced / no edge",
        "signal": signal,
        "confidence": confidence,
    }


# ---------------------------------------------------------------------------
# Code-ready rule panels — the "?" help expander on each framework row.
# Each entry mirrors the actual decision logic of its _read_* function,
# framed for NEPSE daily data (use a 1Y+ range for stable structure).
# ---------------------------------------------------------------------------

_SMC_DETAIL_SECTIONS = [
    {
        "title": "Inputs",
        "items": [
            "Market structure: latest BOS / CHoCH event and its direction.",
            "Liquidity: most recent buy-side / sell-side sweep and its level.",
            "Dealing range: premium / discount zone from Bollinger %B.",
        ],
    },
    {
        "title": "Structure Read",
        "items": [
            "CHoCH (change of character) = reversal bias, confidence 70.",
            "BOS (break of structure) = continuation bias, confidence 62.",
            "No confirmed BOS/CHoCH = Neutral, confidence 45.",
        ],
    },
    {
        "title": "Liquidity Confluence",
        "items": [
            "Sell-side sweep (longs' stops run) → bullish reaction, confidence ≥ 66.",
            "Buy-side sweep (breakout buyers trapped) → bearish reaction, confidence ≥ 66.",
        ],
    },
    {
        "title": "Premium / Discount Filter",
        "items": [
            "Long from discount or short from premium: +12 confidence.",
            "Long from premium or short from discount: −12 confidence.",
        ],
    },
]

_RTM_DETAIL_SECTIONS = [
    {
        "title": "Inputs",
        "items": [
            "Fresh supply / demand (engine) zones near price.",
            "Latest CHoCH event for Quasimodo (QM) detection.",
            "Zone test count (touches) for freshness grading.",
        ],
    },
    {
        "title": "Quasimodo Reversal",
        "items": [
            "QM active when a CHoCH breaks the prior swing: signal = direction, confidence 66.",
            "Target = demand zone (bullish) or supply zone (bearish).",
        ],
    },
    {
        "title": "Fresh-Zone Reaction (no QM)",
        "items": [
            "Trade the nearer of demand / supply by distance to price, confidence 56.",
            "Only one zone in range: take that side, confidence 54.",
        ],
    },
    {
        "title": "Freshness Grade",
        "items": [
            "≤1 touch: fresh / untested (full edge).",
            "2 touches: tested.",
            "≥3 touches: weak → −12 confidence (RTM only trades the first tap).",
        ],
    },
]

_SNR_DETAIL_SECTIONS = [
    {
        "title": "Inputs",
        "items": [
            "Nearest support / resistance walls and distance-to-price (%).",
            "Wall test count (touches) for strength grading.",
            "Latest BOS event for SNR flip (broken level swaps role).",
        ],
    },
    {
        "title": "At a Wall (within 1.5%)",
        "items": [
            "At support, ≤2 touches: bullish bounce, confidence 60.",
            "At support, ≥3 touches: fatigued → bearish breakdown risk, confidence 56.",
            "At resistance, ≤2 touches: bearish rejection, confidence 60.",
            "At resistance, ≥3 touches: fatigued → bullish breakout risk, confidence 56.",
        ],
    },
    {
        "title": "Mid-Range / Flip",
        "items": [
            "Between walls: bias = price vs pivot, confidence 50.",
            "Bullish BOS: broken resistance flips to support.",
            "Bearish BOS: broken support flips to resistance.",
        ],
    },
]

_WYCKOFF_DETAIL_SECTIONS = [
    {
        "title": "Inputs",
        "items": [
            "Liquidity sweep side (Spring vs Upthrust test).",
            "BOS / CHoCH event for SOS / SOW and character change.",
            "Premium / discount zone; volume Point of Control (POC).",
            "Strongest volume node and its share of the profile (cause).",
        ],
    },
    {
        "title": "Phase Map",
        "items": [
            "Sell-side test absorbed: Phase C Spring → Bullish 66.",
            "Buy-side test rejected: Phase C Upthrust / UTAD → Bearish 66.",
            "Bullish BOS: Phase D SOS (sign of strength) → Bullish 62.",
            "Bearish BOS: Phase D SOW (sign of weakness) → Bearish 62.",
            "CHoCH: Phase C/D character change → event direction, 58.",
        ],
    },
    {
        "title": "Phase B (no trigger)",
        "items": [
            "Discount zone: accumulation test → Bullish 50.",
            "Premium zone: distribution test → Bearish 50.",
            "Otherwise: range, cause still building → Neutral.",
            "POC: price accepted above = strength; capped below = weakness.",
        ],
    },
]

_ELLIOTT_DETAIL_SECTIONS = [
    {
        "title": "Inputs",
        "items": [
            "Trend bias (up / down / sideways).",
            "RSI for momentum confirmation and wave-5 divergence.",
            "Nearest resistance (impulse target) and support (ABC).",
        ],
    },
    {
        "title": "Up-Trend",
        "items": [
            "RSI ≥ 60: impulse (likely wave 3), momentum expanding → Bullish 64.",
            "At resistance (≤1%) and RSI < 60: possible wave 5, bearish divergence risk → Neutral 50.",
            "Otherwise: mid-wave impulse up → Bullish 56.",
        ],
    },
    {
        "title": "Down-Trend / Sideways",
        "items": [
            "RSI ≤ 40: impulse down (likely wave 3) → Bearish 64.",
            "Otherwise down: corrective ABC or impulsive down-leg → Bearish 54.",
            "No clear trend: corrective / sideways, count unresolved → Neutral.",
            "Confirmation: RSI ≥ 55 (up) or ≤ 45 (down) backs the wave direction.",
        ],
    },
]

_VOLUME_DETAIL_SECTIONS = [
    {
        "title": "Inputs",
        "items": [
            "Volume Point of Control (POC) — the auction's control price.",
            "Value Area High / Low (VAH / VAL) — the 70% volume band.",
            "Strongest volume node and its share of the profile.",
        ],
    },
    {
        "title": "Acceptance vs Rejection",
        "items": [
            "Price above VAH: accepted above value, breakout / excess → Bullish 64.",
            "Price below VAL: rejected below value, distribution → Bearish 62.",
            "Inside value: balanced auction, bias = price vs POC → 52.",
        ],
    },
    {
        "title": "Notes",
        "items": [
            "No volume profile for the range: Neutral, confidence 35.",
            "NEPSE note: flat-bar / thin-float days distort the profile — prefer a 1Y+ range.",
        ],
    },
]

_CANDLE_DETAIL_SECTIONS = [
    {
        "title": "Inputs",
        "items": [
            "Latest candle open / high / low / close.",
            "Body %, upper wick, lower wick of the range.",
        ],
    },
    {
        "title": "Anatomy Read",
        "items": [
            "Lower wick > 1.5× body and bullish close: sell-side raid absorbed (bullish pin) → Bullish 64.",
            "Upper wick > 1.5× body and bearish close: buy-side raid failed (bearish pin) → Bearish 64.",
            "Body ≥ 65% and bullish: displacement up → Bullish 62.",
            "Body ≥ 65% and bearish: mark-down displacement → Bearish 62.",
            "Compressed body (< 65%, no dominant wick): absorption / indecision → Neutral 44.",
        ],
    },
]

_STRUCTURAL_DETAIL_SECTIONS = [
    {
        "title": "Inputs",
        "items": [
            "Pivot, nearest support and resistance levels.",
            "Reward/Risk ratio = resistance distance / support distance.",
            "Momentum: RSI and Stochastic.",
        ],
    },
    {
        "title": "Pivot Bias",
        "items": [
            "Above pivot and not bearish momentum: constructive → Bullish 58 (+8 if momentum up).",
            "Below pivot and not bullish momentum: defensive → Bearish 58 (+8 if momentum down).",
            "Straddling the pivot with mixed momentum: Neutral.",
        ],
    },
    {
        "title": "R/R Adjustment",
        "items": [
            "Momentum up = RSI ≥ 55 or Stoch ≥ 60; down = RSI ≤ 45 or Stoch ≤ 40.",
            "Long: R/R ≥ 2 → +8; R/R < 1 → −8 confidence.",
            "Short: R/R ≤ 0.5 → +8; R/R > 1 → −8 confidence.",
        ],
    },
]

# system name → rule panel attached to each framework row.
_FRAMEWORK_DETAILS: dict[str, dict[str, Any]] = {
    "SMC / ICT": {
        "detail_title": "SMC / ICT Rules",
        "detail_sections": _SMC_DETAIL_SECTIONS,
    },
    "RTM / QM": {
        "detail_title": "RTM / QM Rules",
        "detail_sections": _RTM_DETAIL_SECTIONS,
    },
    "BTMM": {
        "detail_title": "BTMM Rules",
        "detail_sections": _BTMM_DETAIL_SECTIONS,
    },
    "Malaysian SNR": {
        "detail_title": "Malaysian SNR Rules",
        "detail_sections": _SNR_DETAIL_SECTIONS,
    },
    "Wyckoff Phase": {
        "detail_title": "Wyckoff Phase Rules",
        "detail_sections": _WYCKOFF_DETAIL_SECTIONS,
    },
    "Elliott Wave": {
        "detail_title": "Elliott Wave Rules",
        "detail_sections": _ELLIOTT_DETAIL_SECTIONS,
    },
    "Volume Flow": {
        "detail_title": "Volume Flow Rules",
        "detail_sections": _VOLUME_DETAIL_SECTIONS,
    },
    "Candle Range": {
        "detail_title": "Candle Range Rules",
        "detail_sections": _CANDLE_DETAIL_SECTIONS,
    },
    "Structural S/R / Pivot Matrix": {
        "detail_title": "Structural S/R Rules",
        "detail_sections": _STRUCTURAL_DETAIL_SECTIONS,
    },
}


def build_institutional_analysis_rows(
    support_metrics: dict[str, Any] | None,
    advanced_metrics: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    """
    Build the institutional multi-framework table for the Support & Resistance tab.

    Each of the nine frameworks reads its own data facet and returns
    (institutional_logic, price_sentiment, status, signal, confidence). A final
    "Institutional Consensus" row is the confidence-weighted vote of the nine.
    """
    if not support_metrics or support_metrics.get("error"):
        return []

    advanced = advanced_metrics if isinstance(advanced_metrics, dict) and not advanced_metrics.get("error") else {}
    ctx = _build_institutional_context(support_metrics, advanced)

    rows = [
        {"system": "SMC / ICT", **_read_smc_ict(ctx)},
        {"system": "RTM / QM", **_read_rtm_qm(ctx)},
        {"system": "BTMM", **_read_btmm(ctx)},
        {"system": "Malaysian SNR", **_read_malaysian_snr(ctx)},
        {"system": "Wyckoff Phase", **_read_wyckoff(ctx)},
        {"system": "Elliott Wave", **_read_elliott_wave(ctx)},
        {"system": "Volume Flow", **_read_volume_flow(ctx)},
        {"system": "Candle Range", **_read_candle_range(ctx)},
        {"system": "Structural S/R / Pivot Matrix", **_read_structural_sr(ctx)},
    ]

    # Attach the "?" code-ready rule panel to each framework row.
    for row in rows:
        detail = _FRAMEWORK_DETAILS.get(row["system"])
        if detail:
            row.update(detail)

    alert = _contradiction_alert(rows)
    consensus = _institutional_consensus(rows)
    consensus["alert"] = alert
    rows.append(consensus)

    for row in rows:
        row.setdefault("alert", alert)

    return rows

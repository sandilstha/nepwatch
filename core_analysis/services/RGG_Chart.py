import pandas as pd
import numpy as np


def run_rrg_simulation(
    stock_df: pd.DataFrame,
    benchmark_df: pd.DataFrame,
    lookback: int = 14
):
    """
    Calculates Relative Rotation Graph (RRG) coordinates (RS-Ratio and RS-Momentum)
    for a given stock against a benchmark (typically NEPSE INDEX).

    RRG Quadrants:
    - Leading (Green): RS-Ratio > 100, RS-Momentum > 100
    - Weakening (Yellow): RS-Ratio > 100, RS-Momentum < 100
    - Lagging (Red): RS-Ratio < 100, RS-Momentum < 100
    - Improving (Blue): RS-Ratio < 100, RS-Momentum > 100
    """
    if stock_df.empty or benchmark_df.empty:
        return {"error": "Missing data for RRG calculation."}, pd.DataFrame()
    if lookback < 2:
        return {"error": "RRG lookback must be at least 2 bars."}, pd.DataFrame()

    required_cols = {"business_date", "close_price_adj"}
    if not required_cols.issubset(stock_df.columns) or not required_cols.issubset(benchmark_df.columns):
        return {"error": "Input data missing required columns."}, pd.DataFrame()

    stock = _prepare_price_frame(stock_df, "stock_close")
    bench = _prepare_price_frame(benchmark_df, "bench_close")

    df = pd.merge(stock, bench, on="business_date", how="inner").sort_values("business_date").reset_index(drop=True)
    df = df[(df["stock_close"] > 0) & (df["bench_close"] > 0)].copy()
    source_bars = int(len(stock))
    benchmark_bars = int(len(bench))
    matched_bars = int(len(df))

    if len(df) < lookback * 2:
        return {
            "error": (
                f"Insufficient overlapping data for RRG({lookback}). "
                f"Need at least {lookback * 2} shared trading dates; found {matched_bars}."
            ),
            "source_bars": source_bars,
            "benchmark_bars": benchmark_bars,
            "matched_bars": matched_bars,
            "lookback": lookback,
        }, pd.DataFrame()

    # 1. Calculate Relative Strength (RS)
    df["RS"] = (df["stock_close"] / df["bench_close"]) * 100.0

    # 2. RS-Ratio: z-score of RS against its own rolling window, centred at 100
    #    (the standard public JdK approximation). The RS/SMA×100 form makes the
    #    distance from 100 scale with each symbol's OWN volatility, so on a
    #    multi-symbol chart the volatile series always plot farther out than the
    #    placid ones even with equal relative trend — z-scoring puts every symbol
    #    on the same footing, which is the point of an RRG.
    #    A window with no variation carries no signal → NaN (row not plotted).
    df["RS_Ratio"] = 100.0 + _z(df["RS"], lookback)

    # 3. RS-Momentum: the rate of change of RS-Ratio over the window (JdK
    #    defines momentum as the ROC of the ratio). RS-Ratio is already in
    #    z-units, so the plain difference is on a comparable scale and needs no
    #    second normalisation. (The earlier z-of-z form divided by the rolling
    #    std of RS-Ratio, which → 0 in a steady trend and blew momentum up
    #    precisely in the calmest, most persistent cases.)
    df["RS_Momentum"] = 100.0 + (df["RS_Ratio"] - df["RS_Ratio"].shift(lookback - 1))

    # Determine quadrant
    conditions = [
        (df["RS_Ratio"] >= 100) & (df["RS_Momentum"] >= 100),
        (df["RS_Ratio"] >= 100) & (df["RS_Momentum"] < 100),
        (df["RS_Ratio"] < 100) & (df["RS_Momentum"] < 100),
        (df["RS_Ratio"] < 100) & (df["RS_Momentum"] >= 100),
    ]
    labels = ["Leading", "Weakening", "Lagging", "Improving"]
    df["Quadrant"] = np.select(conditions, labels, default="Unknown")

    out_df = df.dropna().copy()
    if out_df.empty:
        return {
            "error": "Relative strength shows no variation in this window — nothing to rotate.",
            "source_bars": source_bars,
            "benchmark_bars": benchmark_bars,
            "matched_bars": matched_bars,
            "lookback": lookback,
        }, pd.DataFrame()

    latest = out_df.iloc[-1]
    previous = out_df.iloc[-2] if len(out_df) > 1 else latest

    # Rotation context: how long the symbol has been in its current quadrant
    # and where it rotated in from — "just entered Leading" is the actionable
    # RRG event, and the quadrant label alone doesn't carry it.
    quadrants = out_df["Quadrant"].tolist()
    bars_in_quadrant = 1
    for q in reversed(quadrants[:-1]):
        if q != quadrants[-1]:
            break
        bars_in_quadrant += 1
    rotated_from = (
        quadrants[-bars_in_quadrant - 1] if bars_in_quadrant < len(quadrants) else None
    )

    # Staleness: a halted/delisted symbol's last plotted point can be months
    # older than the benchmark's latest session — flag it so it isn't read as
    # today's rotation.
    latest_date = pd.Timestamp(latest["business_date"])
    bench_latest = pd.Timestamp(bench["business_date"].max())
    stale_sessions = int((bench["business_date"] > latest_date).sum())

    metrics = {
        "bars_in_quadrant": int(bars_in_quadrant),
        "rotated_from": rotated_from,
        "latest_date": latest_date.strftime("%Y-%m-%d"),
        "benchmark_latest_date": bench_latest.strftime("%Y-%m-%d"),
        "stale": stale_sessions > 0,
        "stale_sessions": stale_sessions,
        "latest_rs_ratio": round(float(latest["RS_Ratio"]), 2),
        "latest_rs_momentum": round(float(latest["RS_Momentum"]), 2),
        "latest_quadrant": latest["Quadrant"],
        "rs_ratio_delta": round(float(latest["RS_Ratio"] - previous["RS_Ratio"]), 2),
        "rs_momentum_delta": round(float(latest["RS_Momentum"] - previous["RS_Momentum"]), 2),
        "data_points": int(len(out_df)),
        "source_bars": source_bars,
        "benchmark_bars": benchmark_bars,
        "matched_bars": matched_bars,
        "lookback": lookback,
    }

    return metrics, out_df


def _z(series: pd.Series, lookback: int) -> pd.Series:
    """Rolling (sample) z-score of ``series`` over ``lookback`` bars.

    A window with (near-)zero dispersion has no defined deviation: it is
    returned as NaN rather than 0, because 0 would plot as exactly (100, 100)
    and — with the ``>= 100`` quadrant rule — label a flat, signal-less window
    as *Leading*.
    """
    mean = series.rolling(window=lookback).mean()
    std = series.rolling(window=lookback).std(ddof=1)
    # Relative floor so a series quoted in tiny units isn't mistaken for flat.
    floor = (mean.abs() * 1e-9).clip(lower=1e-12)
    std = std.where(std > floor)
    return (series - mean) / std


def _prepare_price_frame(source_df: pd.DataFrame, close_column_name: str) -> pd.DataFrame:
    df = source_df[["business_date", "close_price_adj"]].copy()
    df["business_date"] = pd.to_datetime(df["business_date"], errors="coerce")
    df["close_price_adj"] = pd.to_numeric(df["close_price_adj"], errors="coerce")
    df = df.dropna(subset=["business_date", "close_price_adj"])
    df = df.sort_values("business_date").drop_duplicates(subset=["business_date"], keep="last")
    return df.rename(columns={"close_price_adj": close_column_name})

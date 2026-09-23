"""
New-listing snapshot.

The Momentum / IMM / Stage desks all need a long price history (40 / 200 / 150
bars respectively) before their indicators mean anything. A freshly listed
company has only a handful of bars, so those desks would error, blank out, or
fabricate a neutral score. Rather than hide the company, we show a "New Listing"
snapshot built only from metrics that need no long history, plus a data-
confidence figure so the user knows the indicators aren't ready yet.

All outputs are JSON/template-safe (plain floats/ints/strings, rounded).
"""
import pandas as pd

# Bars each desk wants for its full indicator set — used as the confidence
# denominator so "47 / 200" reads honestly.
FULL_HISTORY_BARS = {"msv": 40, "imm": 200, "stage": 150}

# Below this many bars the desk cannot produce a usable result, so we show the
# snapshot instead. Stage keeps its own provisional mode for 30–149 bars, so it
# only falls back to the snapshot under 30.
SNAPSHOT_TRIGGER_BARS = {"msv": 40, "imm": 200, "stage": 30}


def _pct(a, b):
    """Percent change of a over b, rounded; None when b is missing/zero."""
    if a is None or b is None or b == 0:
        return None
    return round((a / b - 1.0) * 100.0, 2)


def build_new_listing_snapshot(df, required_bars, sector_df=None, symbol=None, desk_label=None):
    """Build a listing-appropriate metric set for a short-history symbol.

    df / sector_df use the standard adjusted-price columns produced by
    _build_standard_dataframe. Returns a dict, or None if there is no usable bar.
    """
    if df is None or df.empty:
        return None
    d = df.dropna(subset=["close_price_adj"]).sort_values("business_date")
    bars = len(d)
    if bars == 0:
        return None

    close = d["close_price_adj"].astype(float)
    high = d["high_price_adj"].astype(float)
    low = d["low_price_adj"].astype(float)
    vol = pd.to_numeric(d.get("volume"), errors="coerce")

    first_close = float(close.iloc[0])
    last_close = float(close.iloc[-1])
    window_high = float(high.max())
    window_low = float(low.min())

    def ret_n(n):
        # Return over the last n trading days (needs n+1 bars).
        if bars <= n:
            return None
        return round((last_close / float(close.iloc[-(n + 1)]) - 1.0) * 100.0, 2)

    # VWAP since listing — a true volume-weighted reference over what exists.
    typical = (high + low + close) / 3.0
    vol_sum = float(vol.sum()) if vol is not None else 0.0
    vwap = float((typical * vol).sum() / vol_sum) if vol_sum else None
    avg_vol = float(vol.mean()) if vol is not None and bars else None
    last_vol = float(vol.iloc[-1]) if vol is not None and bars else None
    rvol = round(last_vol / avg_vol, 2) if (avg_vol and last_vol is not None) else None

    # Relative strength since listing vs a benchmark/sector over the same window.
    # Ratio of price-relatives so it can't flip sign or blow up (see IMM fix).
    rs_vs_sector = None
    if sector_df is not None and not sector_df.empty:
        s = sector_df.dropna(subset=["close_price_adj"]).sort_values("business_date")
        if len(s) >= 2:
            sc = s["close_price_adj"].astype(float)
            sec_ret = float(sc.iloc[-1]) / float(sc.iloc[0]) - 1.0
            stk_ret = last_close / first_close - 1.0
            rs_vs_sector = round((1.0 + stk_ret) / (1.0 + sec_ret), 3)

    confidence = min(100.0, round(100.0 * bars / required_bars, 1)) if required_bars else None
    bars_remaining = max(0, required_bars - bars) if required_bars else None

    return {
        "symbol": symbol,
        "desk_label": desk_label,
        "is_new_listing": True,
        "bars_available": bars,
        "required_bars": required_bars,
        "bars_remaining": bars_remaining,
        "confidence_pct": confidence,
        "first_date": pd.to_datetime(d["business_date"].iloc[0]).strftime("%Y-%m-%d"),
        "last_date": pd.to_datetime(d["business_date"].iloc[-1]).strftime("%Y-%m-%d"),
        "listing_price": round(first_close, 2),
        "last_price": round(last_close, 2),
        "return_since_listing": _pct(last_close, first_close),
        "window_high": round(window_high, 2),
        "window_low": round(window_low, 2),
        "pct_from_high": _pct(last_close, window_high),
        "pct_from_low": _pct(last_close, window_low),
        "momentum_5d": ret_n(5),
        "momentum_10d": ret_n(10),
        "vwap": round(vwap, 2) if vwap is not None else None,
        "price_vs_vwap": _pct(last_close, vwap) if vwap is not None else None,
        "avg_volume": int(avg_vol) if avg_vol is not None and pd.notna(avg_vol) else None,
        "last_volume": int(last_vol) if last_vol is not None and pd.notna(last_vol) else None,
        "rvol": rvol,
        "rs_vs_sector": rs_vs_sector,
    }

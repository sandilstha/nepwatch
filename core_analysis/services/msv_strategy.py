import numpy as np
import pandas as pd

try:
    import pandas_ta as ta
except Exception:
    ta = None


def _to_dataframe(data_source):
    if isinstance(data_source, pd.DataFrame):
        return data_source.copy()

    if data_source is None or not hasattr(data_source, "exists"):
        return pd.DataFrame()
    if not data_source.exists():
        return pd.DataFrame()

    data = list(
        data_source.order_by("business_date").values(
            "business_date",
            "open_price_adj",
            "high_price_adj",
            "low_price_adj",
            "close_price_adj",
            "volume",
        )
    )
    return pd.DataFrame(data)


def calculate_macd(data_source, fast=12, slow=26, signal=9):
    """
    Backward-compatible MACD calculator used by existing architecture.
    Returns a DataFrame with MACD columns and signal placeholders.
    """
    df = _to_dataframe(data_source)
    if df.empty:
        return pd.DataFrame()

    required = {"business_date", "close_price_adj"}
    if not required.issubset(df.columns) or ta is None:
        return pd.DataFrame()

    df = df.sort_values("business_date").reset_index(drop=True)
    df["close_price_adj"] = pd.to_numeric(df["close_price_adj"], errors="coerce")

    macd_df = ta.macd(df["close_price_adj"], fast=fast, slow=slow, signal=signal)
    if macd_df is None or macd_df.empty:
        return pd.DataFrame()

    df["MACD_line"] = macd_df.iloc[:, 0]
    df["MACD_histogram"] = macd_df.iloc[:, 1]
    df["MACD_signal"] = macd_df.iloc[:, 2]
    df["signal"] = "Hold"

    for i in range(1, len(df)):
        if pd.isna(df.loc[i, "MACD_line"]) or pd.isna(df.loc[i - 1, "MACD_line"]):
            continue
        prev_line = df.loc[i - 1, "MACD_line"]
        prev_sig = df.loc[i - 1, "MACD_signal"]
        line = df.loc[i, "MACD_line"]
        sig = df.loc[i, "MACD_signal"]
        if prev_line <= prev_sig and line > sig:
            df.loc[i, "signal"] = "Buy"
        elif prev_line >= prev_sig and line < sig:
            df.loc[i, "signal"] = "Exit"

    return df[["business_date", "close_price_adj", "MACD_line", "MACD_histogram", "MACD_signal", "signal"]]


def run_msv_long_only_simulation(
    data_source,
    macd_fast=12,
    macd_slow=26,
    macd_signal=9,
    atr_length=14,
    atr_multiplier=2.0,
    rvol_period=20,
    rvol_threshold=1.5,
    supertrend_length=10,
    supertrend_multiplier=3.0,
    vwap_period=20,
):
    """
    Long-only NEPSE strategy using MACD + Supertrend + VWAP + ATR + RVOL.
    Entry: all confirmations true.
    Exit: any invalidation true or ATR stop hit.
    """
    warnings = []

    if ta is None:
        return {"error": "pandas_ta library is unavailable.", "warnings": ["Install pandas_ta to run this strategy."]}, pd.DataFrame(), pd.DataFrame()

    settings = {
        "macd_fast": int(macd_fast),
        "macd_slow": int(macd_slow),
        "macd_signal": int(macd_signal),
        "atr_length": int(atr_length),
        "atr_multiplier": float(atr_multiplier),
        "rvol_period": int(rvol_period),
        "rvol_threshold": float(rvol_threshold),
        "supertrend_length": int(supertrend_length),
        "supertrend_multiplier": float(supertrend_multiplier),
        "vwap_period": int(vwap_period),
    }

    if settings["macd_fast"] < 1 or settings["macd_slow"] < 2 or settings["macd_signal"] < 1:
        warnings.append("MACD settings are invalid. Use positive lengths.")
    if settings["macd_fast"] >= settings["macd_slow"]:
        warnings.append("MACD Fast Length should be smaller than MACD Slow Length.")
    if settings["atr_length"] < 2:
        warnings.append("ATR Length is too small. Use at least 2.")
    if settings["supertrend_length"] < 2:
        warnings.append("Supertrend Length is too small. Use at least 2.")
    if settings["rvol_period"] < 2:
        warnings.append("RVOL Period is too small. Use at least 2.")
    if settings["vwap_period"] < 2:
        warnings.append("VWAP Period is too small. Use at least 2.")
    if settings["rvol_threshold"] <= 0 or settings["atr_multiplier"] <= 0 or settings["supertrend_multiplier"] <= 0:
        warnings.append("Multiplier and threshold values must be greater than zero.")

    if warnings:
        return {"error": "Invalid indicator settings.", "warnings": warnings}, pd.DataFrame(), pd.DataFrame()

    df = _to_dataframe(data_source)
    required_cols = {"business_date", "open_price_adj", "high_price_adj", "low_price_adj", "close_price_adj", "volume"}
    missing = required_cols.difference(df.columns)
    if missing:
        return {"error": f"Missing required fields: {', '.join(sorted(missing))}", "warnings": ["Required OHLCV columns are not fully available."]}, pd.DataFrame(), pd.DataFrame()

    df["business_date"] = pd.to_datetime(df["business_date"], errors="coerce")
    if df["business_date"].isna().any():
        return {
            "error": "Invalid business_date values.",
            "warnings": ["MSV strategy requires valid dates to calculate VWAP."],
        }, pd.DataFrame(), pd.DataFrame()

    df = df.sort_values("business_date").reset_index(drop=True)
    for col in ["open_price_adj", "high_price_adj", "low_price_adj", "close_price_adj", "volume"]:
        df[col] = pd.to_numeric(df[col], errors="coerce")

    # Range-based math (ATR stop, Supertrend, VWAP typical price) degenerates on
    # series where most bars have high == low — true of ~63% of NEPSE *index*
    # bars and of ultra-illiquid scrips. Warn instead of silently mis-signalling.
    flat_share = float((df["high_price_adj"] == df["low_price_adj"]).mean())
    if flat_share > 0.30:
        warnings.append(
            f"{flat_share:.0%} of bars have High == Low (typical of NEPSE index series or "
            "very thin scrips). ATR stops and Supertrend are unreliable here — run this "
            "desk on individual stocks, not indices."
        )

    min_bars = max(
        settings["macd_slow"] + settings["macd_signal"],
        settings["atr_length"],
        settings["rvol_period"],
        settings["supertrend_length"],
        settings["vwap_period"],
    ) + 5
    if len(df) < min_bars:
        return {
            "error": f"Insufficient historical data. Need at least {min_bars} rows, got {len(df)}.",
            "warnings": ["Increase date range to satisfy indicator lookback requirements."],
        }, pd.DataFrame(), pd.DataFrame()

    macd_df = ta.macd(df["close_price_adj"], fast=settings["macd_fast"], slow=settings["macd_slow"], signal=settings["macd_signal"])
    atr = ta.atr(df["high_price_adj"], df["low_price_adj"], df["close_price_adj"], length=settings["atr_length"])
    supertrend_df = ta.supertrend(
        df["high_price_adj"],
        df["low_price_adj"],
        df["close_price_adj"],
        length=settings["supertrend_length"],
        multiplier=settings["supertrend_multiplier"],
    )
    # Rolling VWAP over ``vwap_period`` bars: Σ(typical_price·volume) / Σ(volume).
    # NOTE: we do NOT use ta.vwap here. ta.vwap defaults to anchor="D" (daily
    # reset); on daily bars each bar is its own anchor group, so its VWAP
    # collapses to that same bar's (H+L+C)/3 and the "price above VWAP" filter
    # degenerates to "close in the upper third of the bar" — a within-bar check
    # carrying no multi-day demand information. A rolling window restores the
    # intended volume-weighted trend reference.
    typical_price = (
        df["high_price_adj"] + df["low_price_adj"] + df["close_price_adj"]
    ) / 3.0
    vwap_win = settings["vwap_period"]
    tpv_sum = (typical_price * df["volume"]).rolling(window=vwap_win).sum()
    vol_sum = df["volume"].rolling(window=vwap_win).sum().replace(0, np.nan)
    vwap = tpv_sum / vol_sum

    if macd_df is None or macd_df.empty or atr is None or supertrend_df is None or supertrend_df.empty or vwap is None:
        return {
            "error": "Indicator calculation failed.",
            "warnings": ["Check data quality and ensure no missing OHLCV fields for selected range."],
        }, pd.DataFrame(), pd.DataFrame()

    df["MACD_line"] = macd_df.iloc[:, 0]
    df["MACD_histogram"] = macd_df.iloc[:, 1]
    df["MACD_signal"] = macd_df.iloc[:, 2]

    st_col = next((c for c in supertrend_df.columns if c.startswith("SUPERT_")), None)
    if st_col is None:
        return {"error": "Supertrend column not found in pandas_ta output.", "warnings": []}, pd.DataFrame(), pd.DataFrame()

    df["Supertrend"] = supertrend_df[st_col]
    df["VWAP"] = vwap
    df["ATR"] = atr
    # Zero average volume (a thin NEPSE scrip halted for the whole lookback)
    # must yield NaN, not inf — inf > threshold would fabricate a volume
    # confirmation out of a dead tape.
    df["RVOL_avg"] = df["volume"].rolling(window=settings["rvol_period"]).mean().shift(1).replace(0, np.nan)
    df["RVOL"] = df["volume"] / df["RVOL_avg"]

    nan_rows = df[["MACD_line", "MACD_signal", "MACD_histogram", "Supertrend", "VWAP", "ATR", "RVOL"]].isna().all(axis=1).sum()
    if nan_rows > 0:
        warnings.append("NaN values detected in warmup region. Signals are suppressed until indicators are valid.")

    df["signal"] = "Hold"
    df["position_status"] = "No Position"
    df["stop_loss"] = np.nan

    prev_macd = df["MACD_line"].shift(1)
    prev_signal = df["MACD_signal"].shift(1)

    in_position = False
    current_stop = np.nan
    entry_price = np.nan
    entries = 0
    exits = 0

    trades = []
    entry_date = None

    for i in range(1, len(df)):
        ready = not pd.isna(df.loc[i, "MACD_line"]) and not pd.isna(df.loc[i, "MACD_signal"]) and not pd.isna(prev_macd.iloc[i]) and not pd.isna(prev_signal.iloc[i]) and not pd.isna(df.loc[i, "Supertrend"]) and not pd.isna(df.loc[i, "VWAP"]) and not pd.isna(df.loc[i, "ATR"]) and not pd.isna(df.loc[i, "RVOL"])

        if not ready:
            df.loc[i, "position_status"] = "Holding Position" if in_position else "No Position"
            if in_position:
                df.loc[i, "stop_loss"] = current_stop
            continue

        price = float(df.loc[i, "close_price_adj"])

        macd_cross_up = prev_macd.iloc[i] <= prev_signal.iloc[i] and df.loc[i, "MACD_line"] > df.loc[i, "MACD_signal"]
        macd_cross_down = prev_macd.iloc[i] >= prev_signal.iloc[i] and df.loc[i, "MACD_line"] < df.loc[i, "MACD_signal"]
        price_above_supertrend = price > float(df.loc[i, "Supertrend"])
        price_above_vwap = price > float(df.loc[i, "VWAP"])
        rvol_ok = float(df.loc[i, "RVOL"]) > settings["rvol_threshold"]

        buy_cond = macd_cross_up and price_above_supertrend and price_above_vwap and rvol_ok

        if not in_position:
            if buy_cond:
                in_position = True
                entries += 1
                entry_price = price
                entry_date = df.loc[i, "business_date"]
                current_stop = entry_price - (settings["atr_multiplier"] * float(df.loc[i, "ATR"]))
                df.loc[i, "signal"] = "Buy"
                df.loc[i, "position_status"] = "Holding Position"
                df.loc[i, "stop_loss"] = current_stop
            else:
                df.loc[i, "position_status"] = "No Position"
        else:
            stop_hit = price <= current_stop
            supertrend_break = price < float(df.loc[i, "Supertrend"])
            exit_cond = macd_cross_down or supertrend_break or stop_hit

            df.loc[i, "stop_loss"] = current_stop

            if exit_cond:
                exits += 1
                df.loc[i, "signal"] = "Exit"
                df.loc[i, "position_status"] = "Exit Position"
                trades.append(
                    {
                        "entry_date": entry_date,
                        "exit_date": df.loc[i, "business_date"],
                        "entry_price": round(entry_price, 2),
                        "exit_price": round(price, 2),
                        "pnl_pct": round(((price / entry_price) - 1) * 100, 2),
                        "exit_reason": "atr_stop" if stop_hit else ("macd_cross_down" if macd_cross_down else "supertrend_break"),
                    }
                )
                in_position = False
                entry_price = np.nan
                entry_date = None
                current_stop = np.nan
            else:
                df.loc[i, "position_status"] = "Holding Position"

    if in_position:
        last_price = float(df["close_price_adj"].iloc[-1])
        trades.append(
            {
                "entry_date": entry_date,
                "exit_date": df["business_date"].iloc[-1],
                "entry_price": round(entry_price, 2),
                "exit_price": round(last_price, 2),
                "pnl_pct": round(((last_price / entry_price) - 1) * 100, 2),
                "exit_reason": "end_of_data",
            }
        )

    # ── Backtest summary stats ────────────────────────────────────────────
    # Equal-weight, sequential, full-position trades from the per-trade P&L list
    # (the still-open position is included, marked exit_reason=end_of_data).
    pnls = [t["pnl_pct"] for t in trades]
    wins = [p for p in pnls if p > 0]
    losses = [p for p in pnls if p <= 0]
    stats = {
        "trades_count": len(pnls),
        "win_rate": round(100.0 * len(wins) / len(pnls), 1) if pnls else None,
        "avg_win_pct": round(sum(wins) / len(wins), 2) if wins else None,
        "avg_loss_pct": round(sum(losses) / len(losses), 2) if losses else None,
        "best_trade_pct": round(max(pnls), 2) if pnls else None,
        "worst_trade_pct": round(min(pnls), 2) if pnls else None,
        # Profit factor on equal-weight percent P&L (gross win % / gross loss %).
        # "∞" (display string) when there are wins and no losses — a numeric inf
        # renders as the literal text "inf" in the template.
        "profit_factor": (
            round(sum(wins) / abs(sum(losses)), 2) if wins and losses and sum(losses) != 0
            else ("∞" if wins and not losses else None)
        ),
    }
    if pnls:
        # Baseline 1.0 prepended so a drawdown that starts on the very first
        # trade is measured against the starting equity, not against itself.
        equity = np.cumprod([1.0] + [1.0 + p / 100.0 for p in pnls])
        peak = np.maximum.accumulate(equity)
        stats["total_return_pct"] = round((equity[-1] - 1.0) * 100.0, 2)
        stats["max_drawdown_pct"] = round(float((equity / peak - 1.0).min()) * 100.0, 2)
    else:
        stats["total_return_pct"] = None
        stats["max_drawdown_pct"] = None

    metrics = {
        "total_rows": int(len(df)),
        "entries": int(entries),
        "exits": int(exits),
        "open_position": bool(in_position),
        "warnings": warnings,
        **stats,
        "latest_signal": str(df["signal"].iloc[-1]),
        "latest_position_status": str(df["position_status"].iloc[-1]),
        "latest_rvol": round(float(df["RVOL"].iloc[-1]), 3) if not pd.isna(df["RVOL"].iloc[-1]) else None,
        "latest_rvol_ok": bool((not pd.isna(df["RVOL"].iloc[-1])) and (float(df["RVOL"].iloc[-1]) > settings["rvol_threshold"])),
        "latest_atr": round(float(df["ATR"].iloc[-1]), 3) if not pd.isna(df["ATR"].iloc[-1]) else None,
        "latest_supertrend": round(float(df["Supertrend"].iloc[-1]), 3) if not pd.isna(df["Supertrend"].iloc[-1]) else None,
        "latest_vwap": round(float(df["VWAP"].iloc[-1]), 3) if not pd.isna(df["VWAP"].iloc[-1]) else None,
        "latest_stop_loss": round(float(df["stop_loss"].iloc[-1]), 3) if not pd.isna(df["stop_loss"].iloc[-1]) else None,
        "latest_trend_status": "Bullish" if (not pd.isna(df["close_price_adj"].iloc[-1]) and not pd.isna(df["Supertrend"].iloc[-1]) and float(df["close_price_adj"].iloc[-1]) > float(df["Supertrend"].iloc[-1])) else "Bearish",
    }

    output_columns = [
        "business_date",
        "close_price_adj",
        "MACD_line",
        "MACD_signal",
        "MACD_histogram",
        "Supertrend",
        "VWAP",
        "ATR",
        "RVOL",
        "signal",
        "position_status",
        "stop_loss",
    ]

    indicator_df = df[output_columns].copy()
    trades_df = pd.DataFrame(trades)

    return metrics, trades_df, indicator_df

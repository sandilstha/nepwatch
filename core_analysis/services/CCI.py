import pandas as pd
import numpy as np
import pandas_ta as ta


def run_cci_long_only_simulation(
    data_source,
    initial_capital: float = 100000.0,
    cci_period: int = 20,
    adx_period: int = 14,
    adx_threshold: float = 25.0,
    volume_avg_period: int = 20,
    volume_multiplier: float = 1.5,
):
    """
    CCI long-only strategy for one-way markets (NEPSE):

    Entry: CCI crosses above +100 AND ADX > adx_threshold AND
           volume > volume_multiplier * prior volume average(volume_avg_period)
    Exit:  CCI crosses back below +100
    """
    min_bars = max(cci_period, adx_period, volume_avg_period) * 2

    if isinstance(data_source, pd.DataFrame):
        df = data_source.copy()
    else:
        if not data_source.exists() or data_source.count() < min_bars:
            return (
                {"error": f"Insufficient data: need at least {min_bars} bars for CCI({cci_period}) + ADX({adx_period}) + Volume({volume_avg_period})."},
                pd.DataFrame(),
            )
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
        df = pd.DataFrame(data)

    required_cols = {
        "business_date",
        "open_price_adj",
        "high_price_adj",
        "low_price_adj",
        "close_price_adj",
        "volume",
    }
    if not required_cols.issubset(df.columns):
        return {"error": "Input data missing required OHLCV columns for CCI strategy."}, pd.DataFrame()
    if len(df) < min_bars:
        return (
            {"error": f"Insufficient data: need at least {min_bars} bars for CCI({cci_period}) + ADX({adx_period}) + Volume({volume_avg_period})."},
            pd.DataFrame(),
        )

    df = df.sort_values("business_date").reset_index(drop=True)

    for col in ["open_price_adj", "high_price_adj", "low_price_adj", "close_price_adj"]:
        df[col] = df[col].astype(float)

    df["volume"] = pd.to_numeric(df["volume"], errors="coerce")
    if df["volume"].isna().all():
        return {"error": "Volume data is not available for the selected symbol/date range."}, pd.DataFrame()

    try:
        adx_df = ta.adx(
            df["high_price_adj"],
            df["low_price_adj"],
            df["close_price_adj"],
            length=adx_period,
        )
        adx_col = f"ADX_{adx_period}"
        if adx_df is not None and not adx_df.empty and adx_col in adx_df.columns:
            df["ADX"] = adx_df[adx_col]
        else:
            df["ADX"] = np.nan
    except Exception:
        df["ADX"] = np.nan

    tp = (df["high_price_adj"] + df["low_price_adj"] + df["close_price_adj"]) / 3.0
    sma_tp = tp.rolling(window=cci_period).mean()

    tp_vals = tp.to_numpy(dtype=np.float64)
    n = len(tp_vals)
    mad_vals = np.full(n, np.nan)
    for k in range(cci_period - 1, n):
        window = tp_vals[k - cci_period + 1 : k + 1]
        mad_vals[k] = np.abs(window - window.mean()).mean()
    mad = pd.Series(mad_vals, index=tp.index)
    denom = (0.015 * mad).replace(0, np.nan)
    df["cci"] = (tp - sma_tp) / denom

    prev_cci = df["cci"].shift(1)
    buy_cross = (prev_cci <= 100) & (df["cci"] > 100)
    exit_cross = (prev_cci >= 100) & (df["cci"] < 100)

    df["avg_volume"] = df["volume"].rolling(window=volume_avg_period).mean().shift(1)
    volume_breakout = df["volume"] > (df["avg_volume"] * volume_multiplier)

    position = 0
    cash = float(initial_capital)
    entry_price = 0.0
    entry_date = None
    trades = []
    filtered_count = 0

    for i in range(1, len(df)):
        dt = df.loc[i, "business_date"]
        exec_price = df.loc[i, "open_price_adj"]
        if exec_price <= 0:
            continue

        signal_bar = i - 1

        if position > 0 and exit_cross.iloc[signal_bar]:
            proceeds = position * exec_price
            pnl = proceeds - (position * entry_price)
            pnl_pct = (exec_price / entry_price - 1) * 100
            cash += proceeds
            trades.append(
                {
                    "entry_signal": "CCI > +100 + ADX + Volume Breakout",
                    "exit_signal": "CCI < +100",
                    "entry_date": entry_date,
                    "exit_date": dt,
                    "entry_price": round(entry_price, 2),
                    "exit_price": round(exec_price, 2),
                    "shares": int(position),
                    "pnl": round(pnl, 2),
                    "pnl_pct": round(pnl_pct, 2),
                    "exit_reason": "cci_fell_below_100",
                }
            )
            position = 0
            entry_price = 0.0
            entry_date = None

        if position == 0 and buy_cross.iloc[signal_bar]:
            adx_value = df.loc[signal_bar, "ADX"]
            vol_ok = bool(volume_breakout.iloc[signal_bar]) if not pd.isna(volume_breakout.iloc[signal_bar]) else False

            # NaN ADX (flat high==low bars are ~63% of NEPSE index history)
            # means "no measurable trend" — the filter must fail closed, not open.
            adx_ok = (not pd.isna(adx_value)) and float(adx_value) > adx_threshold

            if adx_ok and vol_ok:
                shares = int(cash // exec_price)
                if shares > 0:
                    cash -= shares * exec_price
                    position = shares
                    entry_price = float(exec_price)
                    entry_date = dt
            else:
                filtered_count += 1

    final_equity = cash
    if position > 0:
        last_close = df["close_price_adj"].iloc[-1]
        proceeds = position * last_close
        pnl = proceeds - (position * entry_price)
        pnl_pct = (last_close / entry_price - 1) * 100
        final_equity += proceeds
        trades.append(
            {
                "entry_signal": "CCI > +100 + ADX + Volume Breakout",
                "exit_signal": "CCI < +100",
                "entry_date": entry_date,
                "exit_date": df["business_date"].iloc[-1],
                "entry_price": round(entry_price, 2),
                "exit_price": round(last_close, 2),
                "shares": int(position),
                "pnl": round(pnl, 2),
                "pnl_pct": round(pnl_pct, 2),
                "exit_reason": "end_of_data",
            }
        )

    trades_df = pd.DataFrame(trades)
    total_return = ((final_equity / initial_capital) - 1) * 100

    metrics = {
        "initial_capital": float(initial_capital),
        "final_equity": round(final_equity, 2),
        "total_return": round(total_return, 2),
        "total_trades": len(trades_df),
        "filtered_trades": filtered_count,
        "adx_threshold": adx_threshold,
        "volume_avg_period": volume_avg_period,
        "volume_multiplier": volume_multiplier,
    }

    latest_adx = df["ADX"].iloc[-1] if not df["ADX"].isna().all() else None
    if latest_adx is not None and not np.isnan(latest_adx):
        metrics["latest_adx"] = round(float(latest_adx), 2)
        if latest_adx < 20:
            metrics["adx_color"] = "gray"
            metrics["adx_text"] = "Weak / Sideways Market"
        elif 20 <= latest_adx <= 25:
            metrics["adx_color"] = "yellow"
            metrics["adx_text"] = "Trend Starting"
        elif 25 < latest_adx <= 40:
            metrics["adx_color"] = "green"
            metrics["adx_text"] = "Strong Trend"
        else:
            metrics["adx_color"] = "darkgreen"
            metrics["adx_text"] = "Very Strong Trend"

    latest_volume = df["volume"].iloc[-1] if not df["volume"].isna().all() else None
    latest_avg_volume = df["avg_volume"].iloc[-1] if not df["avg_volume"].isna().all() else None
    latest_breakout = bool(volume_breakout.iloc[-1]) if not pd.isna(volume_breakout.iloc[-1]) else False

    if latest_volume is not None and not np.isnan(latest_volume):
        metrics["latest_volume"] = float(latest_volume)
    if latest_avg_volume is not None and not np.isnan(latest_avg_volume):
        metrics["latest_avg_volume"] = float(latest_avg_volume)

    metrics["volume_breakout_confirmed"] = latest_breakout
    metrics["volume_status_color"] = "green" if latest_breakout else "red"
    metrics["volume_status_text"] = "Breakout Confirmed" if latest_breakout else "Weak Volume"

    if not trades_df.empty:
        metrics["wins"] = int((trades_df["pnl"] >= 0).sum())
        metrics["losses"] = int((trades_df["pnl"] < 0).sum())
        metrics["win_rate"] = round((metrics["wins"] / metrics["total_trades"]) * 100, 2)
    else:
        metrics.update({"wins": 0, "losses": 0, "win_rate": 0.0})

    return metrics, trades_df

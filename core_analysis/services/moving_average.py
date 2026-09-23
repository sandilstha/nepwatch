import pandas as pd


def run_ema_50_200_long_only_simulation(
    data_source,
    initial_capital=100000.0,
    take_profit_pct=15.0,
    stop_loss_pct=7.0,
    fast_ema_period=50,
    slow_ema_period=200,
):
    """
    Long-only strategy:
    1. Buy only when Fast EMA crosses above Slow EMA
    2. Hold position until one of:
       - take profit hit
       - stop loss hit
       - Fast EMA crosses below Slow EMA
    3. No short-selling
    """
    if fast_ema_period <= 0 or slow_ema_period <= 0:
        return {"error": "EMA periods must be positive integers."}, pd.DataFrame()

    if fast_ema_period >= slow_ema_period:
        return {"error": "Fast EMA period must be smaller than Slow EMA period."}, pd.DataFrame()

    required_depth = max(fast_ema_period, slow_ema_period)
    if isinstance(data_source, pd.DataFrame):
        df = data_source.copy()
    else:
        if not data_source.exists() or data_source.count() < required_depth:
            return {"error": f"Insufficient data depth for EMA({fast_ema_period}/{slow_ema_period}) strategy."}, pd.DataFrame()
        data = list(data_source.order_by("business_date").values(
            "business_date", "open_price_adj", "high_price_adj", "low_price_adj", "close_price_adj"
        ))
        df = pd.DataFrame(data)

    required_cols = {"business_date", "open_price_adj", "high_price_adj", "low_price_adj", "close_price_adj"}
    if not required_cols.issubset(df.columns):
        return {"error": "Input data missing required OHLC columns for EMA strategy."}, pd.DataFrame()
    if len(df) < required_depth:
        return {"error": f"Insufficient data depth for EMA({fast_ema_period}/{slow_ema_period}) strategy."}, pd.DataFrame()

    df = df.sort_values("business_date").reset_index(drop=True)

    for col in ["open_price_adj", "high_price_adj", "low_price_adj", "close_price_adj"]:
        df[col] = df[col].astype(float)

    close_ser = df["close_price_adj"]
    df["ema_fast"] = close_ser.ewm(span=fast_ema_period, adjust=False).mean()
    df["ema_slow"] = close_ser.ewm(span=slow_ema_period, adjust=False).mean()

    prev_ema_fast = df["ema_fast"].shift(1)
    prev_ema_slow = df["ema_slow"].shift(1)

    buy_cross = (prev_ema_fast <= prev_ema_slow) & (df["ema_fast"] > df["ema_slow"])
    sell_cross = (prev_ema_fast >= prev_ema_slow) & (df["ema_fast"] < df["ema_slow"])

    position = 0
    cash = float(initial_capital)
    entry_price = 0.0
    entry_date = None
    trades = []

    for i in range(1, len(df)):
        dt = df.loc[i, "business_date"]
        entry_exec_price = df.loc[i, "open_price_adj"]
        day_high = df.loc[i, "high_price_adj"]
        day_low = df.loc[i, "low_price_adj"]

        if entry_exec_price <= 0:
            continue

        if position > 0:
            tp_price = entry_price * (1 + take_profit_pct / 100.0)
            sl_price = entry_price * (1 - stop_loss_pct / 100.0)

            exit_price = None
            exit_reason = None

            if day_low <= sl_price:
                exit_price = sl_price
                exit_reason = "stop_loss"
            elif day_high >= tp_price:
                exit_price = tp_price
                exit_reason = "take_profit"
            elif sell_cross.iloc[i - 1]:
                exit_price = entry_exec_price
                exit_reason = "ema_bear_cross"

            if exit_price is not None:
                proceeds = position * exit_price
                pnl = proceeds - (position * entry_price)
                pnl_pct = (exit_price / entry_price - 1) * 100
                cash += proceeds
                trades.append({
                    "entry_date": entry_date,
                    "exit_date": dt,
                    "entry_price": round(entry_price, 2),
                    "exit_price": round(exit_price, 2),
                    "shares": int(position),
                    "pnl": round(pnl, 2),
                    "pnl_pct": round(pnl_pct, 2),
                    "exit_reason": exit_reason,
                })
                position = 0
                entry_price = 0.0
                entry_date = None

        if position == 0 and buy_cross.iloc[i - 1]:
            shares = int(cash // entry_exec_price)
            if shares > 0:
                cash -= shares * entry_exec_price
                position = shares
                entry_price = float(entry_exec_price)
                entry_date = dt

    final_equity = cash
    if position > 0:
        last_close = df["close_price_adj"].iloc[-1]
        proceeds = position * last_close
        pnl = proceeds - (position * entry_price)
        pnl_pct = (last_close / entry_price - 1) * 100
        final_equity += proceeds
        trades.append({
            "entry_date": entry_date,
            "exit_date": df["business_date"].iloc[-1],
            "entry_price": round(entry_price, 2),
            "exit_price": round(last_close, 2),
            "shares": int(position),
            "pnl": round(pnl, 2),
            "pnl_pct": round(pnl_pct, 2),
            "exit_reason": "end_of_data",
        })

    trades_df = pd.DataFrame(trades)
    total_return = ((final_equity / initial_capital) - 1) * 100

    metrics = {
        "initial_capital": float(initial_capital),
        "final_equity": round(final_equity, 2),
        "total_return": round(total_return, 2),
        "total_trades": len(trades_df),
    }

    if not trades_df.empty:
        metrics["wins"] = int((trades_df["pnl"] >= 0).sum())
        metrics["losses"] = int((trades_df["pnl"] < 0).sum())
        metrics["win_rate"] = round((metrics["wins"] / metrics["total_trades"]) * 100, 2)
    else:
        metrics.update({"wins": 0, "losses": 0, "win_rate": 0.0})

    return metrics, trades_df
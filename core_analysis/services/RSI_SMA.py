import numpy as np
import pandas as pd


def _compute_rsi(close: pd.Series, length: int) -> pd.Series:
    delta = close.diff()
    gain = delta.clip(lower=0)
    loss = -delta.clip(upper=0)

    avg_gain = gain.ewm(alpha=1 / length, adjust=False, min_periods=length).mean()
    avg_loss = loss.ewm(alpha=1 / length, adjust=False, min_periods=length).mean()

    rs = avg_gain / avg_loss.replace(0, np.nan)
    rsi = 100 - (100 / (1 + rs))
    # Flat / one-sided streaks (common on thin NEPSE scrips) would otherwise
    # leave NaN that then poisons the RSI-SMA for another `length` bars.
    rsi = rsi.mask((avg_loss == 0) & (avg_gain > 0), 100)
    rsi = rsi.mask((avg_gain == 0) & (avg_loss > 0), 0)
    rsi = rsi.mask((avg_gain == 0) & (avg_loss == 0), 50)
    return rsi


def run_rsi_sma_long_only_simulation(
    data_source,
    initial_capital=100000.0,
    rsi_length=14,
    rsi_sma_length=9,
):
    """
    RSI/RSI-SMA long-only strategy for one-way markets (NEPSE):
    1. BUY when RSI crosses above RSI SMA.
    2. Hold long until RSI crosses below RSI SMA.
    3. No short entries.
    """
    if rsi_length <= 0 or rsi_sma_length <= 0:
        return {"error": "RSI length and RSI SMA length must be positive integers."}, pd.DataFrame()

    required_depth = rsi_length + rsi_sma_length + 1

    if isinstance(data_source, pd.DataFrame):
        df = data_source.copy()
    else:
        if not data_source.exists() or data_source.count() < required_depth:
            return {"error": f"Insufficient data depth for RSI({rsi_length}) + SMA({rsi_sma_length})."}, pd.DataFrame()
        data = list(data_source.order_by("business_date").values(
            "business_date", "open_price_adj", "high_price_adj", "low_price_adj", "close_price_adj"
        ))
        df = pd.DataFrame(data)

    required_cols = {"business_date", "open_price_adj", "high_price_adj", "low_price_adj", "close_price_adj"}
    if not required_cols.issubset(df.columns):
        return {"error": "Input data missing required OHLC columns for RSI strategy."}, pd.DataFrame()

    if len(df) < required_depth:
        return {"error": f"Insufficient data depth for RSI({rsi_length}) + SMA({rsi_sma_length})."}, pd.DataFrame()

    df = df.sort_values("business_date").reset_index(drop=True)

    for col in ["open_price_adj", "high_price_adj", "low_price_adj", "close_price_adj"]:
        df[col] = df[col].astype(float)

    df["rsi"] = _compute_rsi(df["close_price_adj"], rsi_length)
    df["rsi_sma"] = df["rsi"].rolling(window=rsi_sma_length).mean()

    prev_rsi = df["rsi"].shift(1)
    prev_rsi_sma = df["rsi_sma"].shift(1)
    buy_cross = (prev_rsi <= prev_rsi_sma) & (df["rsi"] > df["rsi_sma"])
    exit_cross = (prev_rsi >= prev_rsi_sma) & (df["rsi"] < df["rsi_sma"])

    position = 0
    cash = float(initial_capital)
    entry_price = 0.0
    entry_date = None
    trades = []

    for i in range(1, len(df)):
        dt = df.loc[i, "business_date"]
        exec_price = df.loc[i, "open_price_adj"]
        if exec_price <= 0:
            continue

        if position > 0 and exit_cross.iloc[i - 1]:
            proceeds = position * exec_price
            pnl = proceeds - (position * entry_price)
            pnl_pct = (exec_price / entry_price - 1) * 100
            cash += proceeds
            trades.append({
                "entry_date": entry_date,
                "exit_date": dt,
                "entry_price": round(entry_price, 2),
                "exit_price": round(exec_price, 2),
                "shares": int(position),
                "pnl": round(pnl, 2),
                "pnl_pct": round(pnl_pct, 2),
                "entry_signal": "BUY",
                "exit_signal": "EXIT",
                "exit_reason": "rsi_cross_below_rsi_sma",
            })
            position = 0
            entry_price = 0.0
            entry_date = None

        if position == 0 and buy_cross.iloc[i - 1]:
            shares = int(cash // exec_price)
            if shares > 0:
                cash -= shares * exec_price
                position = shares
                entry_price = float(exec_price)
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
            "entry_signal": "BUY",
            "exit_signal": "EXIT",
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
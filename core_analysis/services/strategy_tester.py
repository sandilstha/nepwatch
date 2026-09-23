import pandas as pd
import numpy as np

def _calc_t3_internal(close: pd.Series, period: int, vfactor: float) -> pd.Series:
    """Isolated mathematical Tillson T3 Moving Average computation."""
    vf = vfactor
    c1 = -(vf ** 3)
    c2 =  3 * vf**2 + 3 * vf**3
    c3 = -6 * vf**2 - 3 * vf - 3 * vf**3
    c4 =  1 + 3 * vf + vf**3 + 3 * vf**2

    e1 = close.ewm(span=period, adjust=False).mean()
    e2 = e1.ewm(span=period,    adjust=False).mean()
    e3 = e2.ewm(span=period,    adjust=False).mean()
    e4 = e3.ewm(span=period,    adjust=False).mean()
    e5 = e4.ewm(span=period,    adjust=False).mean()
    e6 = e5.ewm(span=period,    adjust=False).mean()

    return c1*e6 + c2*e5 + c3*e4 + c4*e3

def run_t3ma_macd_ribbon_simulation(data_source, initial_capital=100000.0, use_ribbon_filter=True):
    """
    STRICT STRATEGY EXPERIMENTATION ENGINE
    Runs the Tillson T3MA Ribbon + MACD Crossover backtest rules completely in memory.
    Accepts either a Django QuerySet OR a pre-built pandas DataFrame (from _build_standard_dataframe).

    This serves as your structural template. You can append new standalone strategy
    functions below this one inside this file later.
    """
    required_cols = {"business_date", "open_price_adj", "high_price_adj", "low_price_adj", "close_price_adj"}

    if isinstance(data_source, pd.DataFrame):
        df = data_source.copy()
        if not required_cols.issubset(df.columns) or len(df) < 54:
            return {"error": "Insufficient data depth inside the model allocation for this strategy."}, pd.DataFrame()
    else:
        # Legacy QuerySet path
        if not data_source.exists() or data_source.count() < 54:
            return {"error": "Insufficient data depth inside the model allocation for this strategy."}, pd.DataFrame()
        data = list(data_source.order_by('business_date').values(
            'business_date', 'open_price_adj', 'high_price_adj', 'low_price_adj', 'close_price_adj'
        ))
        df = pd.DataFrame(data)

    df = df.sort_values('business_date').reset_index(drop=True)
    for col in ['open_price_adj', 'high_price_adj', 'low_price_adj', 'close_price_adj']:
        df[col] = df[col].astype(float)

    close_ser = df['close_price_adj']

    # 1. Indicator Calculations
    t3_fast = _calc_t3_internal(close_ser, period=36, vfactor=0.54)
    t3_slow = _calc_t3_internal(close_ser, period=54, vfactor=0.63)
    
    fast_ema = close_ser.ewm(span=12, adjust=False).mean()
    slow_ema = close_ser.ewm(span=26, adjust=False).mean()
    macd_main = fast_ema - slow_ema
    macd_signal = macd_main.ewm(span=9, adjust=False).mean()   # MACD signal is an EMA, not an SMA
    hist = macd_main - macd_signal

    # 2. Vector Signal Identifications
    prev_main = macd_main.shift(1)
    prev_signal = macd_signal.shift(1)
    
    raw_bull = (macd_signal < macd_main) & (prev_signal > prev_main) & (hist > 0)
    raw_bear = (macd_signal > macd_main) & (prev_signal < prev_main) & (hist < 0)

    if use_ribbon_filter:
        # Bullish ribbon = fast T3 ABOVE slow T3 (this was inverted before, so
        # the filter admitted bull crosses only while the ribbon was bearish).
        bull = raw_bull & (t3_fast > t3_slow)
        bear = raw_bear & (t3_fast < t3_slow)
    else:
        bull = raw_bull
        bear = raw_bear

    # 3. Execution Simulation Loop (Long-Only NEPSE Parameters)
    position = 0
    cash = initial_capital
    entry_price = 0.0
    entry_date = None
    trades = []

    for i in range(1, len(df)):
        dt = df.loc[i, 'business_date']
        price = df.loc[i, 'open_price_adj']

        if price <= 0:
            continue

        # Liquidation Route
        if bear.iloc[i - 1] and position > 0:
            proceeds = position * price
            pnl = proceeds - (position * entry_price)
            pnl_pct = (price / entry_price - 1) * 100
            cash += proceeds
            trades.append({
                "entry_date": entry_date,
                "exit_date": dt,
                "entry_price": round(entry_price, 2),
                "exit_price": round(price, 2),
                "shares": position,
                "pnl": round(pnl, 2),
                "pnl_pct": round(pnl_pct, 2),
            })
            position = 0

        # Allocation Route
        if bull.iloc[i - 1] and position == 0:
            shares = cash // price
            if shares > 0:
                cash -= shares * price
                position = shares
                entry_price = price
                entry_date = dt

    # Check for terminal open exposure
    final_equity = cash
    if position > 0:
        last_close = df['close_price_adj'].iloc[-1]
        proceeds = position * last_close
        pnl = proceeds - (position * entry_price)
        pnl_pct = (last_close / entry_price - 1) * 100
        final_equity += proceeds
        trades.append({
            "entry_date": entry_date,
            "exit_date": df['business_date'].iloc[-1],
            "entry_price": round(entry_price, 2),
            "exit_price": round(last_close, 2),
            "shares": position,
            "pnl": round(pnl, 2),
            "pnl_pct": round(pnl_pct, 2),
        })

    trades_df = pd.DataFrame(trades)
    
    # 4. Metric Compilation
    total_return = ((final_equity / initial_capital) - 1) * 100
    metrics = {
        "initial_capital": initial_capital,
        "final_equity": round(final_equity, 2),
        "total_return": round(total_return, 2),
        "total_trades": len(trades_df)
    }
    
    if not trades_df.empty:
        metrics["wins"] = int((trades_df["pnl"] >= 0).sum())
        metrics["losses"] = int((trades_df["pnl"] < 0).sum())
        metrics["win_rate"] = round((metrics["wins"] / metrics["total_trades"]) * 100, 2)
    else:
        metrics.update({"wins": 0, "losses": 0, "win_rate": 0})

    return metrics, trades_df
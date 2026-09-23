import pandas as pd
import pandas_ta as ta

def calculate_macd(queryset, fast=12, slow=26, signal=9):
    """
    Ingests a Django QuerySet of StockPriceAdjustment for a specific symbol,
    calculates MACD vectors, flags structural Buy/Sell signals, and returns a DataFrame.
    """
    if not queryset.exists() or queryset.count() < slow:
        return pd.DataFrame()

    data = list(queryset.order_by('business_date').values(
        'business_date', 'close_price_adj'
    ))
    
    df = pd.DataFrame(data)
    df['close_price_adj'] = df['close_price_adj'].astype(float)
    
    macd_df = ta.macd(df['close_price_adj'], fast=fast, slow=slow, signal=signal)
    
    if macd_df is None or macd_df.empty:
        return pd.DataFrame()
        
    df['MACD_line'] = macd_df.iloc[:, 0]
    df['MACD_histogram'] = macd_df.iloc[:, 1]
    df['MACD_signal'] = macd_df.iloc[:, 2]
    
    # Pre-allocate signal columns
    df['signal'] = 'Hold'
    
    # Calculate Crossovers sequentially (Index 1 onwards)
    for i in range(1, len(df)):
        # Skip if technical data points are not yet populated (NaN lookback period)
        if pd.isna(df.loc[i, 'MACD_line']) or pd.isna(df.loc[i-1, 'MACD_line']):
            continue
            
        current_line = df.loc[i, 'MACD_line']
        current_sig = df.loc[i, 'MACD_signal']
        prev_line = df.loc[i-1, 'MACD_line']
        prev_sig = df.loc[i-1, 'MACD_signal']
        
        # Bullish Crossover (Line crossed above Signal)
        if prev_line <= prev_sig and current_line > current_sig:
            df.loc[i, 'signal'] = 'Buy'
        # Bearish Crossover (Line crossed below Signal)
        elif prev_line >= prev_sig and current_line < current_sig:
            df.loc[i, 'signal'] = 'Sell'
            
    return df[['business_date', 'MACD_line', 'MACD_histogram', 'MACD_signal', 'signal']]
import numpy as np
import pandas as pd
import pandas_ta as ta

_MIN_ROWS = 150        # full Weinstein read: needs the real 30-week (150-day) baseline
_MIN_PROVISIONAL = 30  # below this there isn't even enough history for a provisional read


def _coerce_int(value, default, minimum=1):
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return default
    return parsed if parsed >= minimum else default


def _coerce_float(value, default, minimum=None):
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return default
    if minimum is not None and parsed < minimum:
        return default
    return parsed


def _as_bool(value):
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "on"}
    return bool(value)


def calculate_stage_analysis(
    df,
    volume_multiplier=1.5,
    resistance_lookback=20,
    volume_lookback=20,
    momentum_period=60,
    rsi_length=14,
    rsi_threshold=55.0,
    adx_length=14,
    adx_threshold=20.0,
    use_rsi_filter=False,
    use_adx_filter=False,
):
    """
    NEPSE-adapted Stage Analysis (Weinstein method).

    Returns an annotated dataframe with stage classification columns.
    """
    volume_multiplier = _coerce_float(volume_multiplier, 1.5, minimum=0.1)
    resistance_lookback = _coerce_int(resistance_lookback, 20, minimum=2)
    volume_lookback = _coerce_int(volume_lookback, 20, minimum=2)
    momentum_period = _coerce_int(momentum_period, 60, minimum=2)
    rsi_length = _coerce_int(rsi_length, 14, minimum=2)
    rsi_threshold = _coerce_float(rsi_threshold, 55.0, minimum=0.0)
    adx_length = _coerce_int(adx_length, 14, minimum=2)
    adx_threshold = _coerce_float(adx_threshold, 20.0, minimum=0.0)
    use_rsi_filter = _as_bool(use_rsi_filter)
    use_adx_filter = _as_bool(use_adx_filter)

    required_columns = {'close', 'high', 'volume'}
    if use_adx_filter:
        required_columns.add('low')
    if df.empty or not required_columns.issubset(df.columns):
        return df

    # Work on a copy so we never mutate the caller's DataFrame (avoids
    # SettingWithCopyWarning and accidental upstream state corruption).
    df = df.copy()

    n = len(df)
    provisional = False
    long_ema_len, short_ema_len = 150, 50

    if n < _MIN_ROWS:
        if n < _MIN_PROVISIONAL:
            # Truly too little history (e.g. just-listed) — no meaningful read.
            df['stage'] = 'Insufficient Data'
            df['provisional'] = False
            df['history_rows'] = n
            return df
        # Newly listed: scale the 30-week / 10-week baselines down to the
        # available history (preserving the ~3:1 ratio) so we can still produce
        # a usable, clearly-flagged *provisional* classification instead of a
        # bare "Insufficient Data - Unknown". The result is marked provisional
        # so the UI can warn that it's based on a short window.
        provisional = True
        long_ema_len = max(10, min(150, n // 2))
        short_ema_len = max(4, long_ema_len // 3)
        # A 60-bar momentum window would be entirely NaN on a short history;
        # cap it so momentum still contributes to the score.
        momentum_period = min(momentum_period, max(5, n // 3))

    df['provisional'] = provisional
    df['history_rows'] = n
    df['ema_30w'] = ta.ema(df['close'], length=long_ema_len)
    df['ema_10w'] = ta.ema(df['close'], length=short_ema_len)

    df['avg_volume'] = df['volume'].rolling(volume_lookback).mean()
    safe_avg = df['avg_volume'].replace(0, np.nan)
    df['volume_ratio'] = df['volume'] / safe_avg

    df['resistance'] = df['high'].rolling(resistance_lookback).max().shift(1)

    # Normalized 5-bar slope of the 30-week MA, with a flat band. A plain
    # ``ema > ema.shift(5)`` boolean has no "flat" state: any infinitesimal
    # uptick counts as "rising", and topping (Stage 3 = MA flattening after a
    # rise) is indistinguishable from advancing. We split the slope into three
    # mutually exclusive regimes — rising / flat / falling — so each stage maps
    # to exactly one regime. ``flat_band`` is the percent move over 5 bars below
    # which the slow 30-week MA is treated as flat (tunable).
    df['ema30_slope_pct'] = (df['ema_30w'] / df['ema_30w'].shift(5) - 1.0) * 100.0
    flat_band = 0.2
    df['ema30_rising'] = df['ema30_slope_pct'] > flat_band
    df['ema30_falling'] = df['ema30_slope_pct'] < -flat_band
    df['ema30_flat'] = df['ema30_slope_pct'].abs() <= flat_band
    df['returns_3m'] = df['close'].pct_change(periods=momentum_period)

    df['rsi'] = ta.rsi(df['close'], length=rsi_length)
    df['adx'] = np.nan
    if 'low' in df.columns:
        adx_df = ta.adx(df['high'], df['low'], df['close'], length=adx_length)
        if isinstance(adx_df, pd.DataFrame) and not adx_df.empty:
            adx_col = next((col for col in adx_df.columns if col.upper().startswith('ADX_')), None)
            if adx_col is not None:
                df['adx'] = adx_df[adx_col]

    rsi_confirm = df['rsi'] >= rsi_threshold
    adx_confirm = df['adx'] >= adx_threshold
    rsi_gate = rsi_confirm if use_rsi_filter else pd.Series(True, index=df.index)
    adx_gate = adx_confirm if use_adx_filter else pd.Series(True, index=df.index)

    df['above_ema30'] = df['close'] > df['ema_30w']
    df['breakout'] = df['close'] > df['resistance']
    df['volume_confirm'] = df['volume_ratio'] > volume_multiplier
    df['momentum_confirm'] = df['returns_3m'] > 0
    df['rsi_confirm'] = rsi_confirm
    df['adx_confirm'] = adx_confirm
    df['stage2_score'] = (
        df['above_ema30'].fillna(False).astype(int) +
        df['ema30_rising'].fillna(False).astype(int) +
        df['breakout'].fillna(False).astype(int) +
        df['volume_confirm'].fillna(False).astype(int) +
        df['momentum_confirm'].fillna(False).astype(int) +
        df['rsi_confirm'].fillna(False).astype(int) +
        df['adx_confirm'].fillna(False).astype(int)
    )

    conditions_stage2 = (
        df['above_ema30'] &
        (df['ema30_rising']) &
        df['breakout'] &
        df['volume_confirm'] &
        df['momentum_confirm'] &
        rsi_gate &
        adx_gate
    )

    # Stage 3 (topping): the 30-week MA has FLATTENED (no longer rising) while
    # price has rolled below the fast MA but has not yet broken decisively below
    # the 30-week MA. Requiring a *rising* MA here was a bug — a genuine top, by
    # definition, is where the uptrend stalls.
    conditions_stage3 = (
        (df['close'] < df['ema_10w']) &
        (df['ema_10w'] < df['ema_30w']) &
        (df['ema30_flat']) &
        (df['close'] >= df['ema_30w'] * 0.85)
    )

    # Stage 4 (declining): price below a FALLING 30-week MA. Mutually exclusive
    # with Stage 3 on the slope regime (flat vs falling).
    conditions_stage4 = (
        (df['close'] < df['ema_30w']) &
        (df['ema30_falling'])
    )

    # Keep canonical stage labels so downstream UI/filters match exactly.
    # Priority: Stage 2 > Stage 3 > Stage 4 > Stage 1
    df['stage'] = 'Stage 1'
    df.loc[conditions_stage4, 'stage'] = 'Stage 4'
    df.loc[conditions_stage3, 'stage'] = 'Stage 3'
    df.loc[conditions_stage2, 'stage'] = 'Stage 2'

    # Weinstein context numbers the label alone doesn't carry:
    #  * bars_in_stage    — consecutive bars in the current stage (fresh Stage 2
    #    breakout vs late-stage advance read very differently),
    #  * stage_transition — "Stage 1 → Stage 2" on the bar the stage flipped
    #    (empty otherwise), so the desk can flag actionable 1→2 / 3→4 turns,
    #  * pct_from_ema30   — % distance of close from the 30-week MA (extension /
    #    proximity to the Weinstein baseline).
    stage_change = df['stage'].ne(df['stage'].shift(1))
    df['bars_in_stage'] = df.groupby(stage_change.cumsum()).cumcount() + 1
    prev_stage = df['stage'].shift(1)
    df['stage_transition'] = np.where(
        stage_change & prev_stage.notna(),
        prev_stage.fillna('') + ' → ' + df['stage'],
        '',
    )
    df['pct_from_ema30'] = (df['close'] / df['ema_30w'].replace(0, np.nan) - 1.0) * 100.0

    return df

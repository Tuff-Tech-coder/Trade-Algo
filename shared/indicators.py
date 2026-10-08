"""
Pure numpy/pandas technical indicator functions.

Used by all strategy signal generators.  No external TA library
dependency — just numpy and pandas.
"""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytz

ET = pytz.timezone("America/New_York")


# ──────────────────────────────────────────────────────────────────────────────
# Moving averages
# ──────────────────────────────────────────────────────────────────────────────

def ema(series: pd.Series, period: int) -> pd.Series:
    """Exponential moving average (Wilder-style smoothing, span=period)."""
    return series.ewm(span=period, adjust=False).mean()


def sma(series: pd.Series, period: int) -> pd.Series:
    """Simple moving average."""
    return series.rolling(period).mean()


# ──────────────────────────────────────────────────────────────────────────────
# Oscillators
# ──────────────────────────────────────────────────────────────────────────────

def rsi(series: pd.Series, period: int = 14) -> pd.Series:
    """Relative Strength Index."""
    delta = series.diff()
    gain = delta.clip(lower=0.0)
    loss = -delta.clip(upper=0.0)
    avg_gain = gain.ewm(span=period, adjust=False).mean()
    avg_loss = loss.ewm(span=period, adjust=False).mean()
    rs = avg_gain / avg_loss.replace(0.0, np.nan)
    result = 100.0 - (100.0 / (1.0 + rs))
    result = result.mask((avg_loss == 0) & (avg_gain > 0), 100.0)
    result = result.mask((avg_gain == 0) & (avg_loss > 0), 0.0)
    return result.mask((avg_gain == 0) & (avg_loss == 0), 50.0)


# ──────────────────────────────────────────────────────────────────────────────
# Volatility
# ──────────────────────────────────────────────────────────────────────────────

def atr(
    high: pd.Series, low: pd.Series, close: pd.Series, period: int = 14
) -> pd.Series:
    """Average True Range (EMA-smoothed, consistent with Pine Script ta.atr)."""
    tr = pd.concat(
        [
            high - low,
            (high - close.shift()).abs(),
            (low - close.shift()).abs(),
        ],
        axis=1,
    ).max(axis=1)
    return tr.ewm(span=period, adjust=False).mean()


def bollinger_bands(
    series: pd.Series, period: int = 20, mult: float = 2.0
) -> tuple[pd.Series, pd.Series, pd.Series]:
    """Bollinger Bands.  Returns (mid, upper, lower)."""
    mid = sma(series, period)
    std = series.rolling(period).std(ddof=0)
    return mid, mid + mult * std, mid - mult * std


# ──────────────────────────────────────────────────────────────────────────────
# VWAP
# ──────────────────────────────────────────────────────────────────────────────

def vwap_with_bands(
    df: pd.DataFrame,
) -> tuple[pd.Series, pd.Series, pd.Series, pd.Series, pd.Series]:
    """Session VWAP with ±1σ and ±2σ bands, reset per ET calendar day.

    df must contain columns: timestamp (UTC-aware), high, low, close, volume.
    Returns (vwap, upper2, lower2, upper1, lower1).
    """
    hlc3 = (df["high"] + df["low"] + df["close"]) / 3.0
    ts_et = df["timestamp"].dt.tz_convert(ET)
    day_key = ts_et.dt.date.astype(str)

    tpv  = hlc3 * df["volume"]
    tpv2 = hlc3 ** 2 * df["volume"]

    cum_vol  = df.groupby(day_key, sort=False)["volume"].cumsum()
    cum_tpv  = tpv.groupby(day_key, sort=False).cumsum()
    cum_tpv2 = tpv2.groupby(day_key, sort=False).cumsum()

    safe_vol  = cum_vol.replace(0.0, np.nan)
    vwap_val  = cum_tpv / safe_vol
    variance  = (cum_tpv2 / safe_vol) - vwap_val ** 2
    std       = variance.clip(lower=0.0).pow(0.5)

    return (
        vwap_val,
        vwap_val + 2.0 * std,
        vwap_val - 2.0 * std,
        vwap_val + 1.0 * std,
        vwap_val - 1.0 * std,
    )


# ──────────────────────────────────────────────────────────────────────────────
# Trend / directional
# ──────────────────────────────────────────────────────────────────────────────

def adx(
    high: pd.Series, low: pd.Series, close: pd.Series, period: int = 14
) -> pd.Series:
    """Average Directional Index (matching Pine Script ta.dmi / ta.adx)."""
    tr = pd.concat(
        [
            high - low,
            (high - close.shift()).abs(),
            (low  - close.shift()).abs(),
        ],
        axis=1,
    ).max(axis=1)

    up_move   = high - high.shift()
    down_move = low.shift() - low

    dm_plus  = pd.Series(
        np.where((up_move > down_move)   & (up_move > 0),   up_move,   0.0),
        index=high.index,
    )
    dm_minus = pd.Series(
        np.where((down_move > up_move)   & (down_move > 0), down_move, 0.0),
        index=high.index,
    )

    smooth_tr    = tr.ewm(span=period, adjust=False).mean()
    smooth_plus  = dm_plus.ewm(span=period, adjust=False).mean()
    smooth_minus = dm_minus.ewm(span=period, adjust=False).mean()

    di_plus  = 100.0 * smooth_plus  / smooth_tr.replace(0.0, np.nan)
    di_minus = 100.0 * smooth_minus / smooth_tr.replace(0.0, np.nan)
    di_sum   = (di_plus + di_minus).replace(0.0, np.nan)
    dx       = 100.0 * (di_plus - di_minus).abs() / di_sum

    return dx.ewm(span=period, adjust=False).mean()


# ──────────────────────────────────────────────────────────────────────────────
# Utility
# ──────────────────────────────────────────────────────────────────────────────

def round_to_tick(price: float, tick: float) -> float:
    """Round a price to the nearest tick increment."""
    if tick <= 0:
        return price
    return round(round(price / tick) * tick, 10)

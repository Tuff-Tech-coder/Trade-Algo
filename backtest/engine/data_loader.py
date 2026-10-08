"""
CSV data loader for the backtest engine.

Expected schema (matches the live feed / databento export):
    timestamp,open,high,low,close,volume

Timestamps are parsed, localized to ET if tz-naive, then stored as UTC.
Validates: monotonic increasing, no duplicates, warns on large RTH gaps.
"""

from __future__ import annotations

import logging
from datetime import time
from pathlib import Path
from typing import Optional

import pandas as pd
import pytz

log = logging.getLogger(__name__)

ET = pytz.timezone("America/New_York")
UTC = pytz.utc
RTH_OPEN = time(9, 30)
RTH_CLOSE = time(16, 0)


def load_bars(
    path: str | Path,
    start: Optional[str] = None,
    end: Optional[str] = None,
    validate: bool = True,
) -> pd.DataFrame:
    """Load 1-minute OHLCV bars from CSV.

    Returns a DataFrame with a UTC-aware 'timestamp' column and float
    open/high/low/close/volume columns, sorted oldest-first.
    """
    df = pd.read_csv(
        path,
        parse_dates=["timestamp"],
        dtype={"open": float, "high": float, "low": float, "close": float, "volume": float},
    )

    if df["timestamp"].dt.tz is None:
        # Databento/most exports are tz-naive — assume Eastern, store UTC.
        df["timestamp"] = df["timestamp"].dt.tz_localize(ET, ambiguous="infer").dt.tz_convert(UTC)
    else:
        df["timestamp"] = df["timestamp"].dt.tz_convert(UTC)

    df = df.sort_values("timestamp").reset_index(drop=True)

    if start:
        df = df[df["timestamp"] >= pd.Timestamp(start, tz=UTC)]
    if end:
        df = df[df["timestamp"] <= pd.Timestamp(end, tz=UTC)]
    df = df.reset_index(drop=True)

    if validate:
        _validate(df)
    return df


def _validate(df: pd.DataFrame) -> None:
    if df.empty:
        raise ValueError("Empty bar data after filtering")
    if df["timestamp"].duplicated().any():
        dups = df[df["timestamp"].duplicated()]["timestamp"].tolist()[:5]
        raise ValueError(f"Duplicate timestamps found: {dups}")
    if not df["timestamp"].is_monotonic_increasing:
        raise ValueError("Timestamps are not monotonically increasing")

    et_times = df["timestamp"].dt.tz_convert(ET)
    rth_mask = (et_times.dt.time >= RTH_OPEN) & (et_times.dt.time < RTH_CLOSE)
    rth_df = df[rth_mask].copy()
    rth_df["et_date"] = et_times[rth_mask].dt.date
    for day, group in rth_df.groupby("et_date"):
        if len(group) < 390 * 0.5:  # ~390 1-min RTH bars/day
            log.warning("Day %s has only %d RTH bars (~390 expected). Possible gap.",
                        day, len(group))


def generate_synthetic_bars(
    instrument: str,
    start: str,
    end: str,
    base_price: float = 5000.0,
    daily_vol: float = 60.0,
    seed: int = 42,
) -> pd.DataFrame:
    """Generate synthetic 1-minute RTH OHLCV bars (for tests/smoke runs only)."""
    import numpy as np

    rng = np.random.default_rng(seed)
    start_ts = pd.Timestamp(start, tz=ET)
    end_ts = pd.Timestamp(end, tz=ET)

    rows = []
    current = start_ts
    price = base_price
    step = daily_vol / 390.0

    while current <= end_ts:
        et_time = current.time()
        if current.weekday() >= 5:
            current = (current + pd.Timedelta(days=1)).replace(hour=9, minute=30, second=0, microsecond=0)
            continue
        if not (RTH_OPEN <= et_time < RTH_CLOSE):
            if et_time >= RTH_CLOSE:
                current = (current + pd.Timedelta(days=1)).replace(hour=9, minute=30, second=0, microsecond=0)
            else:
                current = current.replace(hour=9, minute=30, second=0, microsecond=0)
            continue

        ret = rng.normal(0, step)
        price = max(price + ret, 1.0)
        o = price
        c = price + rng.normal(0, step * 0.3)
        h = max(o, c) + abs(rng.normal(0, step * 0.5))
        lo = min(o, c) - abs(rng.normal(0, step * 0.5))
        price = c
        rows.append({
            "timestamp": current.astimezone(UTC),
            "open": round(o, 2), "high": round(h, 2), "low": round(lo, 2),
            "close": round(c, 2), "volume": float(rng.integers(100, 2000)),
        })
        current += pd.Timedelta(minutes=1)

    return pd.DataFrame(rows)

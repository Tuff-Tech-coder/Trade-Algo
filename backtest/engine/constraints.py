"""
Optional constraint checkers applied by the engine before each potential entry.
These mirror the SELF-IMPOSED guards in the live risk server — they are your own
choices, not externally imposed rules.
"""

from __future__ import annotations

from datetime import datetime, timedelta

import pandas as pd
import pytz

ET = pytz.timezone("America/New_York")


class NewsBlackout:
    """Blocks entries within ±minutes of scheduled high-impact news events.

    Reads a CSV calendar with a 'datetime' column (and optional 'event'/'impact').
    If the file is missing, no events are loaded and nothing is ever blocked.
    """

    def __init__(self, calendar_path: str | None = None, blackout_minutes: int = 2) -> None:
        self.blackout_minutes = blackout_minutes
        self._events: list[datetime] = []
        if calendar_path:
            self._load(calendar_path)

    def _load(self, path: str) -> None:
        try:
            df = pd.read_csv(path, parse_dates=["datetime"])
        except (FileNotFoundError, ValueError):
            return
        for ts in df["datetime"]:
            if ts.tzinfo is None:
                ts = ET.localize(ts)
            self._events.append(ts.astimezone(pytz.utc))

    def is_blocked(self, bar_ts: datetime) -> bool:
        if not self._events:
            return False
        if bar_ts.tzinfo is None:
            bar_ts = pytz.utc.localize(bar_ts)
        window = timedelta(minutes=self.blackout_minutes)
        for event_ts in self._events:
            if abs(bar_ts - event_ts) <= window:
                return True
        return False


class ContractLimit:
    """Clamps order size to a maximum contract count (0 = unlimited)."""

    def __init__(self, max_contracts: int = 0) -> None:
        self.max_contracts = max_contracts

    def clamp(self, requested_qty: int) -> int:
        if self.max_contracts <= 0:
            return requested_qty
        return min(requested_qty, self.max_contracts)

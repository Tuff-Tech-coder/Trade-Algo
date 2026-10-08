from dataclasses import dataclass
from datetime import datetime


@dataclass(frozen=True)
class Bar:
    """A single OHLCV bar. `timestamp` is UTC and represents the bar's open time."""
    timestamp: datetime
    open: float
    high: float
    low: float
    close: float
    volume: float
    instrument: str = ""

    @property
    def mid(self) -> float:
        return (self.high + self.low) / 2.0

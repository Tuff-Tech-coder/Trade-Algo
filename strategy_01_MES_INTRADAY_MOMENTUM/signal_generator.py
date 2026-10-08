"""
Strategy 01 — MES Intraday Momentum (volatility-scaled noise-area breakout).

Edge basis
----------
Gao, Han, Li & Zhou, "Market Intraday Momentum" (Journal of Financial
Economics, 2018): once price has moved far enough from the session open, the
move tends to CONTINUE into the close (the first-half-hour return predicts the
last-half-hour return). The effect is documented to be stronger on high-
volatility, high-volume, and macro-news days.

Implementation
--------------
  * Anchor on the RTH session OPEN price (open of the first bar at/after 09:30 ET).
  * Build a "noise band" = open Â± band_atr_mult * ATR (volatility-scaled).
  * When a closed bar prints OUTSIDE the band, enter in the direction of the
    breakout (momentum continuation).
  * Bracket the entry: ATR-based stop, R-multiple take-profit (handled by the
    IBKR executor as an OCA bracket — we only emit the prices).
  * Take at most ONE trade per session, only inside [entry_start, entry_cutoff].
  * Force-flat a few minutes before the close (flat_time) — capture the drift,
    never hold overnight.

This generator only emits ENTRY signals (with stop/target) and a single end-of-
day EXIT. The stop and target are managed broker-side once placed.

Enhancement path (not yet implemented): upsize / gate on high-volatility or
macro-news days — the project's news_calendar already flags those.
"""
import csv
import datetime
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).parent.parent / "shared"))

import logging
from typing import Optional

import pandas as pd

from indicators import atr, round_to_tick
from signal_base import SignalGenerator

log = logging.getLogger("strategy.MES_INTRADAY_MOMENTUM")


def _hhmm_to_minutes(hhmm: str) -> int:
    """'0930' -> 570 (minutes since midnight)."""
    hhmm = str(hhmm).zfill(4)
    return int(hhmm[:2]) * 60 + int(hhmm[2:])


class IntradayMomentum(SignalGenerator):
    strategy_id = "MES_INTRADAY_MOMENTUM"
    bar_size = 5
    htf_bar_size = None

    def __init__(self, risk_cfg: dict, main_cfg: dict) -> None:
        super().__init__(risk_cfg, main_cfg)
        self.atr_period = int(risk_cfg.get("atr_period", 14))
        self.band_atr_mult = float(risk_cfg.get("band_atr_mult", 0.5))
        self.stop_atr_mult = float(risk_cfg.get("stop_atr_mult", 1.0))
        self.tp_multiplier = float(risk_cfg.get("tp_multiplier", 2.0))
        self.entry_start_min = _hhmm_to_minutes(risk_cfg.get("entry_start", "1000"))
        self.entry_cutoff_min = _hhmm_to_minutes(risk_cfg.get("entry_cutoff", "1530"))
        self.flat_min = _hhmm_to_minutes(risk_cfg.get("flat_time", "1555"))
        self.rth_open_min = _hhmm_to_minutes(risk_cfg.get("session_start", "0930"))

        # Concentration gates (see check_signal). Defaults are permissive; the
        # values in risk_config.yaml turn them on.
        self.vol_ratio_min = float(risk_cfg.get("vol_ratio_min", 0.0))   # 0 = off
        self.vol_lookback = int(risk_cfg.get("vol_lookback", 200))
        self.news_day_only = bool(risk_cfg.get("news_day_only", False))
        self._news_dates = self._load_news_dates(str(risk_cfg.get("news_calendar_csv", "")))

    def _load_news_dates(self, path_str: str):
        """Load the set of ET dates that have a high-impact news event.

        Reads a CSV with a 'datetime' column (YYYY-MM-DD HH:MM:SS, ET). Returns
        None if no path is given or the file is missing (the gate then no-ops).
        A relative path is resolved against this strategy's folder, so e.g.
        "../backtest/data/news_calendar.csv" points at the backtest data.
        """
        if not path_str:
            return None
        p = pathlib.Path(path_str)
        if not p.is_absolute():
            p = pathlib.Path(__file__).parent / path_str
        dates = set()
        try:
            with open(p, newline="") as f:
                for row in csv.DictReader(f):
                    raw = (row.get("datetime") or "").strip()
                    try:
                        dates.add(datetime.datetime.strptime(raw[:10], "%Y-%m-%d").date())
                    except ValueError:
                        continue
        except FileNotFoundError:
            log.warning("%s: news_calendar_csv not found at %s — news gate disabled",
                        self.strategy_id, p)
            return None
        log.info("%s: loaded %d high-impact news days", self.strategy_id, len(dates))
        return dates

    # ── Session state (reset daily by the base class at ET midnight) ──────────

    def _reset_state(self) -> None:
        super()._reset_state()
        self._session_open: Optional[float] = None
        self._traded_today: bool = False
        self._in_position: bool = False
        self._side: Optional[str] = None
        self._flat_sent: bool = False

    # ── Signal logic ──────────────────────────────────────────────────────────

    def check_signal(self, df: pd.DataFrame, htf_df=None) -> Optional[dict]:
        if len(df) < self.atr_period + 3:
            return None

        bar = df.iloc[-1]                       # last CLOSED bar
        et = self._to_et(bar["timestamp"])
        minute_of_day = et.hour * 60 + et.minute
        close = float(bar["close"])

        completed_et = et + datetime.timedelta(minutes=self.bar_size)
        exit_signal = self.check_exit(completed_et)
        if exit_signal:
            return exit_signal
        if completed_et.hour * 60 + completed_et.minute >= self.flat_min:
            return None
        if self._session_open is None:
            times = df['timestamp'].dt.tz_convert('America/New_York')
            minutes = times.dt.hour * 60 + times.dt.minute
            opening = df[(times.dt.date == et.date()) & (minutes == self.rth_open_min)]
            if opening.empty:
                return None  # never substitute a mid-morning price for the open
            self._session_open = float(opening.iloc[0]['open'])

        # 2) At most one entry per session, inside the entry window.
        if self._traded_today or self._in_position:
            return None
        if not (self.entry_start_min <= completed_et.hour * 60 + completed_et.minute <= self.entry_cutoff_min):
            return None

        atr_series = atr(df["high"], df["low"], df["close"], self.atr_period)
        atr_val = float(atr_series.iloc[-1])
        if not atr_val > 0:
            return None

        # ── Concentration gates ──────────────────────────────────────────────
        # The intraday-momentum edge (Gao et al.) is documented to concentrate on
        # high-volatility and macro-news days. Restricting entries to those
        # conditions trades fewer, higher-conviction days — the goal being to
        # clear the per-trade cost floor that sinks an always-on version.
        # Both gates are optional; see risk_config.yaml.

        # News-day gate: only trade days that carry a high-impact release.
        if self.news_day_only and self._news_dates is not None:
            if et.date() not in self._news_dates:
                return None

        # Volatility-regime gate: only trade when current ATR is elevated vs its
        # own trailing average. The window ends at the last CLOSED bar (iloc[-1]),
        # so there is no look-ahead.
        if self.vol_ratio_min > 0:
            window = atr_series.iloc[-self.vol_lookback:]
            baseline = float(window.mean()) if len(window) else 0.0
            if baseline > 0 and (atr_val / baseline) < self.vol_ratio_min:
                return None

        upper = self._session_open + self.band_atr_mult * atr_val
        lower = self._session_open - self.band_atr_mult * atr_val

        if close > upper:
            stop = close - self.stop_atr_mult * atr_val
            target = close + self.tp_multiplier * (close - stop)
            return self._entry("buy", close, stop, target, atr_val, et)
        if close < lower:
            stop = close + self.stop_atr_mult * atr_val
            target = close - self.tp_multiplier * (stop - close)
            return self._entry("sell", close, stop, target, atr_val, et)
        return None

    # ── Helpers ────────────────────────────────────────────────────────────────

    def _entry(self, action: str, entry: float, stop: float, target: float,
               atr_val: float, et) -> dict:
        stop = round_to_tick(stop, self.tick)
        target = round_to_tick(target, self.tick)
        log.info(
            "%s %s breakout entry=%.2f open=%.2f atr=%.2f stop=%.2f tp=%.2f time=%s",
            self.strategy_id, action.upper(), entry, self._session_open,
            atr_val, stop, target, et.strftime("%H:%M"),
        )
        return {
            "ticker": self.symbol,
            "action": action,
            "quantity": self.contracts,
            "order_type": "market",
            "stop_loss": stop,
            "take_profit": target,
        }

    def on_broker_state(self, state):
        self._traded_today = bool(state.get('traded_today') or state.get('entry_pending'))
        self._in_position = bool(state.get('position_qty'))
        if self._in_position and state.get('ticker'):
            self.symbol = state['ticker']
        if self._in_position and not state.get('exit_pending'):
            self._flat_sent = False

    def check_exit(self, now):
        local = self._to_et(pd.Timestamp(now))
        if local.weekday() < 5 and local.hour * 60 + local.minute >= self.flat_min and not self._flat_sent:
            return {'ticker': self.symbol, 'action': 'exit', 'quantity': self.contracts, 'order_type': 'market'}
        return None

    def on_signal_result(self, signal, result):
        super().on_signal_result(signal, result)
        status = result.get('status')
        if signal.get('action') in ('buy', 'sell') and status in ('submitted', 'forwarded'):
            self._traded_today = True
            self._side = signal['action']
        elif signal.get('action') == 'exit':
            self._flat_sent = status in ('flat', 'close_submitted', 'exit_pending', 'forwarded')

    @staticmethod
    def _to_et(ts):
        """Convert a (UTC-aware or naive) bar timestamp to US/Eastern."""
        import pytz
        et_tz = pytz.timezone("America/New_York")
        if getattr(ts, "tzinfo", None) is None:
            ts = ts.tz_localize("UTC")
        return ts.tz_convert(et_tz)

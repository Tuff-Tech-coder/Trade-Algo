"""
SignalGeneratorAdapter — the keystone of the integration.

Lets a LIVE strategy (a `shared.signal_base.SignalGenerator` subclass, e.g.
strategy_01's IntradayMomentum) run unmodified inside the backtest engine, so
the exact code you trade is the code you test.

How it mirrors live faithfully:
  * The engine feeds 1-minute bars; this adapter aggregates them into the
    strategy's own timeframe (gen.bar_size, e.g. 5-min) exactly like the live
    BarStore, with clock-aligned buckets (09:30, 09:35, ...).
  * When a strategy-timeframe bar COMPLETES, it calls gen.check_signal(df, htf)
    with df.iloc[-1] representing the just-completed bar, exactly as live.
  * The returned signal dict (action / quantity / stop_loss / take_profit) is
    translated into a bracketed Order; the broker fills it on the next bar.
  * Per-session state is reset off BAR time (on_session_start → gen._reset_state),
    not wall-clock time — the live base class resets on datetime.now(), which is
    meaningless when replaying history.

Replay contract:
  * Overnight data warms indicators without trading, matching the live feed.
  * A bucket closes on its final minute; orders fill on the next minute open.
"""

from __future__ import annotations

import logging
from datetime import date
from typing import Optional

import pandas as pd

from engine.orders import Order

log = logging.getLogger("backtest.adapter")


class SignalGeneratorAdapter:
    def __init__(self, generator, history_maxlen: int = 300) -> None:
        self.gen = generator
        self.bar_size = int(getattr(generator, "bar_size", 5))
        self.htf_bar_size = getattr(generator, "htf_bar_size", None)
        self._sid = getattr(generator, "strategy_id", "strategy")
        self._freq = f"{self.bar_size}min"
        self._maxlen = history_maxlen

        self._bars: list[dict] = []          # completed strategy-timeframe bars
        self._cur: Optional[dict] = None     # forming bucket

    # ── Strategy interface used by the engine ─────────────────────────────────

    def on_session_start(self, session_date: date) -> None:
        self._cur = None
        if hasattr(self.gen, "_reset_state"):
            self.gen._reset_state()

    def on_session_end(self, session_date: Optional[date]) -> None:
        if self._cur is not None:
            self._cur = None

    def on_history_bar(self, bar):
        self._aggregate(bar)  # overnight inputs warm indicators, never trade

    def on_order_result(self, order, accepted):
        signal = {'action': 'exit' if order.side == 'flat' else ('buy' if order.side == 'long' else 'sell')}
        self.gen.on_signal_result(signal, {'status': ('flat' if order.side == 'flat' else 'submitted') if accepted else 'rejected'})

    def _aggregate(self, bar):
        ts = pd.Timestamp(bar.timestamp)
        period = ts.floor(self._freq)
        if self._cur is None or self._cur['period'] != period:
            self._cur = self._new_bucket(period, bar)
            self._cur['count'] = 1
        else:
            self._update_bucket(self._cur, bar)
            self._cur['count'] += 1
        # Input timestamps label minute opens. This bucket is closed at ts+1m.
        if ts + pd.Timedelta(minutes=1) >= period + pd.Timedelta(minutes=self.bar_size):
            complete = self._cur['count'] == self.bar_size
            if complete:
                self._append(self._finalize(self._cur))
            self._cur = None
            return complete
        return False

    def on_bar(self, bar, state=None):
        if not self._aggregate(bar):
            return None
        df = self._build_df()
        if len(df) < 3:
            return None
        htf_df = self._build_htf(df) if self.htf_bar_size else None
        signal = self.gen.check_signal(df, htf_df)
        return self._translate(signal) if signal else None

    # ── Bucket aggregation ────────────────────────────────────────────────────

    @staticmethod
    def _new_bucket(period, bar) -> dict:
        return {"period": period, "timestamp": period, "open": bar.open,
                "high": bar.high, "low": bar.low, "close": bar.close, "volume": bar.volume}

    @staticmethod
    def _update_bucket(b: dict, bar) -> None:
        b["high"] = max(b["high"], bar.high)
        b["low"] = min(b["low"], bar.low)
        b["close"] = bar.close
        b["volume"] += bar.volume

    @staticmethod
    def _finalize(b: dict) -> dict:
        return {"timestamp": b["period"], "open": b["open"], "high": b["high"],
                "low": b["low"], "close": b["close"], "volume": b["volume"]}

    def _append(self, bar_dict: dict) -> None:
        self._bars.append(bar_dict)
        if len(self._bars) > self._maxlen:
            self._bars = self._bars[-self._maxlen:]

    def _build_df(self) -> pd.DataFrame:
        df = pd.DataFrame(self._bars, columns=["timestamp", "open", "high", "low", "close", "volume"])
        return df

    def _build_htf(self, df: pd.DataFrame) -> pd.DataFrame:
        d = df.set_index("timestamp")
        htf = (
            d.resample(f"{int(self.htf_bar_size)}min", closed="left", label="left")
            .agg(open=("open", "first"), high=("high", "max"), low=("low", "min"),
                 close=("close", "last"), volume=("volume", "sum"))
            .dropna(subset=["open"])
            .reset_index()
        )
        counts = d.resample(f"{int(self.htf_bar_size)}min", closed="left", label="left").size()
        end = df.iloc[-1]["timestamp"] + pd.Timedelta(minutes=self.bar_size)
        htf = htf[(htf["timestamp"].map(counts) == self.htf_bar_size // self.bar_size) &
                  (htf["timestamp"] + pd.Timedelta(minutes=self.htf_bar_size) <= end)]
        return htf.reset_index(drop=True)

    # ── Signal → Order translation ─────────────────────────────────────────────

    def _translate(self, signal: dict) -> Optional[Order]:
        action = str(signal.get("action", "")).lower()
        qty = int(signal.get("quantity", getattr(self.gen, "contracts", 1)))

        if action in ("exit", "flat", "close"):
            return Order(side="flat", qty=qty, stop=0.0, target=0.0, reason=f"{self._sid}:exit")

        if action == "buy":
            side = "long"
        elif action == "sell":
            side = "short"
        else:
            return None

        stop = float(signal.get("stop_loss") or 0.0)
        target = float(signal.get("take_profit") or 0.0)
        return Order(side=side, qty=qty, stop=stop, target=target,
                     order_type="market", reason=f"{self._sid}:{action}")

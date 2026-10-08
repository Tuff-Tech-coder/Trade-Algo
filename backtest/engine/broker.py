"""
SimBroker: simulated order matching with slippage and commission.

Fills are market-on-next-bar with N-tick slippage applied in the direction that
hurts the trader (conservative). Stops are checked before targets on the same
bar (worst-case). Supports market and stop-entry orders. The entry bar is not a
free pass — a freshly opened position's stop/target is checked on that same bar.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from .bar import Bar


@dataclass
class Position:
    side: str          # "long" | "short"
    qty: int
    entry_price: float
    instrument: str
    tick_value: float  # $ per tick
    tick_size: float
    stop: float = 0.0
    target: float = 0.0

    def unrealized_pnl(self, current_price: float) -> float:
        ticks = (current_price - self.entry_price) / self.tick_size
        if self.side == "short":
            ticks = -ticks
        return ticks * self.tick_value * self.qty

    def pnl_at(self, exit_price: float) -> float:
        return self.unrealized_pnl(exit_price)


@dataclass
class Fill:
    bar_ts: object
    side: str                # "long" | "short" | "flat"
    qty: int
    price: float
    commission: float
    slippage_ticks: int
    instrument: str
    reason: str = ""


@dataclass
class PendingOrder:
    side: str
    qty: int
    stop: float
    target: float
    reason: str
    order_type: str = "market"
    stop_trigger: Optional[float] = None


class SimBroker:
    """Single-instrument fill simulator.

    commission_per_rt : round-trip commission per contract (charged at close).
    slippage_ticks    : ticks applied at every fill (entry and exit).
    """

    def __init__(
        self,
        instrument: str,
        tick_size: float,
        tick_value: float,
        commission_per_rt: float = 1.40,
        slippage_ticks: int = 1,
    ) -> None:
        self.instrument = instrument
        self.tick_size = tick_size
        self.tick_value = tick_value
        self.commission_per_rt = commission_per_rt
        self.slippage_ticks = slippage_ticks

        self.position: Optional[Position] = None
        self._pending: Optional[PendingOrder] = None
        self.fills: list[Fill] = []
        self.last_worst_pnl = 0.0

    # ------------------------------------------------------------------
    # Order management
    # ------------------------------------------------------------------

    def submit_order(self, order) -> None:
        """Accept an Order (duck-typed) from the adapter; queue for next-bar fill."""
        if order.side == "flat":
            self._pending = PendingOrder(side="flat", qty=0, stop=0.0, target=0.0, reason=order.reason)
        else:
            self._pending = PendingOrder(
                side=order.side, qty=order.qty, stop=order.stop, target=order.target,
                reason=order.reason, order_type=getattr(order, "order_type", "market"),
                stop_trigger=getattr(order, "stop_trigger", None),
            )

    def has_pending(self) -> bool:
        return self._pending is not None

    def cancel_pending(self) -> None:
        self._pending = None

    def flatten_now(self, bar: Bar, reason: str = "forced_flat") -> Optional[float]:
        """Immediately flatten any open position at bar close. Returns realized pnl."""
        if self.position is None:
            self._pending = None
            return None
        return self._execute_close(bar.close, bar.timestamp, reason)

    # ------------------------------------------------------------------
    # Per-bar processing
    # ------------------------------------------------------------------

    def on_bar(self, bar: Bar, cap_budget: Optional[float] = None) -> list[tuple[str, float]]:
        """Process the bar. Returns list of (event, pnl) tuples.

        event ∈ {"entry", "exit", "stop_hit", "target_hit", "daily_cap_hit"}.
        cap_budget (optional) lets a caller flatten at a profit-cap price; left
        None here for a personal account (no daily profit cap).
        """
        events: list[tuple[str, float]] = []
        self.last_worst_pnl = self._bounded_adverse_pnl(bar) if self.position else 0.0

        if self.position is not None:
            result = self._check_exit_conditions(bar, cap_budget)
            if result is not None:
                events.append(result)

        if self._pending is not None and self.position is None:
            result = self._fill_pending(bar)
            if result is not None:
                events.append(result)
                if self.position is not None:
                    self.last_worst_pnl = self._bounded_adverse_pnl(bar)
                    exit_result = self._check_exit_conditions(bar, cap_budget)
                    if exit_result is not None:
                        events.append(exit_result)

        return events

    def unrealized_pnl_for_bar(self, bar: Bar) -> float:
        if self.position is None:
            return 0.0
        return self.position.unrealized_pnl(bar.close)

    def unrealized_pnl_worst_case(self, bar: Bar) -> float:
        if self.position is None:
            return 0.0
        if self.position.side == "long":
            return self.position.unrealized_pnl(bar.low)
        return self.position.unrealized_pnl(bar.high)

    # ------------------------------------------------------------------
    # Internal mechanics
    # ------------------------------------------------------------------

    def _bounded_adverse_pnl(self, bar):
        pos = self.position
        if pos.side == 'long':
            price = max(bar.low, min(bar.open, pos.stop)) if pos.stop > 0 else bar.low
            price -= self.slippage_ticks * self.tick_size
        else:
            price = min(bar.high, max(bar.open, pos.stop)) if pos.stop > 0 else bar.high
            price += self.slippage_ticks * self.tick_size
        return pos.unrealized_pnl(price)

    def _check_exit_conditions(self, bar: Bar, cap_budget: Optional[float] = None) -> Optional[tuple[str, float]]:
        pos = self.position
        assert pos is not None

        if pos.side == "long":
            if cap_budget is not None and (self.tick_value * pos.qty) > 0:
                cap_ticks = cap_budget / (self.tick_value * pos.qty)
                cap_price: float = pos.entry_price + cap_ticks * self.tick_size
            else:
                cap_price = float("inf")
            if bar.low <= pos.stop:
                return ("stop_hit", self._execute_close(min(bar.open, pos.stop), bar.timestamp, "stop_hit"))
            if cap_price < pos.target and bar.high >= cap_price:
                return ("daily_cap_hit", self._execute_close(cap_price, bar.timestamp, "daily_cap_hit"))
            if pos.target > 0 and bar.high >= pos.target:
                return ("target_hit", self._execute_close(pos.target, bar.timestamp, "target_hit"))
        else:  # short
            if cap_budget is not None and (self.tick_value * pos.qty) > 0:
                cap_ticks = cap_budget / (self.tick_value * pos.qty)
                cap_price = pos.entry_price - cap_ticks * self.tick_size
            else:
                cap_price = float("-inf")
            if bar.high >= pos.stop:
                return ("stop_hit", self._execute_close(max(bar.open, pos.stop), bar.timestamp, "stop_hit"))
            if cap_price > pos.target and bar.low <= cap_price:
                return ("daily_cap_hit", self._execute_close(cap_price, bar.timestamp, "daily_cap_hit"))
            if pos.target > 0 and bar.low <= pos.target:
                return ("target_hit", self._execute_close(pos.target, bar.timestamp, "target_hit"))
        return None

    def _fill_pending(self, bar: Bar) -> Optional[tuple[str, float]]:
        pending = self._pending
        assert pending is not None
        self._pending = None

        if pending.side == "flat":
            return None

        if pending.order_type == "stop" and pending.stop_trigger is not None:
            triggered = (
                (pending.side == "long" and bar.high >= pending.stop_trigger)
                or (pending.side == "short" and bar.low <= pending.stop_trigger)
            )
            if not triggered:
                self._pending = pending  # may trigger on a later bar
                return None

        slip = self.slippage_ticks * self.tick_size
        if pending.order_type == "stop" and pending.stop_trigger is not None:
            if pending.side == "long":
                fill_price = max(bar.open, pending.stop_trigger) + slip
            else:
                fill_price = min(bar.open, pending.stop_trigger) - slip
        elif pending.side == "long":
            fill_price = bar.open + slip
        else:
            fill_price = bar.open - slip

        self.position = Position(
            side=pending.side, qty=pending.qty, entry_price=fill_price,
            instrument=self.instrument, tick_value=self.tick_value,
            tick_size=self.tick_size, stop=pending.stop, target=pending.target,
        )
        self.fills.append(Fill(
            bar_ts=bar.timestamp, side=pending.side, qty=pending.qty, price=fill_price,
            commission=0.0, slippage_ticks=self.slippage_ticks,
            instrument=self.instrument, reason=pending.reason,
        ))
        return ("entry", 0.0)

    def _execute_close(self, raw_price: float, ts: object, reason: str) -> float:
        pos = self.position
        assert pos is not None
        slip = self.slippage_ticks * self.tick_size
        exit_price = raw_price - slip if pos.side == "long" else raw_price + slip

        gross_pnl = pos.pnl_at(exit_price)
        commission = self.commission_per_rt * pos.qty
        net_pnl = gross_pnl - commission

        self.fills.append(Fill(
            bar_ts=ts, side="flat", qty=pos.qty, price=exit_price, commission=commission,
            slippage_ticks=self.slippage_ticks, instrument=self.instrument, reason=reason,
        ))
        self.position = None
        return net_pnl

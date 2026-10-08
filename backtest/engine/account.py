"""
Plain trading account for the backtest engine.

This replaces the prop-firm eval model (MFFUAccount) entirely. There is no
"eval pass/fail", profit target, or consistency rule. It simply tracks equity
and P&L, and OPTIONALLY enforces the same self-imposed guards as the live risk
server:
  - max_drawdown : halt new entries once equity falls this far below the start.
  - daily_loss_kill : gate new entries for the session after this realized loss.
Both default to 0 (disabled) so a backtest measures the strategy's RAW edge;
enable them to study the effect of your live risk limits.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import Optional


@dataclass
class DayRecord:
    trading_date: date
    realized_pnl: float = 0.0
    trade_count: int = 0


@dataclass
class AccountState:
    """Snapshot passed to the strategy each bar (unused by SignalGenerator
    strategies, which decide off price data, but provided for completeness)."""
    equity: float
    session_realized_pnl: float
    session_trade_count: int
    total_realized_pnl: float
    days_traded: int
    halted: bool
    daily_loss_hit: bool


class Account:
    def __init__(
        self,
        starting_equity: float = 50_000.0,
        max_drawdown: float = 0.0,
        daily_loss_kill: float = 0.0,
    ) -> None:
        self.starting_equity = starting_equity
        self.max_drawdown = max_drawdown
        self.daily_loss_kill = daily_loss_kill
        self.floor: Optional[float] = (starting_equity - max_drawdown) if max_drawdown > 0 else None

        self._cash_equity = starting_equity
        self._unrealized = 0.0
        self._session_realized = 0.0
        self._session_has_entries = False
        self._daily_loss_hit = False
        self.halted = False

        self._day_records: list[DayRecord] = []
        self._current_day: Optional[DayRecord] = None

    # ── Session lifecycle ─────────────────────────────────────────────────────

    def start_session(self, session_date: date, record=True) -> None:
        self._record_session = record
        self._session_realized = 0.0
        self._unrealized = 0.0
        self._session_has_entries = False
        self._daily_loss_hit = False
        self._current_day = DayRecord(trading_date=session_date)

    def end_session(self) -> None:
        if self._current_day is not None and self._record_session:
            self._current_day.realized_pnl = self._session_realized
            self._day_records.append(self._current_day)
        self._current_day = None
        self._unrealized = 0.0

    # ── P&L ───────────────────────────────────────────────────────────────────

    def realize_pnl(self, pnl: float) -> None:
        self._session_realized += pnl
        self._cash_equity += pnl
        if self._current_day is not None:
            self._current_day.realized_pnl += pnl
            self._current_day.trade_count += 1
        self._session_has_entries = True
        if self.daily_loss_kill > 0 and self._session_realized <= -self.daily_loss_kill:
            self._daily_loss_hit = True

    def mark_to_market(self, unrealized_pnl: float) -> None:
        self._unrealized = unrealized_pnl

    def check_floor(self, worst_case_equity: float) -> None:
        """Halt new entries if a configured drawdown floor is breached."""
        if self.floor is not None and worst_case_equity <= self.floor:
            self.halted = True

    # ── Queries ────────────────────────────────────────────────────────────────

    @property
    def equity(self) -> float:
        return self._cash_equity + self._unrealized

    @property
    def cash_equity(self) -> float:
        return self._cash_equity

    @property
    def total_realized_pnl(self) -> float:
        return self._cash_equity - self.starting_equity

    @property
    def days_traded(self) -> int:
        return sum(rec.trade_count > 0 for rec in self._day_records)

    @property
    def daily_loss_hit(self) -> bool:
        return self._daily_loss_hit

    def can_enter(self) -> bool:
        return not self.halted and not self._daily_loss_hit

    def get_day_records(self) -> list[DayRecord]:
        return list(self._day_records)

    def get_state(self) -> AccountState:
        return AccountState(
            equity=self.equity,
            session_realized_pnl=self._session_realized,
            session_trade_count=self._current_day.trade_count if self._current_day else 0,
            total_realized_pnl=self.total_realized_pnl,
            days_traded=self.days_traded,
            halted=self.halted,
            daily_loss_hit=self._daily_loss_hit,
        )

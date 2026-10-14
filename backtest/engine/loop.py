"""
BacktestEngine: the main event loop.

Iterates 1-minute bars chronologically and drives the broker + strategy. There
is no early pass/fail termination — it runs the full period and records
performance. Optional self-imposed guards (drawdown floor,
daily-loss kill) live on the Account and gate entries when enabled.

Per-bar order of operations:
  1. session transition / open (by ET date)
  2. broker stop/target checks on the open position
  3. worst-case equity → equity curve + max drawdown + drawdown-floor check
  4. mark-to-market
  5. end-of-RTH forced flat
  6. strategy.on_bar → submit/flatten (subject to can_enter + news blackout)
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, time
from typing import Optional

import pandas as pd
import pytz

from .account import Account, DayRecord
from .bar import Bar
from .broker import SimBroker
from .constraints import ContractLimit, NewsBlackout

ET = pytz.timezone("America/New_York")
RTH_OPEN = time(9, 30)
RTH_CLOSE = time(16, 0)


@dataclass
class RunResult:
    starting_equity: float
    total_pnl: float
    peak_equity: float
    max_drawdown_dollar: float
    days_traded: int
    trade_pnls: list[float]
    equity_curve: list[tuple[datetime, float]]
    daily_pnl: list[tuple[date, float]]
    day_records: list[DayRecord]
    fills: list = field(default_factory=list)


class BacktestEngine:
    def __init__(
        self,
        bars: pd.DataFrame,
        strategy,
        account: Account,
        broker: SimBroker,
        news_blackout: Optional[NewsBlackout] = None,
        contract_limit: Optional[ContractLimit] = None,
        rth_only: bool = True,
        eod_flat_time: time = time(15, 54),
        trade_start: Optional[date] = None,
    ) -> None:
        self.bars = bars.reset_index(drop=True)
        self.strategy = strategy
        self.account = account
        self.broker = broker
        self.news_blackout = news_blackout
        self.contract_limit = contract_limit
        self.rth_only = rth_only
        self.eod_flat_time = eod_flat_time
        # Bars before trade_start still flow (warming indicators), but no new
        # entries are taken until this ET date — used for sub-period testing.
        self.trade_start = trade_start

    def run(self) -> RunResult:
        acct, broker, strat = self.account, self.broker, self.strategy
        peak_equity = acct.equity
        max_dd = 0.0
        trade_pnls: list[float] = []
        equity_curve: list[tuple[datetime, float]] = []

        cur_date: Optional[date] = None
        session_open = False
        last_bar: Optional[Bar] = None
        last_rth_bar: Optional[Bar] = None

        def _flatten(bar: Bar, reason: str) -> None:
            pnl = broker.flatten_now(bar, reason)
            if pnl is not None:
                acct.realize_pnl(pnl)
                trade_pnls.append(pnl)
                acct.mark_to_market(0.0)

        for r in self.bars.itertuples(index=False):
            ts = r.timestamp
            bar = Bar(ts, float(r.open), float(r.high), float(r.low),
                      float(r.close), float(r.volume), broker.instrument)
            last_bar = bar
            bar_et = ts.astimezone(ET)
            bar_time, bar_date = bar_et.time(), bar_et.date()

            # 1. Session transition
            if bar_date != cur_date:
                if cur_date is not None and session_open:
                    _flatten(last_rth_bar or bar, "session_end")
                    acct.end_session()
                    strat.on_session_end(cur_date)
                cur_date = bar_date
                session_open = False
            if not session_open and (not self.rth_only or RTH_OPEN <= bar_time < RTH_CLOSE):
                session_open = True
                acct.start_session(bar_date, record=self.trade_start is None or bar_date >= self.trade_start)
                strat.on_session_start(bar_date)

            if self.rth_only and (bar_time < RTH_OPEN or bar_time >= RTH_CLOSE):
                if hasattr(strat, 'on_history_bar'):
                    strat.on_history_bar(bar)
                continue
            last_rth_bar = bar
            entry_allowed_now = (acct.can_enter() and (self.trade_start is None or bar_date >= self.trade_start)
                                 and (self.news_blackout is None or not self.news_blackout.is_blocked(ts))
                                 and (not self.rth_only or bar_time < self.eod_flat_time))
            if not entry_allowed_now:
                broker.cancel_pending()

            # 2. Broker stop/target on the position that was open at bar start
            pos_before = broker.position
            cash_before = acct.cash_equity
            for ev, pnl in broker.on_bar(bar):
                if ev in ("stop_hit", "target_hit", "exit", "daily_cap_hit"):
                    acct.realize_pnl(pnl)
                    trade_pnls.append(pnl)

            # 3. Worst-case equity (using the pre-bar position for accurate DD)
            worst_eq = min(cash_before + broker.last_worst_pnl, acct.cash_equity)
            equity_curve.append((ts, acct.cash_equity + broker.unrealized_pnl_for_bar(bar)))
            if peak_equity - worst_eq > max_dd:
                max_dd = peak_equity - worst_eq

            acct.check_floor(worst_eq)
            if acct.halted and broker.position is not None:
                _flatten(bar, "drawdown_halt")

            # 4. Mark to market (close-based)
            acct.mark_to_market(broker.unrealized_pnl_for_bar(bar))
            peak_equity = max(peak_equity, acct.equity)

            # 5. End-of-RTH forced flat
            if self.rth_only and bar_time >= self.eod_flat_time and broker.position is not None:
                _flatten(bar, "eod_flat")
                acct.mark_to_market(0.0)

            # 6. Strategy decision
            can_enter = (
                acct.can_enter()
                and broker.position is None
                and (self.trade_start is None or bar_date >= self.trade_start)
                and (self.news_blackout is None or not self.news_blackout.is_blocked(ts + pd.Timedelta(minutes=1)))
                and (not self.rth_only or (RTH_OPEN <= bar_time < self.eod_flat_time))
            )
            order = strat.on_bar(bar, acct.get_state())
            if order is not None:
                if order.side == "flat" and broker.position is not None:
                    _flatten(bar, order.reason)
                    if hasattr(strat, 'on_order_result'):
                        strat.on_order_result(order, True)
                elif order.side != "flat" and can_enter:
                    qty = self.contract_limit.clamp(order.qty) if self.contract_limit else order.qty
                    # Require a real bracket (stop & target) to avoid a 0-priced insta-exit.
                    if qty > 0 and order.stop > 0 and order.target > 0:
                        order.qty = qty
                        broker.submit_order(order)
                        if hasattr(strat, 'on_order_result'):
                            strat.on_order_result(order, True)

        # Final session close
        if cur_date is not None and session_open:
            if last_bar is not None:
                _flatten(last_rth_bar or last_bar, "end_of_data")
                if equity_curve:
                    equity_curve[-1] = (equity_curve[-1][0], acct.cash_equity)
            acct.end_session()
            strat.on_session_end(cur_date)

        daily_pnl = [(rec.trading_date, rec.realized_pnl) for rec in acct.get_day_records()]
        return RunResult(
            starting_equity=acct.starting_equity,
            total_pnl=acct.total_realized_pnl,
            peak_equity=peak_equity,
            max_drawdown_dollar=max_dd,
            days_traded=acct.days_traded,
            trade_pnls=trade_pnls,
            equity_curve=equity_curve,
            daily_pnl=daily_pnl,
            day_records=acct.get_day_records(),
            fills=list(broker.fills),
        )

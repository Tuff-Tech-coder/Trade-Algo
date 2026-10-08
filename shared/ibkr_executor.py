"""
IBKR Live Algo — Order Execution Module.

Places orders directly through Interactive Brokers (ib_insync), removing the
need for a third-party order router (e.g. TradersPost) or any second broker
account. The same TWS / IB Gateway you connect to for market data also executes
here — just over a separate API client id.

Responsibilities
----------------
  * Maintain an authenticated IB connection (its own clientId, distinct from
    the bar feed's, so both can connect to the same TWS at once).
  * Resolve the futures contract for an incoming signal.
  * Submit bracket orders: entry (market or limit) + stop-loss + take-profit,
    grouped so the stop and target cancel each other (OCA).
  * Flatten / close a position on an exit signal.
  * Track fills, order status (incl. rejections), positions, and daily P&L from
    live IBKR events so the risk server's state stays accurate — and so the
    daily-loss kill switch actually fires off real broker P&L.

Fill / position tracking (hardened)
-----------------------------------
Rather than guessing positions from submitted orders, the executor subscribes
to IBKR's live event streams and treats the broker as the source of truth:
  * execDetailsEvent / commissionReportEvent → fill records + realized P&L
  * orderStatusEvent / errorEvent            → order lifecycle + rejections
  * positionEvent                            → authoritative open positions
  * pnlEvent (reqPnL)                         → daily / realized / unrealized P&L
`reconcile()` re-pulls positions on demand (and after a reconnect) to true up.

Expected signal schema (produced by a strategy's check_signal)
--------------------------------------------------------------
    {
        "ticker":       "MNQM6",      # full front-month contract symbol
        "action":       "buy" | "sell" | "exit",
        "quantity":     2,
        "order_type":   "market" | "limit",   # default "market"
        "limit_price":  21050.25,     # required only for limit entries
        "stop_loss":    20980.00,     # absolute price (optional but recommended)
        "take_profit":  21190.00,     # absolute price (optional but recommended)
        "extras":       { ... }
    }
The strategy owns its risk math (turning risk_usd / tp_multiplier / tick_size
into absolute stop and target prices). This module places what it is given. If
neither stop_loss nor take_profit is present, a bare entry is placed and a
warning is logged.

Safety
------
  * PAPER mode REFUSES to connect to a live-trading port (7496 / 4001).
  * Reference ports — TWS: 7496 live / 7497 paper. Gateway: 4001 live / 4002 paper.

VALIDATE ON A PAPER ACCOUNT before any live use.
"""
from __future__ import annotations

import asyncio
import time
from zoneinfo import ZoneInfo
import calendar
import datetime
import logging
import math
import os
import re
from collections import deque
from typing import Optional

try:
    from ib_insync import IB, Contract, ExecutionFilter, Future, LimitOrder, MarketOrder, StopOrder
    _IB_AVAILABLE = True
except ImportError:  # ib_insync is only required when order_router=ibkr
    _IB_AVAILABLE = False

log = logging.getLogger("ibkr_executor")

# Base symbol → exchange for the micro futures this project trades.
_EXCHANGE_BY_BASE: dict[str, str] = {
    "MNQ": "CME", "MES": "CME", "M2K": "CME", "MYM": "CBOT",
    "MGC": "COMEX", "MCL": "NYMEX",
}

# Ports that correspond to LIVE (real-money) vs PAPER trading.
_LIVE_PORTS = {7496, 4001}
_PAPER_PORTS = {7497, 4002}

# Valid CME/COMEX/NYMEX futures month codes.
_MONTH_CODES = "FGHJKMNQUVXZ"

# IBKR error codes that indicate an order was rejected/cancelled by the system.
_ORDER_REJECT_CODES = {201, 202, 203, 321, 399, 434, 10147, 10148}


def _base_symbol(ticker: str) -> str:
    """MNQM6 -> MNQ, MESU5 -> MES. Strips a trailing month-code + year digits."""
    return re.sub(rf"[{_MONTH_CODES}]\d{{1,2}}$", "", ticker.upper())


def _as_float(v) -> Optional[float]:
    return None if v is None else float(v)


def _num(v) -> Optional[float]:
    """Coerce to float, treating None and NaN (IBKR's 'unset') as None."""
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return f if math.isfinite(f) and abs(f) < 1e100 else None


class IBKRExecutor:
    """Submits orders to Interactive Brokers and tracks fills/positions/P&L."""

    def __init__(
        self,
        mode: str = "PAPER",
        host: Optional[str] = None,
        port: Optional[int] = None,
        client_id: Optional[int] = None,
        max_order_quantity: int = 0,
    ) -> None:
        if not _IB_AVAILABLE:
            raise RuntimeError(
                "ib_insync is not installed but order_router=ibkr. Install it "
                "(it is in shared/requirements.txt) or set order_router: "
                "traderspost in account_config.yaml."
            )
        self.mode = mode.upper()
        if self.mode not in ('PAPER', 'LIVE'):
            raise ValueError('mode must be PAPER or LIVE')
        self.host = host or os.environ.get("TWS_HOST", "127.0.0.1")
        self.port = int(port or os.environ.get("TWS_PORT", "7497"))
        # Execution uses a client id distinct from the bar feed (which defaults
        # to 1) so both can be connected to the same TWS simultaneously.
        self.client_id = int(client_id or os.environ.get("TWS_EXEC_CLIENT_ID", "2"))
        # Hard ceiling on a single order's quantity (0 = no cap). Last line of
        # defense against a buggy strategy emitting a runaway size.
        self.max_order_quantity = int(max_order_quantity or 0)
        self.ib = IB()
        self._contracts: dict[str, "Contract"] = {}

        # ── Tracking state (kept fresh by event handlers) ───────────────────
        self._account: Optional[str] = None
        self._pnl = None                              # PnL subscription object
        self.day_pnl: Optional[float] = None          # mark-to-market day P&L
        self.day_realized_pnl_val: Optional[float] = None  # realized P&L today
        self.day_unrealized_pnl: Optional[float] = None
        self.recent_fills: deque = deque(maxlen=200)
        self.order_status: dict[int, str] = {}        # orderId -> last status
        self.last_reject: Optional[dict] = None
        self._handlers_bound = False
        self._positions_ready = False
        self._executions_ready = False
        self._balance_updated = 0.0
        self._pnl_updated = 0.0
        self._order_lock = asyncio.Lock()
        self._closing = {}
        self._requested_account = os.environ.get('IBKR_ACCOUNT_ID')


    # ── Connection ──────────────────────────────────────────────────────────

    async def connect(self) -> None:
        """Connect to TWS / IB Gateway and subscribe to tracking events."""
        if self.mode == "PAPER" and self.port in _LIVE_PORTS:
            raise RuntimeError(
                f"Refusing to connect: mode=PAPER but port {self.port} is a LIVE "
                f"trading port. Use 7497 (TWS paper) or 4002 (Gateway paper)."
            )
        if self.mode == "LIVE" and self.port in _PAPER_PORTS:
            log.warning(
                "mode=LIVE but port %s is a PAPER port — orders will NOT reach a "
                "live account. Live TWS is 7496, live Gateway is 4001.",
                self.port,
            )
        await self.ib.connectAsync(self.host, self.port, clientId=self.client_id)
        accounts = self.ib.managedAccounts()
        if self._requested_account:
            if self._requested_account not in accounts:
                raise RuntimeError('IBKR_ACCOUNT_ID is not a managed account')
            self._account = self._requested_account
        elif len(accounts) == 1:
            self._account = accounts[0]
        else:
            raise RuntimeError('Set IBKR_ACCOUNT_ID when multiple accounts are connected')
        if self.mode == 'PAPER' and not self._account.startswith('DU'):
            raise RuntimeError('PAPER mode requires a verified DU paper account')
        self._bind_handlers()
        await self._subscribe_state()
        if self.account_balance() is not None:
            self._balance_updated = time.monotonic()
        log.info(
            "ibkr_executor connected  host=%s  port=%s  client_id=%s  mode=%s  account=%s",
            self.host, self.port, self.client_id, self.mode, self._account,
        )

    def _bind_handlers(self) -> None:
        if self._handlers_bound:
            return
        self.ib.execDetailsEvent += self._on_exec
        self.ib.commissionReportEvent += self._on_commission
        self.ib.orderStatusEvent += self._on_order_status
        self.ib.positionEvent += self._on_position
        self.ib.errorEvent += self._on_error
        self.ib.disconnectedEvent += self._on_disconnected
        self.ib.accountValueEvent += self._on_account_value
        self._handlers_bound = True

    async def _subscribe_state(self) -> None:
        """Seed positions and subscribe to account P&L."""
        await self.ib.reqPositionsAsync()
        self._positions_ready = True
        await self.ib.reqExecutionsAsync(ExecutionFilter(acctCode=self._account))
        self._executions_ready = True
        self._pnl = self.ib.reqPnL(self._account)
        self.ib.pnlEvent += self._on_pnl

    def _on_account_value(self, value):
        if value.account == self._account and value.tag == 'NetLiquidation' and _num(value.value) is not None:
            self._balance_updated = time.monotonic()

    def state_ready(self, require_pnl=True):
        now = time.monotonic()
        return (self.is_connected() and self._positions_ready and self._executions_ready
                and self.account_balance() is not None and now - self._balance_updated <= 120
                and (not require_pnl or (self.day_realized_pnl_val is not None and now - self._pnl_updated <= 120)))

    def _account_position(self, position):
        return position.account == self._account and position.contract.secType == 'FUT'

    def _account_trade(self, trade):
        return trade.order.account == self._account and trade.contract.secType == 'FUT'

    def strategy_state(self, base):
        today = datetime.datetime.now(ZoneInfo('America/New_York')).date()
        positions = [p for p in self.ib.positions() if self._account_position(p) and _base_symbol(self._sym(p.contract)) == base]
        trades = [t for t in self.ib.openTrades() if self._account_trade(t) and _base_symbol(self._sym(t.contract)) == base and not t.isDone()]
        fills = [f for f in self.ib.fills() if getattr(f.execution, 'acctNumber', None) == self._account
                 and _base_symbol(self._sym(f.contract)) == base
                 and f.execution.time.astimezone(ZoneInfo('America/New_York')).date() == today]
        return {'ready': self.state_ready(), 'position_qty': sum(int(p.position) for p in positions),
                'ticker': self._sym(positions[0].contract) if positions else None,
                'traded_today': bool(fills),
                'entry_pending': any(not t.order.parentId and not str(t.order.orderRef).startswith('close:') for t in trades),
                'exit_pending': any(str(t.order.orderRef).startswith('close:') for t in trades)}


    async def disconnect(self) -> None:
        if self.ib.isConnected():
            self.ib.disconnect()
            log.info("ibkr_executor disconnected")

    def is_connected(self) -> bool:
        return self.ib.isConnected()

    async def reconcile(self) -> None:
        """Re-pull positions from IBKR to true up local state (use after a gap)."""
        if not self.ib.isConnected():
            log.warning("reconcile_skipped — not connected")
            return
        self._positions_ready = False
        await self.ib.reqPositionsAsync()
        self._positions_ready = True
        log.info("reconciled open_contracts=%s positions=%s",
                 self.open_contracts(), self._positions_summary())

    # ── Event handlers ────────────────────────────────────────────────────────

    def _on_exec(self, trade, fill) -> None:
        try:
            ex = fill.execution
            rec = {
                "time": str(getattr(ex, "time", "")),
                "symbol": self._sym(fill.contract),
                "side": getattr(ex, "side", ""),      # BOT / SLD
                "qty": float(getattr(ex, "shares", 0)),
                "price": float(getattr(ex, "price", 0)),
                "order_id": getattr(trade.order, "orderId", None),
                "exec_id": getattr(ex, "execId", ""),
            }
            self.recent_fills.append(rec)
            log.info("fill symbol=%s side=%s qty=%s price=%s order_id=%s",
                     rec["symbol"], rec["side"], rec["qty"], rec["price"], rec["order_id"])
        except Exception as exc:
            log.warning("on_exec_failed err=%s", exc)

    def _on_commission(self, trade, fill, report) -> None:
        rp = _num(getattr(report, "realizedPNL", None))
        if rp is not None:
            log.info("commission_report symbol=%s realized_pnl=%s commission=%s",
                     self._sym(fill.contract), rp, getattr(report, "commission", None))

    def _on_order_status(self, trade) -> None:
        oid = getattr(trade.order, "orderId", None)
        status = trade.orderStatus.status
        self.order_status[oid] = status
        if status in ("Filled",):
            log.info("order_filled order_id=%s filled=%s avg_price=%s",
                     oid, trade.orderStatus.filled, trade.orderStatus.avgFillPrice)
        elif status in ("Cancelled", "ApiCancelled", "Inactive"):
            log.warning("order_not_working order_id=%s status=%s", oid, status)

    def _on_position(self, position) -> None:
        log.info("position_update symbol=%s qty=%s avg_cost=%s",
                 self._sym(position.contract), position.position, position.avgCost)

    def _on_pnl(self, pnl) -> None:
        if getattr(pnl, 'account', self._account) != self._account:
            return
        d = _num(getattr(pnl, "dailyPnL", None))
        r = _num(getattr(pnl, "realizedPnL", None))
        u = _num(getattr(pnl, "unrealizedPnL", None))
        if d is not None:
            self.day_pnl = d
        self.day_realized_pnl_val = r
        self._pnl_updated = time.monotonic() if r is not None else 0
        if u is not None:
            self.day_unrealized_pnl = u

    def _on_error(self, req_id, code, msg, contract) -> None:
        if code in _ORDER_REJECT_CODES:
            self.last_reject = {"code": code, "msg": msg, "req_id": req_id}
            log.error("order_rejected code=%s req_id=%s msg=%s", code, req_id, msg)
        elif code in (1100, 1300, 2110):  # connectivity-affecting
            log.warning("ibkr_connectivity code=%s msg=%s", code, msg)

    def _on_disconnected(self) -> None:
        self._positions_ready = False
        self._executions_ready = False
        self._pnl_updated = 0
        log.error(
            "ibkr_executor DISCONNECTED — orders cannot be placed until "
            "reconnected. Restart the risk server or call connect()/reconcile()."
        )

    # ── Contract resolution ───────────────────────────────────────────────────

    async def _resolve(self, ticker: str) -> "Contract":
        ticker = ticker.upper()
        if ticker in self._contracts:
            return self._contracts[ticker]

        base = _base_symbol(ticker)
        exch = _EXCHANGE_BY_BASE.get(base)
        if not exch:
            raise ValueError(
                f"Unknown exchange for symbol base '{base}' (ticker={ticker}). "
                f"Add it to _EXCHANGE_BY_BASE in ibkr_executor.py."
            )

        candidate = Future(localSymbol=ticker, exchange=exch, currency="USD")
        qualified = await self.ib.qualifyContractsAsync(candidate)
        if not qualified:
            raise ValueError(f'Cannot qualify {ticker}; restart feed to refresh its contract identity')
        contract = qualified[0]

        self._contracts[ticker] = contract
        log.info("contract_resolved ticker=%s con_id=%s", ticker, contract.conId)
        return contract

    async def _front_month(self, base: str, exch: str) -> "Contract":
        template = Future(symbol=base, exchange=exch, currency="USD")
        details = await self.ib.reqContractDetailsAsync(template)
        if not details:
            raise ValueError(f"No contracts found for {base} on {exch}")

        today = datetime.date.today()
        valid: list[tuple[datetime.date, "Contract"]] = []
        for d in details:
            raw = d.contract.lastTradeDateOrContractMonth
            try:
                exp = datetime.datetime.strptime((raw[:6] + str(calendar.monthrange(int(raw[:4]), int(raw[4:6]))[1])) if len(raw) == 6 else raw[:8], "%Y%m%d").date()
                if exp >= today:
                    valid.append((exp, d.contract))
            except ValueError:
                continue
        if not valid:
            raise ValueError(f"No non-expired contracts for {base}")
        valid.sort(key=lambda x: x[0])
        return valid[0][1]

    # ── Order construction ────────────────────────────────────────────────────

    def _build_bracket(
        self,
        action: str,
        qty: int,
        order_type: str,
        limit_price: Optional[float],
        stop_loss: Optional[float],
        take_profit: Optional[float],
    ) -> list:
        """Return [parent, *children] orders forming a bracket.

        The parent is the entry; the take-profit (limit) and stop-loss (stop)
        children are OCA siblings so a fill on one cancels the other. Only the
        last order transmits, submitting the group atomically.
        """
        action = action.upper()                       # BUY | SELL
        opp = "SELL" if action == "BUY" else "BUY"
        oid = [self.ib.client.getReqId() for _ in range(3)]
        oca = f"oca_{oid[0]}"

        if order_type == "limit":
            if limit_price is None:
                raise ValueError("limit order requires limit_price")
            parent = LimitOrder(action, qty, limit_price)
        else:
            parent = MarketOrder(action, qty)
        parent.orderId = oid[0]
        parent.transmit = False

        children = []
        if take_profit is not None:
            tp = LimitOrder(opp, qty, take_profit)
            tp.orderId, tp.parentId = oid[1], oid[0]
            tp.ocaGroup, tp.ocaType, tp.transmit = oca, 1, False
            children.append(tp)
        if stop_loss is not None:
            sl = StopOrder(opp, qty, stop_loss)
            sl.orderId, sl.parentId = oid[2], oid[0]
            sl.ocaGroup, sl.ocaType, sl.transmit = oca, 1, False
            children.append(sl)

        if children:
            children[-1].transmit = True
        else:
            parent.transmit = True
        for order in [parent] + children:
            order.account = self._account
        return [parent] + children

    # ── Public API ──────────────────────────────────────────────────────────

    async def submit(self, signal: dict) -> dict:
        async with self._order_lock:
            return await self._submit(signal)

    async def _submit(self, signal: dict) -> dict:
        """Place an order (or flatten) for an accepted signal."""
        if not self.ib.isConnected():
            return {"status": "error", "detail": "not connected to IBKR"}

        ticker = str(signal.get("ticker", "")).upper()
        if not ticker:
            return {"status": "error", "detail": "signal missing 'ticker'"}

        action = str(signal.get("action", "")).lower()
        signal_id = signal.get('extras', {}).get('signal_id')
        if action in ('buy', 'sell') and signal_id and (not isinstance(signal_id, str) or signal_id.startswith('close:')):
            return {'status': 'error', 'detail': 'invalid entry signal_id'}
        qty = int(signal.get("quantity", 1))
        if qty <= 0:
            return {"status": "error", "detail": f"non-positive quantity {qty}"}

        try:
            contract = await self._resolve(ticker)
        except Exception as exc:
            log.error("contract_resolution_failed ticker=%s err=%s", ticker, exc)
            return {"status": "error", "detail": f"resolve failed: {exc}"}

        if action in ("exit", "flat", "close"):
            return await self._flatten(contract)

        if action not in ("buy", "sell"):
            return {"status": "error", "detail": f"unknown action '{action}'"}

        if self.max_order_quantity and qty > self.max_order_quantity:
            log.error("order_qty_exceeds_cap ticker=%s qty=%s cap=%s",
                      ticker, qty, self.max_order_quantity)
            return {
                "status": "rejected",
                "detail": f"quantity {qty} exceeds max_order_quantity {self.max_order_quantity}",
            }

        order_type = str(signal.get("order_type", "market")).lower()
        if order_type not in ('market', 'limit'):
            return {'status': 'error', 'detail': 'unsupported order_type'}
        stop_loss = signal.get("stop_loss")
        take_profit = signal.get("take_profit")
        stop_loss, take_profit = _num(stop_loss), _num(take_profit)
        if stop_loss is None or take_profit is None or min(stop_loss, take_profit) <= 0:
            return {'status': 'error', 'detail': 'finite positive stop_loss and take_profit required'}
        if (action == 'buy' and stop_loss >= take_profit) or (action == 'sell' and stop_loss <= take_profit):
            return {'status': 'error', 'detail': 'invalid bracket direction'}
        if order_type == 'limit':
            limit = _num(signal.get('limit_price'))
            if limit is None or not min(stop_loss, take_profit) < limit < max(stop_loss, take_profit):
                return {'status': 'error', 'detail': 'limit price must be inside bracket'}
        trades = []
        try:
            orders = self._build_bracket(
                action, qty, order_type,
                _as_float(signal.get("limit_price")),
                _as_float(stop_loss),
                _as_float(take_profit),
            )
            for order in orders:
                order.orderRef = str(signal.get('extras', {}).get('signal_id', 'entry'))
                trades.append(self.ib.placeOrder(contract, order))
        except Exception as exc:
            for trade in trades:
                self.ib.cancelOrder(trade.order)
            self._positions_ready = False  # uncertain partial submission requires reconciliation
            log.error("order_submit_failed ticker=%s err=%s", ticker, exc)
            return {"status": "error", "detail": str(exc)}

        order_ids = [t.order.orderId for t in trades]
        for oid in order_ids:
            self.order_status.setdefault(oid, "Submitted")
        log.info(
            "order_submitted ticker=%s action=%s qty=%s type=%s sl=%s tp=%s ids=%s",
            ticker, action, qty, order_type, stop_loss, take_profit, order_ids,
        )
        return {
            "status": "submitted",
            "ticker": ticker,
            "action": action,
            "quantity": qty,
            "order_ids": order_ids,
        }

    async def flatten(self, contract: "Contract") -> dict:
        async with self._order_lock:
            return await self._flatten(contract)

    async def _flatten(self, contract: "Contract") -> dict:
        """Cancel resting orders for a contract, then market-close any position."""
        con_id = contract.conId
        existing = self._closing.get(con_id)
        if existing is not None and not existing.isDone():
            return {'status': 'exit_pending', 'order_id': existing.order.orderId}
        trades = [t for t in self.ib.openTrades() if self._account_trade(t) and t.contract.conId == con_id and not t.isDone()]
        for trade in trades:
            self.ib.cancelOrder(trade.order)
        deadline = time.monotonic() + 5
        while any(not t.isDone() for t in trades):
            if time.monotonic() >= deadline:
                return {'status': 'error', 'detail': 'waiting for broker cancellation confirmation'}
            await asyncio.sleep(.05)
        # Read positions after entry cancellations settle, including any racing fills.
        try:
            await self.ib.reqPositionsAsync()
        except Exception as exc:
            return {'status': 'error', 'detail': f'position reconciliation failed: {exc}'}
        pos = self._position_size(contract)
        if pos == 0:
            return {'status': 'flat', 'detail': 'no open position'}
        order = MarketOrder('SELL' if pos > 0 else 'BUY', abs(pos), account=self._account, orderRef=f'close:{con_id}')
        trade = self.ib.placeOrder(contract, order)
        self._closing[con_id] = trade
        return {'status': 'close_submitted', 'quantity': abs(pos), 'order_id': trade.order.orderId}

    # ── State read-back (broker = source of truth) ────────────────────────────

    def _sym(self, contract) -> str:
        return (getattr(contract, "localSymbol", None) or getattr(contract, "symbol", "") or "")

    def _position_size(self, contract: "Contract") -> int:
        con_id = getattr(contract, "conId", None)
        for p in self.ib.positions():
            if self._account_position(p) and getattr(p.contract, "conId", None) == con_id:
                return int(p.position)
        return 0

    def _positions_summary(self) -> dict:
        return {self._sym(p.contract): int(p.position)
                for p in self.ib.positions() if self._account_position(p) and p.position}

    def open_contracts(self) -> int:
        """Total open contracts across all positions (absolute, summed)."""
        held = sum(abs(int(p.position)) for p in self.ib.positions() if self._account_position(p))
        # Conservatively reserve FULL parent quantity until final order status.
        # Partial fills may be counted twice briefly; never undercount exposure.
        pending = sum(int(t.order.totalQuantity) for t in self.ib.openTrades()
                      if self._account_trade(t) and not t.isDone() and not t.order.parentId
                      and not str(t.order.orderRef).startswith('close:'))
        return int(held + pending)

    def account_balance(self) -> Optional[float]:
        """Net liquidation value of the account, or None if unavailable."""
        for v in self.ib.accountValues():
            if v.account == self._account and v.tag == "NetLiquidation" and v.currency in ("USD", "BASE"):
                return _num(v.value)
        return None

    def day_realized_pnl(self) -> Optional[float]:
        """Today's realized P&L from IBKR (drives the daily-loss kill switch)."""
        return self.day_realized_pnl_val

    def day_open_pnl(self) -> Optional[float]:
        """Today's unrealized (mark-to-market) P&L from IBKR."""
        return self.day_unrealized_pnl

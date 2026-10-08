"""
IBKR Live Algo — Risk Server (FastAPI).

Flow:  Python signal generator  →  POST /webhook/{strategy_id}
       → pre_trade_checks()
       → if ACCEPT: submit directly to IBKR (or optional TradersPost routing)
       → structured JSON log of every decision

Direct IBKR routing reads reconciled, fresh broker state. Optional TradersPost
routing requires manual /set_state synchronization of balances and positions.

All risk limits here are SELF-IMPOSED — they are your own safety rails, not
rules imposed by any third party. Any guard can be disabled in
account_config.yaml (set its value to 0, or news blackout enabled: false).

Authentication
--------------
All endpoints require the header:
    X-Api-Key: <RISK_SERVER_API_KEY>

Set RISK_SERVER_API_KEY in shared/.env (see shared/.env.example).
If the env var is unset the server will start but log a loud warning
and reject all requests — do not run without a key in production.

Start with:
    uvicorn risk_module:app --host 127.0.0.1 --port 8000

Local access only. Internet/reverse-proxy deployment is unsupported.
"""

import asyncio
import math
import re
import datetime
import hmac
import json
import logging
import os
import pathlib
import time
from dataclasses import dataclass, field
from typing import Optional

import httpx
import pytz
import structlog
import uvicorn
import yaml
from dotenv import load_dotenv
from fastapi import Depends, FastAPI, Header, HTTPException, Request
from security import valid_secret, matches_secret, read_body, secure_app, signal_summary, validate_router_url
from pydantic import BaseModel  # noqa: F401  (kept for downstream schema use)

# Order execution is optional: only needed when order_router == "ibkr".
# Imported lazily-safe so the server still runs with order_router == "traderspost"
# even if ib_insync is not installed.
try:
    from ibkr_executor import IBKRExecutor
except Exception:  # pragma: no cover
    IBKRExecutor = None

# ──────────────────────────────────────────────────────────────────────────────
# Logging setup
# ──────────────────────────────────────────────────────────────────────────────

structlog.configure(
    processors=[
        structlog.processors.TimeStamper(fmt="iso"),
        structlog.processors.JSONRenderer(),
    ]
)
log = structlog.get_logger()

# ──────────────────────────────────────────────────────────────────────────────
# Secrets — load .env if present (never required; env vars may be set externally)
# ──────────────────────────────────────────────────────────────────────────────

_ENV_FILE = pathlib.Path(__file__).parent / ".env"
if _ENV_FILE.exists():
    load_dotenv(_ENV_FILE)
    log.info("loaded_dotenv", path=str(_ENV_FILE))


def _tp_webhook(strategy_id: str) -> Optional[str]:
    """
    Return the order-routing (TradersPost) webhook URL for a strategy, or None.

    Expected env var name: TP_WEBHOOK_<STRATEGY_ID>
    Example: TP_WEBHOOK_MNQ_ORB_15M_5MCLOSE
    """
    return os.environ.get(f"TP_WEBHOOK_{strategy_id}") or None


# ── API key authentication ────────────────────────────────────────────────────

_RISK_API_KEY: Optional[str] = os.environ.get("RISK_SERVER_API_KEY") or None

if not valid_secret(_RISK_API_KEY):
    log.warning(
        "RISK_SERVER_API_KEY_not_set",
        detail="All requests will be rejected. Set RISK_SERVER_API_KEY in shared/.env.",
    )


async def verify_api_key(x_api_key: str = Header("", alias="X-Api-Key")) -> None:
    """
    FastAPI dependency — enforces X-Api-Key header on every endpoint.

    Raises 403 if the key is wrong or missing.
    Raises 503 if the server was started without RISK_SERVER_API_KEY set.
    """
    if not valid_secret(_RISK_API_KEY):
        raise HTTPException(
            status_code=503,
            detail="Server misconfigured: RISK_SERVER_API_KEY not set.",
        )
    # Constant-time comparison so a wrong key can't be recovered via timing.
    if not matches_secret(x_api_key, _RISK_API_KEY):
        raise HTTPException(status_code=403, detail="Invalid API key.")


_ADMIN_API_KEY = os.environ.get('RISK_ADMIN_API_KEY') or None

async def verify_admin_key(x_api_key: str = Header('', alias='X-Api-Key')):
    if not valid_secret(_ADMIN_API_KEY) or _ADMIN_API_KEY == _RISK_API_KEY:
        raise HTTPException(503, detail='Independent admin key not configured')
    if not matches_secret(x_api_key, _ADMIN_API_KEY):
        raise HTTPException(403, detail='Invalid admin key')

# ──────────────────────────────────────────────────────────────────────────────
# Config loading
# ──────────────────────────────────────────────────────────────────────────────

CONFIG_PATH = pathlib.Path(__file__).parent / "account_config.yaml"
CALENDAR_PATH = pathlib.Path(__file__).parent / "news_calendar.yaml"

with open(CONFIG_PATH) as f:
    CONFIG = yaml.safe_load(f)

with open(CALENDAR_PATH) as f:
    CALENDAR = yaml.safe_load(f)

MODE: str = CONFIG["mode"]            # PAPER | LIVE
ACCOUNT = CONFIG["account"]
LIMITS = CONFIG.get("risk_limits", {})
RISK_CFG = CONFIG["risk_server"]
STALE_THRESHOLD = RISK_CFG.get("stale_signal_threshold_seconds", 30)

# Where accepted orders go: "ibkr" (direct via ib_insync) or "traderspost".
ORDER_ROUTER: str = str(CONFIG.get("order_router", "ibkr")).lower()
if ORDER_ROUTER not in ('ibkr', 'traderspost'):
    raise ValueError('order_router must be ibkr or traderspost')

# Cap incoming request bodies — signals/state updates are tiny JSON objects.
_MAX_BODY_BYTES: int = 65536

# Hard ceiling on order size, passed to the executor (0 = no cap).
MAX_ORDER_QUANTITY: int = int(LIMITS.get("max_order_quantity", 0) or 0)

# Self-imposed risk limits (0 = disabled)
MAX_DRAWDOWN = float(LIMITS.get("max_drawdown", 0) or 0)
DRAWDOWN_TYPE = str(LIMITS.get("drawdown_type", "STATIC")).upper()
NEAR_FLOOR_BUFFER = float(LIMITS.get("near_floor_buffer", 0) or 0)
MAX_OPEN_CONTRACTS = int(LIMITS.get("max_open_contracts", 0) or 0)

ET = pytz.timezone("America/New_York")

# ──────────────────────────────────────────────────────────────────────────────
# Log file setup
# ──────────────────────────────────────────────────────────────────────────────

LOG_DIR = pathlib.Path(CONFIG["logging"]["log_dir"])
LOG_DIR.mkdir(parents=True, exist_ok=True)


def _write_decision_log(entry: dict) -> None:
    today = datetime.date.today().isoformat()
    log_file = LOG_DIR / f"decisions_{today}.jsonl"
    with open(log_file, "a") as f:
        f.write(json.dumps(entry) + "\n")


# ──────────────────────────────────────────────────────────────────────────────
# In-memory account state
# ──────────────────────────────────────────────────────────────────────────────

@dataclass
class AccountState:
    """
    Live snapshot of account metrics.

    All values are tracked in-memory and seeded from account_config.yaml on
    startup.  Use POST /set_state to sync after checking your broker / IBKR
    account.
    """
    balance: float = 50_000.0              # current account balance (EOD cash)
    drawdown_floor: Optional[float] = None # min balance before new entries are
                                           #   blocked. None = no drawdown guard.
    day_realized: float = 0.0              # today's closed P&L
    day_open_pnl: float = 0.0              # today's open P&L (mark-to-market)
    open_positions: int = 0                # total open contracts (all strategies)
    last_updated: float = field(default_factory=time.time)

    @property
    def net_liquidation(self) -> float:
        return self.balance if ORDER_ROUTER == "ibkr" else self.balance + self.day_open_pnl

    @property
    def buffer_to_floor(self) -> Optional[float]:
        """How far the balance can still drop before hitting the drawdown floor."""
        if self.drawdown_floor is None:
            return None
        return self.balance - self.drawdown_floor


def _build_initial_state() -> AccountState:
    """Seed account state from account_config.yaml."""
    balance = float(ACCOUNT["account_size"])
    floor = balance - MAX_DRAWDOWN if MAX_DRAWDOWN > 0 else None
    return AccountState(balance=balance, drawdown_floor=floor)


# ──────────────────────────────────────────────────────────────────────────────
# News blackout (optional, self-imposed)
# ──────────────────────────────────────────────────────────────────────────────

def _is_news_blackout(symbol: str) -> bool:
    """Return True if now falls within a blackout window for the given symbol."""
    rules = CALENDAR.get("blackout_rules", {})
    if not rules.get("enabled", False):
        return False

    pre = rules.get("pre_minutes", 0)
    post = rules.get("post_minutes", 0)
    now_et = datetime.datetime.now(ET).replace(tzinfo=None)

    for event in CALENDAR.get("upcoming_events", []):
        if symbol not in event.get("affects", []):
            continue
        ev_dt = datetime.datetime.strptime(event["datetime_et"], "%Y-%m-%dT%H:%M:%S")
        window_start = ev_dt - datetime.timedelta(minutes=pre)
        window_end = ev_dt + datetime.timedelta(minutes=post)
        if window_start <= now_et <= window_end:
            return True
    return False


# ──────────────────────────────────────────────────────────────────────────────
# Session window check
# ──────────────────────────────────────────────────────────────────────────────

def _within_session(session_start_hhmm: str, session_end_hhmm: str) -> bool:
    """Check whether current ET time is within [start, end] (inclusive)."""
    now_et = datetime.datetime.now(ET)
    sh, sm = int(session_start_hhmm[:2]), int(session_start_hhmm[2:])
    eh, em = int(session_end_hhmm[:2]), int(session_end_hhmm[2:])
    start = now_et.replace(hour=sh, minute=sm, second=0, microsecond=0)
    end = now_et.replace(hour=eh, minute=em, second=0, microsecond=0)
    return start <= now_et <= end


# ──────────────────────────────────────────────────────────────────────────────
# Per-strategy risk config loader
# ──────────────────────────────────────────────────────────────────────────────

_strategy_configs: dict[str, dict] = {}


def _load_strategy_config(strategy_id: str) -> dict:
    if strategy_id in _strategy_configs:
        return _strategy_configs[strategy_id]

    # Directory names follow the pattern: strategy_NN_STRATEGY_ID
    # We split on the first two underscores and compare the remainder exactly.
    # This prevents "MES" matching "strategy_02_MES_EMA_PULLBACK_TREND" when
    # a different shorter strategy ID is looked up.
    root = pathlib.Path(__file__).parent.parent
    for d in root.iterdir():
        if not d.is_dir():
            continue
        parts = d.name.split("_", 2)
        if len(parts) == 3 and parts[0] == "strategy" and parts[2] == strategy_id:
            cfg_path = d / "risk_config.yaml"
            if cfg_path.exists():
                with open(cfg_path) as f:
                    cfg = yaml.safe_load(f)
                _strategy_configs[strategy_id] = cfg
                return cfg
    return {}


# ──────────────────────────────────────────────────────────────────────────────
# Core pre-trade checks
# ──────────────────────────────────────────────────────────────────────────────

class CheckResult:
    def __init__(self, accept: bool, reason: str):
        self.accept = accept
        self.reason = reason

    def __repr__(self):
        verdict = "ACCEPT" if self.accept else "REJECT"
        return f"{verdict}: {self.reason}"


def pre_trade_checks(
    state: AccountState,
    signal: dict,
    strategy_id: str,
) -> CheckResult:
    scfg = _load_strategy_config(strategy_id)
    symbol = re.sub(r"[FGHJKMNQUVXZ]\d{1,2}$", "", signal.get("ticker", "").upper())

    # 1. Stale signal guard
    signal_ts_str = signal.get("extras", {}).get("signal_ts", "")
    if not isinstance(signal_ts_str, str):
        return CheckResult(False, 'invalid_signal_ts')
    if signal_ts_str:
        try:
            sig_dt = datetime.datetime.fromisoformat(signal_ts_str.replace("Z", "+00:00"))
            age = (datetime.datetime.now(datetime.timezone.utc) - sig_dt).total_seconds()
            if age < -1:
                return CheckResult(False, "future_signal_ts")
            if age > STALE_THRESHOLD:
                return CheckResult(False, f"stale_signal age={age:.1f}s > {STALE_THRESHOLD}s")
        except (ValueError, TypeError):
            # Malformed or missing timestamp — reject rather than silently allow.
            return CheckResult(False, "invalid_signal_ts: could not parse signal_ts field")

    else:
        return CheckResult(False, "missing_signal_ts")

    # 2. Drawdown guard (self-imposed; skipped if no floor is configured)
    if state.drawdown_floor is not None:
        buffer = state.balance - state.drawdown_floor
        if buffer < NEAR_FLOOR_BUFFER:
            return CheckResult(False, f"near_drawdown_floor buffer={buffer:.2f}")

    # 3. News blackout (self-imposed; no-op when disabled in news_calendar.yaml)
    if _is_news_blackout(symbol):
        return CheckResult(False, f"news_blackout symbol={symbol}")

    # 4. Max open contracts (self-imposed; 0 = unlimited)
    if MAX_OPEN_CONTRACTS > 0:
        requested_qty = int(signal.get("quantity", 1))
        available = MAX_OPEN_CONTRACTS - state.open_positions
        if available <= 0:
            return CheckResult(
                False,
                f"max_open_contracts open={state.open_positions} max={MAX_OPEN_CONTRACTS}",
            )
        if requested_qty > available:
            signal["quantity"] = str(available)  # clip qty in place
            log.warning("clipped_qty", requested=requested_qty, allowed=available)

    # 5. Trading window
    session_start = scfg.get("session_start", "0930")
    session_end = scfg.get("session_end", "1610")
    if not _within_session(session_start, session_end):
        return CheckResult(False, f"outside_session window={session_start}-{session_end}")

    # 6. Daily loss kill switch
    daily_kill = float(scfg.get("daily_loss_kill", 0) or 0)
    if daily_kill > 0 and state.day_realized <= -daily_kill:
        return CheckResult(False, f"daily_loss_kill day_pnl={state.day_realized:.2f} kill={-daily_kill:.2f}")

    return CheckResult(True, "all_checks_passed")


# ──────────────────────────────────────────────────────────────────────────────
# FastAPI app
# ──────────────────────────────────────────────────────────────────────────────

app = FastAPI(title="IBKR Live Algo — Risk Server", docs_url=None, redoc_url=None, openapi_url=None)
secure_app(app)

_state: AccountState = _build_initial_state()
_http_client = httpx.AsyncClient(timeout=10.0, trust_env=False, follow_redirects=False)
_route_lock = asyncio.Lock()
_entry_results = {}
_manual_synced_at = 0.0
_manual_traded = {}
_executor = None  # IBKRExecutor instance, created on startup if order_router=ibkr


def _sync_state_from_executor(require_pnl=True) -> bool:
    """Pull live positions, balance and P&L from IBKR into the in-memory state.

    With IBKR as the broker, these come from live events rather than a manual
    /set_state sync — so the drawdown floor, position cap and daily-loss kill
    switch all evaluate against real broker numbers.
    """
    if _executor is None or not _executor.state_ready(require_pnl=require_pnl):
        return False
    try:
        _state.open_positions = _executor.open_contracts()
        bal = _executor.account_balance()
        if bal is not None:
            _state.balance = bal
        day_realized = _executor.day_realized_pnl()
        if day_realized is not None:
            _state.day_realized = day_realized
        day_open = _executor.day_open_pnl()
        if day_open is not None:
            _state.day_open_pnl = day_open
        _state.last_updated = time.time()
        return True
    except Exception as exc:
        log.warning("executor_state_sync_failed", error=str(exc))
        return False


@app.on_event("startup")
async def startup():
    global _executor
    if not valid_secret(_RISK_API_KEY):
        raise RuntimeError("Configure a generated RISK_SERVER_API_KEY before starting")
    if ORDER_ROUTER == "ibkr":
        if IBKRExecutor is None:
            log.error(
                "ibkr_executor_import_failed",
                detail="order_router=ibkr but ib_insync/ibkr_executor unavailable.",
            )
        else:
            _executor = IBKRExecutor(mode=MODE, max_order_quantity=MAX_ORDER_QUANTITY)
            try:
                await _executor.connect()
                _sync_state_from_executor()  # seed balance/positions from broker
            except Exception as exc:
                log.error("ibkr_executor_connect_failed", error=str(exc))
                await _executor.disconnect()
                _executor = None
    log.info("risk_server_started", mode=MODE, order_router=ORDER_ROUTER,
             balance=_state.balance, drawdown_floor=_state.drawdown_floor,
             auth="enabled" if _RISK_API_KEY else "DISABLED_NO_KEY_SET")


@app.on_event("shutdown")
async def shutdown():
    await _http_client.aclose()
    if _executor is not None:
        await _executor.disconnect()


# ──────────────────────────────────────────────────────────────────────────────
# Webhook endpoint
# ──────────────────────────────────────────────────────────────────────────────

@app.post("/webhook/{strategy_id}", dependencies=[Depends(verify_api_key)])
async def receive_webhook(strategy_id: str, request: Request):
    async with _route_lock:
        return await _receive_webhook(strategy_id, request)


async def _receive_webhook(strategy_id: str, request: Request):
    """
    Signal generators POST strategy alert JSON here.
    Risk checks run; if ACCEPT the payload is forwarded to the order router.

    Requires header:  X-Api-Key: <RISK_SERVER_API_KEY>
    """
    raw = await read_body(request, _MAX_BODY_BYTES)
    if len(raw) > _MAX_BODY_BYTES:
        raise HTTPException(status_code=413, detail="payload too large")
    try:
        signal = json.loads(raw)
    except json.JSONDecodeError:
        raise HTTPException(status_code=400, detail="invalid JSON")

    scfg = _load_strategy_config(strategy_id)
    if not scfg:
        raise HTTPException(404, detail='unknown strategy')
    if not isinstance(signal, dict) or not isinstance(signal.get('extras', {}), dict):
        raise HTTPException(422, detail='signal and extras must be objects')
    allowed_fields = {'ticker','action','quantity','order_type','limit_price','stop_loss','take_profit','extras'}
    if set(signal) - allowed_fields:
        raise HTTPException(422, detail='unsupported signal fields')
    if set(signal.get('extras', {})) - {'strategy','mode','signal_ts','signal_id'}:
        raise HTTPException(422, detail='unsupported extras fields')
    action = str(signal.get('action', '')).lower()
    if action not in ('buy', 'sell', 'exit', 'flat', 'close'):
        raise HTTPException(422, detail='unknown action')
    try:
        if isinstance(signal.get('quantity'), bool):
            raise ValueError()
        quantity = float(signal.get('quantity', 1))
        if not math.isfinite(quantity) or quantity <= 0 or not quantity.is_integer():
            raise ValueError('quantity must be a positive integer')
        signal['quantity'] = int(quantity)
    except (ValueError, TypeError, OverflowError):
        raise HTTPException(422, detail='quantity must be a positive integer')
    ticker = str(signal.get('ticker', '')).upper()
    if re.sub(r'[FGHJKMNQUVXZ]\d{1,2}$', '', ticker) != re.sub(r'[FGHJKMNQUVXZ]\d{1,2}$', '', scfg.get('live_symbol', '').upper()):
        raise HTTPException(422, detail='ticker does not match strategy instrument')
    signal['ticker'] = ticker
    if len(ticker) > 16 or not re.fullmatch(r'[A-Z0-9]+', ticker):
        raise HTTPException(422, detail='invalid ticker')
    signal_id = signal.get('extras', {}).get('signal_id')
    if signal_id is not None and (not isinstance(signal_id, str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,128}', signal_id)):
        raise HTTPException(422, detail='invalid signal_id')
    if action in ('buy', 'sell') and not signal_id:
        raise HTTPException(422, detail='entry signal_id required')
    if action in ('buy', 'sell'):
        for field in ('stop_loss', 'take_profit'):
            try:
                value = float(signal.get(field))
                if not math.isfinite(value) or value <= 0:
                    raise ValueError()
            except (ValueError, TypeError):
                raise HTTPException(422, detail=f'finite positive {field} required')
    if action in ('buy', 'sell') and (strategy_id, signal_id) in _entry_results:
        return _entry_results[(strategy_id, signal_id)]

    # Inject strategy_id from URL if not present in payload
    if "extras" not in signal:
        signal["extras"] = {}
    signal["extras"]["strategy"] = strategy_id
    signal["extras"]["mode"] = MODE

    action = str(signal.get("action", "")).lower()
    qty    = int(signal.get("quantity", 1))
    is_exit = action in ("exit", "flat", "close")

    # When trading through IBKR, refresh balance/positions/P&L from the broker
    # BEFORE running checks so the drawdown floor, position cap and daily-loss
    # kill switch evaluate against live numbers rather than stale state.
    if ORDER_ROUTER == "ibkr" and not is_exit:
        needs_pnl = float(scfg.get('daily_loss_kill', 0) or 0) > 0
        if not _sync_state_from_executor(require_pnl=needs_pnl):
            return {'status': 'rejected', 'reason': 'broker_state_unavailable_or_stale'}
    elif ORDER_ROUTER == 'traderspost' and not is_exit:
        if time.time() - _manual_synced_at > 60:
            return {'status': 'rejected', 'reason': 'manual_broker_sync_required'}

    # Exit signals bypass ALL pre-trade checks — you always want to be able
    # to close a position regardless of session, drawdown state, or news window.
    if is_exit:
        try:
            timestamp = signal['extras']['signal_ts']
            if not isinstance(timestamp, str):
                raise ValueError()
            parsed = datetime.datetime.fromisoformat(timestamp.replace('Z', '+00:00'))
            age = (datetime.datetime.now(datetime.timezone.utc) - parsed).total_seconds()
            if age < -1 or age > STALE_THRESHOLD:
                raise ValueError()
        except (KeyError, TypeError, ValueError):
            return {'status': 'rejected', 'reason': 'invalid_or_stale_exit_timestamp'}
        result = CheckResult(True, "exit_bypass")
    else:
        result = pre_trade_checks(_state, signal, strategy_id)

    log_entry = {
        "ts": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        "strategy_id": strategy_id,
        "mode": MODE,
        "decision": "ACCEPT" if result.accept else "REJECT",
        "reason": result.reason,
        "account_state": {
            "balance": _state.balance,
            "drawdown_floor": _state.drawdown_floor,
            "day_realized": _state.day_realized,
            "open_positions": _state.open_positions,
        },
        "signal": signal_summary(signal),
        "payload_sent": signal_summary(signal) if result.accept else None,
    }
    _write_decision_log(log_entry)
    log.info("webhook_decision", strategy=strategy_id,
             decision=log_entry["decision"], reason=result.reason)

    if not result.accept:
        return {"status": "rejected", "reason": result.reason}

    # ── Route the accepted order ──────────────────────────────────────────────
    if ORDER_ROUTER == "ibkr":
        # Direct execution through Interactive Brokers.
        if _executor is None or not _executor.is_connected():
            log.error("ibkr_executor_unavailable", strategy=strategy_id)
            return {"status": "error", "detail": "IBKR executor not connected"}
        exec_result = await _executor.submit(signal)
        # The broker is the source of truth for positions/balance — resync.
        _sync_state_from_executor()
        if action in ('buy', 'sell') and signal_id and exec_result.get('status') == 'submitted':
            _entry_results[(strategy_id, signal_id)] = exec_result
            if len(_entry_results) > 10000:
                _entry_results.pop(next(iter(_entry_results)))
        return exec_result

    # order_router == "traderspost": forward to the webhook router.
    # TradersPost returns no fills, so track open positions in memory here.
    tp_url = _tp_webhook(strategy_id)
    if not tp_url:
        log.error("order_router_url_not_configured", strategy=strategy_id,
                  hint=f"Set env var TP_WEBHOOK_{strategy_id}")
        return {"status": "error", "detail": f"TP_WEBHOOK_{strategy_id} not set"}

    try:
        payload = dict(signal)
        if action in ('buy', 'sell'):
            payload['orderType'] = signal.get('order_type', 'market')
            payload['stopLoss'] = {'stopPrice': signal['stop_loss']}
            payload['takeProfit'] = {'limitPrice': signal['take_profit']}
        validate_router_url(tp_url)
        resp = await _http_client.post(tp_url, json=payload)
        resp.raise_for_status()
        qty = int(signal['quantity'])  # effective, possibly clipped quantity
        if action in ('buy', 'sell'):
            _state.open_positions += qty
            _manual_traded[strategy_id] = datetime.datetime.now(ET).date()
        elif is_exit:
            _state.open_positions = max(0, _state.open_positions - qty)
        result = {'status': 'forwarded', 'router_status': resp.status_code}
        if signal_id and not is_exit:
            _entry_results[(strategy_id, signal_id)] = result
        return result
    except (httpx.HTTPError, ValueError) as exc:
        log.error("order_forward_failed", strategy=strategy_id, error_type=type(exc).__name__)
        return {"status": "error", "detail": "order router request failed"}


# ──────────────────────────────────────────────────────────────────────────────
# State management endpoints
# ──────────────────────────────────────────────────────────────────────────────

@app.get('/strategy-state/{strategy_id}', dependencies=[Depends(verify_api_key)])
async def strategy_state(strategy_id: str):
    cfg = _load_strategy_config(strategy_id)
    if not cfg:
        raise HTTPException(404, detail='unknown strategy')
    if ORDER_ROUTER == 'traderspost':
        return {'ready': time.time() - _manual_synced_at <= 60,
                'position_qty': _state.open_positions,
                'traded_today': _manual_traded.get(strategy_id) == datetime.datetime.now(ET).date(),
                'entry_pending': False, 'exit_pending': False}
    if _executor is None:
        return {'ready': False, 'detail': 'broker reconciliation requires direct IBKR routing'}
    base = re.sub(r'[FGHJKMNQUVXZ]\d{1,2}$', '', cfg['live_symbol'].upper())
    return _executor.strategy_state(base)

@app.get("/health", dependencies=[Depends(verify_api_key)])
async def health():
    """Live account state snapshot. Requires X-Api-Key header."""
    return {
        "mode": MODE,
        "balance": _state.balance,
        "drawdown_floor": _state.drawdown_floor,
        "buffer_to_floor": _state.buffer_to_floor,
        "day_realized": _state.day_realized,
        "open_positions": _state.open_positions,
        "last_updated": _state.last_updated,
    }


@app.post("/set_state", dependencies=[Depends(verify_admin_key)])
async def set_state(request: Request):
    global _manual_synced_at
    """
    Sync account state from your broker / IBKR account.
    Requires header:  X-Api-Key: <RISK_ADMIN_API_KEY>

    Direct IBKR routing refreshes risk state from the broker. In optional
    TradersPost mode, balance/day_realized/open_positions must be supplied
    together to refresh entry readiness for 60 seconds. Partial updates may
    change settings but do not refresh that readiness.

    To use a TRAILING drawdown floor, raise `drawdown_floor` here as your
    balance grows to lock in gains (the server never moves it for you).

    Example body:
        {
            "balance": 51250.00,
            "drawdown_floor": 49750.00,
            "day_realized": 450.00,
            "open_positions": 0
        }
    """
    raw = await read_body(request, _MAX_BODY_BYTES)
    if len(raw) > _MAX_BODY_BYTES:
        raise HTTPException(status_code=413, detail="payload too large")
    try:
        body = json.loads(raw)
    except json.JSONDecodeError:
        raise HTTPException(status_code=400, detail="invalid JSON")
    if not isinstance(body, dict):
        raise HTTPException(422, detail='state must be an object')
    try:
        for key in ('balance', 'drawdown_floor', 'day_realized'):
            if key in body and not (key == 'drawdown_floor' and body[key] is None):
                if isinstance(body[key], bool) or not math.isfinite(float(body[key])):
                    raise ValueError('state values must be finite numbers')
        if 'open_positions' in body:
            qty = body['open_positions']
            if isinstance(qty, bool) or float(qty) != int(qty) or int(qty) < 0:
                raise ValueError('open_positions must be a nonnegative integer')
    except (ValueError, TypeError, OverflowError) as exc:
        raise HTTPException(422, detail='invalid account state')
    if "balance" in body:
        _state.balance = float(body["balance"])
    if "drawdown_floor" in body:
        _state.drawdown_floor = (
            None if body["drawdown_floor"] is None else float(body["drawdown_floor"])
        )
    if "day_realized" in body:
        _state.day_realized = float(body["day_realized"])
    if "open_positions" in body:
        _state.open_positions = int(body["open_positions"])
    _state.last_updated = time.time()
    if all(key in body for key in ('balance', 'day_realized', 'open_positions')):
        _manual_synced_at = time.time()
    log.info("state_updated", fields=[key for key in body if key in ("balance","drawdown_floor","day_realized","open_positions")])
    return {"ok": True, "state": {
        "balance": _state.balance,
        "drawdown_floor": _state.drawdown_floor,
        "day_realized": _state.day_realized,
        "open_positions": _state.open_positions,
    }}


@app.post("/reset_day", dependencies=[Depends(verify_admin_key)])
async def reset_day():
    """
    Reset intraday counters at the start of each trading day.
    Requires header:  X-Api-Key: <RISK_ADMIN_API_KEY>

    Call this each morning before the session opens (or automate via cron).
    Clears day_realized and day_open_pnl; preserves open_positions.
    Does NOT change balance or drawdown_floor — update those via /set_state.
    """
    _state.day_realized = 0.0
    _state.day_open_pnl = 0.0
    _state.last_updated = time.time()
    log.info("day_reset")
    return {"ok": True, "message": "Intraday counters cleared"}


if __name__ == "__main__":
    uvicorn.run(
        "risk_module:app",
        host=RISK_CFG["host"],
        port=RISK_CFG["port"],
        reload=False,
    )

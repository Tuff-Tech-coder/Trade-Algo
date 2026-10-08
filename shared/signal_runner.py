"""
IBKR Live Algo — Signal Runner. FastAPI receiver for OHLCV bar data.

ibkr_feed.py (or a TradingView Essential alert) POSTs one OHLCV bar here on
every bar close. The runner dispatches each bar to all registered signal
generators, which evaluate entry conditions and POST to the risk server if
triggered.

─────────────────────────────────────────────────────────────────────────
QUICK START
─────────────────────────────────────────────────────────────────────────
1. Start the risk server first (port 8000):
       cd shared && uvicorn risk_module:app --host 127.0.0.1 --port 8000

2. Start the signal runner (port 8001):
       cd shared && uvicorn signal_runner:app --host 127.0.0.1 --port 8001

3. Run ibkr_feed.py on the same machine — it posts bars to localhost:8001,
   so no internet exposure is required. (Only if you choose to drive this
   from TradingView instead of ibkr_feed do you need to expose port 8001,
   such deployment is unsupported and requires a separate security design.)

─────────────────────────────────────────────────────────────────────────
BAR SOURCE FORMAT (ibkr_feed.py posts this; a TradingView alert can too)
─────────────────────────────────────────────────────────────────────────
Alert condition : any indicator that fires on every bar close
                  (e.g. a script that always evaluates true)
Trigger         : Once Per Bar Close
Webhook URL     : https://your-server/tv-bar/MNQ/5    ← MNQ 5-min bars
                  https://your-server/tv-bar/MES/5
                  https://your-server/tv-bar/MCL/5
                  https://your-server/tv-bar/MGC/15   ← MGC 15-min bars
                  https://your-server/tv-bar/M2K/5
Message body (paste exactly):
    {"time":"{{time}}","open":{{open}},"high":{{high}},"low":{{low}},"close":{{close}},"volume":{{volume}}}

Webhook header (required — prevents fake bar injection):
    Authorization: Bearer <TV_WEBHOOK_SECRET>
    In TradingView: Alert → Notifications → Webhook URL → Headers (JSON):
    {"Authorization": "Bearer <your TV_WEBHOOK_SECRET value from shared/.env>"}

The symbol/timeframe set is defined per mode in ibkr_feed/contract_map.py
(PAPER_SUBSCRIPTIONS / LIVE_SUBSCRIPTIONS) — edit those lists to match the
strategies you run.
─────────────────────────────────────────────────────────────────────────
"""
from __future__ import annotations

import asyncio
import contextlib
import datetime
import re
import hmac
import importlib.util
import json
import logging
import os
import pathlib
import sys

import uvicorn
import yaml
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Request

from security import valid_secret, matches_secret, read_body, secure_app
from live_feed import BarStore, normalize_bar

# ──────────────────────────────────────────────────────────────────────────────
# Logging
# ──────────────────────────────────────────────────────────────────────────────

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-8s %(name)s: %(message)s",
    datefmt="%Y-%m-%dT%H:%M:%S",
)
log = logging.getLogger("signal_runner")

# ──────────────────────────────────────────────────────────────────────────────
# Paths & config
# ──────────────────────────────────────────────────────────────────────────────

SHARED_DIR = pathlib.Path(__file__).parent
PROJECT_ROOT = SHARED_DIR.parent
CONFIG_PATH = SHARED_DIR / "account_config.yaml"

# Add shared/ to sys.path so imports inside signal_generator.py files work
if str(SHARED_DIR) not in sys.path:
    sys.path.insert(0, str(SHARED_DIR))

# Load .env before reading config (generators need env vars at startup)
_ENV_FILE = SHARED_DIR / ".env"
if _ENV_FILE.exists():
    load_dotenv(_ENV_FILE)

with open(CONFIG_PATH) as _f:
    CONFIG = yaml.safe_load(_f)

MODE: str = CONFIG["mode"]  # PAPER | LIVE

# Which strategy numbers to load in each mode. An empty list means "load every
# strategy discovered at the project root" — handy before you start splitting
# strategies between paper-testing and live trading. To run only a subset live,
# list its numbers here, e.g. "LIVE": [1, 3].
MODE_STRATEGIES: dict[str, list[int]] = {
    "PAPER": [],
    "LIVE":  [],
}

# ── TradingView webhook authentication ────────────────────────────────────────

_TV_SECRET: str | None = os.environ.get("TV_WEBHOOK_SECRET") or None

# Authentication cannot be disabled; use generated keys for local development.
_MAX_BODY_BYTES = 65536


def _check_tv_auth(authorization: str) -> None:
    """
    Authenticate a bar POST via the Authorization header (header only):
        Authorization: Bearer <TV_WEBHOOK_SECRET>

    Fails CLOSED: if no secret is configured the request is rejected (503)
    including local development. The token comparison is
    constant-time to avoid leaking it through timing.
    """
    if not valid_secret(_TV_SECRET):
        raise HTTPException(
            status_code=503,
            detail="Bar ingestion disabled: TV_WEBHOOK_SECRET not set.",
        )
    if not matches_secret(authorization, f"Bearer {_TV_SECRET}"):
        raise HTTPException(status_code=403, detail="Invalid or missing token.")


# ──────────────────────────────────────────────────────────────────────────────
# Strategy loader
# ──────────────────────────────────────────────────────────────────────────────

def _load_generators(mode: str) -> list:
    """Discover and instantiate signal generators for the current mode.

    If MODE_STRATEGIES[mode] is empty, every strategy folder found at the
    project root is loaded; otherwise only the listed strategy numbers are.
    """
    from signal_base import SignalGenerator

    active_nums = MODE_STRATEGIES.get(mode, [])
    load_all = not active_nums
    generators = []

    for d in sorted(PROJECT_ROOT.iterdir()):
        if not d.is_dir():
            continue
        parts = d.name.split("_")
        if len(parts) < 2 or parts[0] != "strategy":
            continue
        try:
            num = int(parts[1])
        except ValueError:
            continue
        if not load_all and num not in active_nums:
            continue

        sg_path = d / "signal_generator.py"
        risk_path = d / "risk_config.yaml"
        if not sg_path.exists() or not risk_path.exists():
            log.warning("Skipping %s — missing signal_generator.py or risk_config.yaml", d.name)
            continue

        # Safety check: ensure the file being loaded lives inside the project root.
        # Guards against symlink attacks or path traversal tricks that could cause
        # an arbitrary Python file elsewhere on disk to be executed.
        try:
            sg_path.resolve().relative_to(PROJECT_ROOT.resolve())
        except ValueError:
            log.error(
                "SECURITY: refusing to load %s — path escapes project root", sg_path
            )
            continue

        with open(risk_path) as f:
            risk_cfg = yaml.safe_load(f)

        spec = importlib.util.spec_from_file_location(
            f"strat_{num:02d}_sg", sg_path
        )
        mod = importlib.util.module_from_spec(spec)
        try:
            spec.loader.exec_module(mod)
        except Exception as exc:
            log.error("Failed to load %s: %s", sg_path, exc)
            continue

        # Find the SignalGenerator subclass
        gen_cls = None
        for attr in vars(mod).values():
            if (
                isinstance(attr, type)
                and issubclass(attr, SignalGenerator)
                and attr is not SignalGenerator
            ):
                gen_cls = attr
                break

        if gen_cls is None:
            log.warning("No SignalGenerator subclass found in %s", sg_path)
            continue

        gen = gen_cls(risk_cfg, CONFIG)
        generators.append(gen)
        log.info(
            "Loaded  %-35s  symbol=%-4s  tf=%sm  htf=%s",
            gen.strategy_id, gen.base_symbol, gen.bar_size,
            f"{gen.htf_bar_size}m" if gen.htf_bar_size else "—",
        )

    return generators


# ──────────────────────────────────────────────────────────────────────────────
# FastAPI app
# ──────────────────────────────────────────────────────────────────────────────

app = FastAPI(title="IBKR Live Algo — Signal Runner", version="2.0", docs_url=None, redoc_url=None, openapi_url=None)
secure_app(app)

bar_store = BarStore(maxlen=300)
generators: list = []
_timer_task = None

# Dispatch table: (BASE_SYMBOL, tf_minutes) → [generators that watch it]
_dispatch: dict[tuple[str, int], list] = {}


@app.on_event("startup")
async def startup() -> None:
    global generators, _dispatch, _timer_task
    if not valid_secret(_TV_SECRET):
        raise RuntimeError("Configure a generated TV_WEBHOOK_SECRET before starting")
    _dispatch = {}

    # Restore bar history from the last session before generators start
    bar_store.load()

    generators = _load_generators(MODE)

    if not generators:
        log.error("No signal generators loaded for mode=%s", MODE)
        return

    async def timers():
        while True:
            for gen in generators:
                try:
                    gen.symbol = bar_store.contracts.get(gen.base_symbol, gen.symbol)
                    await gen.on_timer()
                except Exception:
                    log.exception('strategy timer failed: %s', gen.strategy_id)
            await asyncio.sleep(1)
    _timer_task = asyncio.create_task(timers())
    for gen in generators:
        key = (gen.base_symbol, gen.bar_size)
        _dispatch.setdefault(key, []).append(gen)

    log.info(
        "Signal runner ready — mode=%s  generators=%d  watching=%s  tv_auth=%s",
        MODE,
        len(generators),
        [f"{s}/{t}m" for s, t in sorted(_dispatch)],
        "enabled" if _TV_SECRET else "DISABLED",
    )


@app.on_event("shutdown")
async def shutdown() -> None:
    if _timer_task:
        _timer_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await _timer_task
    for gen in generators:
        await gen.close()
    # Persist bar history so the next startup skips the warmup period
    bar_store.save()
    log.info("signal_runner stopped")


# ──────────────────────────────────────────────────────────────────────────────
# Bar ingestion endpoint
# ──────────────────────────────────────────────────────────────────────────────

def _check_subscription(symbol, timeframe):
    if (symbol.upper(), timeframe) not in _dispatch:
        raise HTTPException(422, detail='unregistered symbol/timeframe')

def _check_closed_bar(bar, timeframe):
    completion = datetime.datetime.fromisoformat(bar['timestamp']) + datetime.timedelta(minutes=timeframe)
    if completion > datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(seconds=1):
        raise ValueError('future/forming bar is not closed')

def _set_contract(symbol, ticker):
    if ticker:
        ticker = str(ticker).upper()
        if re.sub(r'[FGHJKMNQUVXZ]\d{1,2}$', '', ticker) != symbol:
            raise ValueError('contract does not match feed symbol')
        if bar_store.contracts.get(symbol) not in (None, ticker):
            # Never combine price history across contracts on a roll.
            bar_store._buffers = {k: v for k, v in bar_store._buffers.items() if k[0] != symbol}
        bar_store.contracts[symbol] = ticker

@app.post('/history/{symbol}/{tf_minutes}')
async def receive_history(symbol: str, tf_minutes: int, request: Request):
    _check_tv_auth(request.headers.get('authorization', ''))
    _check_subscription(symbol, tf_minutes)
    raw = await read_body(request, _MAX_BODY_BYTES)
    if len(raw) > _MAX_BODY_BYTES:
        raise HTTPException(413, detail='payload too large')
    try:
        data = json.loads(raw)
        rows = data['bars']
        if not isinstance(rows, list) or len(rows) > 300 or tf_minutes <= 0:
            raise ValueError('invalid history batch')
        rows = [normalize_bar(row) for row in rows]
        for row in rows:
            _check_closed_bar(row, tf_minutes)
        _set_contract(symbol.upper(), data.get('contract_ticker'))
        for row in rows:
            bar_store.push(symbol, tf_minutes, row)
    except (ValueError, TypeError, KeyError) as exc:
        raise HTTPException(422, detail='invalid history batch')
    return {'status': 'seeded', 'bar_count': bar_store.bar_count(symbol, tf_minutes)}

@app.post("/tv-bar/{symbol}/{tf_minutes}")
async def receive_bar(symbol: str, tf_minutes: int, request: Request):
    """
    ibkr_feed.py (or a TradingView alert) posts one bar here on every bar close.

    URL params:
        symbol     — base futures symbol, e.g. MNQ, MES, MCL, MGC, M2K
        tf_minutes — bar size in minutes, e.g. 5 or 15

    Authentication (header only):
        Authorization: Bearer <TV_WEBHOOK_SECRET>

    Expected JSON body:
        {"time":"...","open":...,"high":...,"low":...,"close":...,"volume":...}
    """
    _check_tv_auth(request.headers.get("authorization", ""))
    _check_subscription(symbol, tf_minutes)

    raw = await read_body(request, _MAX_BODY_BYTES)
    if len(raw) > _MAX_BODY_BYTES:
        raise HTTPException(status_code=413, detail="payload too large")
    try:
        data = json.loads(raw)
    except Exception:
        raise HTTPException(status_code=400, detail="invalid JSON body")

    sym = symbol.upper()
    try:
        bar = normalize_bar(data)
        _check_closed_bar(bar, tf_minutes)
        _set_contract(sym, data.get('contract_ticker'))
        changed = bar_store.push(sym, tf_minutes, bar)
    except (ValueError, TypeError, KeyError) as exc:
        raise HTTPException(422, detail='invalid closed bar')

    key = (sym, tf_minutes)
    dispatched_to = _dispatch.get(key, [])

    log.info(
        "bar_received  symbol=%-4s  tf=%2sm  close=%-10s  dispatched_to=%d  bar_count=%d",
        sym, tf_minutes, bar["close"],
        len(dispatched_to),
        bar_store.bar_count(sym, tf_minutes),
    )

    if not dispatched_to:
        log.warning(
            "bar_ignored  symbol=%s  tf=%sm — no generators watching this symbol/timeframe. "
            "Check that the alert URL matches a subscribed symbol.",
            sym, tf_minutes,
        )

    for gen in dispatched_to:
        try:
            await gen.on_bar(bar_store)
        except Exception:
            log.exception('strategy bar handler failed: %s', gen.strategy_id)

    return {
        "status": "ok" if changed else "duplicate",
        "symbol": sym,
        "tf_minutes": tf_minutes,
        "close": bar["close"],
        "bar_count": bar_store.bar_count(sym, tf_minutes),
        "dispatched": len(dispatched_to),
    }


# ──────────────────────────────────────────────────────────────────────────────
# Health endpoint — basic liveness is open; detail requires the bearer token
# so the server's mode, strategy inventory and symbols aren't disclosed openly.
# ──────────────────────────────────────────────────────────────────────────────

@app.get("/health")
async def health(request: Request):
    base = {"status": "ok"}

    authed = False
    if valid_secret(_TV_SECRET):
        authed = matches_secret(
            request.headers.get("authorization", "") or "", f"Bearer {_TV_SECRET}"
        )
    if not authed:
        return base

    base.update({
        "mode": MODE,
        "generators": [
            {
                "id": g.strategy_id,
                "symbol": g.base_symbol,
                "tf_minutes": g.bar_size,
                "htf_minutes": g.htf_bar_size,
            }
            for g in generators
        ],
        "bar_counts": {
            f"{sym}/{tf}m": bar_store.bar_count(sym, tf)
            for sym, tf in sorted(_dispatch)
        },
    })
    return base


# ──────────────────────────────────────────────────────────────────────────────
# Entry point
# ──────────────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    port = CONFIG.get("risk_server", {}).get("signal_runner_port", 8001)
    host = CONFIG.get("risk_server", {}).get("signal_runner_host", "127.0.0.1")
    uvicorn.run("signal_runner:app", host=host, port=port, reload=False)

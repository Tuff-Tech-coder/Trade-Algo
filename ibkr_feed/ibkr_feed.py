"""
IBKR Live Algo — Bar Feed.  A drop-in replacement for TradingView webhook bars.

Connects to Interactive Brokers TWS or IB Gateway running on this machine,
subscribes to real-time bar streams for all required symbols/timeframes, and
POSTs each completed bar to the signal runner's /tv-bar/ endpoint using the
exact same JSON format and Authorization header as TradingView webhook alerts.

No changes are required to signal_runner.py, risk_module.py, or any strategy
file. Simply run this script instead of configuring TradingView alerts.

─────────────────────────────────────────────────────────────────────────
QUICK START
─────────────────────────────────────────────────────────────────────────
1. Make sure TWS is running and logged in (port 7497 for paper accounts)
2. Make sure signal_runner.py is running on port 8001
3. Copy .env.example to .env and fill in TV_WEBHOOK_SECRET
4. Install dependencies:
       pip install -r requirements.txt
5. Run:
       python ibkr_feed.py --mode PAPER

─────────────────────────────────────────────────────────────────────────
HOW IT WORKS
─────────────────────────────────────────────────────────────────────────
For each (symbol, timeframe) pair in the active mode, ibkr_feed:
  1. Resolves the front-month futures contract via IBKR API
  2. Requests historical bars with keepUpToDate=True — IBKR streams
     real-time updates and fires an event each time a bar completes
  3. POSTs the completed bar to http://localhost:8001/tv-bar/{symbol}/{tf}
     with the Authorization: Bearer header, exactly like TradingView would

The signal runner receives the bar, pushes it into BarStore, and dispatches
it to all registered signal generators — nothing downstream changes.
─────────────────────────────────────────────────────────────────────────
"""
from __future__ import annotations

import argparse
import asyncio
import calendar
import datetime
import logging
import os
import pathlib
import sys

import httpx
from dotenv import load_dotenv
from ib_insync import IB, Future, util

# Allow standalone invocation from ibkr_feed/ as documented.
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / 'shared'))
from security import validate_local_url, valid_secret
from contract_map import (
    BAR_SIZE_STRINGS,
    CONTRACTS,
    LIVE_SUBSCRIPTIONS,
    PAPER_SUBSCRIPTIONS,
)

# ──────────────────────────────────────────────────────────────────────────────
# Logging
# ──────────────────────────────────────────────────────────────────────────────

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-8s %(name)s: %(message)s",
    datefmt="%Y-%m-%dT%H:%M:%S",
)
log = logging.getLogger("ibkr_feed")

# ──────────────────────────────────────────────────────────────────────────────
# Config — loaded from environment / .env file
# ──────────────────────────────────────────────────────────────────────────────

_ENV_FILE = pathlib.Path(__file__).parent / ".env"
if _ENV_FILE.exists():
    load_dotenv(_ENV_FILE)
    log.info("loaded .env from %s", _ENV_FILE)

# Where bars are posted — must match the running signal_runner.py
SIGNAL_RUNNER_URL: str = os.environ.get(
    "SIGNAL_RUNNER_URL", "http://localhost:8001"
).rstrip("/")

# Same secret used by the main project's signal runner.
# Loaded from env — never hardcode here.
TV_WEBHOOK_SECRET: str | None = os.environ.get("TV_WEBHOOK_SECRET") or None

# TWS / IB Gateway connection parameters
TWS_HOST:      str = os.environ.get("TWS_HOST", "127.0.0.1")
TWS_PORT:      int = int(os.environ.get("TWS_PORT", "7497"))
TWS_CLIENT_ID: int = int(os.environ.get("TWS_CLIENT_ID", "1"))

if not TV_WEBHOOK_SECRET:
    log.warning(
        "TV_WEBHOOK_SECRET not set — bars will be posted without the "
        "Authorization header. The signal runner will allow this only if "
        "TV_WEBHOOK_SECRET is also unset there."
    )

# ──────────────────────────────────────────────────────────────────────────────
# Contract resolution
# ──────────────────────────────────────────────────────────────────────────────

async def get_front_month(ib: IB, symbol: str) -> Future:
    """
    Resolve and return the front-month futures contract for a base symbol.

    Uses reqContractDetails to get all listed contracts, filters to those
    that have not yet expired, and returns the nearest expiry.
    """
    spec = CONTRACTS[symbol]
    template = Future(
        symbol=symbol,
        exchange=spec["exchange"],
        currency=spec["currency"],
    )

    details = await ib.reqContractDetailsAsync(template)
    if not details:
        raise ValueError(
            f"No contracts returned for {symbol} on {spec['exchange']}. "
            "Check that you have a market data subscription for this instrument."
        )

    today = datetime.date.today()
    valid: list[tuple[datetime.date, Future]] = []

    for d in details:
        exp_str = d.contract.lastTradeDateOrContractMonth
        try:
            # IBKR returns expiry as YYYYMMDD or YYYYMM — normalise to 8 chars
            exp_date = datetime.datetime.strptime(
                (exp_str[:6] + str(calendar.monthrange(int(exp_str[:4]), int(exp_str[4:6]))[1])) if len(exp_str) == 6 else exp_str[:8], "%Y%m%d"
            ).date()
            if exp_date >= today:
                valid.append((exp_date, d.contract))
        except ValueError:
            continue

    if not valid:
        raise ValueError(
            f"No non-expired contracts found for {symbol}. "
            "The front month may have just rolled — retry in a few minutes."
        )

    valid.sort(key=lambda x: x[0])
    contract = valid[0][1]

    log.info(
        "contract_resolved  symbol=%-4s  local=%-10s  expiry=%s",
        symbol,
        contract.localSymbol,
        valid[0][0],
    )
    return contract


# ──────────────────────────────────────────────────────────────────────────────
# Bar posting
# ──────────────────────────────────────────────────────────────────────────────

async def post_bar(
    http: httpx.AsyncClient,
    symbol: str,
    tf_minutes: int,
    bar,
    contract_ticker=None,
) -> bool:
    """
    POST a single completed OHLCV bar to the signal runner.

    Matches the exact JSON format TradingView uses so the signal runner
    processes it identically regardless of data source.
    """
    url = f"{SIGNAL_RUNNER_URL}/tv-bar/{symbol}/{tf_minutes}"

    headers: dict[str, str] = {}
    if TV_WEBHOOK_SECRET:
        headers["Authorization"] = f"Bearer {TV_WEBHOOK_SECRET}"

    if bar.date.tzinfo is None:
        raise ValueError('IBKR bar timestamps must be timezone aware (formatDate=2)')
    bar_time = bar.date.astimezone(datetime.timezone.utc).isoformat()

    # IBKR returns -1 for volume when market data is delayed or unavailable
    volume = float(bar.volume) if bar.volume != -1 else 0.0

    payload = {
        "time":   bar_time,
        "open":   float(bar.open),
        "high":   float(bar.high),
        "low":    float(bar.low),
        "close":  float(bar.close),
        "volume": volume,
    }

    if contract_ticker:
        payload['contract_ticker'] = contract_ticker
    try:
        resp = await http.post(url, json=payload, headers=headers, timeout=10.0)
        resp.raise_for_status()
        log.info(
            "bar_posted  symbol=%-4s  tf=%2sm  time=%s  close=%s  vol=%s",
            symbol, tf_minutes, bar_time, bar.close, int(volume),
        )
        return True
    except httpx.HTTPStatusError as exc:
        log.error(
            "bar_rejected  symbol=%s  tf=%sm  status=%s  detail=%s",
            symbol, tf_minutes, exc.response.status_code, "response body omitted",
        )
    except httpx.HTTPError as exc:
        log.error(
            "bar_post_failed  symbol=%s  tf=%sm  error=%s",
            symbol, tf_minutes, exc,
        )

    return False


# ──────────────────────────────────────────────────────────────────────────────
# Bar subscription
# ──────────────────────────────────────────────────────────────────────────────

async def subscribe_bars(
    ib: IB,
    contract: Future,
    symbol: str,
    tf_minutes: int,
    http: httpx.AsyncClient,
    loop: asyncio.AbstractEventLoop,
):
    """
    Subscribe to a real-time bar stream for one (symbol, timeframe) pair.

    Uses reqHistoricalData with keepUpToDate=True:
      - IBKR returns recent historical bars first (warms up the BarStore
        and provides enough history for indicator calculations)
      - Then streams live updates; updateEvent fires with has_new_bar=True
        each time a bar period completes and a new one begins
      - bars[-2] is the just-completed bar; bars[-1] is the forming bar

    Returns the bars object — the caller must keep a reference to it or
    IBKR will cancel the subscription.
    """
    bar_size = BAR_SIZE_STRINGS.get(tf_minutes)
    if not bar_size:
        raise ValueError(
            f"No IBKR bar size string defined for {tf_minutes} minutes. "
            f"Add it to BAR_SIZE_STRINGS in contract_map.py."
        )

    state = {'last_posted_time': None}
    bars = await ib.reqHistoricalDataAsync(
        contract, endDateTime='', durationStr='2 D', barSizeSetting=bar_size,
        whatToShow='TRADES', useRTH=False, formatDate=2, keepUpToDate=True)
    queue = asyncio.Queue(maxsize=300)
    def on_bar_update(updated, has_new_bar):
        if has_new_bar and len(updated) >= 2:
            try:
                queue.put_nowait(updated[-2])
            except asyncio.QueueFull:
                log.error('bar_queue_overflow symbol=%s', symbol)
                ib.disconnect()  # fail visibly rather than quietly lose the stream
    bars.updateEvent += on_bar_update
    try:
        # Warmup is stored atomically and NEVER dispatched as trade signals.
        history = []
        snapshot = list(bars)
        seeded_time = str(snapshot[-2].date) if len(snapshot) > 1 else None
        for bar in snapshot[:-1]:
            if bar.date.tzinfo is None:
                raise ValueError('history timestamp missing timezone')
            history.append({'time': bar.date.astimezone(datetime.timezone.utc).isoformat(),
                            'open': bar.open, 'high': bar.high, 'low': bar.low,
                            'close': bar.close, 'volume': max(0, bar.volume)})
        headers = {'Authorization': f'Bearer {TV_WEBHOOK_SECRET}'} if TV_WEBHOOK_SECRET else {}
        # Bound bulk payloads to the receiver's request limit.
        for start in range(0, len(history), 100):
            response = await http.post(f'{SIGNAL_RUNNER_URL}/history/{symbol}/{tf_minutes}',
                json={'bars': history[start:start+100], 'contract_ticker': contract.localSymbol}, headers=headers, timeout=10)
            response.raise_for_status()
        state['last_posted_time'] = seeded_time
    except BaseException:
        bars.updateEvent -= on_bar_update
        ib.cancelHistoricalData(bars)
        raise

    async def deliver():
        while True:
            completed = await queue.get()
            stamp = str(completed.date)
            if stamp == state['last_posted_time']:
                continue
            while not await post_bar(http, symbol, tf_minutes, completed, contract.localSymbol):
                await asyncio.sleep(1)
            state['last_posted_time'] = stamp
    worker = asyncio.create_task(deliver())
    bars.delivery_task = worker
    worker.add_done_callback(lambda task: ib.disconnect() if not task.cancelled() and task.exception() else None)
    log.info(
        "subscribed  symbol=%-4s  tf=%2sm  contract=%-10s  history=%d bars",
        symbol, tf_minutes, contract.localSymbol, len(bars),
    )
    return bars


# ──────────────────────────────────────────────────────────────────────────────
# Connection watchdog
# ──────────────────────────────────────────────────────────────────────────────

def _setup_connection_handlers(ib: IB) -> None:
    """Log connection state changes so issues are visible in the console."""

    def on_disconnected():
        log.error(
            "TWS_DISCONNECTED — bars will stop flowing. "
            "Check that TWS is still running and restart ibkr_feed.py."
        )

    def on_error(req_id, error_code, error_string, contract):
        # 2104/2106 = market data farm connection — informational, not errors
        if error_code in (2104, 2106, 2158):
            log.debug("ibkr_info code=%s msg=%s", error_code, error_string)
            return
        # 10167 = delayed market data — means no live subscription
        if error_code == 10167:
            log.warning(
                "DELAYED_DATA  symbol=%s — you may be missing a market data "
                "subscription. Bars will still arrive but will be delayed.",
                contract.symbol if contract else "unknown",
            )
            return
        log.error(
            "ibkr_error  req_id=%s  code=%s  msg=%s",
            req_id, error_code, error_string,
        )

    ib.disconnectedEvent += on_disconnected
    ib.errorEvent += on_error


# ──────────────────────────────────────────────────────────────────────────────
# Main
# ──────────────────────────────────────────────────────────────────────────────

async def main(mode: str) -> None:
    subscriptions = PAPER_SUBSCRIPTIONS if mode == "PAPER" else LIVE_SUBSCRIPTIONS
    ib = IB()
    validate_local_url(SIGNAL_RUNNER_URL)
    if not valid_secret(TV_WEBHOOK_SECRET):
        raise RuntimeError("Configure a generated TV_WEBHOOK_SECRET before starting the feed")
    http = httpx.AsyncClient(timeout=10.0, trust_env=False, follow_redirects=False)
    loop = asyncio.get_running_loop()
    handles = []
    _setup_connection_handlers(ib)
    disconnected = asyncio.Event()
    ib.disconnectedEvent += disconnected.set
    try:
        await ib.connectAsync(TWS_HOST, TWS_PORT, clientId=TWS_CLIENT_ID)
        for symbol, tf_minutes in subscriptions:
            try:
                contract = await get_front_month(ib, symbol)
                handles.append(await subscribe_bars(ib, contract, symbol, tf_minutes, http, loop))
            except Exception:
                log.exception("subscription_failed symbol=%s tf=%s", symbol, tf_minutes)
        if not handles:
            raise RuntimeError("No subscriptions succeeded")
        log.info("ibkr_feed_ready mode=%s subscriptions=%d", mode, len(handles))
        await disconnected.wait()
    finally:
        for handle in handles:
            handle.delivery_task.cancel()
            ib.cancelHistoricalData(handle)
        await asyncio.gather(*(h.delivery_task for h in handles), return_exceptions=True)
        await http.aclose()
        ib.disconnect()
        log.info("ibkr_feed stopped")


# ──────────────────────────────────────────────────────────────────────────────
# Entry point
# ──────────────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="IBKR Live Algo Bar Feed — streams real-time bars from TWS "
                    "to the signal runner."
    )
    parser.add_argument(
        "--mode",
        choices=["PAPER", "LIVE"],
        default="PAPER",
        help=(
            "Trading mode. Selects which subscription list to open: "
            "PAPER uses PAPER_SUBSCRIPTIONS, LIVE uses LIVE_SUBSCRIPTIONS "
            "(both defined in contract_map.py). Default: PAPER"
        ),
    )
    args = parser.parse_args()

    asyncio.run(main(args.mode))

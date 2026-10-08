# IBKR Live Algo — Automated Futures Trading Infrastructure

Automated futures trading infrastructure for a personally funded broker account. Uses Interactive Brokers for both market data and order execution — a single broker, no third-party router required. TradersPost remains available as an optional fallback. No TradingView subscription required.

Stack: **IBKR TWS** (live bar data) → **Signal Runner** (strategy logic) → **Risk Server** (self-imposed risk checks) → **order execution** (direct via IBKR by default; TradersPost optional)

---

## Project Structure

```
IBKR Live Algo/
├── README.md                       ← you are here
├── .gitignore
├── ibkr_feed/
│   ├── ibkr_feed.py               ← connects to TWS, streams bars to signal runner
│   ├── contract_map.py            ← IBKR contract specs and per-mode subscriptions
│   ├── .env.example               ← template for IBKR feed secrets
│   └── README.md                  ← full IBKR setup guide
└── shared/
    ├── risk_module.py             ← FastAPI risk server (port 8000)
    ├── signal_runner.py           ← FastAPI bar receiver + strategy dispatcher (port 8001)
    ├── signal_base.py             ← base class for strategy signal generators
    ├── live_feed.py               ← BarStore: in-memory bar history with persistence
    ├── indicators.py              ← EMA, RSI, ATR, VWAP, Bollinger, ADX
    ├── news_updater.py            ← weekly high-impact news calendar refresh script
    ├── news_calendar.yaml         ← optional news-event blackout schedule
    ├── account_config.yaml        ← risk parameters and server config
    ├── requirements.txt           ← all Python dependencies
    └── .env.example               ← template for shared secrets
```

Strategies live in their own folders at the project root alongside `shared/` and `ibkr_feed/`. See [Adding a Strategy](#adding-a-strategy) below.

---

## How It Works

IBKR TWS streams real-time OHLCV bars to `ibkr_feed.py`, which POSTs each completed bar to the signal runner's `/tv-bar/{symbol}/{tf_minutes}` endpoint. The signal runner pushes bars into an in-memory `BarStore` and dispatches them to all registered signal generators. When a generator detects an entry condition, it POSTs a signal to the risk server. The risk server runs your self-imposed pre-trade checks (drawdown floor, session window, news blackout, position cap) and, if accepted, places the order. By default it executes directly through Interactive Brokers (`order_router: ibkr`) — resolving the contract and submitting a bracket order (entry + stop + target); a TradersPost webhook remains available as an optional fallback (`order_router: traderspost`).

Every risk check is your own choice and can be disabled — there are no externally imposed trading rules in this system.

IBKR provides 2 days of historical bars on startup, so indicators are warm from the very first live bar — no manual seeding or warmup delay.

---

## Setup

### 1. Install dependencies

```bash
cd "IBKR Live Algo"

python -m venv .venv
.venv\Scripts\activate

pip install --require-hashes --only-binary=:all: -r shared/requirements.lock --prefer-binary
```

### 2. Configure secrets

```bash
copy shared\.env.example shared\.env
notepad shared\.env
```

Generate two random secrets (run twice):
```bash
python -c "import secrets; print(secrets.token_hex(32))"
```

Set:
- `RISK_SERVER_API_KEY` — protects all risk server endpoints
- `TV_WEBHOOK_SECRET` — authenticates bar POSTs from ibkr_feed to the signal runner
- `TP_WEBHOOK_<STRATEGY_ID>` — one per strategy, from your TradersPost subscription page

```bash
copy ibkr_feed\.env.example ibkr_feed\.env
notepad ibkr_feed\.env
```

Set `TV_WEBHOOK_SECRET` to the same value as above, and configure your TWS connection settings.

Alternatively, set all variables as Windows environment variables — no `.env` files needed.

### 3. Configure the risk server

Edit `shared/account_config.yaml`:
- Set `mode` to `PAPER` while testing, `LIVE` when you're ready to trade real money
- Set `account.account_size` to your account's starting/reference balance
- Set `risk_limits.max_drawdown` to your own max drawdown limit (or `0` to disable)
- Set `risk_limits.max_open_contracts` to your own position cap (or `0` for unlimited)
- Set `broker.account_label` to any label you like (used only in log lines)

All risk limits are optional self-imposed guards; set any of them to `0` to turn them off.

### 4. Set up TWS

See `ibkr_feed/README.md` for full TWS configuration instructions including enabling the API socket, disabling the connection dialog, and configuring auto-logoff.

### 5. Order execution

By default (`order_router: ibkr`) orders are placed directly through Interactive Brokers — the same TWS/Gateway used for data, over a separate API client id (`TWS_EXEC_CLIENT_ID`, default 2). Nothing extra to set up: just confirm `mode`, `TWS_HOST`, `TWS_PORT`, and `TWS_EXEC_CLIENT_ID` in `shared/.env`. PAPER mode refuses to connect to a live-trading port as a safety guard, and strategies are expected to supply absolute `stop_loss` / `take_profit` prices in their signals.

**Optional — TradersPost fallback** (`order_router: traderspost`): for each strategy, create a TradersPost strategy, connect it to your broker, and add its webhook URL to `shared/.env` as `TP_WEBHOOK_<STRATEGY_ID>`.

### 6. Run

Open three terminals, all with `.venv` activated:

**Terminal 1 — risk server:**
```
cd "IBKR Live Algo\shared"
uvicorn risk_module:app --host 127.0.0.1 --port 8000
```

**Terminal 2 — signal runner:**
```
cd "IBKR Live Algo\shared"
uvicorn signal_runner:app --host 127.0.0.1 --port 8001
```

**Terminal 3 — IBKR bar feed:**
```
cd "IBKR Live Algo\ibkr_feed"
python ibkr_feed.py --mode PAPER
```

Verify everything is running:
```bash
curl -H "X-Api-Key: YOUR_RISK_ADMIN_API_KEY" http://localhost:8000/health
curl -H "Authorization: Bearer YOUR_TV_WEBHOOK_SECRET" http://localhost:8001/health
```
The signal runner returns `{"status":"ok"}` without the header; the full detail (mode, strategies, `bar_counts`) requires the `Authorization` bearer token so it isn't exposed openly.

---

## Adding a Strategy

Create a new folder at the project root alongside `shared/`:

```
IBKR Live Algo/
└── strategy_01_MY_STRATEGY_NAME/
    ├── signal_generator.py   ← strategy logic
    └── risk_config.yaml      ← strategy-specific parameters
```

**`risk_config.yaml` minimum required fields:**
```yaml
strategy_id: MY_STRATEGY_NAME    # must match TP_WEBHOOK_MY_STRATEGY_NAME env var
mode: PAPER                      # PAPER | LIVE
live_symbol: MNQM6               # current front-month contract symbol
session_start: "0930"
session_end:   "1610"
risk_usd: 200
tp_multiplier: 2.0
daily_loss_kill: 500             # per-strategy daily loss limit (0 = disabled)
contracts: 2
bar_size: 5
tick_size: 0.25
```

**`signal_generator.py` minimum structure:**
```python
import sys, pathlib
sys.path.insert(0, str(pathlib.Path(__file__).parent.parent / "shared"))

from signal_base import SignalGenerator
from indicators import ema, rsi, atr

class MyStrategy(SignalGenerator):
    strategy_id  = "MY_STRATEGY_NAME"
    bar_size     = 5       # primary timeframe in minutes
    htf_bar_size = None    # set to e.g. 15 for a multi-timeframe strategy

    def check_signal(self, df, htf_df=None):
        # df.iloc[-1] is the last closed bar; timestamps label bar opens
        # return a signal dict to fire, or None to skip
        return None
```

The signal runner auto-discovers all `strategy_*/` folders at startup. By default it loads every strategy it finds; to run only a subset in a given mode, list the strategy numbers in `MODE_STRATEGIES` in `signal_runner.py`.

---

## Maintenance

**Weekly:** run `python shared/news_updater.py` to refresh the high-impact news calendar.

**On contract roll:** stop `ibkr_feed.py`, restart it — it auto-resolves the new front month. Update `live_symbol` in each strategy's `risk_config.yaml` to the new contract code.

**Syncing account state:** after reviewing your broker / IBKR account, call:
```bash
curl -X POST http://localhost:8000/set_state \
  -H "X-Api-Key: YOUR_RISK_ADMIN_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{"balance": 51500.00, "open_positions": 0}'
```
If you use a trailing drawdown floor, raise `drawdown_floor` here as your balance grows to lock in gains.

**Each morning:** reset intraday counters before the session opens:
```bash
curl -X POST http://localhost:8000/reset_day \
  -H "X-Api-Key: YOUR_RISK_ADMIN_API_KEY"
```

---

## Roadmap

**Direct IBKR order execution — implemented (validate on paper first).** Orders are placed directly through Interactive Brokers via `shared/ibkr_executor.py`, selected by `order_router: ibkr`. It qualifies the exact contract supplied by the feed, submits bracket orders (entry + stop + target as an OCA group), flattens on exit, and reads positions/balance back from IBKR to keep the risk server in sync. TradersPost remains available as a fallback.

Before trusting it with real money:

- Run it against a **paper** account (`mode: PAPER`, `TWS_PORT=7497`) and confirm orders, brackets, and fills behave as expected.
- Verify `ib_insync` runs cleanly inside the uvicorn event loop on your machine; if you hit "event loop already running" errors, `pip install nest_asyncio` and call `nest_asyncio.apply()` at the top of `risk_module.py`.

Still open:

- Handle partial fills, reversals, and order rejections explicitly (positions are currently re-read from IBKR after each order rather than tracked incrementally).
- Subscribe to live fill/position events to update risk state continuously, not only after a submitted order.
- Optionally collapse the three local processes into one runtime, sharing a single IBKR connection for data and execution and dropping the inter-process HTTP hops.


## Debugging update — October 2, 2026

Offline regression tests (run from the project root):

```powershell
.\.venv\Scripts\python.exe -X utf8 -B -m unittest discover -s tests -v
```

These tests mock IBKR and HTTP routing and never connect to a broker. Use
`python -X utf8` for replay commands on Windows to avoid console encoding errors.

The feed requests timezone-aware UTC historical data asynchronously. It seeds
`/history/{symbol}/{tf}` before delivering buffered live bar closes; history
does not dispatch signals. Both live and replay use the last closed bar at
`df.iloc[-1]`. Signals older than the configured freshness threshold or dated
in the future are skipped. Duplicate timestamps replace history rather than
inflating it; generators deduplicate decisions and retry transport errors.

The runner polls an independent one-second exit timer, so the 15:55 ET close
does not depend on the next market-data bar. Entries reconcile broker position,
working orders, and today's executions before evaluating strategy state.
Accepted submission is distinct from a confirmed fill. Failed entry/close
attempts do not permanently consume the strategy's attempt flag. Active close
orders suppress duplicate exits; cancelled/rejected closes can be retried.

Set `IBKR_ACCOUNT_ID` in `shared/.env` when more than one account is managed by
TWS. Orders, positions, balance, P&L and execution history consistently use that
account. PAPER execution requires a paper port and a DU account. Entries require
reconciled positions/executions and fresh balance/P&L (120-second freshness
limit); missing or invalid data blocks entries. Outstanding entry parent orders
reserve their full quantity in addition to filled positions, conservatively
double-counting partial fills until the parent completes. Bracket legs carry
the selected account; an incomplete submission cancels created legs and blocks
new entries pending reconciliation. Exits wait for cancellation confirmation,
refresh positions, then submit a single close order per contract.

The feed supplies the actual resolved contract ticker, including after rolls.
The executor qualifies that exact ticker and never silently substitutes another
expiry. A recovered open position supplies its own ticker for closure. Configured
`live_symbol` still supplies the base instrument; keep it current for external
signals and clarity. Contract selection remains nearest non-expired expiry,
not a liquidity-based roll rule. Feed/executor disconnects require a restart;
automatic reconnection is not implemented.

Optional TradersPost routing still needs manual broker synchronization. Only a
`/set_state` body containing balance, day_realized and open_positions refreshes
entry readiness, for 60 seconds; partial settings updates do not. Local counts
change after successful forwarding using the effective clipped quantity, but
forwarding does not prove a broker fill. Direct IBKR is the tested default path.

Refresh the enabled news calendar before a paper run: the checked-in events are
from May/June 2026 and will not block October events. No trading parameters,
credentials, calendars or deployment settings were changed in this debugging
pass. Paper integration still needs to verify actual bracket/OCA behavior,
partial fills, account updates, cancellation acknowledgments and reconnect
recovery. Existing performance reports predate the corrected replay logic and
must be regenerated before comparing strategy results.


## Security and publication

Read [SECURITY.md](SECURITY.md) before starting services or making a public repo.
The APIs accept local clients and local Host headers only; documentation/schema
routes are disabled. Never expose them through ngrok or an internet reverse
proxy. A proxy running locally would also appear local, so middleware does not
make such a deployment safe. Authentication is mandatory in development too.
Generate independent secrets with `python -c "import secrets; print(secrets.token_hex(32))"`
and put them only in ignored local `.env` files. Example keys are intentionally
blank; copied placeholder values cannot start the services. `RISK_ADMIN_API_KEY`
is separate from `RISK_SERVER_API_KEY` and protects account-state writes.

Offline audits and tests must never place real orders. No credentials are
needed to run tests. Install the tested dependency set with:

```powershell
python -m pip --isolated install --require-hashes --only-binary=:all: -r shared/requirements.lock
python -X utf8 -B -m unittest discover -s tests -v
python tools/publication_guard.py --tree
```

After initializing Git, run `python tools/publication_guard.py` to inspect the
Git index and reachable commit history before pushing. The tree-only mode
checks candidate public files and excludes private local files; it cannot
prove what Git will publish. GitHub CI adds tests, that publication guard and
known-vulnerability checks without any broker credentials. Configure repository
secret scanning/push protection and require this workflow to pass before merges.
Generated reports, data CSVs, caches, credentials and backups are excluded from
the public candidate by default. Review market-data redistribution rights before
including any dataset. Do not publish the working directory as an unfiltered ZIP.

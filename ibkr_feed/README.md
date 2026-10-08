# IBKR Bar Feed

Connects to Interactive Brokers TWS running on your PC, subscribes to real-time bar streams for all required futures symbols, and POSTs each completed bar to the signal runner's `/tv-bar/` endpoint. This is the data source for the entire system — no TradingView subscription required.

---

## How it fits into the stack

```
TWS (running on your PC)
        │  real-time bar stream via IBKR API
        ▼
ibkr_feed.py
        │  POST {"time","open","high","low","close","volume"}
        │  Authorization: Bearer <TV_WEBHOOK_SECRET>
        ▼
signal_runner.py  (port 8001)
        │
        ▼
risk_module.py → TradersPost → broker execution
```

IBKR also provides 2 days of historical bars automatically on startup, so indicators are warm and ready to evaluate signals from the first bar — no manual seeding required.

---

## Prerequisites

### 1. Interactive Brokers account

A live account at interactivebrokers.com. IBKR has no minimum deposit requirement. This account is used for market data only — order execution still routes through TradersPost to your broker. (A direct-to-IBKR execution path is planned; see the project README roadmap.)

### 2. Trader Workstation (TWS)

Download TWS from interactivebrokers.com/en/trading/tws.php. TWS must be running and logged in whenever the bar feed is active. IB Gateway is a lighter alternative (no GUI) and works identically — recommended for a dedicated trading machine.

### 3. Market data subscriptions

Subscribe inside Account Management → Settings → Market Data Subscriptions.

| Instrument | Exchange | Subscription name | Monthly cost (non-pro) |
|---|---|---|---|
| MNQ, MES, M2K | CME | US Equity Index Futures (HMDS) | ~$4.50 |
| MGC | COMEX | COMEX Metals Futures | ~$4.50 |
| MCL | NYMEX | NYMEX Energy Futures | ~$4.50 |

Subscribe only to the exchanges covering the symbols your strategies trade. Total cost is typically $9–14/month.

---

## TWS Configuration

### Enable the API

1. **Edit → Global Configuration → API → Settings**
2. Check **Enable ActiveX and Socket Clients**
3. Set **Socket port** to `7496` (live) or `7497` (paper)
4. Check **Allow connections from localhost only**
5. Uncheck **Read-Only API**
6. Click **OK** and restart TWS

### Disable the connection confirmation dialog

1. **Edit → Global Configuration → API → Settings**
2. Uncheck **Show a dialog when I connect an API client**

### Prevent auto-logoff

1. **Edit → Global Configuration → Lock and Exit**
2. Set **Auto log off timer** to a time after your trading day, or disable it

Note: IBKR performs daily server maintenance at approximately 23:45–00:15 ET on weekdays. TWS disconnects briefly during this window. The script logs the disconnect and stops posting bars — restart ibkr_feed.py after the window if running overnight. For uninterrupted 24/7 operation, run IB Gateway on a VPS with auto-reconnect configured.

---

## Installation

From the project root:

```bash
cd Trade-Algo

# Create a virtual environment
python -m venv .venv
.venv\Scripts\activate

# Install all dependencies (shared + IBKR)
pip install -r shared/requirements.txt --prefer-binary
```

### Environment variables

```bash
copy ibkr_feed\.env.example ibkr_feed\.env
notepad ibkr_feed\.env
```

Fill in `TV_WEBHOOK_SECRET` (must match the value in `shared/.env` or your Windows environment variables) and verify `TWS_PORT` matches your TWS configuration.

---

## Running

Start servers in this order:

**Terminal 1 — risk server:**
```
cd Trade-Algo\shared
uvicorn risk_module:app --host 127.0.0.1 --port 8000
```

**Terminal 2 — signal runner:**
```
cd Trade-Algo\shared
uvicorn signal_runner:app --host 127.0.0.1 --port 8001
```

**Terminal 3 — IBKR bar feed:**
```
cd Trade-Algo\ibkr_feed
python ibkr_feed.py --mode PAPER
```

Replace `--mode` with whatever mode matches your `account_config.yaml`. The mode controls which symbol/timeframe subscriptions are opened.

### Expected startup output

```
ibkr_feed: connecting  host=127.0.0.1  port=7497  client_id=1
ibkr_feed: connected to TWS  account=[<PRIVATE_ACCOUNT_ID>]
ibkr_feed: contract_resolved  symbol=MNQ   local=MNQH5   expiry=2025-03-21
ibkr_feed: subscribed  symbol=MNQ   tf= 5m  contract=MNQH5  history=576 bars
ibkr_feed: ibkr_feed_ready  mode=PAPER  subscriptions=4
```

The `history=576 bars` line confirms IBKR loaded 2 days of historical bars — indicators will be warm from bar 1.

### Verifying bars are reaching the signal runner

```bash
curl -H "Authorization: Bearer YOUR_TV_WEBHOOK_SECRET" http://localhost:8001/health
```

Check `bar_counts` — non-zero values confirm bars are flowing. (The bearer token is required to see the detail; without it `/health` returns only `{"status":"ok"}`.)

---

## Contract rolls

Futures contracts expire and must be rolled to the new front month.

**When to roll:**
- CME equity index micros (MNQ, MES, M2K): third Friday of March, June, September, December
- MGC, MCL: monthly — check IBKR contract details for exact dates

**How to roll:** stop ibkr_feed.py and restart it. It resolves the front month automatically on startup and will pick up the new contract.

Watch for IBKR error code 200 in the logs ("No security definition found") — this signals that a contract has expired and the feed for that symbol has stopped.

---

## Troubleshooting

**`Cannot connect to TWS at 127.0.0.1:7497`**
TWS is not running, not logged in, or the API socket is not enabled. Verify the TWS Configuration steps above. Check the port — paper accounts use 7496.

**`No contracts returned for [symbol]`**
Missing market data subscription for that exchange, or TWS is in read-only mode.

**`DELAYED_DATA` warning**
Market data subscription is on the delayed tier. Switch to the real-time version in Account Management.

**Signal runner returns 403**
`TV_WEBHOOK_SECRET` in ibkr_feed does not match the value in the signal runner. Both must be identical.

**Signal runner returns 503**
`RISK_SERVER_API_KEY` is not set. Ensure the risk server started with the key configured.

**No signals firing despite bars arriving**
Check the signal runner terminal for `skip —` log lines from your strategy's signal generator. These show exactly which filter is rejecting each bar.

**Duplicate client ID error**
Change `TWS_CLIENT_ID` in `ibkr_feed/.env` to a number not used by any other API client connected to TWS.


Security update: only local API access is supported; use independently generated
keys in ignored `.env` files. `TV_WEBHOOK_SECRET` must match the runner's key.
Do not expose either server through a tunnel or reverse proxy. Read SECURITY.md
at the project root before deployment/publication. Expected account log labels
above are placeholders; real account IDs and logs must remain private.

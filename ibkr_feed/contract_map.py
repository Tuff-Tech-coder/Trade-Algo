"""
IBKR contract specifications for the micro futures traded by this project.

Exchange codes:
  CME   — Chicago Mercantile Exchange  (equity index micros: MNQ, MES, M2K)
  COMEX — Commodity Exchange           (metals: MGC)
  NYMEX — New York Mercantile Exchange (energy: MCL)

Point values and tick sizes are provided for reference only — the signal
generators in the main project use their own risk_config.yaml values.
"""

# ── Contract specifications ───────────────────────────────────────────────────

CONTRACTS: dict[str, dict] = {
    "MNQ": {
        "exchange":    "CME",
        "currency":    "USD",
        "name":        "Micro E-mini Nasdaq-100",
        "tick_size":   0.25,
        "point_value": 2.0,
    },
    "MES": {
        "exchange":    "CME",
        "currency":    "USD",
        "name":        "Micro E-mini S&P 500",
        "tick_size":   0.25,
        "point_value": 5.0,
    },
    "M2K": {
        "exchange":    "CME",
        "currency":    "USD",
        "name":        "Micro E-mini Russell 2000",
        "tick_size":   0.10,
        "point_value": 5.0,
    },
    "MGC": {
        "exchange":    "COMEX",
        "currency":    "USD",
        "name":        "Micro Gold",
        "tick_size":   0.10,
        "point_value": 10.0,
    },
    "MCL": {
        "exchange":    "NYMEX",
        "currency":    "USD",
        "name":        "Micro WTI Crude Oil",
        "tick_size":   0.01,
        "point_value": 100.0,
    },
}

# ── Subscriptions by mode ─────────────────────────────────────────────────────
# Each entry: (base_symbol, tf_minutes). The active mode (PAPER or LIVE) decides
# which list ibkr_feed.py subscribes to. They are independent so you can stream
# extra symbols while paper-testing without touching your live set — edit freely.

PAPER_SUBSCRIPTIONS: list[tuple[str, int]] = [
    ("MNQ", 5),
    ("MES", 5),
    ("M2K", 5),
    ("MGC", 15),
]

LIVE_SUBSCRIPTIONS: list[tuple[str, int]] = [
    ("MNQ", 5),
    ("MES", 5),
    ("M2K", 5),
    ("MGC", 15),
]

# ── IBKR bar size strings ─────────────────────────────────────────────────────
# Maps tf_minutes → IBKR barSizeSetting string used in reqHistoricalData.

BAR_SIZE_STRINGS: dict[int, str] = {
    1:  "1 min",
    2:  "2 mins",
    3:  "3 mins",
    5:  "5 mins",
    10: "10 mins",
    15: "15 mins",
    30: "30 mins",
    60: "1 hour",
}

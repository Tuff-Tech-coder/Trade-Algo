# Backtest

Event-driven backtester that runs the project's **live** strategies (the
`SignalGenerator` subclasses in `strategy_NN_*/`) on historical 1-minute data —
so the code you test is the code you trade. Adapted from a prop-firm harness,
with the evaluation model removed and the engine reframed to plain performance.

## Layout

```
backtest/
├── engine/
│   ├── bar.py            ← OHLCV bar
│   ├── orders.py         ← Order type
│   ├── data_loader.py    ← CSV loader (timestamp,open,high,low,close,volume) + synthetic
│   ├── broker.py         ← SimBroker: next-bar fills, adverse slippage, commission, brackets
│   ├── constraints.py    ← optional NewsBlackout / ContractLimit
│   ├── account.py        ← plain Account (no eval; optional self-imposed limits)
│   ├── loop.py           ← BacktestEngine event loop + RunResult
│   └── metrics.py        ← performance metrics + console formatter
├── adapter.py            ← drives a live SignalGenerator inside the engine (keystone)
└── run.py                ← CLI runner
```

## Usage

```bash
python backtest/run.py --strategy-dir strategy_01_MES_INTRADAY_MOMENTUM --instrument MES
```

By default it measures the strategy's **raw edge** (no self-imposed limits) and
reads `backtest/data/<instrument>_1m.csv` (see `data/README.md`). During the
transition it also falls back to the legacy `mffu_backtest/data/` if present.
Useful flags:

- `--limits` apply the strategy's `daily_loss_kill`
- `--max-drawdown 2000` add a drawdown halt
- `--data PATH` use a specific CSV; `--start/--end` to slice the period
- `--commission 1.40 --slippage-ticks 1` cost assumptions
- `--news PATH` enable a high-impact-news blackout
- `--mc-mode bootstrap|shuffle|none` and `--mc-trials 2000` — Monte Carlo over the
  trade sequence (default: bootstrap, 2000 trials). `shuffle` isolates path/drawdown
  risk; `bootstrap` adds sampling uncertainty (final P&L distribution).

Results — `metrics.json`, `equity_curve.csv`, and a self-contained `report.html`
(equity curve + performance table + Monte Carlo distributions, inline SVG, no
external dependencies) — are written to `backtest/reports/<timestamp>_<strategy>/`.

## Sub-period testing

`run.py --start/--end` runs one custom window. To check consistency *across* the
year, use the period walker, which slices the data and tabulates each piece (with
a warmup buffer so indicators aren't cold-started):

```bash
python backtest/run_periods.py --strategy-dir strategy_01_MES_INTRADAY_MOMENTUM --instrument MES --by quarter
python backtest/run_periods.py --strategy-dir strategy_01_MES_INTRADAY_MOMENTUM --instrument MES --by month
```

`--by month|quarter|week|chunks` (with `--chunks N`), `--warmup-days 7`. Shorter
periods are noisier — read the trend across rows, not any single cell. A summary
CSV lands in `backtest/reports/`.

## Faithful-to-live + caveats

The adapter aggregates 1-min bars into the strategy's timeframe (clock-aligned),
calls `check_signal` with `iloc[-1]` representing the just-completed bar, as in
`signal_base.on_bar`, and resets per-session state off **bar time**. Overnight
bars warm the same bounded 300-bar history without triggering entries. Only
complete higher-timeframe buckets are exposed. The engine forces flat at the
close of the 15:54 minute (15:55 ET); live uses an independent wall-clock timer.
Broker latency, exchange queueing, partial fills, and disconnects are not
reproduced by the simulator.

Read results for **relative comparison**, not gospel absolute returns — fills
assume next-bar execution with 1-tick adverse slippage and a flat commission.

## Next steps

- Full-engine bootstrap (resampling whole bar *sessions* and re-running the
  strategy) for a richer distribution than trade-level resampling.
- Retiring `mffu_backtest/` entirely: move its CSVs into `backtest/data/`, then
  delete the folder (it carries a nested `.git`, the TradersPost/Tradovate live
  bridge, and stale reports). `run.py` already prefers `backtest/data/`.

"""
Walk a strategy across consecutive sub-periods of the data (month / quarter /
week / N chunks) and tabulate performance per period — so you can see whether an
edge is consistent through the year or whether the full-year number hides wild
swings.

Each period gets a warmup buffer: bars from before the period start are fed to
warm the indicators (ATR, the volatility baseline), but trades are only counted
from the period start onward (via the engine's trade_start).

Examples:
    py backtest/run_periods.py --strategy-dir strategy_01_MES_INTRADAY_MOMENTUM --instrument MES --by quarter
    py backtest/run_periods.py --strategy-dir strategy_01_MES_INTRADAY_MOMENTUM --instrument MES --by month
    py backtest/run_periods.py --strategy-dir strategy_01_MES_INTRADAY_MOMENTUM --by chunks --chunks 6
"""
from __future__ import annotations

import argparse
import csv
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytz

THIS_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = THIS_DIR.parent
sys.path.insert(0, str(THIS_DIR))
sys.path.insert(0, str(PROJECT_ROOT / "shared"))

import run as _run  # reuse _load_generator, POINT_VALUE, _base_symbol (+ path setup)  # noqa: E402
from engine import (  # noqa: E402
    Account, BacktestEngine, SimBroker, compute_metrics, load_bars, run_mc,
)
from adapter import SignalGeneratorAdapter  # noqa: E402

ET = pytz.timezone("America/New_York")


def _periods(et_dates, by: str, chunks: int):
    """Return a list of (label, start_date, end_date) sub-periods."""
    et_dates = sorted(et_dates)
    if not et_dates:
        return []

    if by == "chunks":
        n = max(1, chunks)
        size = (len(et_dates) + n - 1) // n
        out = []
        for i in range(0, len(et_dates), size):
            block = et_dates[i:i + size]
            if block:
                out.append((f"{block[0]}..{block[-1]}", block[0], block[-1]))
        return out

    groups: dict = {}
    order: list = []
    for d in et_dates:
        if by == "month":
            key, label = (d.year, d.month), f"{d.year}-{d.month:02d}"
        elif by == "week":
            iso = d.isocalendar()
            key, label = (iso[0], iso[1]), f"{iso[0]}-W{iso[1]:02d}"
        else:  # quarter
            q = (d.month - 1) // 3 + 1
            key, label = (d.year, q), f"{d.year}-Q{q}"
        if key not in groups:
            groups[key] = [label, d, d]
            order.append(key)
        else:
            groups[key][2] = d
    return [(groups[k][0], groups[k][1], groups[k][2]) for k in order]


def _resolve_data(instrument: str, args) -> Path:
    if args.data:
        return Path(args.data)
    fname = f"{instrument.lower()}_1m.csv"
    if args.data_dir:
        return Path(args.data_dir) / fname
    return THIS_DIR / "data" / fname


def main() -> None:
    ap = argparse.ArgumentParser(description="Run a strategy across sub-periods")
    ap.add_argument("--strategy-dir", required=True)
    ap.add_argument("--instrument")
    ap.add_argument("--data")
    ap.add_argument("--data-dir")
    ap.add_argument("--start")
    ap.add_argument("--end")
    ap.add_argument("--by", choices=["month", "quarter", "week", "chunks"], default="quarter")
    ap.add_argument("--chunks", type=int, default=4)
    ap.add_argument("--warmup-days", type=int, default=7)
    ap.add_argument("--equity", type=float, default=50_000.0)
    ap.add_argument("--commission", type=float, default=1.40)
    ap.add_argument("--slippage-ticks", type=int, default=1)
    ap.add_argument("--mc-trials", type=int, default=2000)
    args = ap.parse_args()

    strategy_dir = (PROJECT_ROOT / args.strategy_dir
                    if not Path(args.strategy_dir).is_absolute() else Path(args.strategy_dir))
    _, risk_cfg = _run._load_generator(strategy_dir)
    sid = risk_cfg.get("strategy_id", "strategy")
    instrument = (args.instrument or _run._base_symbol(risk_cfg["live_symbol"])).upper()
    tick_size = float(risk_cfg.get("tick_size", 0.25))
    if instrument not in _run.POINT_VALUE:
        raise SystemExit(f"Unknown point value for {instrument}; add it to POINT_VALUE in run.py")
    tick_value = tick_size * _run.POINT_VALUE[instrument]

    data_path = _resolve_data(instrument, args)
    if not data_path.exists():
        raise SystemExit(f"Data not found: {data_path}")
    print(f"Loading {data_path} ...")
    bars = load_bars(str(data_path), start=args.start, end=args.end)
    et_date = bars["timestamp"].dt.tz_convert(ET).dt.date
    periods = _periods(set(et_date), args.by, args.chunks)

    print(f"\n{sid} / {instrument}   by={args.by}   periods={len(periods)}   "
          f"warmup={args.warmup_days}d   (limits off)\n")
    hdr = (f"{'period':<16}{'trades':>7}{'win%':>8}{'PF':>7}{'exp$':>9}"
           f"{'P&L$':>10}{'maxDD$':>10}{'P(profit)':>11}")
    print(hdr)
    print("-" * len(hdr))

    summary = []
    for label, p_start, p_end in periods:
        warm_start = p_start - timedelta(days=args.warmup_days)
        mask = (et_date >= warm_start) & (et_date <= p_end)
        slice_bars = bars[mask].reset_index(drop=True)
        if slice_bars.empty:
            continue

        gen, _ = _run._load_generator(strategy_dir)  # fresh instance → fresh state
        acct = Account(starting_equity=args.equity)
        broker = SimBroker(instrument, tick_size, tick_value,
                           commission_per_rt=args.commission, slippage_ticks=args.slippage_ticks)
        engine = BacktestEngine(slice_bars, SignalGeneratorAdapter(gen), acct, broker,
                                rth_only=True, trade_start=p_start)
        res = engine.run()
        m = compute_metrics(res)
        mc = run_mc(res.trade_pnls, starting_equity=args.equity, trials=args.mc_trials, mode="bootstrap")

        pf = m["profit_factor"]
        pf_s = "inf" if pf == float("inf") else f"{pf:.2f}"
        pprof = f"{mc['prob_profit'] * 100:>9.1f}%" if mc else f"{'n/a':>10}"
        print(f"{label:<16}{m['trades']:>7}{m['win_rate'] * 100:>7.1f}%{pf_s:>7}"
              f"{m['expectancy_per_trade']:>9.2f}{m['total_pnl']:>10.0f}"
              f"{m['max_drawdown_dollar']:>10.0f}{pprof}")
        summary.append({
            "period": label, "trades": m["trades"], "win_rate": m["win_rate"],
            "profit_factor": pf if pf != float("inf") else "",
            "expectancy": m["expectancy_per_trade"], "total_pnl": m["total_pnl"],
            "max_drawdown": m["max_drawdown_dollar"],
            "prob_profit": (mc["prob_profit"] if mc else ""),
        })

    print("-" * len(hdr))
    print(f"each period is independent with a {args.warmup_days}-day indicator warmup; "
          f"shorter periods are noisier — read the trend across rows, not any one cell.")

    if summary:
        tag = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
        out = THIS_DIR / "reports" / f"{tag}_{sid}_periods_{args.by}.csv"
        out.parent.mkdir(parents=True, exist_ok=True)
        with open(out, "w", newline="") as f:
            w = csv.DictWriter(f, fieldnames=list(summary[0].keys()))
            w.writeheader()
            w.writerows(summary)
        print(f"Saved → {out}")


if __name__ == "__main__":
    main()

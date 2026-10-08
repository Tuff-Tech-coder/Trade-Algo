"""
Backtest runner — drives a LIVE strategy through the backtest engine on
historical 1-minute data, so you test the exact code you trade.

Example:
    python backtest/run.py \
        --strategy-dir strategy_01_MES_INTRADAY_MOMENTUM \
        --instrument MES

By default it measures the strategy's RAW edge (no self-imposed limits). Pass
--limits to apply the strategy's daily_loss_kill, and --max-drawdown to add a
drawdown halt. Data is read from --data, or <data-dir>/<instrument>_1m.csv.
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

import yaml

THIS_DIR = Path(__file__).resolve().parent          # .../backtest
PROJECT_ROOT = THIS_DIR.parent                       # project root
sys.path.insert(0, str(THIS_DIR))                    # so `import engine...` works
sys.path.insert(0, str(PROJECT_ROOT / "shared"))     # so the strategy can import signal_base

from engine import (  # noqa: E402
    Account, BacktestEngine, ContractLimit, NewsBlackout, SimBroker,
    compute_metrics, format_metrics, load_bars, render_html, run_mc,
)
from adapter import SignalGeneratorAdapter  # noqa: E402

# CME/COMEX/NYMEX micro point values ($ per 1.00 move). tick_value = tick * point.
POINT_VALUE = {"MES": 5.0, "MNQ": 2.0, "M2K": 5.0, "MYM": 0.5, "MGC": 10.0, "MCL": 100.0}
_MONTH_CODES = "FGHJKMNQUVXZ"


def _base_symbol(ticker: str) -> str:
    return re.sub(rf"[{_MONTH_CODES}]\d{{1,2}}$", "", ticker.upper())


def _load_generator(strategy_dir: Path):
    """Dynamically load the strategy's SignalGenerator subclass + its risk_config."""
    sg_path = strategy_dir / "signal_generator.py"
    rc_path = strategy_dir / "risk_config.yaml"
    if not sg_path.exists() or not rc_path.exists():
        raise FileNotFoundError(f"{strategy_dir} must contain signal_generator.py and risk_config.yaml")

    with open(rc_path) as f:
        risk_cfg = yaml.safe_load(f)

    from signal_base import SignalGenerator  # noqa: E402

    spec = importlib.util.spec_from_file_location("bt_strategy_sg", sg_path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)

    gen_cls = None
    for attr in vars(mod).values():
        if isinstance(attr, type) and issubclass(attr, SignalGenerator) and attr is not SignalGenerator:
            gen_cls = attr
            break
    if gen_cls is None:
        raise RuntimeError(f"No SignalGenerator subclass found in {sg_path}")

    main_cfg = {"risk_server": {"webhook_url": "http://localhost:8000/webhook"}}
    return gen_cls(risk_cfg, main_cfg), risk_cfg


def main() -> None:
    ap = argparse.ArgumentParser(description="Backtest a live strategy on historical data")
    ap.add_argument("--strategy-dir", required=True, help="Path to a strategy_NN_* folder")
    ap.add_argument("--instrument", help="Symbol base (e.g. MES). Default: derived from live_symbol")
    ap.add_argument("--data", help="Explicit CSV path. Overrides --data-dir/--instrument")
    ap.add_argument("--data-dir", default=None,
                    help="Folder with <instrument>_1m.csv (default: backtest/data, then legacy mffu_backtest/data)")
    ap.add_argument("--start", default=None)
    ap.add_argument("--end", default=None)
    ap.add_argument("--equity", type=float, default=50_000.0, help="Starting account equity")
    ap.add_argument("--commission", type=float, default=1.40, help="Round-trip commission per contract")
    ap.add_argument("--slippage-ticks", type=int, default=1)
    ap.add_argument("--limits", action="store_true", help="Apply the strategy's daily_loss_kill")
    ap.add_argument("--max-drawdown", type=float, default=0.0, help="Drawdown halt in $ (0 = off)")
    ap.add_argument("--max-contracts", type=int, default=0, help="Clamp order size (0 = unlimited)")
    ap.add_argument("--news", default=None, help="Optional news_calendar.csv for blackouts")
    ap.add_argument("--mc-mode", choices=["bootstrap", "shuffle", "none"], default="bootstrap",
                    help="Monte Carlo mode (default: bootstrap; 'none' to skip)")
    ap.add_argument("--mc-trials", type=int, default=2000)
    args = ap.parse_args()

    strategy_dir = (PROJECT_ROOT / args.strategy_dir) if not Path(args.strategy_dir).is_absolute() else Path(args.strategy_dir)
    gen, risk_cfg = _load_generator(strategy_dir)

    instrument = (args.instrument or _base_symbol(risk_cfg["live_symbol"])).upper()
    tick_size = float(risk_cfg.get("tick_size", 0.25))
    if instrument not in POINT_VALUE:
        raise SystemExit(f"Unknown point value for {instrument}; add it to POINT_VALUE in run.py")
    tick_value = tick_size * POINT_VALUE[instrument]

    if args.data:
        data_path = Path(args.data)
    else:
        fname = f"{instrument.lower()}_1m.csv"
        if args.data_dir:
            data_path = Path(args.data_dir) / fname
        else:
            # Prefer backtest/data; fall back to the legacy mffu_backtest/data.
            candidates = [THIS_DIR / "data" / fname, PROJECT_ROOT / "mffu_backtest" / "data" / fname]
            data_path = next((c for c in candidates if c.exists()), candidates[0])
    if not data_path.exists():
        raise SystemExit(
            f"Data not found: {data_path}\n"
            f"  Put {instrument.lower()}_1m.csv in backtest/data/ (or pass --data / --data-dir)."
        )
    print(f"Loading {data_path} ...")
    bars = load_bars(str(data_path), start=args.start, end=args.end)
    print(f"  {len(bars):,} 1-min bars  "
          f"({bars['timestamp'].min()} → {bars['timestamp'].max()})")

    daily_loss_kill = float(risk_cfg.get("daily_loss_kill", 0)) if args.limits else 0.0
    account = Account(starting_equity=args.equity, max_drawdown=args.max_drawdown,
                      daily_loss_kill=daily_loss_kill)
    broker = SimBroker(instrument, tick_size, tick_value,
                       commission_per_rt=args.commission, slippage_ticks=args.slippage_ticks)
    adapter = SignalGeneratorAdapter(gen)
    news = NewsBlackout(args.news, blackout_minutes=2) if args.news else None
    contract_limit = ContractLimit(args.max_contracts)

    engine = BacktestEngine(bars, adapter, account, broker,
                            news_blackout=news, contract_limit=contract_limit, rth_only=True)
    print(f"Running {getattr(gen, 'strategy_id', 'strategy')} on {instrument} "
          f"(bar_size={getattr(gen, 'bar_size', '?')}m, limits={'on' if args.limits else 'off'}) ...")
    result = engine.run()
    metrics = compute_metrics(result)

    sid = getattr(gen, "strategy_id", "strategy")
    print("\n" + format_metrics(metrics, title=f"{sid} / {instrument}"))

    mc = None
    if args.mc_mode != "none":
        mc = run_mc(result.trade_pnls, starting_equity=args.equity,
                    trials=args.mc_trials, mode=args.mc_mode)
        if mc:
            print(f"\nMonte Carlo ({mc['mode']}, {mc['trials']:,} trials):  "
                  f"P(profit)={mc['prob_profit'] * 100:.1f}%   "
                  f"final p5/median/p95 = ${mc['final_p5']:,.0f} / ${mc['final_median']:,.0f} / ${mc['final_p95']:,.0f}   "
                  f"maxDD median/p95 = ${mc['maxdd_median']:,.0f} / ${mc['maxdd_p95']:,.0f}")

    tag = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    out_dir = THIS_DIR / "reports" / f"{tag}_{sid}"
    out_dir.mkdir(parents=True, exist_ok=True)

    payload = dict(metrics)
    if mc:
        payload["monte_carlo"] = {k: v for k, v in mc.items() if k not in ("finals", "maxdds")}
    (out_dir / "metrics.json").write_text(json.dumps(payload, indent=2, default=str))
    with open(out_dir / "equity_curve.csv", "w") as f:
        f.write("timestamp,equity\n")
        for ts, eq in result.equity_curve:
            f.write(f"{ts},{eq:.2f}\n")

    period = f"{bars['timestamp'].min().date()} → {bars['timestamp'].max().date()}"
    render_html(out_dir / "report.html", title=f"{sid} / {instrument}",
                metrics=metrics, period=period, equity_curve=result.equity_curve, mc=mc)
    print(f"\nSaved → {out_dir}  (metrics.json, equity_curve.csv, report.html)")


if __name__ == "__main__":
    main()

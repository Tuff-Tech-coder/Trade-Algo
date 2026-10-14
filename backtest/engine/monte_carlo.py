"""
Monte Carlo over a backtest's trade sequence.

Answers the questions that matter before trusting a strategy: what range of outcomes and drawdowns is
this edge likely to produce, and how robust is it to the order/sampling of trades?

Two modes (operating on per-trade P&L from a RunResult):
  - "shuffle"   : permute trade ORDER. The total P&L is unchanged (sum is
                  order-invariant), so this isolates PATH dependence — i.e. the
                  distribution of max drawdown you might have lived through.
  - "bootstrap" : resample trades WITH REPLACEMENT. Both the final P&L and the
                  max drawdown vary, giving a sampling-uncertainty distribution
                  (how much of the result is luck vs. edge).
"""

from __future__ import annotations

import random
from typing import Optional


def _path_stats(pnls: list[float], starting_equity: float) -> tuple[float, float]:
    """Return (total_pnl, max_drawdown_dollar) for a sequence of trade P&Ls."""
    equity = starting_equity
    peak = starting_equity
    max_dd = 0.0
    for p in pnls:
        equity += p
        if equity > peak:
            peak = equity
        dd = peak - equity
        if dd > max_dd:
            max_dd = dd
    return equity - starting_equity, max_dd


def _percentile(sorted_vals: list[float], q: float) -> float:
    if not sorted_vals:
        return 0.0
    idx = int(round(q * (len(sorted_vals) - 1)))
    idx = max(0, min(len(sorted_vals) - 1, idx))
    return sorted_vals[idx]


def run_mc(
    trade_pnls: list[float],
    starting_equity: float = 50_000.0,
    trials: int = 2000,
    mode: str = "bootstrap",
    seed: int = 42,
) -> Optional[dict]:
    """Run a Monte Carlo over the trade P&L sequence. Returns a summary dict."""
    if not trade_pnls:
        return None

    rng = random.Random(seed)
    n = len(trade_pnls)
    finals: list[float] = []
    dds: list[float] = []

    for _ in range(trials):
        if mode == "shuffle":
            seq = trade_pnls[:]
            rng.shuffle(seq)
        else:  # bootstrap
            seq = [trade_pnls[rng.randrange(n)] for _ in range(n)]
        total, mdd = _path_stats(seq, starting_equity)
        finals.append(total)
        dds.append(mdd)

    finals.sort()
    dds.sort()
    prob_profit = sum(1 for x in finals if x > 0) / len(finals)

    return {
        "mode": mode,
        "trials": trials,
        "n_trades": n,
        "starting_equity": starting_equity,
        "final_mean": sum(finals) / len(finals),
        "final_median": _percentile(finals, 0.5),
        "final_p5": _percentile(finals, 0.05),
        "final_p95": _percentile(finals, 0.95),
        "maxdd_median": _percentile(dds, 0.5),
        "maxdd_p95": _percentile(dds, 0.95),
        "maxdd_worst": dds[-1],
        "prob_profit": prob_profit,
        # raw arrays kept for the report histograms
        "finals": finals,
        "maxdds": dds,
    }

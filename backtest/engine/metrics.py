"""
Performance metrics for a backtest run.

Reframed from the prop-firm eval metrics (pass-rate / days-to-pass / consistency)
to standard trading-performance stats for a personal account.
"""

from __future__ import annotations

import math
from typing import Optional

from .loop import RunResult


def compute_metrics(result: RunResult) -> dict:
    trades = result.trade_pnls
    n = len(trades)
    start = result.starting_equity or 1.0

    wins = [p for p in trades if p > 0]
    losses = [p for p in trades if p <= 0]
    gross_profit = sum(wins)
    gross_loss = abs(sum(losses))

    win_rate = len(wins) / n if n else 0.0
    avg_win = (gross_profit / len(wins)) if wins else 0.0
    avg_loss = (-gross_loss / len(losses)) if losses else 0.0
    profit_factor = (gross_profit / gross_loss) if gross_loss > 0 else (float("inf") if gross_profit > 0 else 0.0)
    expectancy = (sum(trades) / n) if n else 0.0

    total_pnl = result.total_pnl
    max_dd = result.max_drawdown_dollar
    max_dd_pct = (max_dd / result.peak_equity) if result.peak_equity else 0.0

    # Sharpe from daily P&L (returns relative to starting equity, annualized).
    daily = [pnl for _, pnl in result.daily_pnl]
    sharpe = _sharpe([p / start for p in daily])

    # Return on account, and a simple annualization over the traded span.
    total_return_pct = total_pnl / start
    n_days = max(1, len(result.daily_pnl))
    ann_return_pct = total_return_pct * (252.0 / n_days)

    return {
        "trades": n,
        "days_traded": result.days_traded,
        "win_rate": win_rate,
        "profit_factor": profit_factor,
        "expectancy_per_trade": expectancy,
        "avg_win": avg_win,
        "avg_loss": avg_loss,
        "gross_profit": gross_profit,
        "gross_loss": -gross_loss,
        "total_pnl": total_pnl,
        "total_return_pct": total_return_pct,
        "annualized_return_pct": ann_return_pct,
        "max_drawdown_dollar": max_dd,
        "max_drawdown_pct": max_dd_pct,
        "sharpe_daily_annualized": sharpe,
        "best_trade": max(trades) if trades else 0.0,
        "worst_trade": min(trades) if trades else 0.0,
        "starting_equity": start,
        "ending_equity": start + total_pnl,
    }


def _sharpe(daily_returns: list[float]) -> Optional[float]:
    if len(daily_returns) < 2:
        return None
    mean = sum(daily_returns) / len(daily_returns)
    var = sum((x - mean) ** 2 for x in daily_returns) / (len(daily_returns) - 1)
    std = math.sqrt(var)
    if std == 0:
        return None
    return (mean / std) * math.sqrt(252.0)


def format_metrics(m: dict, title: str = "") -> str:
    def pct(x):
        return f"{x * 100:,.2f}%"

    def usd(x):
        return f"${x:,.2f}"

    pf = m["profit_factor"]
    pf_s = "inf" if pf == float("inf") else f"{pf:.2f}"
    sharpe = m["sharpe_daily_annualized"]
    sharpe_s = "n/a" if sharpe is None else f"{sharpe:.2f}"

    lines = [
        "=" * 56,
        f"  {title}" if title else "  Backtest performance",
        "=" * 56,
        f"  Trades                {m['trades']:>14}",
        f"  Days traded           {m['days_traded']:>14}",
        f"  Win rate              {pct(m['win_rate']):>14}",
        f"  Profit factor         {pf_s:>14}",
        f"  Expectancy/trade      {usd(m['expectancy_per_trade']):>14}",
        f"  Avg win / avg loss    {usd(m['avg_win'])} / {usd(m['avg_loss'])}",
        f"  Total P&L             {usd(m['total_pnl']):>14}",
        f"  Total return          {pct(m['total_return_pct']):>14}",
        f"  Annualized (approx)   {pct(m['annualized_return_pct']):>14}",
        f"  Max drawdown          {usd(m['max_drawdown_dollar'])} ({pct(m['max_drawdown_pct'])})",
        f"  Sharpe (daily, ann.)  {sharpe_s:>14}",
        f"  Best / worst trade    {usd(m['best_trade'])} / {usd(m['worst_trade'])}",
        f"  Ending equity         {usd(m['ending_equity']):>14}",
        "=" * 56,
    ]
    return "\n".join(lines)

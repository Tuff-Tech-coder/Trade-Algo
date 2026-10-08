"""
Self-contained HTML report (inline SVG — no Plotly/Jinja/JS dependencies).

render_html() writes a single .html file with:
  - the performance metrics table,
  - an equity-curve line chart,
  - a Monte Carlo summary + distribution histograms (final P&L, max drawdown).
The file opens in any browser offline.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from html import escape
from typing import Optional


def _usd(x) -> str:
    try:
        return f"${x:,.2f}"
    except (TypeError, ValueError):
        return "n/a"


def _pct(x) -> str:
    try:
        return f"{x * 100:,.2f}%"
    except (TypeError, ValueError):
        return "n/a"


def _svg_line(values: list[float], w: int = 860, h: int = 240,
              baseline: Optional[float] = None) -> str:
    if not values:
        return "<p class='muted'>No equity data.</p>"
    if len(values) > 800:
        step = len(values) // 800
        values = values[::step]
    pad = 34
    iw, ih = w - 2 * pad, h - 2 * pad
    pool = values + ([baseline] if baseline is not None else [])
    lo, hi = min(pool), max(pool)
    if hi == lo:
        hi = lo + 1.0
    n = len(values)

    def X(i):
        return pad + iw * (i / (n - 1 if n > 1 else 1))

    def Y(v):
        return pad + ih * (1 - (v - lo) / (hi - lo))

    d = "M " + " L ".join(f"{X(i):.1f} {Y(v):.1f}" for i, v in enumerate(values))
    base = ""
    if baseline is not None:
        by = Y(baseline)
        base = (f"<line x1='{pad}' y1='{by:.1f}' x2='{w - pad}' y2='{by:.1f}' "
                f"stroke='#5b647a' stroke-dasharray='4 4' stroke-width='1'/>")
    last_up = values[-1] >= (baseline if baseline is not None else values[0])
    color = "#4ade80" if last_up else "#f87171"
    return f"""<svg viewBox="0 0 {w} {h}" width="100%" preserveAspectRatio="xMidYMid meet">
      <rect x="0" y="0" width="{w}" height="{h}" fill="#13161f" rx="8"/>
      {base}
      <path d="{d}" fill="none" stroke="{color}" stroke-width="1.6"/>
      <text x="{pad}" y="16" fill="#94a3b8" font-size="11">{_usd(hi)}</text>
      <text x="{pad}" y="{h - 8}" fill="#94a3b8" font-size="11">{_usd(lo)}</text>
    </svg>"""


def _svg_hist(values: list[float], w: int = 420, h: int = 200,
              color: str = "#7dd3fc", ref: Optional[float] = None) -> str:
    if not values:
        return "<p class='muted'>No data.</p>"
    bins = 30
    lo, hi = min(values), max(values)
    if hi == lo:
        hi = lo + 1.0
    bw = (hi - lo) / bins
    counts = [0] * bins
    for v in values:
        b = int((v - lo) / bw)
        b = max(0, min(bins - 1, b))
        counts[b] += 1
    cmax = max(counts) or 1
    pad = 28
    iw, ih = w - 2 * pad, h - 2 * pad
    barw = iw / bins
    rects = []
    for i, c in enumerate(counts):
        bh = ih * c / cmax
        x = pad + i * barw
        y = pad + ih - bh
        rects.append(f"<rect x='{x:.1f}' y='{y:.1f}' width='{max(0.5, barw - 1):.1f}' "
                     f"height='{bh:.1f}' fill='{color}' opacity='0.85'/>")
    refline = ""
    if ref is not None and lo <= ref <= hi:
        rx = pad + iw * (ref - lo) / (hi - lo)
        refline = (f"<line x1='{rx:.1f}' y1='{pad}' x2='{rx:.1f}' y2='{pad + ih}' "
                   f"stroke='#fbbf24' stroke-dasharray='3 3' stroke-width='1'/>")
    return f"""<svg viewBox="0 0 {w} {h}" width="100%" preserveAspectRatio="xMidYMid meet">
      <rect x="0" y="0" width="{w}" height="{h}" fill="#13161f" rx="8"/>
      {''.join(rects)}{refline}
      <text x="{pad}" y="{h - 6}" fill="#94a3b8" font-size="11">{_usd(lo)}</text>
      <text x="{w - pad}" y="{h - 6}" fill="#94a3b8" font-size="11" text-anchor="end">{_usd(hi)}</text>
    </svg>"""


def _metrics_rows(m: dict) -> str:
    pf = m["profit_factor"]
    pf_s = "∞" if pf == float("inf") else f"{pf:.2f}"
    sharpe = m["sharpe_daily_annualized"]
    sharpe_s = "n/a" if sharpe is None else f"{sharpe:.2f}"
    items = [
        ("Trades", f"{m['trades']:,}"),
        ("Days traded", f"{m['days_traded']:,}"),
        ("Win rate", _pct(m["win_rate"])),
        ("Profit factor", pf_s),
        ("Expectancy / trade", _usd(m["expectancy_per_trade"])),
        ("Avg win / avg loss", f"{_usd(m['avg_win'])} / {_usd(m['avg_loss'])}"),
        ("Total P&L", _usd(m["total_pnl"])),
        ("Total return", _pct(m["total_return_pct"])),
        ("Annualized (approx)", _pct(m["annualized_return_pct"])),
        ("Max drawdown", f"{_usd(m['max_drawdown_dollar'])} ({_pct(m['max_drawdown_pct'])})"),
        ("Sharpe (daily, ann.)", sharpe_s),
        ("Best / worst trade", f"{_usd(m['best_trade'])} / {_usd(m['worst_trade'])}"),
        ("Ending equity", _usd(m["ending_equity"])),
    ]
    return "".join(f"<tr><td>{escape(str(k))}</td><td class='num'>{escape(str(v))}</td></tr>" for k, v in items)


def _mc_section(mc: Optional[dict]) -> str:
    if not mc:
        return ""
    rows = [
        ("Mode / trials", f"{mc['mode']} / {mc['trials']:,}"),
        ("Prob. of profit", _pct(mc["prob_profit"])),
        ("Final P&L — median", _usd(mc["final_median"])),
        ("Final P&L — 5th pct", _usd(mc["final_p5"])),
        ("Final P&L — 95th pct", _usd(mc["final_p95"])),
        ("Max DD — median", _usd(mc["maxdd_median"])),
        ("Max DD — 95th pct", _usd(mc["maxdd_p95"])),
        ("Max DD — worst", _usd(mc["maxdd_worst"])),
    ]
    table = "".join(f"<tr><td>{escape(str(k))}</td><td class='num'>{escape(str(v))}</td></tr>" for k, v in rows)
    return f"""
    <div class="card">
      <p class="section-title">Monte Carlo ({escape(str(mc['mode']))}, {mc['trials']:,} trials over {mc['n_trades']:,} trades)</p>
      <div class="grid">
        <table>{table}</table>
        <div>
          <p class="chart-label">Final P&amp;L distribution</p>
          {_svg_hist(mc['finals'], color='#7dd3fc', ref=0.0)}
          <p class="chart-label">Max drawdown distribution</p>
          {_svg_hist(mc['maxdds'], color='#f87171')}
        </div>
      </div>
    </div>"""


def render_html(
    out_path: str | Path,
    title: str,
    metrics: dict,
    period: str = "",
    equity_curve: Optional[list] = None,
    mc: Optional[dict] = None,
) -> None:
    title, period = escape(str(title)), escape(str(period))
    equities = [eq for _, eq in equity_curve] if equity_curve else []
    eq_svg = _svg_line(equities, baseline=metrics.get("starting_equity"))
    generated = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

    html = f"""<!DOCTYPE html>
<html lang="en"><head><meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Backtest — {title}</title>
<style>
  * {{ box-sizing: border-box; margin: 0; padding: 0; }}
  body {{ background:#0d0f1a; color:#e0e0e0; font-family:'Segoe UI',system-ui,sans-serif; padding:24px; }}
  h1 {{ color:#7dd3fc; font-size:1.4rem; margin-bottom:4px; }}
  .meta {{ color:#94a3b8; font-size:.82rem; margin-bottom:20px; }}
  .card {{ background:#13161f; border:1px solid #252836; border-radius:10px; padding:18px; margin-bottom:18px; }}
  .section-title {{ color:#7dd3fc; font-weight:600; margin-bottom:12px; }}
  .chart-label {{ color:#94a3b8; font-size:.8rem; margin:10px 0 4px; }}
  table {{ width:100%; border-collapse:collapse; font-size:.9rem; }}
  td {{ padding:6px 10px; border-bottom:1px solid #1e2230; }}
  td.num {{ text-align:right; font-variant-numeric:tabular-nums; color:#dbeafe; }}
  .grid {{ display:grid; grid-template-columns:1fr 1fr; gap:18px; align-items:start; }}
  .muted {{ color:#64748b; }}
  @media (max-width:760px) {{ .grid {{ grid-template-columns:1fr; }} }}
</style></head>
<body>
  <h1>{title}</h1>
  <p class="meta">Period: {period or 'n/a'} &nbsp;|&nbsp; Generated: {generated} &nbsp;|&nbsp;
     <span class="muted">relative comparison only — costs/slippage are modeled, not guaranteed</span></p>

  <div class="card">
    <p class="section-title">Equity curve</p>
    {eq_svg}
  </div>

  <div class="card">
    <p class="section-title">Performance</p>
    <table>{_metrics_rows(metrics)}</table>
  </div>

  {_mc_section(mc)}
</body></html>"""

    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(html, encoding="utf-8")

"""
TTFM Backtest Report Generator
───────────────────────────────
Generates a rich interactive HTML report with:
  - Equity curve (R-multiples)
  - Summary stats table
  - Trade-by-trade log
  - Factor score breakdown heatmap
"""

from __future__ import annotations
import json, sys
from pathlib import Path
from datetime import datetime
from typing import List

# Force UTF-8 on Windows so Unicode chars don't crash on cp1252 console
if sys.platform == "win32":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

from backtest_engine import BacktestResult, Trade


_HTML_TEMPLATE = """<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1.0"/>
  <title>TTFM Backtest Report</title>
  <link rel="preconnect" href="https://fonts.googleapis.com"/>
  <link href="https://fonts.googleapis.com/css2?family=Inter:wght@300;400;500;600;700&family=JetBrains+Mono&display=swap" rel="stylesheet"/>
  <script src="https://cdn.jsdelivr.net/npm/chart.js@4.4.0/dist/chart.umd.min.js"></script>
  <style>
    :root {
      --bg: #0a0d14;
      --surface: #111520;
      --card: #161b27;
      --border: #1f2535;
      --accent: #6c63ff;
      --accent2: #00d4aa;
      --danger: #ff4b6e;
      --win: #00d4aa;
      --loss: #ff4b6e;
      --timeout: #f0a500;
      --text: #e4e8f0;
      --muted: #6b7590;
      --font: 'Inter', sans-serif;
      --mono: 'JetBrains Mono', monospace;
    }
    * { box-sizing: border-box; margin: 0; padding: 0; }
    body { background: var(--bg); color: var(--text); font-family: var(--font); min-height: 100vh; }

    .header {
      background: linear-gradient(135deg, #161b27 0%, #1a1030 100%);
      border-bottom: 1px solid var(--border);
      padding: 28px 40px;
      display: flex; align-items: center; gap: 20px;
    }
    .header-logo { font-size: 32px; }
    .header-text h1 { font-size: 22px; font-weight: 700; letter-spacing: -0.5px; }
    .header-text p  { font-size: 13px; color: var(--muted); margin-top: 3px; }
    .badge {
      margin-left: auto;
      background: linear-gradient(135deg, var(--accent), #9c88ff);
      color: #fff;
      font-size: 11px; font-weight: 600; letter-spacing: 1px;
      padding: 5px 14px; border-radius: 20px; text-transform: uppercase;
    }

    .container { max-width: 1400px; margin: 0 auto; padding: 36px 40px; }

    /* ── Stats grid ── */
    .stats-grid {
      display: grid;
      grid-template-columns: repeat(auto-fit, minmax(180px, 1fr));
      gap: 16px; margin-bottom: 36px;
    }
    .stat-card {
      background: var(--card);
      border: 1px solid var(--border);
      border-radius: 14px;
      padding: 20px 22px;
      transition: transform .2s, border-color .2s;
    }
    .stat-card:hover { transform: translateY(-2px); border-color: var(--accent); }
    .stat-label { font-size: 11px; color: var(--muted); text-transform: uppercase; letter-spacing: 1px; margin-bottom: 8px; }
    .stat-value { font-size: 28px; font-weight: 700; line-height: 1; }
    .stat-value.win  { color: var(--win); }
    .stat-value.loss { color: var(--loss); }
    .stat-value.neutral { color: var(--accent); }
    .stat-sub { font-size: 11px; color: var(--muted); margin-top: 6px; }

    /* ── Chart card ── */
    .chart-card {
      background: var(--card);
      border: 1px solid var(--border);
      border-radius: 14px;
      padding: 24px 28px;
      margin-bottom: 28px;
    }
    .chart-title { font-size: 14px; font-weight: 600; margin-bottom: 18px; color: var(--text); }
    .chart-wrap  { position: relative; height: 260px; }

    /* ── Trade log ── */
    .table-card {
      background: var(--card);
      border: 1px solid var(--border);
      border-radius: 14px;
      overflow: hidden;
      margin-bottom: 28px;
    }
    .table-header {
      padding: 18px 24px;
      border-bottom: 1px solid var(--border);
      font-size: 14px; font-weight: 600;
      display: flex; justify-content: space-between; align-items: center;
    }
    .table-filter { display: flex; gap: 8px; }
    .filter-btn {
      font-size: 11px; font-weight: 600; padding: 4px 12px;
      border-radius: 20px; border: 1px solid var(--border);
      background: transparent; color: var(--muted); cursor: pointer;
      transition: all .2s;
    }
    .filter-btn.active, .filter-btn:hover { background: var(--accent); color: #fff; border-color: var(--accent); }
    table { width: 100%; border-collapse: collapse; font-size: 12.5px; }
    th { padding: 10px 16px; text-align: left; font-size: 11px; color: var(--muted);
         text-transform: uppercase; letter-spacing: .8px; font-weight: 500;
         border-bottom: 1px solid var(--border); background: var(--surface); }
    td { padding: 10px 16px; border-bottom: 1px solid #1a1f2e; font-family: var(--mono); }
    tr:last-child td { border-bottom: none; }
    tr:hover td { background: #1a1f2e; }
    .pill {
      display: inline-block; padding: 2px 10px; border-radius: 20px;
      font-size: 10px; font-weight: 700; letter-spacing: .5px;
    }
    .pill-win  { background: rgba(0,212,170,.15); color: var(--win); }
    .pill-loss { background: rgba(255,75,110,.15); color: var(--loss); }
    .pill-timeout { background: rgba(240,165,0,.15); color: var(--timeout); }
    .pill-buy  { background: rgba(0,212,170,.1); color: var(--win); }
    .pill-sell { background: rgba(255,75,110,.1); color: var(--loss); }

    .pnl-pos { color: var(--win); }
    .pnl-neg { color: var(--loss); }

    /* ── Factor heatmap ── */
    .heatmap-grid {
      display: grid;
      grid-template-columns: repeat(auto-fit, minmax(120px, 1fr));
      gap: 10px; padding: 0 24px 24px;
    }
    .hm-cell {
      border-radius: 10px; padding: 14px 12px; text-align: center;
      transition: transform .2s;
    }
    .hm-cell:hover { transform: scale(1.05); }
    .hm-label { font-size: 10px; text-transform: uppercase; letter-spacing: .8px; color: var(--muted); margin-bottom: 4px; }
    .hm-val   { font-size: 20px; font-weight: 700; }

    footer { text-align: center; padding: 36px; color: var(--muted); font-size: 12px; }
  </style>
</head>
<body>

<div class="header">
  <span class="header-logo">📊</span>
  <div class="header-text">
    <h1>TTFM Backtest Report — {{symbol}}</h1>
    <p>Generated {{generated}} · Strategy: TTFM Alpha Combiner v7.1</p>
  </div>
  <span class="badge">Backtest</span>
</div>

<div class="container">

  <!-- Stats grid -->
  <div class="stats-grid">
    <div class="stat-card">
      <div class="stat-label">Total Trades</div>
      <div class="stat-value neutral">{{total}}</div>
      <div class="stat-sub">{{wins}}W / {{losses}}L / {{timeouts}}TO</div>
    </div>
    <div class="stat-card">
      <div class="stat-label">Win Rate</div>
      <div class="stat-value {{wr_class}}">{{win_rate}}%</div>
      <div class="stat-sub">Target ≥ 50%</div>
    </div>
    <div class="stat-card">
      <div class="stat-label">Expectancy</div>
      <div class="stat-value {{exp_class}}">{{expectancy}}R</div>
      <div class="stat-sub">Per trade</div>
    </div>
    <div class="stat-card">
      <div class="stat-label">Total PnL</div>
      <div class="stat-value {{pnl_class}}">{{total_pnl_r}}R</div>
      <div class="stat-sub">${{total_pnl_usd}}</div>
    </div>
    <div class="stat-card">
      <div class="stat-label">Avg Win RR</div>
      <div class="stat-value win">{{avg_rr_win}}R</div>
    </div>
    <div class="stat-card">
      <div class="stat-label">Avg Loss RR</div>
      <div class="stat-value loss">{{avg_rr_loss}}R</div>
    </div>
    <div class="stat-card">
      <div class="stat-label">Max Drawdown</div>
      <div class="stat-value loss">{{max_dd}}R</div>
    </div>
    <div class="stat-card">
      <div class="stat-label">Sharpe (R)</div>
      <div class="stat-value neutral">{{sharpe}}</div>
    </div>
  </div>

  <!-- Equity Curve -->
  <div class="chart-card">
    <div class="chart-title">📈 Equity Curve (R-Multiples)</div>
    <div class="chart-wrap">
      <canvas id="equityChart"></canvas>
    </div>
  </div>

  <!-- Win/Loss Bar -->
  <div class="chart-card">
    <div class="chart-title">🎯 Trade Outcomes Breakdown</div>
    <div class="chart-wrap" style="height:200px">
      <canvas id="outcomeChart"></canvas>
    </div>
  </div>

  <!-- Factor Score Averages -->
  <div class="table-card">
    <div class="table-header">⚡ Average Factor Scores (Signal Bars)</div>
    <div class="heatmap-grid" id="heatmapGrid"></div>
  </div>

  <!-- Trade Log -->
  <div class="table-card">
    <div class="table-header">
      📋 Trade Log
      <div class="table-filter">
        <button class="filter-btn active" onclick="filterTrades('all',this)">All</button>
        <button class="filter-btn" onclick="filterTrades('WIN',this)">Wins</button>
        <button class="filter-btn" onclick="filterTrades('LOSS',this)">Losses</button>
        <button class="filter-btn" onclick="filterTrades('TIMEOUT',this)">Timeout</button>
      </div>
    </div>
    <div style="overflow-x:auto">
    <table id="tradeTable">
      <thead>
        <tr>
          <th>#</th>
          <th>Entry Time</th>
          <th>Exit Time</th>
          <th>Dir</th>
          <th>Entry</th>
          <th>SL</th>
          <th>TP</th>
          <th>Exit</th>
          <th>Score</th>
          <th>RR Planned</th>
          <th>PnL (R)</th>
          <th>Outcome</th>
        </tr>
      </thead>
      <tbody id="tradeBody"></tbody>
    </table>
    </div>
  </div>

</div>

<footer>TTFM Alpha Combiner Backtester · {{generated}}</footer>

<script>
const TRADES = {{trades_json}};
const EQUITY = {{equity_json}};
const LABELS = {{labels_json}};
const FACTORS = {{factors_json}};

// ── Equity chart ──────────────────────────────────────────────────────────
new Chart(document.getElementById('equityChart'), {
  type: 'line',
  data: {
    labels: LABELS,
    datasets: [{
      label: 'Equity (R)',
      data: EQUITY,
      borderColor: '#6c63ff',
      backgroundColor: 'rgba(108,99,255,0.08)',
      borderWidth: 2,
      fill: true,
      tension: 0.3,
      pointRadius: 3,
      pointBackgroundColor: EQUITY.map((v,i) => {
        if (i === 0) return '#6c63ff';
        return EQUITY[i] >= EQUITY[i-1] ? '#00d4aa' : '#ff4b6e';
      }),
    }]
  },
  options: {
    responsive: true, maintainAspectRatio: false,
    plugins: { legend: { display: false }, tooltip: { callbacks: { label: ctx => ` ${ctx.parsed.y.toFixed(2)}R` } } },
    scales: {
      x: { grid: { color: '#1f2535' }, ticks: { color: '#6b7590', maxTicksLimit: 10, font: { size: 10 } } },
      y: { grid: { color: '#1f2535' }, ticks: { color: '#6b7590', callback: v => v.toFixed(1) + 'R' } }
    }
  }
});

// ── Outcome chart ─────────────────────────────────────────────────────────
const wins     = TRADES.filter(t => t.outcome === 'WIN').length;
const losses   = TRADES.filter(t => t.outcome === 'LOSS').length;
const timeouts = TRADES.filter(t => t.outcome === 'TIMEOUT').length;
new Chart(document.getElementById('outcomeChart'), {
  type: 'bar',
  data: {
    labels: ['Wins', 'Losses', 'Timeouts'],
    datasets: [{
      data: [wins, losses, timeouts],
      backgroundColor: ['rgba(0,212,170,.7)', 'rgba(255,75,110,.7)', 'rgba(240,165,0,.7)'],
      borderRadius: 8,
      borderSkipped: false,
    }]
  },
  options: {
    responsive: true, maintainAspectRatio: false,
    plugins: { legend: { display: false } },
    scales: {
      x: { grid: { display: false }, ticks: { color: '#6b7590' } },
      y: { grid: { color: '#1f2535' }, ticks: { color: '#6b7590', stepSize: 1 } }
    }
  }
});

// ── Heatmap ───────────────────────────────────────────────────────────────
const grid = document.getElementById('heatmapGrid');
const factorKeys = ['trend_bull','sweep_bull','disp_bull','vol_score','volm_score','penalty'];
const factorLabels = { trend_bull:'Trend', sweep_bull:'Sweep', disp_bull:'Displacement', vol_score:'ATR Exp', volm_score:'Volume Spike', penalty:'Spread Penalty' };
factorKeys.forEach(k => {
  const vals = FACTORS.map(f => f[k] || 0);
  const avg  = vals.length ? vals.reduce((a,b)=>a+b,0)/vals.length : 0;
  const pct  = Math.min(avg / 20, 1);
  const r = Math.round(255 * (1-pct));
  const g = Math.round(212 * pct);
  const cell = document.createElement('div');
  cell.className = 'hm-cell';
  cell.style.background = k === 'penalty'
    ? `rgba(255,75,110,${0.1 + pct * 0.4})`
    : `rgba(${r},${g},${Math.round(170*pct)},${0.15 + pct*0.35})`;
  cell.innerHTML = `<div class="hm-label">${factorLabels[k]}</div><div class="hm-val">${avg.toFixed(1)}</div>`;
  grid.appendChild(cell);
});

// ── Trade table ───────────────────────────────────────────────────────────
function renderTrades(filter) {
  const tbody = document.getElementById('tradeBody');
  tbody.innerHTML = '';
  const list = filter === 'all' ? TRADES : TRADES.filter(t => t.outcome === filter);
  list.forEach((t, idx) => {
    const pnlClass = t.pnl_r >= 0 ? 'pnl-pos' : 'pnl-neg';
    const outcomeClass = `pill-${t.outcome.toLowerCase()}`;
    const dirClass = `pill-${t.direction.toLowerCase()}`;
    tbody.innerHTML += `<tr>
      <td>${idx+1}</td>
      <td>${t.entry_time}</td>
      <td>${t.exit_time || '—'}</td>
      <td><span class="pill ${dirClass}">${t.direction.toUpperCase()}</span></td>
      <td>${(+t.entry).toFixed(5)}</td>
      <td>${(+t.sl).toFixed(5)}</td>
      <td>${(+t.tp).toFixed(5)}</td>
      <td>${t.exit_price ? (+t.exit_price).toFixed(5) : '—'}</td>
      <td>${t.score}</td>
      <td>${(+t.rr).toFixed(2)}R</td>
      <td class="${pnlClass}">${t.pnl_r >= 0 ? '+' : ''}${(+t.pnl_r).toFixed(2)}R</td>
      <td><span class="pill ${outcomeClass}">${t.outcome}</span></td>
    </tr>`;
  });
}

function filterTrades(f, btn) {
  document.querySelectorAll('.filter-btn').forEach(b => b.classList.remove('active'));
  btn.classList.add('active');
  renderTrades(f);
}

renderTrades('all');
</script>
</body>
</html>"""


def generate_html_report(results: List[BacktestResult], output_path: str = "backtest_report.html"):
    """Generate a rich interactive HTML report for one or more symbols."""
    
    # If multiple symbols, concatenate trades and use first symbol name
    all_trades: List[Trade] = []
    for r in results:
        all_trades.extend(r.trades)
    
    # Sort by entry time
    all_trades.sort(key=lambda t: t.entry_time)
    
    # Aggregate stats across all symbols
    total   = sum(r.total   for r in results)
    wins    = sum(r.wins    for r in results)
    losses  = sum(r.losses  for r in results)
    timeouts = sum(r.timeouts for r in results)
    win_rate = (wins / total * 100) if total else 0.0

    import numpy as np
    all_pnl_r = [t.pnl_r for t in all_trades]
    pnl_series = list(np.cumsum(all_pnl_r)) if all_pnl_r else [0]
    total_pnl_r   = round(float(sum(r.total_pnl_r   for r in results)), 2)
    total_pnl_usd = round(float(sum(r.total_pnl_usd for r in results)), 2)

    win_rr_all  = [t.pnl_r for t in all_trades if t.outcome == "WIN"]
    loss_rr_all = [t.pnl_r for t in all_trades if t.outcome == "LOSS"]
    avg_rr_win  = round(float(np.mean(win_rr_all))  if win_rr_all  else 0.0, 2)
    avg_rr_loss = round(float(np.mean(loss_rr_all)) if loss_rr_all else 0.0, 2)
    expectancy  = round((win_rate/100 * avg_rr_win) + ((1-win_rate/100) * avg_rr_loss), 3)

    max_dd = max(r.max_drawdown_r for r in results) if results else 0.0
    sharpe = round(float(np.mean([r.sharpe_ratio for r in results])), 2) if results else 0.0
    symbol = ", ".join(r.symbol for r in results)

    # Build JSON payloads
    trades_json = json.dumps([{
        "entry_time":  t.entry_time.strftime("%Y-%m-%d %H:%M") if t.entry_time else "",
        "exit_time":   t.exit_time.strftime("%Y-%m-%d %H:%M") if t.exit_time else None,
        "direction":   t.direction,
        "entry":       t.entry,
        "sl":          t.sl,
        "tp":          t.tp,
        "exit_price":  t.exit_price,
        "score":       t.score,
        "rr":          round(t.rr, 2),
        "pnl_r":       round(t.pnl_r, 2),
        "pnl_usd":     round(t.pnl_usd, 2),
        "outcome":     t.outcome,
    } for t in all_trades])

    labels_json = json.dumps([t.entry_time.strftime("%m/%d %H:%M") for t in all_trades])
    equity_json = json.dumps([round(v, 2) for v in pnl_series])
    factors_json = json.dumps([t.factors for t in all_trades])

    html = _HTML_TEMPLATE
    for k, v in {
        "{{symbol}}":       symbol,
        "{{generated}}":    datetime.now().strftime("%Y-%m-%d %H:%M"),
        "{{total}}":        str(total),
        "{{wins}}":         str(wins),
        "{{losses}}":       str(losses),
        "{{timeouts}}":     str(timeouts),
        "{{win_rate}}":     f"{win_rate:.1f}",
        "{{wr_class}}":     "win" if win_rate >= 50 else "loss",
        "{{expectancy}}":   f"{expectancy:+.3f}",
        "{{exp_class}}":    "win" if expectancy >= 0 else "loss",
        "{{total_pnl_r}}":  f"{total_pnl_r:+.2f}",
        "{{pnl_class}}":    "win" if total_pnl_r >= 0 else "loss",
        "{{total_pnl_usd}}": f"{total_pnl_usd:+.2f}",
        "{{avg_rr_win}}":   f"{avg_rr_win:.2f}",
        "{{avg_rr_loss}}":  f"{avg_rr_loss:.2f}",
        "{{max_dd}}":       f"{max_dd:.2f}",
        "{{sharpe}}":       f"{sharpe:.2f}",
        "{{trades_json}}":  trades_json,
        "{{labels_json}}":  labels_json,
        "{{equity_json}}":  equity_json,
        "{{factors_json}}": factors_json,
    }.items():
        html = html.replace(k, v)

    Path(output_path).write_text(html, encoding="utf-8")
    print(f"\n[OK] Report saved -> {output_path}")
    return output_path

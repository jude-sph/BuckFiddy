"""BuckFiddy Dashboard — localhost web UI for monitoring the trading agent.

Run:  python -m buckfiddy.dashboard
Then open http://localhost:8050
"""

import sys

from fastapi import FastAPI
from fastapi.responses import HTMLResponse, JSONResponse
import uvicorn

from buckfiddy.config import Settings
from buckfiddy.state.store import StateStore

app = FastAPI(title="BuckFiddy Dashboard")
store: StateStore | None = None


def get_store() -> StateStore:
    global store
    if store is None:
        settings = Settings()
        store = StateStore(settings.DB_PATH)
    return store


# ── API Routes ──────────────────────────────────────────────


@app.get("/api/summary")
def api_summary():
    s = get_store()
    wallet = s.fetchone("SELECT balance FROM wallet WHERE id = 1")
    balance = wallet["balance"] if wallet else 0

    latest_snap = s.fetchone(
        "SELECT * FROM equity_snapshots ORDER BY id DESC LIMIT 1"
    )
    first_snap = s.fetchone(
        "SELECT equity FROM equity_snapshots ORDER BY id ASC LIMIT 1"
    )

    starting = first_snap["equity"] if first_snap else 100.0
    equity = latest_snap["equity"] if latest_snap else balance
    pos_value = latest_snap["position_value"] if latest_snap else 0
    num_pos = latest_snap["num_positions"] if latest_snap else 0
    api_cost = latest_snap["cumulative_api_cost"] if latest_snap else 0

    pnl = equity - starting
    pnl_pct = (pnl / starting * 100) if starting else 0

    cycles = s.fetchone("SELECT COUNT(*) as n FROM cycle_log")
    total_trades = s.fetchone("SELECT COUNT(*) as n FROM trades")
    wins = s.fetchone(
        "SELECT COUNT(*) as n FROM trades WHERE side='SELL' AND pnl > 0"
    )
    losses = s.fetchone(
        "SELECT COUNT(*) as n FROM trades WHERE side='SELL' AND pnl < 0"
    )
    realized = s.fetchone(
        "SELECT COALESCE(SUM(pnl), 0) as v FROM trades WHERE pnl IS NOT NULL"
    )
    est_count = s.fetchone("SELECT COUNT(*) as n FROM estimates")
    tradeable_count = s.fetchone(
        "SELECT COUNT(*) as n FROM estimates WHERE tradeable = 1"
    )

    return {
        "balance": round(balance, 2),
        "equity": round(equity, 2),
        "position_value": round(pos_value, 2),
        "num_positions": num_pos,
        "starting_equity": round(starting, 2),
        "pnl": round(pnl, 2),
        "pnl_pct": round(pnl_pct, 1),
        "api_cost": round(api_cost, 4),
        "net_pnl": round(pnl - api_cost, 4),
        "cycles": cycles["n"] if cycles else 0,
        "total_trades": total_trades["n"] if total_trades else 0,
        "wins": wins["n"] if wins else 0,
        "losses": losses["n"] if losses else 0,
        "realized_pnl": round(realized["v"], 2) if realized else 0,
        "estimates": est_count["n"] if est_count else 0,
        "tradeable_edges": tradeable_count["n"] if tradeable_count else 0,
    }


@app.get("/api/equity")
def api_equity():
    s = get_store()
    rows = s.fetchall("SELECT * FROM equity_snapshots ORDER BY id")
    return [
        {
            "cycle": r["cycle_number"],
            "balance": r["balance"],
            "position_value": r["position_value"],
            "equity": r["equity"],
            "num_positions": r["num_positions"],
            "api_cost": r["cumulative_api_cost"],
            "time": r["created_at"],
        }
        for r in rows
    ]


@app.get("/api/positions")
def api_positions():
    s = get_store()
    rows = s.fetchall("SELECT * FROM positions WHERE size > 0")
    return [
        {
            "position_id": r["position_id"],
            "market_question": r["market_question"],
            "outcome": r["outcome"],
            "size": r["size"],
            "avg_entry_price": r["avg_entry_price"],
            "created_at": r["created_at"],
        }
        for r in rows
    ]


@app.get("/api/trades")
def api_trades():
    s = get_store()
    rows = s.fetchall("SELECT * FROM trades ORDER BY executed_at DESC LIMIT 50")
    return [
        {
            "trade_id": r["trade_id"],
            "outcome": r["outcome"],
            "side": r["side"],
            "price": r["price"],
            "size": r["size"],
            "pnl": r["pnl"],
            "executed_at": r["executed_at"],
        }
        for r in rows
    ]


@app.get("/api/estimates")
def api_estimates():
    s = get_store()
    rows = s.fetchall(
        "SELECT * FROM estimates ORDER BY created_at DESC LIMIT 30"
    )
    return [
        {
            "outcome": r["outcome"],
            "claude_estimate": r["claude_estimate"],
            "market_midpoint": r["market_midpoint"],
            "edge": r["edge"],
            "tradeable": bool(r["tradeable"]),
            "reasoning": r["reasoning"][:200],
            "created_at": r["created_at"],
        }
        for r in rows
    ]


@app.get("/api/costs")
def api_costs():
    s = get_store()
    rows = s.fetchall("SELECT * FROM api_usage ORDER BY id")
    return [
        {
            "cycle": r["cycle_number"],
            "api_calls": r["api_calls"],
            "input_tokens": r["input_tokens"],
            "output_tokens": r["output_tokens"],
            "web_searches": r["web_searches"],
            "cost": r["cost_usd"],
            "time": r["created_at"],
        }
        for r in rows
    ]


@app.get("/api/cycles")
def api_cycles():
    s = get_store()
    rows = s.fetchall(
        "SELECT * FROM cycle_log ORDER BY id DESC LIMIT 20"
    )
    return [
        {
            "cycle": r["cycle_number"],
            "stop_losses": r["stop_losses_triggered"],
            "balance": r["balance_after"],
            "equity": r["equity_after"],
            "summary": r["claude_summary"],
            "time": r["created_at"],
        }
        for r in rows
    ]


# ── HTML Dashboard ──────────────────────────────────────────


DASHBOARD_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>BuckFiddy Dashboard</title>
<script src="https://cdn.tailwindcss.com"></script>
<script src="https://cdn.jsdelivr.net/npm/chart.js@4"></script>
<style>
  body { background: #0a0a0f; color: #e2e8f0; }
  .card { background: #13131a; border: 1px solid #1e1e2e; border-radius: 12px; }
  .stat-up { color: #22c55e; }
  .stat-down { color: #ef4444; }
  .stat-neutral { color: #94a3b8; }
  .badge-buy { background: #14532d; color: #22c55e; }
  .badge-sell { background: #7f1d1d; color: #ef4444; }
  .badge-yes { background: #1e3a5f; color: #60a5fa; }
  .badge-no { background: #3b1f2b; color: #f472b6; }
  .pulse { animation: pulse 2s cubic-bezier(0.4, 0, 0.6, 1) infinite; }
  @keyframes pulse { 0%, 100% { opacity: 1; } 50% { opacity: .5; } }
  ::-webkit-scrollbar { width: 6px; }
  ::-webkit-scrollbar-track { background: #13131a; }
  ::-webkit-scrollbar-thumb { background: #2d2d3f; border-radius: 3px; }
</style>
</head>
<body class="min-h-screen p-6">

<div class="max-w-7xl mx-auto">
  <!-- Header -->
  <div class="flex items-center justify-between mb-8">
    <div>
      <h1 class="text-3xl font-bold text-white">BuckFiddy</h1>
      <p class="text-sm text-slate-500 mt-1">Autonomous Polymarket Trading Agent</p>
    </div>
    <div class="flex items-center gap-3">
      <span id="status-dot" class="w-2 h-2 rounded-full bg-slate-600"></span>
      <span id="status-text" class="text-xs text-slate-500">Loading...</span>
      <span id="last-update" class="text-xs text-slate-600 ml-4"></span>
    </div>
  </div>

  <!-- Stats Row -->
  <div class="grid grid-cols-2 md:grid-cols-4 lg:grid-cols-7 gap-4 mb-6">
    <div class="card p-4">
      <div class="text-xs text-slate-500 uppercase tracking-wider">Balance</div>
      <div id="stat-balance" class="text-xl font-bold text-white mt-1">-</div>
    </div>
    <div class="card p-4">
      <div class="text-xs text-slate-500 uppercase tracking-wider">Equity</div>
      <div id="stat-equity" class="text-xl font-bold text-white mt-1">-</div>
    </div>
    <div class="card p-4">
      <div class="text-xs text-slate-500 uppercase tracking-wider">P&L</div>
      <div id="stat-pnl" class="text-xl font-bold mt-1">-</div>
    </div>
    <div class="card p-4">
      <div class="text-xs text-slate-500 uppercase tracking-wider">Positions</div>
      <div id="stat-positions" class="text-xl font-bold text-white mt-1">-</div>
    </div>
    <div class="card p-4">
      <div class="text-xs text-slate-500 uppercase tracking-wider">Trades</div>
      <div id="stat-trades" class="text-xl font-bold text-white mt-1">-</div>
    </div>
    <div class="card p-4">
      <div class="text-xs text-slate-500 uppercase tracking-wider">Win Rate</div>
      <div id="stat-winrate" class="text-xl font-bold text-white mt-1">-</div>
    </div>
    <div class="card p-4">
      <div class="text-xs text-slate-500 uppercase tracking-wider">API Cost</div>
      <div id="stat-apicost" class="text-xl font-bold text-orange-400 mt-1">-</div>
    </div>
  </div>

  <!-- Charts Row -->
  <div class="grid grid-cols-1 lg:grid-cols-2 gap-6 mb-6">
    <div class="card p-6">
      <h2 class="text-sm font-semibold text-slate-400 uppercase tracking-wider mb-4">Equity Curve</h2>
      <canvas id="equity-chart" height="200"></canvas>
    </div>
    <div class="card p-6">
      <h2 class="text-sm font-semibold text-slate-400 uppercase tracking-wider mb-4">API Costs Per Cycle</h2>
      <canvas id="cost-chart" height="200"></canvas>
    </div>
  </div>

  <!-- Estimates Row -->
  <div class="card p-6 mb-6">
    <h2 class="text-sm font-semibold text-slate-400 uppercase tracking-wider mb-4">Claude's Estimates vs Market</h2>
    <canvas id="estimates-chart" height="120"></canvas>
  </div>

  <!-- Tables Row -->
  <div class="grid grid-cols-1 lg:grid-cols-2 gap-6 mb-6">
    <!-- Open Positions -->
    <div class="card p-6">
      <h2 class="text-sm font-semibold text-slate-400 uppercase tracking-wider mb-4">Open Positions</h2>
      <div id="positions-table" class="overflow-x-auto">
        <p class="text-slate-600 text-sm">No positions</p>
      </div>
    </div>

    <!-- Recent Trades -->
    <div class="card p-6">
      <h2 class="text-sm font-semibold text-slate-400 uppercase tracking-wider mb-4">Recent Trades</h2>
      <div id="trades-table" class="overflow-x-auto">
        <p class="text-slate-600 text-sm">No trades yet</p>
      </div>
    </div>
  </div>

  <!-- Estimates Table -->
  <div class="card p-6 mb-6">
    <h2 class="text-sm font-semibold text-slate-400 uppercase tracking-wider mb-4">Probability Estimates</h2>
    <div id="estimates-table" class="overflow-x-auto">
      <p class="text-slate-600 text-sm">No estimates yet</p>
    </div>
  </div>

  <!-- Cycle Log -->
  <div class="card p-6 mb-6">
    <h2 class="text-sm font-semibold text-slate-400 uppercase tracking-wider mb-4">Agent Activity</h2>
    <div id="cycles-list" class="space-y-3">
      <p class="text-slate-600 text-sm">No cycles yet</p>
    </div>
  </div>

</div>

<script>
const $ = id => document.getElementById(id);
let equityChart, costChart, estimatesChart;

function fmtUsd(v) { return '$' + Number(v).toFixed(2); }
function fmtPct(v) { return (v >= 0 ? '+' : '') + Number(v).toFixed(1) + '%'; }
function fmtTime(iso) {
  if (!iso) return '-';
  const d = new Date(iso);
  return d.toLocaleDateString('en-GB', {day:'2-digit',month:'short'}) + ' ' +
         d.toLocaleTimeString('en-GB', {hour:'2-digit',minute:'2-digit'});
}
function pnlClass(v) { return v > 0 ? 'stat-up' : v < 0 ? 'stat-down' : 'stat-neutral'; }

async function fetchJson(url) {
  const r = await fetch(url);
  return r.ok ? r.json() : null;
}

async function updateSummary() {
  const d = await fetchJson('/api/summary');
  if (!d) return;

  $('stat-balance').textContent = fmtUsd(d.balance);
  $('stat-equity').textContent = fmtUsd(d.equity);

  const pnlEl = $('stat-pnl');
  pnlEl.textContent = fmtUsd(d.pnl) + ' (' + fmtPct(d.pnl_pct) + ')';
  pnlEl.className = 'text-xl font-bold mt-1 ' + pnlClass(d.pnl);

  $('stat-positions').textContent = d.num_positions;
  $('stat-trades').textContent = d.total_trades;
  $('stat-apicost').textContent = '$' + d.api_cost.toFixed(4);

  const closed = d.wins + d.losses;
  $('stat-winrate').textContent = closed > 0 ? Math.round(d.wins / closed * 100) + '%' : '-';

  // Status indicator
  $('status-dot').className = 'w-2 h-2 rounded-full ' + (d.cycles > 0 ? 'bg-green-500 pulse' : 'bg-slate-600');
  $('status-text').textContent = d.cycles > 0 ? 'Cycle ' + d.cycles : 'Waiting...';
  $('last-update').textContent = 'Updated ' + new Date().toLocaleTimeString();
}

async function updateEquityChart() {
  const data = await fetchJson('/api/equity');
  if (!data || data.length === 0) return;

  const labels = data.map(d => 'C' + d.cycle);
  const equity = data.map(d => d.equity);
  const balance = data.map(d => d.balance);
  const posValue = data.map(d => d.position_value);

  if (equityChart) equityChart.destroy();
  equityChart = new Chart($('equity-chart'), {
    type: 'line',
    data: {
      labels,
      datasets: [
        { label: 'Equity', data: equity, borderColor: '#818cf8', backgroundColor: 'rgba(129,140,248,0.1)', fill: true, tension: 0.3, pointRadius: 2 },
        { label: 'Cash', data: balance, borderColor: '#22c55e', borderDash: [4,4], tension: 0.3, pointRadius: 0 },
        { label: 'Position Value', data: posValue, borderColor: '#f59e0b', borderDash: [4,4], tension: 0.3, pointRadius: 0 },
      ]
    },
    options: {
      responsive: true,
      plugins: { legend: { labels: { color: '#94a3b8', boxWidth: 12, font: { size: 11 } } } },
      scales: {
        x: { ticks: { color: '#475569', font: { size: 10 } }, grid: { color: '#1e1e2e' } },
        y: { ticks: { color: '#475569', callback: v => '$' + v }, grid: { color: '#1e1e2e' } }
      }
    }
  });
}

async function updateCostChart() {
  const data = await fetchJson('/api/costs');
  if (!data || data.length === 0) return;

  const labels = data.map(d => 'C' + d.cycle);
  const costs = data.map(d => d.cost);
  const tokens = data.map(d => Math.round((d.input_tokens + d.output_tokens) / 1000));

  if (costChart) costChart.destroy();
  costChart = new Chart($('cost-chart'), {
    type: 'bar',
    data: {
      labels,
      datasets: [
        { label: 'Cost ($)', data: costs, backgroundColor: '#f97316', yAxisID: 'y', borderRadius: 4 },
        { label: 'Tokens (K)', data: tokens, type: 'line', borderColor: '#60a5fa', yAxisID: 'y1', tension: 0.3, pointRadius: 2 },
      ]
    },
    options: {
      responsive: true,
      plugins: { legend: { labels: { color: '#94a3b8', boxWidth: 12, font: { size: 11 } } } },
      scales: {
        x: { ticks: { color: '#475569', font: { size: 10 } }, grid: { color: '#1e1e2e' } },
        y: { position: 'left', ticks: { color: '#f97316', callback: v => '$' + v.toFixed(2) }, grid: { color: '#1e1e2e' } },
        y1: { position: 'right', ticks: { color: '#60a5fa', callback: v => v + 'K' }, grid: { display: false } }
      }
    }
  });
}

async function updateEstimatesChart() {
  const data = await fetchJson('/api/estimates');
  if (!data || data.length === 0) return;

  const items = data.slice().reverse().slice(-15);
  const labels = items.map((d, i) => d.outcome.slice(0, 12));
  const estimates = items.map(d => d.claude_estimate);
  const markets = items.map(d => d.market_midpoint);
  const colors = items.map(d => d.tradeable ? '#22c55e' : '#475569');

  if (estimatesChart) estimatesChart.destroy();
  estimatesChart = new Chart($('estimates-chart'), {
    type: 'bar',
    data: {
      labels,
      datasets: [
        { label: 'Claude Est.', data: estimates, backgroundColor: colors, borderRadius: 4, barPercentage: 0.4 },
        { label: 'Market Price', data: markets, backgroundColor: '#1e1e2e', borderColor: '#818cf8', borderWidth: 2, borderRadius: 4, barPercentage: 0.4 },
      ]
    },
    options: {
      responsive: true,
      plugins: { legend: { labels: { color: '#94a3b8', boxWidth: 12, font: { size: 11 } } } },
      scales: {
        x: { ticks: { color: '#475569', font: { size: 9 }, maxRotation: 45 }, grid: { display: false } },
        y: { ticks: { color: '#475569', callback: v => (v * 100).toFixed(0) + '%' }, grid: { color: '#1e1e2e' }, max: 1 }
      }
    }
  });
}

async function updatePositions() {
  const data = await fetchJson('/api/positions');
  const el = $('positions-table');
  if (!data || data.length === 0) { el.innerHTML = '<p class="text-slate-600 text-sm">No open positions</p>'; return; }

  el.innerHTML = `<table class="w-full text-sm">
    <thead><tr class="text-slate-500 text-xs uppercase">
      <th class="text-left pb-2">Market</th>
      <th class="text-left pb-2">Side</th>
      <th class="text-right pb-2">Shares</th>
      <th class="text-right pb-2">Entry</th>
      <th class="text-right pb-2">Opened</th>
    </tr></thead>
    <tbody>${data.map(p => `<tr class="border-t border-slate-800">
      <td class="py-2 pr-3 max-w-[250px] truncate">${p.market_question}</td>
      <td><span class="px-2 py-0.5 rounded text-xs font-medium badge-yes">${p.outcome}</span></td>
      <td class="text-right font-mono">${p.size.toFixed(1)}</td>
      <td class="text-right font-mono">$${p.avg_entry_price.toFixed(3)}</td>
      <td class="text-right text-slate-500">${fmtTime(p.created_at)}</td>
    </tr>`).join('')}</tbody></table>`;
}

async function updateTrades() {
  const data = await fetchJson('/api/trades');
  const el = $('trades-table');
  if (!data || data.length === 0) { el.innerHTML = '<p class="text-slate-600 text-sm">No trades yet</p>'; return; }

  el.innerHTML = `<table class="w-full text-sm">
    <thead><tr class="text-slate-500 text-xs uppercase">
      <th class="text-left pb-2">Time</th>
      <th class="text-left pb-2">Side</th>
      <th class="text-left pb-2">Outcome</th>
      <th class="text-right pb-2">Price</th>
      <th class="text-right pb-2">Size</th>
      <th class="text-right pb-2">P&L</th>
    </tr></thead>
    <tbody>${data.map(t => `<tr class="border-t border-slate-800">
      <td class="py-2 text-slate-400">${fmtTime(t.executed_at)}</td>
      <td><span class="px-2 py-0.5 rounded text-xs font-medium ${t.side === 'BUY' ? 'badge-buy' : 'badge-sell'}">${t.side}</span></td>
      <td class="max-w-[150px] truncate">${t.outcome}</td>
      <td class="text-right font-mono">$${t.price.toFixed(3)}</td>
      <td class="text-right font-mono">${t.size.toFixed(1)}</td>
      <td class="text-right font-mono ${t.pnl !== null ? pnlClass(t.pnl) : 'text-slate-600'}">${t.pnl !== null ? '$' + t.pnl.toFixed(2) : '-'}</td>
    </tr>`).join('')}</tbody></table>`;
}

async function updateEstimatesTable() {
  const data = await fetchJson('/api/estimates');
  const el = $('estimates-table');
  if (!data || data.length === 0) { el.innerHTML = '<p class="text-slate-600 text-sm">No estimates yet</p>'; return; }

  el.innerHTML = `<table class="w-full text-sm">
    <thead><tr class="text-slate-500 text-xs uppercase">
      <th class="text-left pb-2">Time</th>
      <th class="text-left pb-2">Outcome</th>
      <th class="text-right pb-2">Claude</th>
      <th class="text-right pb-2">Market</th>
      <th class="text-right pb-2">Edge</th>
      <th class="text-center pb-2">Trade?</th>
      <th class="text-left pb-2 pl-4">Reasoning</th>
    </tr></thead>
    <tbody>${data.map(e => `<tr class="border-t border-slate-800">
      <td class="py-2 text-slate-400">${fmtTime(e.created_at)}</td>
      <td>${e.outcome}</td>
      <td class="text-right font-mono">${(e.claude_estimate * 100).toFixed(1)}%</td>
      <td class="text-right font-mono">${(e.market_midpoint * 100).toFixed(1)}%</td>
      <td class="text-right font-mono ${pnlClass(e.edge)}">${(e.edge * 100).toFixed(1)}%</td>
      <td class="text-center">${e.tradeable ? '<span class="text-green-400 font-bold">YES</span>' : '<span class="text-slate-600">no</span>'}</td>
      <td class="text-slate-500 text-xs pl-4 max-w-[300px] truncate">${e.reasoning}</td>
    </tr>`).join('')}</tbody></table>`;
}

async function updateCycles() {
  const data = await fetchJson('/api/cycles');
  const el = $('cycles-list');
  if (!data || data.length === 0) { el.innerHTML = '<p class="text-slate-600 text-sm">No cycles yet</p>'; return; }

  el.innerHTML = data.map(c => `
    <div class="border-l-2 ${c.stop_losses > 0 ? 'border-red-500' : 'border-slate-700'} pl-4 py-1">
      <div class="flex items-center gap-3">
        <span class="text-xs text-slate-500">${fmtTime(c.time)}</span>
        <span class="text-xs font-medium text-slate-400">Cycle ${c.cycle}</span>
        <span class="text-xs text-slate-500">Equity: ${fmtUsd(c.equity)}</span>
        ${c.stop_losses > 0 ? '<span class="text-xs text-red-400">Stop loss x' + c.stop_losses + '</span>' : ''}
      </div>
      ${c.summary ? '<p class="text-xs text-slate-600 mt-1 line-clamp-3">' + c.summary.slice(0, 300) + '</p>' : ''}
    </div>
  `).join('');
}

async function refreshAll() {
  await Promise.all([
    updateSummary(),
    updateEquityChart(),
    updateCostChart(),
    updateEstimatesChart(),
    updatePositions(),
    updateTrades(),
    updateEstimatesTable(),
    updateCycles(),
  ]);
}

refreshAll();
setInterval(refreshAll, 15000);
</script>
</body>
</html>"""


@app.get("/", response_class=HTMLResponse)
def dashboard():
    return DASHBOARD_HTML


def main():
    print("BuckFiddy Dashboard starting at http://localhost:8050")
    uvicorn.run(app, host="0.0.0.0", port=8050, log_level="warning")


if __name__ == "__main__":
    main()

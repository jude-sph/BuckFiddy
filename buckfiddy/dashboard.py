"""BuckFiddy Dashboard — localhost web UI for monitoring the trading agent.

Run:  python -m buckfiddy.dashboard
Then open http://localhost:8050
"""

import collections
import logging
import sys
import threading
import time

from fastapi import FastAPI
from fastapi.responses import HTMLResponse, JSONResponse
import uvicorn

from buckfiddy.agent.loop import AgentLoop
from buckfiddy.config import Settings
from buckfiddy.markets.scanner import MarketScanner
from buckfiddy.state.store import StateStore
from buckfiddy.trading.mock import MockTradingBackend

app = FastAPI(title="BuckFiddy Dashboard")
store: StateStore | None = None
agent: AgentLoop | None = None
agent_thread: threading.Thread | None = None

# ── Log capture ────────────────────────────────────────────
LOG_BUFFER: collections.deque[str] = collections.deque(maxlen=500)
_log_counter = 0


class DashboardLogHandler(logging.Handler):
    """Captures log records into an in-memory ring buffer for the dashboard."""
    def emit(self, record):
        global _log_counter
        try:
            msg = self.format(record)
            _log_counter += 1
            LOG_BUFFER.append(msg)
        except Exception:
            pass


def _prefill_log_buffer():
    """Load recent lines from the log file into LOG_BUFFER on startup."""
    global _log_counter
    try:
        import os
        log_path = os.path.join("data", "buckfiddy.log")
        if os.path.exists(log_path):
            with open(log_path, "r") as f:
                lines = f.readlines()
            # Take the last 500 lines (matching buffer size)
            recent = lines[-500:] if len(lines) > 500 else lines
            for line in recent:
                stripped = line.rstrip("\n")
                if stripped:
                    _log_counter += 1
                    LOG_BUFFER.append(stripped)
    except Exception:
        pass


_prefill_log_buffer()


def get_store() -> StateStore:
    global store
    if store is None:
        settings = Settings()
        store = StateStore(settings.DB_PATH, check_same_thread=False)
    return store


def get_or_create_agent() -> AgentLoop:
    global agent
    if agent is None:
        settings = Settings()
        if settings.TRADING_BACKEND == "mock":
            backend = MockTradingBackend(settings)
        else:
            from buckfiddy.trading.real import RealTradingBackend
            backend = RealTradingBackend(settings)
        scanner = MarketScanner(settings)
        agent = AgentLoop(backend, scanner, settings)
    return agent


# ── Live price updater ─────────────────────────────────────
# Caches the latest wallet state (with live prices) so the dashboard
# doesn't need the agent to be running to show current values.

_live_state: dict = {}
_live_state_lock = threading.Lock()
_price_updater_started = False


def _price_update_loop():
    """Background thread: fetch live prices for open positions every 30s."""
    while True:
        try:
            a = get_or_create_agent()
            wallet = a.backend.get_wallet_state()
            with _live_state_lock:
                _live_state["cash"] = wallet.balance
                _live_state["position_value"] = wallet.total_position_value
                _live_state["total_value"] = wallet.total_equity
                _live_state["num_positions"] = len(wallet.positions)
                _live_state["positions"] = [
                    {
                        "position_id": p.position_id,
                        "market_question": p.market_question,
                        "outcome": p.outcome,
                        "size": p.size,
                        "avg_entry_price": p.avg_entry_price,
                        "current_value": p.current_value,
                        "unrealized_pnl": p.unrealized_pnl,
                        "unrealized_pnl_pct": p.unrealized_pnl_pct,
                    }
                    for p in wallet.positions
                ]
        except Exception as e:
            logging.getLogger("buckfiddy.price_updater").debug(f"Price update failed: {e}")
        time.sleep(30)


def _ensure_price_updater():
    global _price_updater_started
    if not _price_updater_started:
        _price_updater_started = True
        t = threading.Thread(target=_price_update_loop, daemon=True, name="price-updater")
        t.start()


# ── API Routes ──────────────────────────────────────────────


@app.get("/api/summary")
def api_summary():
    _ensure_price_updater()
    s = get_store()

    # Use live state if available (updated every 30s with real prices)
    with _live_state_lock:
        live = dict(_live_state)

    if live:
        cash = live["cash"]
        pos_value = live["position_value"]
        total_value = live["total_value"]
        num_pos = live["num_positions"]
    else:
        wallet = s.fetchone("SELECT balance FROM wallet WHERE id = 1")
        cash = wallet["balance"] if wallet else 0
        pos_value = 0
        total_value = cash
        num_pos = 0

    starting = 100.0
    first_snap = s.fetchone(
        "SELECT equity FROM equity_snapshots ORDER BY id ASC LIMIT 1"
    )
    if first_snap:
        starting = first_snap["equity"]

    # Read live API cost from api_usage (updated mid-cycle)
    live_cost = s.fetchone(
        "SELECT COALESCE(SUM(cost_usd), 0) as total FROM api_usage"
    )
    api_cost = live_cost["total"] if live_cost and live_cost["total"] else 0

    pnl = total_value - starting
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

    # Use agent's live settings (reflects model switches) instead of fresh Settings()
    a = get_or_create_agent()
    live_settings = a.settings

    # Model names (short form for display)
    def _short_model(m):
        parts = m.split("-")
        if len(parts) >= 2:
            return parts[1].capitalize() + (" " + parts[2] if len(parts) > 2 else "")
        return m

    return {
        "cash": round(cash, 2),
        "position_value": round(pos_value, 2),
        "total_value": round(total_value, 2),
        "num_positions": num_pos,
        "starting": round(starting, 2),
        "pnl": round(pnl, 2),
        "pnl_pct": round(pnl_pct, 1),
        "api_cost": round(api_cost, 4),
        "cycles": cycles["n"] if cycles else 0,
        "total_trades": total_trades["n"] if total_trades else 0,
        "wins": wins["n"] if wins else 0,
        "backend": live_settings.TRADING_BACKEND,
        "losses": losses["n"] if losses else 0,
        "realized_pnl": round(realized["v"], 2) if realized else 0,
        "estimates": est_count["n"] if est_count else 0,
        "tradeable_edges": tradeable_count["n"] if tradeable_count else 0,
        "model_fast": _short_model(live_settings.CLAUDE_MODEL_FAST),
        "model_fast_id": live_settings.CLAUDE_MODEL_FAST,
        "model_research": _short_model(live_settings.CLAUDE_MODEL_RESEARCH),
        "model_research_id": live_settings.CLAUDE_MODEL_RESEARCH,
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
    # Use live state (with real-time prices) if available
    with _live_state_lock:
        live_positions = _live_state.get("positions")

    if live_positions:
        return live_positions

    # Fallback: read from DB (no live P&L)
    s = get_store()
    rows = s.fetchall("SELECT * FROM positions WHERE size > 0")
    return [
        {
            "position_id": r["position_id"],
            "market_question": r["market_question"],
            "outcome": r["outcome"],
            "size": r["size"],
            "avg_entry_price": r["avg_entry_price"],
            "current_value": round(r["size"] * r["avg_entry_price"], 4),
            "unrealized_pnl": 0,
            "unrealized_pnl_pct": 0,
        }
        for r in rows
    ]


@app.get("/api/trades")
def api_trades():
    s = get_store()
    rows = s.fetchall(
        "SELECT t.*, e.claude_estimate, e.market_midpoint, e.edge, "
        "e.reasoning AS estimate_reasoning, e.market_question "
        "FROM trades t LEFT JOIN estimates e ON t.estimate_id = e.id "
        "ORDER BY t.executed_at DESC LIMIT 50"
    )
    return [
        {
            "trade_id": r["trade_id"],
            "outcome": r["outcome"],
            "side": r["side"],
            "price": r["price"],
            "size": r["size"],
            "pnl": r["pnl"],
            "estimate_id": r["estimate_id"],
            "claude_estimate": r["claude_estimate"],
            "market_midpoint": r["market_midpoint"],
            "edge": r["edge"],
            "reasoning": r["estimate_reasoning"],
            "market_question": r["market_question"],
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
            "id": r["id"],
            "market_question": r["market_question"] if "market_question" in r.keys() else "",
            "outcome": r["outcome"],
            "claude_estimate": r["claude_estimate"],
            "market_midpoint": r["market_midpoint"],
            "edge": r["edge"],
            "tradeable": bool(r["tradeable"]),
            "reasoning": r["reasoning"],
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


@app.post("/api/reset")
def api_reset():
    if agent and agent.running:
        return JSONResponse({"error": "Stop the agent before resetting"}, status_code=400)
    s = get_store()
    for table in ["estimates", "trades", "orders", "positions", "cycle_log", "api_usage", "equity_snapshots"]:
        s.execute(f"DELETE FROM {table}")
    s.execute("UPDATE wallet SET balance = 100.0 WHERE id = 1")
    s.execute("DELETE FROM sqlite_sequence")  # Reset autoincrement counters
    s.commit()
    # Reset agent cycle counter so next run starts fresh
    if agent:
        agent.cycle_count = 0
        agent.dispatcher.reset_cycle_counters()
    return {"status": "ok", "message": "All data cleared, balance reset to $100"}


@app.get("/api/agent/status")
def api_agent_status():
    running = agent is not None and agent.running
    settings = Settings()
    a = agent
    api_key = a.settings.ANTHROPIC_API_KEY if a else settings.ANTHROPIC_API_KEY
    return {
        "running": running,
        "cycle": a.cycle_count if a else 0,
        "backend": settings.TRADING_BACKEND,
        "has_api_key": bool(api_key),
        "cycle_type": a.current_cycle_type if a else "",
        "phase": a.current_phase if a else "",
        "full_interval": a.settings.FULL_CYCLE_INTERVAL_SECONDS if a else settings.FULL_CYCLE_INTERVAL_SECONDS,
        "light_interval": a.settings.POSITION_CHECK_INTERVAL_SECONDS if a else settings.POSITION_CHECK_INTERVAL_SECONDS,
        "markets_per_cycle": a.settings.MAX_NEW_ESTIMATES_PER_CYCLE if a else settings.MAX_NEW_ESTIMATES_PER_CYCLE,
    }


@app.post("/api/backend/switch")
def api_backend_switch(body: dict):
    global agent, agent_thread
    target = body.get("backend", "").lower()
    if target not in ("mock", "real"):
        return JSONResponse({"error": "backend must be 'mock' or 'real'"}, status_code=400)

    if agent and agent.running:
        return JSONResponse({"error": "Stop the agent before switching backends"}, status_code=400)

    # Update the .env file
    import os
    env_path = os.path.join(os.path.dirname(os.path.dirname(__file__)), ".env")
    if os.path.exists(env_path):
        with open(env_path) as f:
            lines = f.readlines()
        found = False
        new_lines = []
        for line in lines:
            if line.strip().startswith("BF_TRADING_BACKEND"):
                new_lines.append(f"BF_TRADING_BACKEND={target}\n")
                found = True
            else:
                new_lines.append(line)
        if not found:
            new_lines.append(f"BF_TRADING_BACKEND={target}\n")
        with open(env_path, "w") as f:
            f.writelines(new_lines)

    # Force recreate agent on next start
    agent = None
    agent_thread = None

    # Clear settings cache by reloading
    os.environ["BF_TRADING_BACKEND"] = target

    return {"status": "ok", "backend": target}


# Available models for the dropdowns
AVAILABLE_MODELS = [
    {"id": "claude-haiku-4-5", "label": "Haiku 4.5"},
    {"id": "claude-sonnet-4-5", "label": "Sonnet 4.5"},
    {"id": "claude-opus-4-6", "label": "Opus 4.6"},
]


@app.get("/api/models")
def api_models():
    settings = Settings()
    return {
        "available": AVAILABLE_MODELS,
        "fast": settings.CLAUDE_MODEL_FAST,
        "research": settings.CLAUDE_MODEL_RESEARCH,
    }


@app.post("/api/models/switch")
def api_models_switch(body: dict):
    if agent and agent.running:
        return JSONResponse(
            {"error": "Stop the agent before changing models"}, status_code=400
        )

    model_ids = {m["id"] for m in AVAILABLE_MODELS}
    fast = body.get("fast")
    research = body.get("research")

    if fast and fast not in model_ids:
        return JSONResponse({"error": f"Unknown model: {fast}"}, status_code=400)
    if research and research not in model_ids:
        return JSONResponse({"error": f"Unknown model: {research}"}, status_code=400)

    # Update the agent's settings in memory
    a = get_or_create_agent()
    if fast:
        a.settings.CLAUDE_MODEL_FAST = fast
    if research:
        a.settings.CLAUDE_MODEL_RESEARCH = research

    return {
        "status": "ok",
        "fast": a.settings.CLAUDE_MODEL_FAST,
        "research": a.settings.CLAUDE_MODEL_RESEARCH,
    }


@app.post("/api/timing/update")
def api_timing_update(body: dict):
    full = body.get("full_interval")
    light = body.get("light_interval")
    markets = body.get("markets_per_cycle")

    a = get_or_create_agent()
    if full is not None:
        val = max(60, int(full))  # Minimum 1 minute
        a.settings.FULL_CYCLE_INTERVAL_SECONDS = val
    if light is not None:
        val = max(30, int(light))  # Minimum 30 seconds
        a.settings.POSITION_CHECK_INTERVAL_SECONDS = val
    if markets is not None:
        val = max(1, min(5, int(markets)))  # 1-5 markets
        a.settings.MAX_NEW_ESTIMATES_PER_CYCLE = val

    return {
        "status": "ok",
        "full_interval": a.settings.FULL_CYCLE_INTERVAL_SECONDS,
        "light_interval": a.settings.POSITION_CHECK_INTERVAL_SECONDS,
        "markets_per_cycle": a.settings.MAX_NEW_ESTIMATES_PER_CYCLE,
    }


_agent_lock = threading.Lock()


@app.post("/api/agent/start")
def api_agent_start():
    global agent_thread
    with _agent_lock:
        a = get_or_create_agent()
        if not a.settings.ANTHROPIC_API_KEY:
            return JSONResponse(
                {"error": "No API key configured. Set BF_ANTHROPIC_API_KEY in your .env file."},
                status_code=400,
            )
        if a.running or (agent_thread and agent_thread.is_alive()):
            return {"status": "already_running", "cycle": a.cycle_count}
        agent_thread = threading.Thread(target=a.run, daemon=True, name="buckfiddy-agent")
        agent_thread.start()
    return {"status": "started"}


@app.post("/api/agent/stop")
def api_agent_stop():
    if agent is None or not agent.running:
        return {"status": "not_running"}
    agent.request_stop()
    return {"status": "stopping", "message": "Agent will stop after current cycle completes"}


@app.post("/api/agent/single")
def api_agent_single():
    global agent_thread
    with _agent_lock:
        a = get_or_create_agent()
        if not a.settings.ANTHROPIC_API_KEY:
            return JSONResponse(
                {"error": "No API key configured. Set BF_ANTHROPIC_API_KEY in your .env file."},
                status_code=400,
            )
        if a.running or (agent_thread and agent_thread.is_alive()):
            return JSONResponse({"error": "Agent is already running"}, status_code=400)
        agent_thread = threading.Thread(target=a.run_single_cycle, daemon=True, name="buckfiddy-single")
        agent_thread.start()
    return {"status": "started_single"}


@app.get("/api/logs")
def api_logs(after: int = 0):
    """Return log lines. Client passes `after=N` to get only new lines."""
    current = _log_counter
    # If client is caught up, return nothing
    if after > 0 and after >= current:
        return {"lines": [], "cursor": current}
    # If client has a cursor, only send new lines
    if after > 0:
        new_count = current - after
        lines = list(LOG_BUFFER)
        lines = lines[-new_count:] if new_count < len(lines) else lines
    else:
        lines = list(LOG_BUFFER)
    return {"lines": lines, "cursor": current}


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
  <!-- Status Banner -->
  <div id="status-banner" class="mb-6 rounded-xl px-5 py-3 flex items-center justify-between border transition-all duration-300 bg-slate-900/50 border-slate-800">
    <div class="flex items-center gap-4">
      <span id="status-dot" class="w-3 h-3 rounded-full bg-slate-600 shrink-0"></span>
      <div>
        <span id="status-text" class="text-sm font-semibold text-slate-400">Stopped</span>
        <span id="status-detail" class="text-xs text-slate-600 ml-3"></span>
        <span id="phase-badge" class="hidden ml-2 px-2 py-0.5 rounded text-xs font-medium bg-purple-900/50 text-purple-300 border border-purple-800"></span>
      </div>
    </div>
    <div class="flex items-center gap-2">
      <span id="last-update" class="text-xs text-slate-600 mr-2"></span>
      <button id="btn-start" onclick="agentStart()" class="px-4 py-2 text-sm font-medium text-green-400 border border-green-900 rounded-lg hover:bg-green-900/30 transition-colors">Start Agent</button>
      <button id="btn-single" onclick="agentSingle()" class="px-4 py-2 text-sm font-medium text-blue-400 border border-blue-900 rounded-lg hover:bg-blue-900/30 transition-colors">Run 1 Cycle</button>
      <button id="btn-stop" onclick="agentStop()" class="px-4 py-2 text-sm font-medium text-amber-400 border border-amber-900 rounded-lg hover:bg-amber-900/30 transition-colors hidden">Stop Agent</button>
      <select id="backend-select" onchange="switchBackend(this.value)" class="px-3 py-2 text-sm font-medium text-slate-300 bg-slate-800 border border-slate-700 rounded-lg cursor-pointer">
        <option value="mock">Mock Trading</option>
        <option value="real">Real Trading</option>
      </select>
      <button onclick="resetData()" class="px-4 py-2 text-sm font-medium text-red-400 border border-red-900/50 rounded-lg hover:bg-red-900/30 transition-colors">Reset</button>
    </div>
  </div>

  <!-- Header -->
  <div class="flex items-center justify-between mb-8">
    <div>
      <h1 class="text-3xl font-bold text-white">BuckFiddy</h1>
      <p class="text-sm text-slate-500 mt-1">Autonomous Polymarket Trading Agent
        <span id="backend-badge" class="ml-2 px-2 py-0.5 rounded text-xs font-medium bg-blue-900/50 text-blue-400 border border-blue-800">MOCK</span>
      </p>
      <div class="flex items-center gap-3 mt-1 flex-wrap">
        <span class="text-xs text-slate-600">Fast:</span>
        <select id="model-fast-select" onchange="switchModel('fast', this.value)" class="px-2 py-0.5 text-xs text-slate-300 bg-slate-800 border border-slate-700 rounded cursor-pointer"></select>
        <span class="text-xs text-slate-600">Research:</span>
        <select id="model-research-select" onchange="switchModel('research', this.value)" class="px-2 py-0.5 text-xs text-slate-300 bg-slate-800 border border-slate-700 rounded cursor-pointer"></select>
        <span class="text-slate-700 mx-1">|</span>
        <span class="text-xs text-slate-600">Full cycle every</span>
        <input id="timing-full" type="number" min="1" step="1" class="w-12 px-1 py-0.5 text-xs font-mono text-slate-300 bg-slate-800 border border-slate-700 rounded text-center" onchange="updateTiming()">
        <span class="text-xs text-slate-600">min, check every</span>
        <input id="timing-light" type="number" min="1" step="1" class="w-12 px-1 py-0.5 text-xs font-mono text-slate-300 bg-slate-800 border border-slate-700 rounded text-center" onchange="updateTiming()">
        <span class="text-xs text-slate-600">min</span>
        <span class="text-slate-700 mx-1">|</span>
        <span class="text-xs text-slate-600">Research</span>
        <input id="timing-markets" type="number" min="1" max="5" step="1" class="w-12 px-1 py-0.5 text-xs font-mono text-slate-300 bg-slate-800 border border-slate-700 rounded text-center" onchange="updateTiming()">
        <span class="text-xs text-slate-600">markets/cycle</span>
      </div>
    </div>
  </div>

  <!-- Stats Row -->
  <div class="grid grid-cols-2 md:grid-cols-4 lg:grid-cols-7 gap-4 mb-6">
    <div class="card p-4">
      <div class="text-xs text-slate-500 uppercase tracking-wider">Cash</div>
      <div id="stat-cash" class="text-xl font-bold text-white mt-1">-</div>
    </div>
    <div class="card p-4">
      <div class="text-xs text-slate-500 uppercase tracking-wider">Total Value</div>
      <div id="stat-total-value" class="text-xl font-bold text-white mt-1">-</div>
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
      <div class="mt-2 flex items-center gap-1">
        <span class="text-xs text-slate-600">Limit $</span>
        <input id="cost-limit" type="number" step="0.5" min="0" value="5" class="w-14 px-1 py-0.5 text-xs font-mono text-orange-300 bg-slate-800 border border-slate-700 rounded" onchange="saveCostLimit()">
      </div>
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

  <!-- Open Positions -->
  <div class="card p-6 mb-6">
    <h2 class="text-sm font-semibold text-slate-400 uppercase tracking-wider mb-4">Open Positions</h2>
    <div id="positions-table" class="overflow-x-auto">
      <p class="text-slate-600 text-sm">No positions</p>
    </div>
  </div>

  <!-- Recent Trades -->
  <div class="card p-6 mb-6">
    <h2 class="text-sm font-semibold text-slate-400 uppercase tracking-wider mb-4">Recent Trades</h2>
    <div id="trades-table" class="overflow-x-auto">
      <p class="text-slate-600 text-sm">No trades yet</p>
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

  <!-- Terminal Log -->
  <div class="card p-6 mb-6">
    <div class="flex items-center justify-between mb-4">
      <h2 class="text-sm font-semibold text-slate-400 uppercase tracking-wider">Terminal Output</h2>
      <button onclick="$('log-output').textContent='';" class="text-xs text-slate-600 hover:text-slate-400 transition-colors">Clear</button>
    </div>
    <div id="log-output" class="bg-black/50 rounded-lg p-4 font-mono text-xs text-green-400 h-80 overflow-y-auto whitespace-pre-wrap leading-relaxed border border-slate-800"></div>
  </div>

</div>

<script>
const $ = id => document.getElementById(id);
let equityChart, costChart, estimatesChart;
let logCursor = 0;
// Track expanded rows so refreshes don't collapse them
const expandedTrades = new Set();
const expandedEstimates = new Set();

// Cost limit (persisted in localStorage)
let costLimitAlerted = false;
function getCostLimit() {
  const v = localStorage.getItem('bf_cost_limit');
  return v ? parseFloat(v) : 5.0;
}
function saveCostLimit() {
  const v = parseFloat($('cost-limit').value) || 0;
  localStorage.setItem('bf_cost_limit', v);
}
// Restore on load
(function() {
  const saved = getCostLimit();
  const el = document.getElementById('cost-limit');
  if (el) el.value = saved;
})();

function fmtUsd(v) { return '$' + Number(v).toFixed(2); }
function fmtPct(v) { return (v >= 0 ? '+' : '') + Number(v).toFixed(1) + '%'; }
function fmtTime(iso) {
  if (!iso) return '-';
  const d = new Date(iso);
  return d.toLocaleDateString('en-GB', {day:'2-digit',month:'short'}) + ' ' +
         d.toLocaleTimeString('en-GB', {hour:'2-digit',minute:'2-digit'});
}
function pnlClass(v) { return v > 0 ? 'stat-up' : v < 0 ? 'stat-down' : 'stat-neutral'; }

function toggleTrade(id) {
  if (expandedTrades.has(id)) expandedTrades.delete(id); else expandedTrades.add(id);
  const el = document.getElementById('trade-detail-' + id);
  if (el) el.classList.toggle('hidden');
}
function toggleEstimate(id) {
  if (expandedEstimates.has(id)) expandedEstimates.delete(id); else expandedEstimates.add(id);
  const el = document.getElementById('est-detail-' + id);
  if (el) el.classList.toggle('hidden');
}

async function fetchJson(url) {
  const r = await fetch(url);
  return r.ok ? r.json() : null;
}

async function updateSummary() {
  const d = await fetchJson('/api/summary');
  if (!d) return;

  $('stat-cash').textContent = fmtUsd(d.cash);
  $('stat-total-value').textContent = fmtUsd(d.total_value);

  const pnlEl = $('stat-pnl');
  pnlEl.textContent = fmtUsd(d.pnl) + ' (' + fmtPct(d.pnl_pct) + ')';
  pnlEl.className = 'text-xl font-bold mt-1 ' + pnlClass(d.pnl);

  $('stat-positions').textContent = d.num_positions;
  $('stat-trades').textContent = d.total_trades;
  $('stat-apicost').textContent = '$' + d.api_cost.toFixed(4);

  // Check cost limit — auto-stop agent if exceeded
  const costLimit = getCostLimit();
  if (costLimit > 0 && d.api_cost >= costLimit) {
    $('stat-apicost').className = 'text-xl font-bold text-red-500 mt-1';
    if (!costLimitAlerted) {
      costLimitAlerted = true;
      fetch('/api/agent/status').then(r => r.json()).then(s => {
        if (s.running) {
          fetch('/api/agent/stop', { method: 'POST' });
          alert('API cost limit ($' + costLimit.toFixed(2) + ') reached! Agent has been stopped.');
          updateAgentStatus();
        }
      });
    }
  } else {
    $('stat-apicost').className = 'text-xl font-bold text-orange-400 mt-1';
    costLimitAlerted = false;
  }

  const closed = d.wins + d.losses;
  $('stat-winrate').textContent = closed > 0 ? Math.round(d.wins / closed * 100) + '%' : '-';

  $('last-update').textContent = 'Updated ' + new Date().toLocaleTimeString();

  // Sync model dropdowns with current values
  if (d.model_fast_id) $('model-fast-select').value = d.model_fast_id;
  if (d.model_research_id) $('model-research-select').value = d.model_research_id;
}

async function updateAgentStatus() {
  const d = await fetchJson('/api/agent/status');
  if (!d) return;
  const running = d.running;
  const banner = $('status-banner');

  const phaseBadge = $('phase-badge');
  if (running) {
    banner.className = 'mb-6 rounded-xl px-5 py-3 flex items-center justify-between border transition-all duration-300 bg-green-950/40 border-green-800';
    $('status-dot').className = 'w-3 h-3 rounded-full bg-green-500 pulse shrink-0';
    $('status-text').textContent = 'AGENT RUNNING';
    $('status-text').className = 'text-sm font-bold text-green-400';
    const cycleLabel = d.cycle_type === 'full' ? 'Full Cycle' : d.cycle_type === 'light' ? 'Position Check' : 'Cycle';
    $('status-detail').textContent = cycleLabel + ' ' + d.cycle;
    $('status-detail').className = 'text-xs text-green-600 ml-3';
    document.title = 'RUNNING — BuckFiddy';
    // Phase badge
    if (d.phase) {
      phaseBadge.textContent = d.phase;
      phaseBadge.classList.remove('hidden');
      if (d.phase === 'Sleeping') {
        phaseBadge.className = 'ml-2 px-2 py-0.5 rounded text-xs font-medium bg-slate-800 text-slate-400 border border-slate-700';
      } else if (d.phase.startsWith('Research')) {
        phaseBadge.className = 'ml-2 px-2 py-0.5 rounded text-xs font-medium bg-amber-900/50 text-amber-300 border border-amber-800';
      } else {
        phaseBadge.className = 'ml-2 px-2 py-0.5 rounded text-xs font-medium bg-purple-900/50 text-purple-300 border border-purple-800';
      }
    } else {
      phaseBadge.classList.add('hidden');
    }
  } else {
    banner.className = 'mb-6 rounded-xl px-5 py-3 flex items-center justify-between border transition-all duration-300 bg-slate-900/50 border-slate-800';
    $('status-dot').className = 'w-3 h-3 rounded-full bg-slate-600 shrink-0';
    $('status-text').textContent = 'STOPPED';
    $('status-text').className = 'text-sm font-semibold text-slate-400';
    $('status-detail').textContent = d.cycle > 0 ? 'Completed ' + d.cycle + ' cycles' : '';
    $('status-detail').className = 'text-xs text-slate-600 ml-3';
    document.title = 'BuckFiddy Dashboard';
    $('btn-stop').disabled = false;
    $('btn-stop').textContent = 'Stop Agent';
    phaseBadge.classList.add('hidden');
  }

  // API key warning
  if (!d.has_api_key) {
    banner.className = 'mb-6 rounded-xl px-5 py-3 flex items-center justify-between border transition-all duration-300 bg-red-950/40 border-red-800';
    $('status-dot').className = 'w-3 h-3 rounded-full bg-red-500 shrink-0';
    $('status-text').textContent = 'NO API KEY';
    $('status-text').className = 'text-sm font-bold text-red-400';
    $('status-detail').textContent = 'Set BF_ANTHROPIC_API_KEY in .env and restart the dashboard';
    $('status-detail').className = 'text-xs text-red-600 ml-3';
  }

  // Sync timing inputs (convert seconds to minutes for display)
  if (d.full_interval && !$('timing-full').matches(':focus')) {
    $('timing-full').value = Math.round(d.full_interval / 60);
  }
  if (d.light_interval && !$('timing-light').matches(':focus')) {
    $('timing-light').value = Math.round(d.light_interval / 60);
  }
  if (d.markets_per_cycle && !$('timing-markets').matches(':focus')) {
    $('timing-markets').value = d.markets_per_cycle;
  }

  $('btn-start').classList.toggle('hidden', running);
  $('btn-single').classList.toggle('hidden', running);
  $('btn-stop').classList.toggle('hidden', !running);

  // Backend badge + select
  const be = d.backend || 'mock';
  const badge = $('backend-badge');
  if (be === 'real') {
    badge.textContent = 'REAL';
    badge.className = 'ml-2 px-2 py-0.5 rounded text-xs font-medium bg-red-900/50 text-red-400 border border-red-800';
  } else {
    badge.textContent = 'MOCK';
    badge.className = 'ml-2 px-2 py-0.5 rounded text-xs font-medium bg-blue-900/50 text-blue-400 border border-blue-800';
  }
  $('backend-select').value = be;
  $('backend-select').disabled = running;
  $('model-fast-select').disabled = running;
  $('model-research-select').disabled = running;
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
      <th class="text-right pb-2">Value</th>
      <th class="text-right pb-2">P&L</th>
    </tr></thead>
    <tbody>${data.map(p => `<tr class="border-t border-slate-800">
      <td class="py-2 pr-3 max-w-[200px] truncate" title="${(p.market_question || '').replace(/"/g, '&quot;')}">${p.market_question}</td>
      <td><span class="px-2 py-0.5 rounded text-xs font-medium badge-yes">${p.outcome}</span></td>
      <td class="text-right font-mono">${p.size.toFixed(1)}</td>
      <td class="text-right font-mono">$${p.avg_entry_price.toFixed(3)}</td>
      <td class="text-right font-mono">${fmtUsd(p.current_value)}</td>
      <td class="text-right font-mono ${pnlClass(p.unrealized_pnl)}">${fmtUsd(p.unrealized_pnl)} (${fmtPct(p.unrealized_pnl_pct * 100)})</td>
    </tr>`).join('')}</tbody></table>`;
}

async function updateTrades() {
  const data = await fetchJson('/api/trades');
  const el = $('trades-table');
  if (!data || data.length === 0) { el.innerHTML = '<p class="text-slate-600 text-sm">No trades yet</p>'; return; }

  el.innerHTML = `<table class="w-full text-sm">
    <thead><tr class="text-slate-500 text-xs uppercase">
      <th class="text-left pb-2">Time</th>
      <th class="text-left pb-2">Market</th>
      <th class="text-left pb-2">Side</th>
      <th class="text-right pb-2">Price</th>
      <th class="text-right pb-2">Size</th>
      <th class="text-right pb-2">Edge</th>
      <th class="text-right pb-2">P&L</th>
    </tr></thead>
    <tbody>${data.map(t => {
      const id = t.trade_id;
      const expanded = expandedTrades.has(id);
      return `<tr class="border-t border-slate-800 ${t.reasoning ? 'cursor-pointer' : ''}" ${t.reasoning ? 'onclick="toggleTrade(\\''+id+'\\')"' : ''}>
      <td class="py-2 text-slate-400">${fmtTime(t.executed_at)}</td>
      <td class="max-w-[250px] truncate" title="${(t.market_question || '').replace(/"/g, '&quot;')}">${t.market_question || t.outcome}</td>
      <td><span class="px-2 py-0.5 rounded text-xs font-medium ${t.side === 'BUY' ? 'badge-buy' : 'badge-sell'}">${t.side} ${t.outcome}</span></td>
      <td class="text-right font-mono">$${t.price.toFixed(3)}</td>
      <td class="text-right font-mono">${t.size.toFixed(1)}</td>
      <td class="text-right font-mono ${t.edge ? pnlClass(t.edge) : 'text-slate-600'}">${t.edge ? (t.edge * 100).toFixed(1) + '%' : '-'}</td>
      <td class="text-right font-mono ${t.pnl !== null ? pnlClass(t.pnl) : 'text-slate-600'}">${t.pnl !== null ? '$' + t.pnl.toFixed(2) : '-'}</td>
    </tr>
    ${t.reasoning ? '<tr id="trade-detail-'+id+'" class="'+(expanded ? '' : 'hidden ')+' border-t border-slate-800/50"><td colspan="7" class="py-3 px-4"><div class="text-xs text-slate-400 whitespace-pre-wrap bg-slate-900/50 rounded-lg p-3"><span class="text-slate-500 font-semibold">Claude\\'s estimate:</span> ' + (t.claude_estimate * 100).toFixed(1) + '% vs market ' + (t.market_midpoint * 100).toFixed(1) + '%<br><br>' + t.reasoning + '</div></td></tr>' : ''}`;
    }).join('')}</tbody></table>`;
}

async function updateEstimatesTable() {
  const data = await fetchJson('/api/estimates');
  const el = $('estimates-table');
  if (!data || data.length === 0) { el.innerHTML = '<p class="text-slate-600 text-sm">No estimates yet</p>'; return; }

  el.innerHTML = `<table class="w-full text-sm">
    <thead><tr class="text-slate-500 text-xs uppercase">
      <th class="text-left pb-2">Time</th>
      <th class="text-left pb-2">Market</th>
      <th class="text-left pb-2">Side</th>
      <th class="text-right pb-2">Claude</th>
      <th class="text-right pb-2">Price</th>
      <th class="text-right pb-2">Edge</th>
      <th class="text-center pb-2">Trade?</th>
    </tr></thead>
    <tbody>${data.map((e, i) => {
      const id = '' + (e.id || i);
      const expanded = expandedEstimates.has(id);
      return `<tr class="border-t border-slate-800 cursor-pointer" onclick="toggleEstimate('${id}')">
      <td class="py-2 text-slate-400">${fmtTime(e.created_at)}</td>
      <td class="max-w-[350px] truncate" title="${(e.market_question || '').replace(/"/g, '&quot;')}">${e.market_question || '<span class=\\'text-slate-600\\'>—</span>'}</td>
      <td><span class="px-2 py-0.5 rounded text-xs font-medium badge-yes">${e.outcome}</span></td>
      <td class="text-right font-mono">${(e.claude_estimate * 100).toFixed(1)}%</td>
      <td class="text-right font-mono">${(e.market_midpoint * 100).toFixed(1)}%</td>
      <td class="text-right font-mono ${pnlClass(e.edge)}">${(e.edge * 100).toFixed(1)}%</td>
      <td class="text-center">${e.tradeable ? '<span class="text-green-400 font-bold">YES</span>' : '<span class="text-slate-600">no</span>'}</td>
    </tr>
    <tr id="est-detail-${id}" class="${expanded ? '' : 'hidden'} border-t border-slate-800/50">
      <td colspan="7" class="py-3 px-4">
        <div class="text-xs text-slate-400 whitespace-pre-wrap bg-slate-900/50 rounded-lg p-3">${e.reasoning}</div>
      </td>
    </tr>`;
    }).join('')}</tbody></table>`;
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

async function switchBackend(target) {
  const r = await fetch('/api/backend/switch', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({backend: target})
  });
  if (r.ok) {
    updateAgentStatus();
  } else {
    const d = await r.json();
    alert(d.error || 'Switch failed');
    updateAgentStatus();
  }
}

async function switchModel(role, modelId) {
  const body = {};
  body[role] = modelId;
  const r = await fetch('/api/models/switch', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify(body)
  });
  if (!r.ok) {
    const d = await r.json();
    alert(d.error || 'Model switch failed');
    loadModels();  // Reset dropdowns
  }
}

async function updateTiming() {
  const fullMin = parseInt($('timing-full').value) || 120;
  const lightMin = parseInt($('timing-light').value) || 30;
  const markets = parseInt($('timing-markets').value) || 2;
  await fetch('/api/timing/update', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({ full_interval: fullMin * 60, light_interval: lightMin * 60, markets_per_cycle: markets })
  });
}

async function loadModels() {
  const d = await fetchJson('/api/models');
  if (!d) return;
  for (const [role, selectId] of [['fast', 'model-fast-select'], ['research', 'model-research-select']]) {
    const sel = $(selectId);
    if (!sel.options.length) {
      d.available.forEach(m => {
        const opt = document.createElement('option');
        opt.value = m.id;
        opt.textContent = m.label;
        sel.appendChild(opt);
      });
    }
    sel.value = d[role];
  }
}

async function agentStart() {
  const r = await fetch('/api/agent/start', { method: 'POST' });
  if (r.ok) { updateAgentStatus(); }
  else { const d = await r.json(); alert(d.error || 'Start failed'); }
}

async function agentStop() {
  const r = await fetch('/api/agent/stop', { method: 'POST' });
  if (r.ok) {
    $('status-text').textContent = 'Stopping...';
    $('btn-stop').disabled = true;
    $('btn-stop').textContent = 'Stopping...';
  }
}

async function agentSingle() {
  const r = await fetch('/api/agent/single', { method: 'POST' });
  if (r.ok) { updateAgentStatus(); }
  else { const d = await r.json(); alert(d.error || 'Start failed'); }
}

async function resetData() {
  if (!confirm('Reset all data? This clears all trades, positions, estimates, and resets balance to $100.')) return;
  const r = await fetch('/api/reset', { method: 'POST' });
  if (r.ok) { refreshAll(); }
  else {
    const d = await r.json();
    alert(d.error || 'Reset failed');
  }
}

async function refreshAll() {
  await Promise.all([
    updateSummary(),
    updateAgentStatus(),
    updateEquityChart(),
    updateCostChart(),
    updateEstimatesChart(),
    updatePositions(),
    updateTrades(),
    updateEstimatesTable(),
    updateCycles(),
  ]);
}

let logAutoScroll = true;
// Detect when user manually scrolls up in the log window
document.addEventListener('DOMContentLoaded', () => {
  const el = $('log-output');
  if (el) el.addEventListener('scroll', () => {
    logAutoScroll = el.scrollHeight - el.scrollTop - el.clientHeight < 80;
  });
});

async function updateLogs() {
  const d = await fetchJson('/api/logs?after=' + logCursor);
  if (!d || !d.lines || d.lines.length === 0) return;
  const el = $('log-output');
  el.textContent += (el.textContent ? '\\n' : '') + d.lines.join('\\n');
  logCursor = d.cursor;
  if (logAutoScroll) el.scrollTop = el.scrollHeight;
}

refreshAll();
loadModels();
updateLogs();
setInterval(refreshAll, 15000);
setInterval(updateAgentStatus, 3000);
setInterval(updateLogs, 3000);
</script>
</body>
</html>"""


@app.get("/", response_class=HTMLResponse)
def dashboard():
    return DASHBOARD_HTML


def main():
    # Set up logging so agent output goes to console + file + dashboard buffer
    import os
    os.makedirs("data", exist_ok=True)
    fmt = logging.Formatter(
        "%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    dashboard_handler = DashboardLogHandler()
    dashboard_handler.setFormatter(fmt)

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
        handlers=[
            logging.StreamHandler(sys.stdout),
            logging.FileHandler("data/buckfiddy.log"),
            dashboard_handler,
        ],
    )
    print("BuckFiddy Dashboard starting at http://localhost:8050")
    print("Use the Start/Stop buttons in the UI to control the agent")
    uvicorn.run(app, host="0.0.0.0", port=8050, log_level="warning")


if __name__ == "__main__":
    main()

"""BuckFiddy reporting — view performance, costs, and export data for graphing.

Usage:
    python -m buckfiddy.report                  # Full summary
    python -m buckfiddy.report --trades         # Trade journal
    python -m buckfiddy.report --costs          # API cost breakdown
    python -m buckfiddy.report --equity         # Equity curve data
    python -m buckfiddy.report --estimates      # Claude's estimates vs market
    python -m buckfiddy.report --export-csv     # Export all tables to CSV files
"""

import csv
import os
import sys
from datetime import datetime

from buckfiddy.config import Settings
from buckfiddy.state.store import StateStore


def _header(title: str):
    print(f"\n{'=' * 60}")
    print(f"  {title}")
    print(f"{'=' * 60}")


def show_summary(store: StateStore):
    _header("BUCKFIDDY PERFORMANCE SUMMARY")

    # Wallet
    row = store.fetchone("SELECT balance FROM wallet WHERE id = 1")
    balance = row["balance"] if row else 0

    # Equity history
    first = store.fetchone(
        "SELECT equity FROM equity_snapshots ORDER BY id ASC LIMIT 1"
    )
    latest = store.fetchone(
        "SELECT * FROM equity_snapshots ORDER BY id DESC LIMIT 1"
    )

    starting = first["equity"] if first else 100.0
    current_equity = latest["equity"] if latest else balance
    positions = latest["num_positions"] if latest else 0
    cumulative_cost = latest["cumulative_api_cost"] if latest else 0

    pnl = current_equity - starting
    pnl_pct = (pnl / starting * 100) if starting > 0 else 0
    net_pnl = pnl - cumulative_cost  # P&L after API costs

    print(f"\n  Starting equity:     ${starting:.2f}")
    print(f"  Current equity:      ${current_equity:.2f}")
    print(f"  Cash balance:        ${balance:.2f}")
    print(f"  Open positions:      {positions}")
    print(f"  Trading P&L:         ${pnl:+.2f} ({pnl_pct:+.1f}%)")
    print(f"  API costs:           ${cumulative_cost:.4f}")
    print(f"  Net P&L (after API): ${net_pnl:+.4f}")

    # Cycles
    cycles = store.fetchone("SELECT COUNT(*) as n FROM cycle_log")
    print(f"\n  Cycles run:          {cycles['n'] if cycles else 0}")

    # Trades
    trades = store.fetchone("SELECT COUNT(*) as n FROM trades")
    wins = store.fetchone(
        "SELECT COUNT(*) as n FROM trades WHERE side='SELL' AND pnl > 0"
    )
    losses = store.fetchone(
        "SELECT COUNT(*) as n FROM trades WHERE side='SELL' AND pnl < 0"
    )
    total_pnl = store.fetchone(
        "SELECT COALESCE(SUM(pnl), 0) as total FROM trades WHERE pnl IS NOT NULL"
    )

    total_trades = trades["n"] if trades else 0
    win_count = wins["n"] if wins else 0
    loss_count = losses["n"] if losses else 0
    closed = win_count + loss_count

    print(f"\n  Total trades:        {total_trades}")
    print(f"  Closed trades:       {closed}")
    if closed > 0:
        print(f"  Wins / Losses:       {win_count} / {loss_count}")
        print(f"  Win rate:            {win_count / closed * 100:.0f}%")
    print(f"  Realized P&L:        ${total_pnl['total']:+.2f}")

    # Estimates
    est_total = store.fetchone("SELECT COUNT(*) as n FROM estimates")
    est_tradeable = store.fetchone(
        "SELECT COUNT(*) as n FROM estimates WHERE tradeable = 1"
    )
    print(f"\n  Estimates made:      {est_total['n'] if est_total else 0}")
    print(
        f"  Tradeable edges:     {est_tradeable['n'] if est_tradeable else 0}"
    )

    # Stop losses
    sl = store.fetchone(
        "SELECT COALESCE(SUM(stop_losses_triggered), 0) as n FROM cycle_log"
    )
    print(f"  Stop losses:         {sl['n'] if sl else 0}")


def show_trades(store: StateStore):
    _header("TRADE JOURNAL")

    rows = store.fetchall(
        "SELECT * FROM trades ORDER BY executed_at DESC LIMIT 50"
    )
    if not rows:
        print("\n  No trades recorded yet.")
        return

    print(
        f"\n  {'Time':<20} {'Side':<5} {'Outcome':<20} "
        f"{'Price':>8} {'Size':>8} {'P&L':>10}"
    )
    print(f"  {'-'*20} {'-'*5} {'-'*20} {'-'*8} {'-'*8} {'-'*10}")

    for r in rows:
        ts = r["executed_at"][:16].replace("T", " ")
        pnl_str = f"${r['pnl']:+.2f}" if r["pnl"] is not None else "—"
        outcome = r["outcome"][:20]
        print(
            f"  {ts:<20} {r['side']:<5} {outcome:<20} "
            f"${r['price']:>7.3f} {r['size']:>7.1f} {pnl_str:>10}"
        )


def show_costs(store: StateStore):
    _header("API COST BREAKDOWN")

    rows = store.fetchall("SELECT * FROM api_usage ORDER BY id DESC LIMIT 20")
    if not rows:
        print("\n  No API usage recorded yet.")
        return

    total = store.fetchone(
        "SELECT SUM(cost_usd) as cost, SUM(input_tokens) as inp, "
        "SUM(output_tokens) as out, SUM(web_searches) as ws, "
        "SUM(api_calls) as calls FROM api_usage"
    )

    print(f"\n  TOTALS:")
    print(f"    API calls:       {total['calls']:,}")
    print(f"    Input tokens:    {total['inp']:,}")
    print(f"    Output tokens:   {total['out']:,}")
    print(f"    Web searches:    {total['ws']:,}")
    print(f"    Total cost:      ${total['cost']:.4f}")

    avg_cost = total["cost"] / len(rows) if rows else 0
    print(f"    Avg cost/cycle:  ${avg_cost:.4f}")

    print(
        f"\n  {'Cycle':>6} {'Calls':>6} {'In Tok':>9} {'Out Tok':>9} "
        f"{'Search':>7} {'Cost':>10}"
    )
    print(f"  {'-'*6} {'-'*6} {'-'*9} {'-'*9} {'-'*7} {'-'*10}")

    for r in reversed(rows):
        print(
            f"  {r['cycle_number']:>6} {r['api_calls']:>6} "
            f"{r['input_tokens']:>9,} {r['output_tokens']:>9,} "
            f"{r['web_searches']:>7} ${r['cost_usd']:>9.4f}"
        )


def show_equity(store: StateStore):
    _header("EQUITY CURVE")

    rows = store.fetchall("SELECT * FROM equity_snapshots ORDER BY id")
    if not rows:
        print("\n  No equity data recorded yet.")
        return

    print(
        f"\n  {'Cycle':>6} {'Balance':>10} {'Pos Value':>10} "
        f"{'Equity':>10} {'#Pos':>5} {'API Cost':>10} {'Time':<20}"
    )
    print(
        f"  {'-'*6} {'-'*10} {'-'*10} {'-'*10} "
        f"{'-'*5} {'-'*10} {'-'*20}"
    )

    for r in rows:
        ts = r["created_at"][:16].replace("T", " ")
        print(
            f"  {r['cycle_number']:>6} ${r['balance']:>9.2f} "
            f"${r['position_value']:>9.2f} ${r['equity']:>9.2f} "
            f"{r['num_positions']:>5} ${r['cumulative_api_cost']:>9.4f} {ts}"
        )


def show_estimates(store: StateStore):
    _header("CLAUDE'S ESTIMATES VS MARKET")

    rows = store.fetchall(
        "SELECT * FROM estimates ORDER BY created_at DESC LIMIT 30"
    )
    if not rows:
        print("\n  No estimates recorded yet.")
        return

    print(
        f"\n  {'Time':<17} {'Outcome':<20} {'Est':>6} {'Mkt':>6} "
        f"{'Edge':>7} {'Trade':>6}"
    )
    print(
        f"  {'-'*17} {'-'*20} {'-'*6} {'-'*6} {'-'*7} {'-'*6}"
    )

    for r in rows:
        ts = r["created_at"][:16].replace("T", " ")
        outcome = r["outcome"][:20]
        tradeable = "YES" if r["tradeable"] else "no"
        print(
            f"  {ts:<17} {outcome:<20} {r['claude_estimate']:>5.1%} "
            f"{r['market_midpoint']:>5.1%} {r['edge']:>+6.1%} {tradeable:>6}"
        )


def export_csv(store: StateStore):
    _header("EXPORTING CSV FILES")

    os.makedirs("data/exports", exist_ok=True)
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")

    tables = {
        "trades": "SELECT * FROM trades ORDER BY executed_at",
        "estimates": "SELECT * FROM estimates ORDER BY created_at",
        "equity_snapshots": "SELECT * FROM equity_snapshots ORDER BY id",
        "api_usage": "SELECT * FROM api_usage ORDER BY id",
        "cycle_log": "SELECT * FROM cycle_log ORDER BY id",
    }

    for name, query in tables.items():
        rows = store.fetchall(query)
        if not rows:
            print(f"  {name}: (empty, skipped)")
            continue

        filename = f"data/exports/{name}_{ts}.csv"
        with open(filename, "w", newline="") as f:
            writer = csv.writer(f)
            writer.writerow(rows[0].keys())
            for row in rows:
                writer.writerow(tuple(row))

        print(f"  {name}: {len(rows)} rows -> {filename}")

    print(f"\n  CSV files ready in data/exports/")
    print(f"  Import into a spreadsheet or use pandas/matplotlib to graph.")


def main():
    try:
        settings = Settings()
    except Exception as e:
        print(f"Failed to load settings: {e}")
        sys.exit(1)

    store = StateStore(settings.DB_PATH)
    args = sys.argv[1:]

    if "--trades" in args:
        show_trades(store)
    elif "--costs" in args:
        show_costs(store)
    elif "--equity" in args:
        show_equity(store)
    elif "--estimates" in args:
        show_estimates(store)
    elif "--export-csv" in args:
        export_csv(store)
    else:
        show_summary(store)
        show_costs(store)
        show_trades(store)
        show_equity(store)


if __name__ == "__main__":
    main()

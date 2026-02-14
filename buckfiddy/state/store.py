import os
import sqlite3


SCHEMA = """
CREATE TABLE IF NOT EXISTS wallet (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    balance REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS positions (
    position_id TEXT PRIMARY KEY,
    market_id TEXT NOT NULL,
    token_id TEXT NOT NULL,
    market_question TEXT NOT NULL,
    outcome TEXT NOT NULL,
    size REAL NOT NULL,
    avg_entry_price REAL NOT NULL,
    created_at TEXT NOT NULL,
    slug TEXT NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS orders (
    order_id TEXT PRIMARY KEY,
    market_id TEXT NOT NULL,
    token_id TEXT NOT NULL,
    outcome TEXT NOT NULL,
    side TEXT NOT NULL,
    price REAL NOT NULL,
    size REAL NOT NULL,
    status TEXT NOT NULL DEFAULT 'OPEN',
    estimate_id INTEGER,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS trades (
    trade_id TEXT PRIMARY KEY,
    order_id TEXT NOT NULL,
    market_id TEXT NOT NULL,
    token_id TEXT NOT NULL,
    outcome TEXT NOT NULL,
    side TEXT NOT NULL,
    price REAL NOT NULL,
    size REAL NOT NULL,
    pnl REAL,
    entry_price REAL,
    estimate_id INTEGER,
    executed_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS estimates (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    market_id TEXT NOT NULL,
    token_id TEXT NOT NULL,
    outcome TEXT NOT NULL,
    market_question TEXT NOT NULL DEFAULT '',
    slug TEXT NOT NULL DEFAULT '',
    claude_estimate REAL NOT NULL,
    market_midpoint REAL NOT NULL,
    edge REAL NOT NULL,
    reasoning TEXT NOT NULL,
    tradeable INTEGER NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS cycle_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    cycle_number INTEGER NOT NULL,
    markets_scanned INTEGER NOT NULL DEFAULT 0,
    estimates_made INTEGER NOT NULL DEFAULT 0,
    trades_placed INTEGER NOT NULL DEFAULT 0,
    positions_closed INTEGER NOT NULL DEFAULT 0,
    stop_losses_triggered INTEGER NOT NULL DEFAULT 0,
    balance_after REAL NOT NULL DEFAULT 0,
    equity_after REAL NOT NULL DEFAULT 0,
    claude_summary TEXT,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS api_usage (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    cycle_number INTEGER NOT NULL,
    api_calls INTEGER NOT NULL DEFAULT 0,
    input_tokens INTEGER NOT NULL DEFAULT 0,
    output_tokens INTEGER NOT NULL DEFAULT 0,
    cache_creation_tokens INTEGER NOT NULL DEFAULT 0,
    cache_read_tokens INTEGER NOT NULL DEFAULT 0,
    web_searches INTEGER NOT NULL DEFAULT 0,
    cost_usd REAL NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS equity_snapshots (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    cycle_number INTEGER NOT NULL,
    balance REAL NOT NULL,
    position_value REAL NOT NULL,
    equity REAL NOT NULL,
    num_positions INTEGER NOT NULL DEFAULT 0,
    cumulative_api_cost REAL NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL
);
"""


class StateStore:
    def __init__(self, db_path: str, check_same_thread: bool = False):
        os.makedirs(os.path.dirname(db_path) or ".", exist_ok=True)
        self.conn = sqlite3.connect(db_path, check_same_thread=check_same_thread)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")
        self._init_schema()

    def _init_schema(self):
        self.conn.executescript(SCHEMA)
        self.conn.commit()
        self._migrate()

    def _migrate(self):
        """Apply incremental schema migrations for existing databases."""
        trade_cols = [r[1] for r in self.conn.execute("PRAGMA table_info(trades)").fetchall()]
        if "entry_price" not in trade_cols:
            self.conn.execute("ALTER TABLE trades ADD COLUMN entry_price REAL")
            self.conn.execute(
                "UPDATE trades SET entry_price = price WHERE side = 'BUY' AND entry_price IS NULL"
            )
            self.conn.execute(
                "UPDATE trades SET entry_price = price - (pnl / size) "
                "WHERE side = 'SELL' AND pnl IS NOT NULL AND size > 0 AND entry_price IS NULL"
            )
            self.conn.commit()

        pos_cols = [r[1] for r in self.conn.execute("PRAGMA table_info(positions)").fetchall()]
        if "slug" not in pos_cols:
            self.conn.execute("ALTER TABLE positions ADD COLUMN slug TEXT NOT NULL DEFAULT ''")
            self.conn.commit()

        est_cols = [r[1] for r in self.conn.execute("PRAGMA table_info(estimates)").fetchall()]
        if "slug" not in est_cols:
            self.conn.execute("ALTER TABLE estimates ADD COLUMN slug TEXT NOT NULL DEFAULT ''")
            self.conn.commit()

    def execute(self, sql: str, params: tuple = ()) -> sqlite3.Cursor:
        return self.conn.execute(sql, params)

    def commit(self):
        self.conn.commit()

    def fetchone(self, sql: str, params: tuple = ()) -> sqlite3.Row | None:
        return self.conn.execute(sql, params).fetchone()

    def fetchall(self, sql: str, params: tuple = ()) -> list[sqlite3.Row]:
        return self.conn.execute(sql, params).fetchall()

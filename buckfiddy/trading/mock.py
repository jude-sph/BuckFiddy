import logging
from datetime import datetime, timezone
from uuid import uuid4

import requests

from buckfiddy.config import Settings
from buckfiddy.state.store import StateStore
from buckfiddy.trading.models import (
    MarketPrice,
    Order,
    Position,
    TradeResult,
    WalletState,
)

logger = logging.getLogger(__name__)

CLOB_API = "https://clob.polymarket.com"
GAMMA_API = "https://gamma-api.polymarket.com"


class MockTradingBackend:
    """Simulated trading backend using real Polymarket prices but local execution.

    All prices come from the live Gamma/CLOB API. Execution and balance
    tracking happen locally in SQLite.
    """

    def __init__(self, settings: Settings):
        self.settings = settings
        self.store = StateStore(settings.DB_PATH)
        self._ensure_wallet(settings.STARTING_BALANCE)
        self._repair_balance(settings.STARTING_BALANCE)
        # Cache: token_id -> {market_id, outcome, market_question}
        self._token_meta: dict[str, dict] = {}
        # Cache: token_id -> last successfully fetched midpoint
        self._price_cache: dict[str, float] = {}

    def _ensure_wallet(self, starting_balance: float):
        row = self.store.fetchone("SELECT balance FROM wallet WHERE id = 1")
        if row is None:
            self.store.execute(
                "INSERT INTO wallet (id, balance) VALUES (1, ?)", (starting_balance,)
            )
            self.store.commit()

    def _repair_balance(self, starting_balance: float):
        """One-time fix: recalculate balance from trade history.

        Corrects balances that were wrongly reduced by API cost deductions.
        """
        trades = self.store.fetchall(
            "SELECT side, price, size FROM trades ORDER BY executed_at"
        )
        if not trades:
            return  # No trades — nothing to repair
        correct = starting_balance
        for t in trades:
            if t["side"] == "BUY":
                correct -= t["price"] * t["size"]
            else:
                correct += t["price"] * t["size"]
        correct = round(correct, 6)
        current = self._get_balance()
        if abs(current - correct) > 0.001:
            logger.info(
                f"Balance repair: {current:.4f} -> {correct:.4f} "
                f"(removed {current - correct:+.4f} of API cost deductions)"
            )
            self._set_balance(correct)

    def _get_balance(self) -> float:
        row = self.store.fetchone("SELECT balance FROM wallet WHERE id = 1")
        return row["balance"]

    def _set_balance(self, balance: float):
        self.store.execute("UPDATE wallet SET balance = ? WHERE id = 1", (balance,))
        self.store.commit()

    def _now_iso(self) -> str:
        return datetime.now(timezone.utc).isoformat()

    def _fetch_midpoint(self, token_id: str) -> float:
        """Fetch the live midpoint price from the CLOB API.

        Caches successful results so we can return the last known price
        when the API is temporarily unreachable.
        """
        resp = requests.get(
            f"{CLOB_API}/midpoint", params={"token_id": token_id}, timeout=10
        )
        resp.raise_for_status()
        data = resp.json()
        mid = float(data.get("mid", 0))
        if mid <= 0:
            raise ValueError(f"Invalid midpoint for token {token_id}: {data}")
        self._price_cache[token_id] = mid
        return mid

    def _fetch_price(self, token_id: str, side: str) -> float:
        """Fetch the best bid or ask from the CLOB API."""
        resp = requests.get(
            f"{CLOB_API}/price", params={"token_id": token_id, "side": side},
            timeout=10,
        )
        resp.raise_for_status()
        data = resp.json()
        return float(data.get("price", 0))

    def register_token_meta(
        self, token_id: str, market_id: str, outcome: str, market_question: str,
        slug: str = "",
    ):
        """Register metadata for a token so we can track positions properly."""
        self._token_meta[token_id] = {
            "market_id": market_id,
            "outcome": outcome,
            "market_question": market_question,
            "slug": slug,
        }

    def _get_token_meta(self, token_id: str) -> dict:
        if token_id in self._token_meta:
            return self._token_meta[token_id]
        # Try to find from existing positions
        row = self.store.fetchone(
            "SELECT market_id, outcome, market_question, slug FROM positions WHERE token_id = ?",
            (token_id,),
        )
        if row:
            meta = {
                "market_id": row["market_id"],
                "outcome": row["outcome"],
                "market_question": row["market_question"],
                "slug": row["slug"] if "slug" in row.keys() else "",
            }
            self._token_meta[token_id] = meta
            return meta
        return {"market_id": "unknown", "outcome": "unknown", "market_question": "unknown", "slug": ""}

    def get_wallet_state(self) -> WalletState:
        balance = self._get_balance()

        # Fetch all positions and update current values
        pos_rows = self.store.fetchall("SELECT * FROM positions WHERE size > 0")
        positions = []
        total_position_value = 0.0

        for row in pos_rows:
            try:
                # Use best bid (what we'd actually get if we sold now)
                bid = self._fetch_price(row["token_id"], "sell")
                if bid > 0:
                    self._price_cache[row["token_id"]] = bid
                else:
                    bid = self._fetch_midpoint(row["token_id"])
            except Exception:
                bid = self._price_cache.get(
                    row["token_id"], row["avg_entry_price"]
                )

            current_value = row["size"] * bid
            entry_value = row["size"] * row["avg_entry_price"]
            unrealized_pnl = current_value - entry_value
            unrealized_pnl_pct = unrealized_pnl / entry_value if entry_value > 0 else 0

            positions.append(
                Position(
                    position_id=row["position_id"],
                    market_id=row["market_id"],
                    token_id=row["token_id"],
                    market_question=row["market_question"],
                    outcome=row["outcome"],
                    size=row["size"],
                    avg_entry_price=row["avg_entry_price"],
                    current_value=round(current_value, 4),
                    unrealized_pnl=round(unrealized_pnl, 4),
                    unrealized_pnl_pct=round(unrealized_pnl_pct, 4),
                )
            )
            total_position_value += current_value

        # Fetch open orders
        order_rows = self.store.fetchall("SELECT * FROM orders WHERE status = 'OPEN'")
        open_orders = [
            Order(
                order_id=r["order_id"],
                market_id=r["market_id"],
                token_id=r["token_id"],
                outcome=r["outcome"],
                side=r["side"],
                price=r["price"],
                size=r["size"],
                status=r["status"],
                created_at=datetime.fromisoformat(r["created_at"]),
            )
            for r in order_rows
        ]

        return WalletState(
            balance=round(balance, 4),
            total_position_value=round(total_position_value, 4),
            total_equity=round(balance + total_position_value, 4),
            positions=positions,
            open_orders=open_orders,
        )

    def get_market_price(self, token_id: str) -> MarketPrice:
        mid = self._fetch_midpoint(token_id)
        bid = self._fetch_price(token_id, "sell")
        ask = self._fetch_price(token_id, "buy")
        meta = self._get_token_meta(token_id)

        return MarketPrice(
            token_id=token_id,
            outcome=meta.get("outcome", "unknown"),
            best_bid=bid or mid * 0.99,
            best_ask=ask or mid * 1.01,
            midpoint=mid,
        )

    def place_limit_order(
        self, token_id: str, side: str, price: float, size: float,
        estimate_id: int | None = None,
    ) -> TradeResult:
        meta = self._get_token_meta(token_id)
        balance = self._get_balance()

        if side == "BUY":
            cost = price * size
            if cost > balance:
                return TradeResult(
                    success=False,
                    message=f"Insufficient balance: need ${cost:.2f}, have ${balance:.2f}",
                )

        order_id = str(uuid4())[:8]
        now = self._now_iso()

        # For simplicity in mock: try to fill immediately at limit price
        # if the market midpoint is favorable
        try:
            mid = self._fetch_midpoint(token_id)
        except Exception as e:
            return TradeResult(success=False, message=f"Failed to fetch price: {e}")

        can_fill = (side == "BUY" and mid <= price) or (
            side == "SELL" and mid >= price
        )

        if can_fill:
            return self._execute_fill(
                order_id, token_id, meta, side, mid, size, now, estimate_id
            )

        # Otherwise, place as open order (will be checked on future cycles)
        if side == "BUY":
            self._set_balance(balance - price * size)

        self.store.execute(
            "INSERT INTO orders (order_id, market_id, token_id, outcome, side, "
            "price, size, status, estimate_id, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, 'OPEN', ?, ?)",
            (
                order_id,
                meta["market_id"],
                token_id,
                meta["outcome"],
                side,
                price,
                size,
                estimate_id,
                now,
            ),
        )
        self.store.commit()

        return TradeResult(
            success=True,
            order_id=order_id,
            message=f"Limit order placed: {side} {size:.2f} shares at ${price:.3f}",
        )

    def place_market_order(
        self, token_id: str, side: str, amount: float,
        estimate_id: int | None = None,
    ) -> TradeResult:
        meta = self._get_token_meta(token_id)
        balance = self._get_balance()

        if side == "BUY" and amount > balance:
            return TradeResult(
                success=False,
                message=f"Insufficient balance: need ${amount:.2f}, have ${balance:.2f}",
            )

        try:
            mid = self._fetch_midpoint(token_id)
        except Exception as e:
            return TradeResult(success=False, message=f"Failed to fetch price: {e}")

        order_id = str(uuid4())[:8]
        now = self._now_iso()

        if side == "BUY":
            size = amount / mid
            return self._execute_fill(
                order_id, token_id, meta, "BUY", mid, size, now, estimate_id
            )
        else:
            # SELL: amount is number of shares to sell
            return self._execute_fill(
                order_id, token_id, meta, "SELL", mid, amount, now, estimate_id
            )

    def _execute_fill(
        self,
        order_id: str,
        token_id: str,
        meta: dict,
        side: str,
        fill_price: float,
        size: float,
        now: str,
        estimate_id: int | None = None,
    ) -> TradeResult:
        balance = self._get_balance()
        pnl = None
        entry_price = None

        if side == "BUY":
            cost = fill_price * size
            if cost > balance:
                return TradeResult(
                    success=False,
                    message=f"Insufficient balance for fill: need ${cost:.2f}, have ${balance:.2f}",
                )
            self._set_balance(balance - cost)
            self._add_to_position(token_id, meta, size, fill_price, now)
            entry_price = fill_price
        else:
            # Sell: reduce position and credit proceeds
            pos = self.store.fetchone(
                "SELECT * FROM positions WHERE token_id = ? AND size > 0",
                (token_id,),
            )
            if not pos or pos["size"] < size:
                avail = pos["size"] if pos else 0
                return TradeResult(
                    success=False,
                    message=f"Insufficient position: want to sell {size:.2f}, have {avail:.2f}",
                )
            entry_price = pos["avg_entry_price"]
            proceeds = fill_price * size
            pnl = (fill_price - entry_price) * size
            self._set_balance(balance + proceeds)
            self._reduce_position(token_id, size)

        # Record the trade
        trade_id = str(uuid4())[:8]
        self.store.execute(
            "INSERT INTO trades (trade_id, order_id, market_id, token_id, outcome, "
            "side, price, size, pnl, entry_price, estimate_id, executed_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                trade_id,
                order_id,
                meta["market_id"],
                token_id,
                meta["outcome"],
                side,
                fill_price,
                size,
                pnl,
                entry_price,
                estimate_id,
                now,
            ),
        )

        # Record the order as filled
        self.store.execute(
            "INSERT OR REPLACE INTO orders (order_id, market_id, token_id, outcome, "
            "side, price, size, status, estimate_id, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, 'FILLED', ?, ?)",
            (
                order_id,
                meta["market_id"],
                token_id,
                meta["outcome"],
                side,
                fill_price,
                size,
                estimate_id,
                now,
            ),
        )
        self.store.commit()

        pnl_str = f" (P&L: ${pnl:+.2f})" if pnl is not None else ""
        return TradeResult(
            success=True,
            order_id=order_id,
            message=f"Filled: {side} {size:.2f} shares at ${fill_price:.3f}{pnl_str}",
            filled_price=round(fill_price, 4),
            filled_size=round(size, 4),
        )

    def _add_to_position(
        self, token_id: str, meta: dict, size: float, price: float, now: str
    ):
        existing = self.store.fetchone(
            "SELECT * FROM positions WHERE token_id = ? AND size > 0",
            (token_id,),
        )
        if existing:
            # Average in
            old_size = existing["size"]
            old_avg = existing["avg_entry_price"]
            new_size = old_size + size
            new_avg = (old_size * old_avg + size * price) / new_size
            self.store.execute(
                "UPDATE positions SET size = ?, avg_entry_price = ? WHERE position_id = ?",
                (new_size, new_avg, existing["position_id"]),
            )
        else:
            position_id = str(uuid4())[:8]
            self.store.execute(
                "INSERT INTO positions (position_id, market_id, token_id, market_question, "
                "outcome, size, avg_entry_price, created_at, slug) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    position_id,
                    meta["market_id"],
                    token_id,
                    meta["market_question"],
                    meta["outcome"],
                    size,
                    price,
                    now,
                    meta.get("slug", ""),
                ),
            )
        self.store.commit()

    def _reduce_position(self, token_id: str, size: float):
        pos = self.store.fetchone(
            "SELECT * FROM positions WHERE token_id = ? AND size > 0",
            (token_id,),
        )
        if not pos:
            return
        new_size = pos["size"] - size
        if new_size <= 0.001:
            self.store.execute(
                "DELETE FROM positions WHERE position_id = ?",
                (pos["position_id"],),
            )
        else:
            self.store.execute(
                "UPDATE positions SET size = ? WHERE position_id = ?",
                (new_size, pos["position_id"]),
            )
        self.store.commit()

    def cancel_order(self, order_id: str) -> TradeResult:
        row = self.store.fetchone(
            "SELECT * FROM orders WHERE order_id = ? AND status = 'OPEN'",
            (order_id,),
        )
        if not row:
            return TradeResult(success=False, message=f"Order {order_id} not found or not open")

        # Refund reserved balance for BUY orders
        if row["side"] == "BUY":
            balance = self._get_balance()
            refund = row["price"] * row["size"]
            self._set_balance(balance + refund)

        self.store.execute(
            "UPDATE orders SET status = 'CANCELLED' WHERE order_id = ?",
            (order_id,),
        )
        self.store.commit()
        return TradeResult(success=True, order_id=order_id, message="Order cancelled")

    def cancel_all_orders(self) -> TradeResult:
        open_orders = self.store.fetchall(
            "SELECT * FROM orders WHERE status = 'OPEN'"
        )
        count = 0
        for row in open_orders:
            self.cancel_order(row["order_id"])
            count += 1
        return TradeResult(
            success=True, message=f"Cancelled {count} open orders"
        )

    def close_position(self, position_id: str) -> TradeResult:
        pos = self.store.fetchone(
            "SELECT * FROM positions WHERE position_id = ? AND size > 0",
            (position_id,),
        )
        if not pos:
            return TradeResult(
                success=False, message=f"Position {position_id} not found"
            )

        token_id = pos["token_id"]
        meta = self._get_token_meta(token_id)

        try:
            mid = self._fetch_midpoint(token_id)
        except Exception as e:
            return TradeResult(
                success=False, message=f"Failed to fetch price for close: {e}"
            )

        order_id = str(uuid4())[:8]
        now = self._now_iso()
        return self._execute_fill(
            order_id, token_id, meta, "SELL", mid, pos["size"], now
        )

    def check_open_orders(self):
        """Check if any open limit orders can now be filled.

        Called each cycle to simulate order matching.
        """
        open_orders = self.store.fetchall(
            "SELECT * FROM orders WHERE status = 'OPEN'"
        )
        for row in open_orders:
            try:
                mid = self._fetch_midpoint(row["token_id"])
            except Exception:
                continue

            can_fill = (row["side"] == "BUY" and mid <= row["price"]) or (
                row["side"] == "SELL" and mid >= row["price"]
            )
            if can_fill:
                meta = self._get_token_meta(row["token_id"])
                now = self._now_iso()

                if row["side"] == "BUY":
                    # Refund the reserved balance first (was deducted at placement)
                    balance = self._get_balance()
                    self._set_balance(balance + row["price"] * row["size"])

                self.store.execute(
                    "UPDATE orders SET status = 'FILLED' WHERE order_id = ?",
                    (row["order_id"],),
                )
                self._execute_fill(
                    row["order_id"],
                    row["token_id"],
                    meta,
                    row["side"],
                    mid,
                    row["size"],
                    now,
                )

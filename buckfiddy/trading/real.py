import logging
import time
from datetime import datetime, timezone
from uuid import uuid4

import requests
from py_clob_client.client import ClobClient
from py_clob_client.clob_types import MarketOrderArgs, OrderArgs, OrderType
from py_clob_client.order_builder.constants import BUY, SELL

from buckfiddy.config import Settings
from buckfiddy.trading.models import (
    MarketPrice,
    Order,
    Position,
    TradeResult,
    WalletState,
)

logger = logging.getLogger(__name__)

DATA_API = "https://data-api.polymarket.com"

# Transient network errors safe to retry
_RETRYABLE = (
    requests.ConnectionError,
    requests.Timeout,
    ConnectionError,
    TimeoutError,
    OSError,
)


class RealTradingBackend:
    """Real Polymarket trading backend using py-clob-client.

    Executes real trades on Polymarket via the CLOB API.
    Includes retry logic for read operations and a circuit breaker
    to prevent runaway API calls after sustained failures.
    """

    def __init__(self, settings: Settings):
        self.settings = settings
        self.client = ClobClient(
            host="https://clob.polymarket.com",
            key=settings.POLYMARKET_PRIVATE_KEY,
            chain_id=settings.POLYMARKET_CHAIN_ID,
            signature_type=settings.POLYMARKET_SIGNATURE_TYPE,
            funder=settings.POLYMARKET_FUNDER_ADDRESS,
        )
        # Create or derive L2 API credentials
        api_creds = self.client.create_or_derive_api_creds()
        self.client.set_api_creds(api_creds)
        logger.info("Real trading backend initialized")

        # Token metadata cache
        self._token_meta: dict[str, dict] = {}

        # Circuit breaker: shuts down after too many consecutive failures
        self._consecutive_failures = 0
        self._max_consecutive_failures = 5
        self._circuit_open = False
        self._circuit_opened_at: float = 0
        self._circuit_cooldown = 300  # 5 min auto-reset

    # ── Circuit breaker ─────────────────────────────────────────

    def _record_failure(self):
        self._consecutive_failures += 1
        if self._consecutive_failures >= self._max_consecutive_failures:
            self._circuit_open = True
            self._circuit_opened_at = time.monotonic()
            logger.error(
                f"Circuit breaker OPEN after {self._consecutive_failures} "
                f"consecutive failures. Operations suspended for "
                f"{self._circuit_cooldown}s."
            )

    def _record_success(self):
        if self._consecutive_failures > 0:
            self._consecutive_failures = 0
        if self._circuit_open:
            self._circuit_open = False
            logger.info("Circuit breaker CLOSED — operations resumed.")

    def _check_circuit(self):
        """Raise if circuit breaker is open (auto-resets after cooldown)."""
        if not self._circuit_open:
            return
        elapsed = time.monotonic() - self._circuit_opened_at
        if elapsed >= self._circuit_cooldown:
            logger.info(
                f"Circuit breaker auto-reset after {elapsed:.0f}s cooldown."
            )
            self._circuit_open = False
            self._consecutive_failures = 0
        else:
            remaining = self._circuit_cooldown - elapsed
            raise RuntimeError(
                f"Circuit breaker open — {remaining:.0f}s until auto-reset. "
                f"Too many consecutive API failures."
            )

    def reset_circuit_breaker(self):
        """Manual reset (e.g., from dashboard)."""
        self._circuit_open = False
        self._consecutive_failures = 0
        logger.info("Circuit breaker manually reset.")

    # ── Retry helpers ───────────────────────────────────────────

    def _retry_read(self, fn, *args, max_retries=3, **kwargs):
        """Retry a read-only operation on transient network failures.

        NEVER use this for write operations (order placement, cancellation).
        """
        self._check_circuit()
        last_err = None
        for attempt in range(max_retries):
            try:
                result = fn(*args, **kwargs)
                self._record_success()
                return result
            except _RETRYABLE as e:
                last_err = e
                self._record_failure()
                if attempt < max_retries - 1:
                    wait = 2 ** attempt  # 1s, 2s, 4s
                    logger.warning(
                        f"Retry {attempt + 1}/{max_retries} for "
                        f"{getattr(fn, '__name__', 'call')}: {e}"
                    )
                    time.sleep(wait)
            except requests.HTTPError as e:
                # Retry 5xx server errors, but not 4xx client errors
                if hasattr(e, 'response') and e.response is not None:
                    if e.response.status_code >= 500:
                        last_err = e
                        self._record_failure()
                        if attempt < max_retries - 1:
                            wait = 2 ** attempt
                            logger.warning(
                                f"Retry {attempt + 1}/{max_retries} (5xx): {e}"
                            )
                            time.sleep(wait)
                        continue
                # 4xx — don't retry, don't count toward circuit breaker
                raise
            except Exception:
                raise
        raise last_err

    def _fetch_json(self, url, params=None, timeout=15):
        """HTTP GET that returns parsed JSON. Used with _retry_read."""
        resp = requests.get(url, params=params, timeout=timeout)
        resp.raise_for_status()
        return resp.json()

    # ── Token metadata ──────────────────────────────────────────

    def register_token_meta(
        self, token_id: str, market_id: str, outcome: str, market_question: str,
        slug: str = "",
    ):
        self._token_meta[token_id] = {
            "market_id": market_id,
            "outcome": outcome,
            "market_question": market_question,
            "slug": slug,
        }

    def _get_token_meta(self, token_id: str) -> dict:
        return self._token_meta.get(
            token_id,
            {"market_id": "unknown", "outcome": "unknown", "market_question": "unknown", "slug": ""},
        )

    # ── Read operations (with retry) ────────────────────────────

    def get_wallet_state(self) -> WalletState:
        # Get positions from Data API
        try:
            raw_positions = self._retry_read(
                self._fetch_json,
                f"{DATA_API}/positions",
                params={"user": self.settings.POLYMARKET_FUNDER_ADDRESS},
            )
        except Exception as e:
            logger.error(f"Failed to fetch positions: {e}")
            raw_positions = []

        positions = []
        total_position_value = 0.0

        for raw in raw_positions:
            try:
                token_id = raw.get("asset", "")
                size = float(raw.get("size", 0))
                if size <= 0:
                    continue

                avg_price = float(raw.get("avgPrice", 0))
                mid = float(self._retry_read(
                    self.client.get_midpoint, token_id
                ))
                current_value = size * mid
                entry_value = size * avg_price
                pnl = current_value - entry_value
                pnl_pct = pnl / entry_value if entry_value > 0 else 0

                meta = self._get_token_meta(token_id)
                positions.append(
                    Position(
                        position_id=raw.get("id", str(uuid4())[:8]),
                        market_id=raw.get("conditionId", meta["market_id"]),
                        token_id=token_id,
                        market_question=raw.get("title", meta["market_question"]),
                        outcome=raw.get("outcome", meta["outcome"]),
                        size=size,
                        avg_entry_price=avg_price,
                        current_value=round(current_value, 4),
                        unrealized_pnl=round(pnl, 4),
                        unrealized_pnl_pct=round(pnl_pct, 4),
                    )
                )
                total_position_value += current_value
            except Exception as e:
                logger.warning(f"Failed to process position: {e}")

        # Get open orders
        open_orders = []
        try:
            raw_orders = self._retry_read(self.client.get_orders)
            for raw in raw_orders:
                if raw.get("status") == "LIVE":
                    open_orders.append(
                        Order(
                            order_id=raw["id"],
                            market_id=raw.get("market", ""),
                            token_id=raw.get("asset_id", ""),
                            outcome=self._get_token_meta(raw.get("asset_id", ""))["outcome"],
                            side=raw.get("side", "BUY"),
                            price=float(raw.get("price", 0)),
                            size=float(raw.get("original_size", 0)),
                            status="OPEN",
                            created_at=datetime.fromisoformat(
                                raw.get("created_at", datetime.now(timezone.utc).isoformat())
                            ),
                        )
                    )
        except Exception as e:
            logger.error(f"Failed to fetch open orders: {e}")

        # Get balance from allowance
        try:
            allowance = self._retry_read(self.client.get_balance_allowance)
            balance = float(allowance.get("balance", 0)) / 1e6  # USDC has 6 decimals
        except Exception:
            balance = 0.0

        return WalletState(
            balance=round(balance, 4),
            total_position_value=round(total_position_value, 4),
            total_equity=round(balance + total_position_value, 4),
            positions=positions,
            open_orders=open_orders,
        )

    def get_market_price(self, token_id: str) -> MarketPrice:
        mid = float(self._retry_read(self.client.get_midpoint, token_id))
        bid = float(self._retry_read(self.client.get_price, token_id, "sell"))
        ask = float(self._retry_read(self.client.get_price, token_id, "buy"))
        meta = self._get_token_meta(token_id)

        return MarketPrice(
            token_id=token_id,
            outcome=meta["outcome"],
            best_bid=bid,
            best_ask=ask,
            midpoint=mid,
        )

    # ── Write operations (NO retry — risk of double execution) ──

    def place_limit_order(
        self, token_id: str, side: str, price: float, size: float,
        estimate_id: int | None = None,
    ) -> TradeResult:
        self._check_circuit()
        try:
            order_args = OrderArgs(
                price=price,
                size=size,
                side=BUY if side == "BUY" else SELL,
                token_id=token_id,
            )
            signed = self.client.create_order(order_args)
            resp = self.client.post_order(signed, OrderType.GTC)

            if resp.get("success"):
                self._record_success()
                # Limit orders fill at the limit price if matched immediately
                status = resp.get("status", "")
                filled_price = None
                filled_size = None
                if status == "matched":
                    filled_price = round(price, 6)
                    filled_size = round(size, 4)

                return TradeResult(
                    success=True,
                    order_id=resp.get("orderID", ""),
                    message=f"Limit order placed: {side} {size:.2f} at ${price:.3f}",
                    filled_price=filled_price,
                    filled_size=filled_size,
                )
            else:
                self._record_failure()
                return TradeResult(
                    success=False,
                    message=f"Order rejected: {resp.get('errorMsg', 'unknown error')}",
                )
        except Exception as e:
            self._record_failure()
            return TradeResult(success=False, message=f"Order failed: {e}")

    def place_market_order(
        self, token_id: str, side: str, amount: float,
        estimate_id: int | None = None,
    ) -> TradeResult:
        self._check_circuit()
        try:
            # Fetch pre-trade midpoint for fill price approximation.
            # FOK orders fill at order book prices, but midpoint is a
            # reasonable approximation and always available.
            try:
                pre_mid = float(self.client.get_midpoint(token_id))
            except Exception:
                pre_mid = None

            order_args = MarketOrderArgs(
                token_id=token_id,
                amount=amount,
                side=BUY if side == "BUY" else SELL,
            )
            signed = self.client.create_market_order(order_args)
            resp = self.client.post_order(signed, OrderType.FOK)

            if resp.get("success"):
                self._record_success()

                # Compute fill approximation from pre-trade midpoint
                filled_price = pre_mid
                filled_size = None
                if pre_mid:
                    if side == "BUY":
                        filled_size = round(amount / pre_mid, 4)
                    else:
                        filled_size = round(amount, 4)

                return TradeResult(
                    success=True,
                    order_id=resp.get("orderID", ""),
                    message=f"Market order filled: {side} ${amount:.2f}",
                    filled_price=round(filled_price, 6) if filled_price else None,
                    filled_size=filled_size,
                )
            else:
                self._record_failure()
                return TradeResult(
                    success=False,
                    message=f"Market order rejected: {resp.get('errorMsg', 'unknown')}",
                )
        except Exception as e:
            self._record_failure()
            return TradeResult(success=False, message=f"Market order failed: {e}")

    def cancel_order(self, order_id: str) -> TradeResult:
        self._check_circuit()
        try:
            self.client.cancel(order_id)
            self._record_success()
            return TradeResult(
                success=True, order_id=order_id, message="Order cancelled"
            )
        except Exception as e:
            self._record_failure()
            return TradeResult(success=False, message=f"Cancel failed: {e}")

    def cancel_all_orders(self) -> TradeResult:
        self._check_circuit()
        try:
            self.client.cancel_all()
            self._record_success()
            return TradeResult(success=True, message="All orders cancelled")
        except Exception as e:
            self._record_failure()
            return TradeResult(success=False, message=f"Cancel all failed: {e}")

    def close_position(self, position_id: str) -> TradeResult:
        # Find the position to get token_id and size
        wallet = self.get_wallet_state()
        position = next(
            (p for p in wallet.positions if p.position_id == position_id), None
        )
        if not position:
            return TradeResult(
                success=False, message=f"Position {position_id} not found"
            )

        return self.place_market_order(
            token_id=position.token_id,
            side="SELL",
            amount=position.size,
        )

    def check_open_orders(self):
        """No-op for real backend — the exchange handles matching."""
        pass

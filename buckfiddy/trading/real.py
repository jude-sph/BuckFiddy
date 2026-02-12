import logging
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


class RealTradingBackend:
    """Real Polymarket trading backend using py-clob-client.

    Executes real trades on Polymarket via the CLOB API.
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

    def register_token_meta(
        self, token_id: str, market_id: str, outcome: str, market_question: str
    ):
        self._token_meta[token_id] = {
            "market_id": market_id,
            "outcome": outcome,
            "market_question": market_question,
        }

    def _get_token_meta(self, token_id: str) -> dict:
        return self._token_meta.get(
            token_id,
            {"market_id": "unknown", "outcome": "unknown", "market_question": "unknown"},
        )

    def get_wallet_state(self) -> WalletState:
        # Get positions from Data API
        try:
            resp = requests.get(
                f"{DATA_API}/positions",
                params={"user": self.settings.POLYMARKET_FUNDER_ADDRESS},
                timeout=15,
            )
            resp.raise_for_status()
            raw_positions = resp.json()
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
                mid = float(self.client.get_midpoint(token_id))
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
            raw_orders = self.client.get_orders()
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

        # Estimate balance (CLOB API doesn't have a direct balance endpoint)
        # We approximate from allowance
        try:
            allowance = self.client.get_balance_allowance()
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
        mid = float(self.client.get_midpoint(token_id))
        bid = float(self.client.get_price(token_id, "sell"))
        ask = float(self.client.get_price(token_id, "buy"))
        meta = self._get_token_meta(token_id)

        return MarketPrice(
            token_id=token_id,
            outcome=meta["outcome"],
            best_bid=bid,
            best_ask=ask,
            midpoint=mid,
        )

    def place_limit_order(
        self, token_id: str, side: str, price: float, size: float,
        estimate_id: int | None = None,
    ) -> TradeResult:
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
                return TradeResult(
                    success=True,
                    order_id=resp.get("orderID", ""),
                    message=f"Limit order placed: {side} {size:.2f} at ${price:.3f}",
                )
            else:
                return TradeResult(
                    success=False,
                    message=f"Order rejected: {resp.get('errorMsg', 'unknown error')}",
                )
        except Exception as e:
            return TradeResult(success=False, message=f"Order failed: {e}")

    def place_market_order(
        self, token_id: str, side: str, amount: float,
        estimate_id: int | None = None,
    ) -> TradeResult:
        try:
            order_args = MarketOrderArgs(
                token_id=token_id,
                amount=amount,
                side=BUY if side == "BUY" else SELL,
            )
            signed = self.client.create_market_order(order_args)
            resp = self.client.post_order(signed, OrderType.FOK)

            if resp.get("success"):
                return TradeResult(
                    success=True,
                    order_id=resp.get("orderID", ""),
                    message=f"Market order filled: {side} ${amount:.2f}",
                )
            else:
                return TradeResult(
                    success=False,
                    message=f"Market order rejected: {resp.get('errorMsg', 'unknown')}",
                )
        except Exception as e:
            return TradeResult(success=False, message=f"Market order failed: {e}")

    def cancel_order(self, order_id: str) -> TradeResult:
        try:
            self.client.cancel(order_id)
            return TradeResult(
                success=True, order_id=order_id, message="Order cancelled"
            )
        except Exception as e:
            return TradeResult(success=False, message=f"Cancel failed: {e}")

    def cancel_all_orders(self) -> TradeResult:
        try:
            self.client.cancel_all()
            return TradeResult(success=True, message="All orders cancelled")
        except Exception as e:
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

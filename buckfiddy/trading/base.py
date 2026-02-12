from typing import Protocol

from buckfiddy.trading.models import MarketPrice, TradeResult, WalletState


class TradingBackend(Protocol):
    """Interface that both mock and real backends implement.
    Claude never knows which one it is talking to."""

    def get_wallet_state(self) -> WalletState: ...

    def get_market_price(self, token_id: str) -> MarketPrice: ...

    def place_limit_order(
        self, token_id: str, side: str, price: float, size: float,
        estimate_id: int | None = None,
    ) -> TradeResult: ...

    def place_market_order(
        self, token_id: str, side: str, amount: float,
        estimate_id: int | None = None,
    ) -> TradeResult: ...

    def cancel_order(self, order_id: str) -> TradeResult: ...

    def cancel_all_orders(self) -> TradeResult: ...

    def close_position(self, position_id: str) -> TradeResult: ...

from datetime import datetime
from typing import Literal

from pydantic import BaseModel


class MarketInfo(BaseModel):
    """Market metadata from Gamma API — given to Claude for analysis."""

    market_id: str
    question: str
    description: str
    outcomes: list[str]
    clob_token_ids: list[str]
    end_date: str
    volume: float
    liquidity: float
    slug: str
    tags: list[str] = []


class MarketPrice(BaseModel):
    """Current market pricing — Claude NEVER sees this during estimation."""

    token_id: str
    outcome: str
    best_bid: float
    best_ask: float
    midpoint: float


class Order(BaseModel):
    order_id: str
    market_id: str
    token_id: str
    outcome: str
    side: Literal["BUY", "SELL"]
    price: float
    size: float
    status: Literal["OPEN", "FILLED", "CANCELLED", "PARTIAL"]
    created_at: datetime


class Position(BaseModel):
    position_id: str
    market_id: str
    token_id: str
    market_question: str
    outcome: str
    size: float
    avg_entry_price: float
    current_value: float
    unrealized_pnl: float
    unrealized_pnl_pct: float


class WalletState(BaseModel):
    """Complete wallet overview — given to Claude at the start of each cycle."""

    balance: float
    total_position_value: float
    total_equity: float
    positions: list[Position]
    open_orders: list[Order]


class TradeResult(BaseModel):
    success: bool
    order_id: str | None = None
    message: str
    filled_price: float | None = None
    filled_size: float | None = None

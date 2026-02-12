from typing import Literal

from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    # Backend selection
    TRADING_BACKEND: Literal["mock", "real"] = "mock"

    # Claude API
    ANTHROPIC_API_KEY: str = ""
    ANTHROPIC_BASE_URL: str = ""  # Custom endpoint (e.g. Azure-hosted)
    CLAUDE_MODEL: str = "claude-sonnet-4-5-20250929"
    CLAUDE_MAX_TOKENS: int = 4096

    # Polymarket (real backend only)
    POLYMARKET_PRIVATE_KEY: str = ""
    POLYMARKET_CHAIN_ID: int = 137
    POLYMARKET_FUNDER_ADDRESS: str = ""
    POLYMARKET_SIGNATURE_TYPE: int = 0  # 0=EOA

    # Agent behavior
    SCAN_INTERVAL_SECONDS: int = 300
    POSITION_REVIEW_INTERVAL_SECONDS: int = 600
    EDGE_THRESHOLD: float = 0.08
    MAX_POSITION_PCT: float = 0.15
    MAX_OPEN_POSITIONS: int = 10
    STOP_LOSS_PCT: float = 0.50
    STARTING_BALANCE: float = 100.0

    # Market filters
    MIN_MARKET_LIQUIDITY: float = 5000.0
    MIN_MARKET_VOLUME: float = 10000.0
    MAX_MARKETS_PER_SCAN: int = 5

    # Persistence
    DB_PATH: str = "data/buckfiddy.db"

    model_config = {"env_file": ".env", "env_prefix": "BF_"}

from typing import Literal

from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    # Backend selection
    TRADING_BACKEND: Literal["mock", "real"] = "mock"

    # Claude API
    ANTHROPIC_API_KEY: str = ""
    ANTHROPIC_BASE_URL: str = ""  # Custom endpoint (e.g. Azure-hosted)
    CLAUDE_MODEL: str = "claude-sonnet-4-5"  # Legacy: used if FAST/RESEARCH not set
    CLAUDE_MODEL_FAST: str = "claude-haiku-4-5"  # Position review, market selection
    CLAUDE_MODEL_RESEARCH: str = "claude-sonnet-4-5"  # Web research + trading
    CLAUDE_MAX_TOKENS: int = 4096

    # Polymarket (real backend only)
    POLYMARKET_PRIVATE_KEY: str = ""
    POLYMARKET_CHAIN_ID: int = 137
    POLYMARKET_FUNDER_ADDRESS: str = ""
    POLYMARKET_SIGNATURE_TYPE: int = 0  # 0=EOA

    # Agent behavior — cycle scheduling
    FULL_CYCLE_INTERVAL_SECONDS: int = 7200  # 2 hours between full scan+trade cycles
    POSITION_CHECK_INTERVAL_SECONDS: int = 1800  # 30 min between lightweight position checks
    SCAN_INTERVAL_SECONDS: int = 300  # Legacy fallback
    POSITION_REVIEW_INTERVAL_SECONDS: int = 600  # Legacy fallback

    # Agent behavior — trading
    EDGE_THRESHOLD: float = 0.08
    MAX_POSITION_PCT: float = 0.15
    MAX_OPEN_POSITIONS: int = 10
    MAX_NEW_ESTIMATES_PER_CYCLE: int = 2  # Hard limit on new market estimates per cycle
    STOP_LOSS_PCT: float = 0.50
    STARTING_BALANCE: float = 100.0

    # Market filters
    MIN_MARKET_LIQUIDITY: float = 5000.0
    MIN_MARKET_VOLUME: float = 10000.0
    MAX_MARKETS_PER_SCAN: int = 5

    # Persistence
    DB_PATH: str = "data/buckfiddy.db"

    model_config = {"env_file": ".env", "env_prefix": "BF_"}

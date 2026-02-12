import logging
import sys

from buckfiddy.agent.loop import AgentLoop
from buckfiddy.config import Settings
from buckfiddy.markets.scanner import MarketScanner
from buckfiddy.trading.mock import MockTradingBackend


def setup_logging():
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
        handlers=[
            logging.StreamHandler(sys.stdout),
            logging.FileHandler("data/buckfiddy.log"),
        ],
    )


def main():
    setup_logging()
    logger = logging.getLogger(__name__)

    try:
        settings = Settings()
    except Exception as e:
        print(f"Failed to load settings: {e}")
        print("Make sure you have a .env file — copy .env.example to .env and fill in values")
        sys.exit(1)

    if settings.TRADING_BACKEND == "mock":
        backend = MockTradingBackend(settings)
        logger.info("Using MOCK trading backend")
    elif settings.TRADING_BACKEND == "real":
        from buckfiddy.trading.real import RealTradingBackend

        backend = RealTradingBackend(settings)
        logger.info("Using REAL Polymarket trading backend")
    else:
        print(f"Unknown backend: {settings.TRADING_BACKEND}")
        sys.exit(1)

    scanner = MarketScanner(settings)
    agent = AgentLoop(backend, scanner, settings)

    logger.info(f"BuckFiddy starting")
    logger.info(f"  Backend: {settings.TRADING_BACKEND}")
    logger.info(f"  Model: {settings.CLAUDE_MODEL}")
    logger.info(f"  Edge threshold: {settings.EDGE_THRESHOLD:.0%}")
    logger.info(f"  Scan interval: {settings.SCAN_INTERVAL_SECONDS}s")
    logger.info(f"  Max position: {settings.MAX_POSITION_PCT:.0%} of balance")
    logger.info(f"  Stop loss: {settings.STOP_LOSS_PCT:.0%}")

    if "--single" in sys.argv:
        logger.info("Running single cycle")
        agent.run_single_cycle()
    else:
        agent.run()


if __name__ == "__main__":
    main()

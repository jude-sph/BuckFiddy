import logging

from buckfiddy.trading.models import TradeResult

logger = logging.getLogger(__name__)


def check_stop_losses(backend, stop_loss_pct: float) -> list[TradeResult]:
    """Check all positions against stop loss threshold.

    Runs BEFORE Claude's agent turn. Non-negotiable.
    If a position has lost more than stop_loss_pct of its entry value,
    it is closed immediately.

    Example: stop_loss_pct = 0.50
        Bought 10 shares at 0.60 = $6.00 entry
        Current price 0.25, current value = $2.50
        PnL% = (2.50 - 6.00) / 6.00 = -58.3%
        -58.3% < -50% => STOP LOSS TRIGGERED
    """
    results = []

    try:
        wallet = backend.get_wallet_state()
    except Exception as e:
        logger.error(f"Failed to get wallet state for stop loss check: {e}")
        return results

    for position in wallet.positions:
        if position.unrealized_pnl_pct <= -stop_loss_pct:
            logger.warning(
                f"STOP LOSS triggered: {position.market_question} "
                f"({position.outcome}) — loss: {position.unrealized_pnl_pct:.1%}"
            )
            try:
                result = backend.close_position(position.position_id)
                results.append(result)
            except Exception as e:
                logger.error(f"Failed to close position {position.position_id}: {e}")
                results.append(
                    TradeResult(success=False, message=f"Stop loss close failed: {e}")
                )

    return results

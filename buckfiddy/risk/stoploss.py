import logging
from dataclasses import dataclass

from buckfiddy.trading.models import TradeResult

logger = logging.getLogger(__name__)


@dataclass
class GuardResult:
    """Result from a risk guard (stop-loss or take-profit)."""
    trade_result: TradeResult
    guard_type: str  # "stop_loss" or "take_profit"
    message: str


def check_risk_guards(
    backend,
    stop_loss_pct: float,
    take_profit_pct: float,
) -> list[GuardResult]:
    """Check all positions against stop-loss and take-profit thresholds.

    Runs BEFORE Claude's agent turn. Non-negotiable.
    - Stop loss: if a position has lost more than stop_loss_pct → close
    - Take profit: if a position has gained more than take_profit_pct → close

    Example: stop_loss_pct = 0.50
        Bought 10 shares at 0.60 = $6.00 entry
        Current price 0.25, current value = $2.50
        PnL% = (2.50 - 6.00) / 6.00 = -58.3%
        -58.3% < -50% => STOP LOSS TRIGGERED

    Example: take_profit_pct = 0.40
        Bought 10 shares at 0.60 = $6.00 entry
        Current price 0.90, current value = $9.00
        PnL% = (9.00 - 6.00) / 6.00 = +50.0%
        +50.0% > +40% => TAKE PROFIT TRIGGERED
    """
    results = []

    try:
        wallet = backend.get_wallet_state()
    except Exception as e:
        logger.error(f"Failed to get wallet state for risk guard check: {e}")
        return results

    for position in wallet.positions:
        guard_type = None
        reason = ""

        if position.unrealized_pnl_pct <= -stop_loss_pct:
            guard_type = "stop_loss"
            reason = (
                f"STOP LOSS triggered: {position.market_question} "
                f"({position.outcome}) — loss: {position.unrealized_pnl_pct:.1%}"
            )
        elif take_profit_pct > 0 and position.unrealized_pnl_pct >= take_profit_pct:
            guard_type = "take_profit"
            reason = (
                f"TAKE PROFIT triggered: {position.market_question} "
                f"({position.outcome}) — gain: {position.unrealized_pnl_pct:+.1%}"
            )

        if guard_type:
            logger.warning(reason)
            try:
                trade_result = backend.close_position(position.position_id)
                results.append(GuardResult(
                    trade_result=trade_result,
                    guard_type=guard_type,
                    message=reason,
                ))
            except Exception as e:
                logger.error(f"Failed to close position {position.position_id}: {e}")
                results.append(GuardResult(
                    trade_result=TradeResult(success=False, message=f"{guard_type} close failed: {e}"),
                    guard_type=guard_type,
                    message=reason,
                ))

    return results

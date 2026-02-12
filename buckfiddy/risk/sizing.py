def calculate_position_size(
    balance: float,
    edge: float,
    market_price: float,
    max_position_pct: float,
    max_open_positions: int,
    current_position_count: int,
) -> float:
    """Calculate position size using simplified half-Kelly criterion.

    Args:
        balance: Available cash balance
        edge: Claude's estimate minus market price (positive = underpriced)
        market_price: Current market midpoint price
        max_position_pct: Maximum fraction of balance per position
        max_open_positions: Maximum number of simultaneous positions
        current_position_count: How many positions are currently open

    Returns:
        Dollar amount to allocate to this trade (0 if no trade)
    """
    if current_position_count >= max_open_positions:
        return 0.0

    if balance <= 0 or market_price <= 0 or market_price >= 1.0:
        return 0.0

    # Base allocation cap
    max_dollars = balance * max_position_pct

    # Simplified Kelly: fraction = edge / (1 - price) for buying underpriced
    abs_edge = abs(edge)
    kelly_fraction = abs_edge / (1.0 - market_price)

    # Half-Kelly for safety, capped at 1.0
    half_kelly = min(kelly_fraction * 0.5, 1.0)

    # Scale the base allocation by kelly fraction
    amount = max_dollars * half_kelly

    # Floor at $2, cap at max
    return max(min(round(amount, 2), max_dollars), 2.0)

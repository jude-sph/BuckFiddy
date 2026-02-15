def calculate_position_size(
    balance: float,
    edge: float,
    market_price: float,
    max_position_pct: float,
    kelly_fraction: float = 0.5,
) -> float:
    """Calculate position size using Kelly criterion.

    Args:
        balance: Available cash balance
        edge: Claude's estimate minus market price (positive = underpriced)
        market_price: Current market midpoint price
        max_position_pct: Maximum fraction of balance per position
        kelly_fraction: Kelly multiplier (0.25=conservative, 0.5=moderate, 1.0=aggressive)

    Returns:
        Dollar amount to allocate to this trade (0 if no trade)
    """
    if balance <= 0 or market_price <= 0 or market_price >= 1.0:
        return 0.0

    # Base allocation cap
    max_dollars = balance * max_position_pct

    # Kelly: fraction = edge / (1 - price) for buying underpriced
    abs_edge = abs(edge)
    kelly = abs_edge / (1.0 - market_price)

    # Apply Kelly multiplier and cap at 1.0
    scaled_kelly = min(kelly * kelly_fraction, 1.0)

    # Scale the base allocation by kelly fraction
    amount = max_dollars * scaled_kelly

    # Floor at $2, cap at max
    return max(min(round(amount, 2), max_dollars), 2.0)

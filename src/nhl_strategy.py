"""
NHL Mean Reversion Trading Strategy

Based on backtest results showing:
- 95% win rate
- +7.88 cents avg P&L per trade
- Best performance on deep dips (≤35% entry)

Strategy:
1. Identify favorites (≥57% at market open)
2. Wait for price drop to ≤40%
3. Enter with tiered position sizing
4. Exit at target range or hold to outcome for deep dips
"""

import os
from typing import Dict, Tuple, Optional
from datetime import datetime


def should_enter_position(
    current_price: float,
    opening_price: float,
    min_favorite_threshold: float = 57.0,
    max_entry_price: float = 45.0
) -> bool:
    """
    Determine if we should enter a position.

    Args:
        current_price: Current market price (YES side, 0-100)
        opening_price: Opening price when market first appeared
        min_favorite_threshold: Minimum opening price to qualify as favorite
        max_entry_price: Maximum price to enter (below 45 = 44 or less)

    Returns:
        True if we should enter, False otherwise
    """
    # Must have started as a favorite
    if opening_price < min_favorite_threshold:
        return False

    # Current price must be below entry threshold (44 or less)
    if current_price >= max_entry_price:
        return False

    return True


def get_position_size(
    entry_price: float,
    base_position_size: float = 100.0
) -> float:
    """
    Calculate position size based on entry depth.

    Position sizing tiers (from backtest):
    - 40-44: 0.5x (shallow dips)
    - 36-39: 1.0x (medium dips)
    - ≤35: 1.5x (deep dips - best performance)

    Args:
        entry_price: Price at which we're entering (0-100)
        base_position_size: Base position size in dollars

    Returns:
        Position size in dollars
    """
    multiplier = float(os.getenv('POSITION_SIZE_MULTIPLIER', '1.0'))

    if entry_price >= 40:
        # Shallow dip: 0.5x
        return base_position_size * 0.5 * multiplier
    elif entry_price >= 36:
        # Medium dip: 1.0x
        return base_position_size * 1.0 * multiplier
    else:  # entry_price <= 35
        # Deep dip: 1.5x (best performance)
        return base_position_size * 1.5 * multiplier


def get_exit_targets(entry_price: float) -> Tuple[float, float]:
    """
    Determine exit target range based on entry depth.

    Exit strategy (from backtest):
    - Shallow dips (40-45): Quick exit at +3 to +6 cents
    - Deep dips (≤39): Target +10 to +15 cents

    Args:
        entry_price: Price at which we entered (0-100)

    Returns:
        Tuple of (exit_min, exit_max) in cents
    """
    if entry_price >= 40:
        # Shallow dip: Take quick profit
        return (entry_price + 3, entry_price + 6)
    else:
        # Deep dip: Target larger move
        return (entry_price + 10, entry_price + 15)


def should_exit_position(
    entry_price: float,
    current_price: float,
    time_in_position_minutes: int
) -> Tuple[bool, str]:
    """
    Determine if we should exit a position during the 90-minute window.

    This is for in-game monitoring ONLY (puck drop + 90 minutes).
    All positions are force-closed at the 90-minute mark.

    Args:
        entry_price: Price at which we entered
        current_price: Current market price
        time_in_position_minutes: How long we've been in the position

    Returns:
        Tuple of (should_exit, reason)
    """
    exit_min, exit_max = get_exit_targets(entry_price)

    # Check if we've hit target range - TAKE PROFIT
    if exit_min <= current_price <= exit_max:
        return (True, f"hit_target_range_{exit_min}-{exit_max}¢")

    # If price jumped above our target range, exit immediately
    if current_price > exit_max:
        return (True, f"price_above_target_{current_price}¢")

    # Deep dips (≤35¢): More patient, let it bounce
    if entry_price <= 35:
        # Within 90-min window, hold for bigger bounce
        if current_price >= 45:
            # Strong bounce, take profit
            return (True, f"deep_dip_strong_bounce_{current_price}¢")
        # Otherwise keep monitoring (will force close at 90min)
        return (False, "deep_dip_monitoring")

    # Shallow/medium dips (36-44¢): Take quick profits
    if entry_price >= 36:
        # If we've recovered back to entry + 3-6¢, exit
        if current_price >= entry_price + 3:
            return (True, f"shallow_bounce_{current_price}¢")
        # Otherwise keep monitoring
        return (False, "shallow_monitoring")

    # Default: keep monitoring
    return (False, "monitoring")


def calculate_expected_value(
    entry_price: float,
    historical_win_rate: Optional[float] = None
) -> Dict[str, float]:
    """
    Calculate expected value based on entry price and historical performance.

    Args:
        entry_price: Price at which we're considering entry
        historical_win_rate: Optional override for win rate (defaults to backtest data)

    Returns:
        Dict with EV metrics
    """
    # Historical performance from backtest
    if entry_price <= 35:
        win_rate = historical_win_rate or 0.95
        avg_win = 24.60
        avg_loss = -18.0
    elif entry_price <= 40:
        win_rate = historical_win_rate or 0.94
        avg_win = 4.94
        avg_loss = -18.0
    else:  # 41-45
        win_rate = historical_win_rate or 0.88
        avg_win = 1.17
        avg_loss = -3.6

    expected_value = (win_rate * avg_win) + ((1 - win_rate) * avg_loss)

    return {
        'expected_value': expected_value,
        'win_rate': win_rate,
        'avg_win': avg_win,
        'avg_loss': avg_loss,
        'profit_factor': abs(avg_win / avg_loss) if avg_loss != 0 else float('inf')
    }


def get_strategy_summary() -> str:
    """Return a summary of the strategy for logging."""
    return """
NHL In-Game Mean Reversion Strategy
------------------------------------
Entry: Limit orders at 30min pregame for favorites ≥57%
       Orders fill if price drops <45¢ during first 90min of game
Position Sizing: 0.5x (40-44¢), 1.0x (36-39¢), 1.5x (≤35¢)
Exit: Target bounces within 90-minute window
      Force close all positions at window close
Historical: 95% win rate, +7.88¢ avg per trade
"""

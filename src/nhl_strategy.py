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
    max_entry_price: float = 40.0
) -> bool:
    """
    Determine if we should enter a position.

    Args:
        current_price: Current market price (YES side, 0-100)
        opening_price: Opening price when market first appeared
        min_favorite_threshold: Minimum opening price to qualify as favorite
        max_entry_price: Maximum price to enter (inclusive)

    Returns:
        True if we should enter, False otherwise
    """
    # Must have started as a favorite
    if opening_price < min_favorite_threshold:
        return False

    # Current price must be at or below entry threshold
    if current_price > max_entry_price:
        return False

    # Skip the 46-50 range (poor performance in backtest)
    if 46 <= current_price <= 50:
        return False

    return True


def get_position_size(
    entry_price: float,
    base_position_size: float = 100.0
) -> float:
    """
    Calculate position size based on entry depth.

    Position sizing tiers (from backtest):
    - 46-50: 0x (skip - already filtered in should_enter_position)
    - 41-45: 0.5x
    - 36-40: 1.0x
    - ≤35: 1.5x

    Args:
        entry_price: Price at which we're entering (0-100)
        base_position_size: Base position size in dollars

    Returns:
        Position size in dollars
    """
    multiplier = float(os.getenv('POSITION_SIZE_MULTIPLIER', '1.0'))

    if entry_price >= 46:
        # Should never reach here due to should_enter_position filter
        return 0.0
    elif entry_price >= 41:
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
    Determine if we should exit a position.

    Args:
        entry_price: Price at which we entered
        current_price: Current market price
        time_in_position_minutes: How long we've been in the position

    Returns:
        Tuple of (should_exit, reason)
    """
    exit_min, exit_max = get_exit_targets(entry_price)

    # Check if we've hit target range
    if exit_min <= current_price <= exit_max:
        return (True, f"hit_target_range_{exit_min}-{exit_max}")

    # If price jumped above our target range, exit immediately
    # (This means price passed through our target)
    if current_price > exit_max:
        return (True, f"price_above_target_exiting_at_{current_price}")

    # Deep dips: Consider holding to outcome if conditions are right
    if entry_price <= 35:
        # If still in 90-minute window, keep monitoring
        if time_in_position_minutes < 90:
            # Check if price has recovered significantly (≥40)
            if current_price >= 40:
                # Recovered well, take profit
                return (True, f"deep_dip_recovered_to_{current_price}")
            # Otherwise keep holding
            return (False, "deep_dip_monitoring")
        else:
            # Past 90 minutes, let it ride to outcome
            return (False, "deep_dip_holding_to_outcome")

    # For shallow/medium dips, exit after 90 minutes regardless
    if time_in_position_minutes >= 90:
        return (True, f"time_limit_exited_at_{current_price}")

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
NHL Mean Reversion Strategy
---------------------------
Entry: Favorites (≥57% open) dropping to ≤40%
Position Sizing: 0.5x (41-45), 1.0x (36-40), 1.5x (≤35)
Exit: Quick profits for shallow, hold for deep dips
Historical: 95% win rate, +7.88¢ avg per trade
"""

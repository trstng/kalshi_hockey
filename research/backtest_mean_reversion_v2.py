#!/usr/bin/env python3
"""
Backtest improved mean reversion strategy on NHL markets.

Improvements:
1. Tighter entry filters (prefer ≤40, best ≤35)
2. Position sizing by entry depth
3. Two-track exits:
   - Shallow dips (40-45): Quick exit at +3 to +6
   - Deep dips (≤35): Tiered take-profit or hold to outcome
"""

import pandas as pd
import numpy as np
from datetime import datetime, timedelta
from pathlib import Path


def identify_favorites(merged_df):
    """Identify which team was the favorite for each game."""
    print("\nIdentifying favorites...")

    favorites = []
    for (date, matchup), group in merged_df.groupby(['date', 'matchup']):
        if len(group) != 2:
            continue

        team1 = group.iloc[0]
        team2 = group.iloc[1]

        if team1['last_price'] > team2['last_price']:
            favorite = team1
        else:
            favorite = team2

        if favorite['last_price'] >= 57:
            favorites.append(favorite)

    favorites_df = pd.DataFrame(favorites)
    print(f"✓ Found {len(favorites_df)} favorites (>=57% opening price)")

    return favorites_df


def get_position_size(entry_price):
    """
    Determine position size based on entry depth.

    Returns multiplier for position size:
    - 50-46: 0x (skip)
    - 45-41: 0.5x
    - 40-36: 1x
    - ≤35: 1.5x
    """
    if entry_price >= 46:
        return 0  # Skip
    elif entry_price >= 41:
        return 0.5
    elif entry_price >= 36:
        return 1.0
    else:  # ≤35
        return 1.5


def get_exit_targets(entry_price):
    """
    Determine exit targets based on entry depth.

    Returns (quick_exit_min, quick_exit_max) or None for hold_to_outcome track
    """
    if entry_price >= 40:
        # Shallow dip: Quick exit at +3 to +6
        return (entry_price + 3, entry_price + 6)
    else:
        # Deep dip: Tiered take-profit at +10
        return (entry_price + 10, entry_price + 15)


def get_price_movements(ticker, puck_drop_time, trades_df):
    """Get price movements for a ticker during the first 90 minutes after puck drop."""
    ticker_trades = trades_df[trades_df['ticker'] == ticker].copy()

    # Convert timestamps - trade timestamps are in local time (PT), puck drop is UTC
    ticker_trades['timestamp'] = pd.to_datetime(ticker_trades['timestamp']).dt.tz_localize('US/Pacific')
    puck_drop = pd.to_datetime(puck_drop_time).tz_convert('US/Pacific')

    # Filter to 90 minutes after puck drop
    window_end = puck_drop + timedelta(minutes=90)

    window_trades = ticker_trades[
        (ticker_trades['timestamp'] >= puck_drop) &
        (ticker_trades['timestamp'] <= window_end)
    ].copy()

    window_trades = window_trades.sort_values('timestamp')
    return window_trades[['timestamp', 'yes_price']]


def check_second_leg_down(price_moves, entry_time, entry_price, lookback_minutes=30):
    """
    Check if there's a second leg down after entry.

    A second leg down = price drops below entry_price again within lookback_minutes
    """
    future_moves = price_moves[price_moves['timestamp'] > entry_time]
    window_end = entry_time + timedelta(minutes=lookback_minutes)
    window_moves = future_moves[future_moves['timestamp'] <= window_end]

    if len(window_moves) == 0:
        return False

    # Check if price went below entry again
    return (window_moves['yes_price'] < entry_price).any()


def simulate_strategy(favorites_df, trades_df):
    """
    Simulate the improved mean reversion strategy.
    """
    print(f"\nSimulating improved strategy...")

    results = []

    for idx, market in favorites_df.iterrows():
        ticker = market['ticker']
        puck_drop = market['start_time_utc']
        opening_price = market['last_price']
        outcome = market['settled_yes']

        # Get price movements in 90-minute window
        price_moves = get_price_movements(ticker, puck_drop, trades_df)

        if len(price_moves) == 0:
            results.append({
                'ticker': ticker,
                'opening_price': opening_price,
                'entry_price': None,
                'exit_price': None,
                'outcome': outcome,
                'position_size': 0,
                'pnl': 0,
                'pnl_1x': 0,
                'status': 'no_trades_in_window'
            })
            continue

        # Look for entry signal: price drops to ≤50
        entry_trades = price_moves[price_moves['yes_price'] <= 50]

        if len(entry_trades) == 0:
            results.append({
                'ticker': ticker,
                'opening_price': opening_price,
                'entry_price': None,
                'exit_price': None,
                'outcome': outcome,
                'position_size': 0,
                'pnl': 0,
                'pnl_1x': 0,
                'status': 'no_entry_signal'
            })
            continue

        # Take first entry signal
        entry_price = entry_trades.iloc[0]['yes_price']
        entry_time = entry_trades.iloc[0]['timestamp']

        # Determine position size
        position_size = get_position_size(entry_price)

        if position_size == 0:
            # Skip 46-50 entries
            results.append({
                'ticker': ticker,
                'opening_price': opening_price,
                'entry_price': entry_price,
                'exit_price': None,
                'outcome': outcome,
                'position_size': 0,
                'pnl': 0,
                'pnl_1x': 0,
                'status': 'skipped_shallow_entry'
            })
            continue

        # Get exit targets
        exit_min, exit_max = get_exit_targets(entry_price)

        # Look for ANY trade at or above exit_min after entry
        # (If price reaches 83, it must have passed through our target range)
        exit_trades = price_moves[
            (price_moves['timestamp'] > entry_time) &
            (price_moves['yes_price'] >= exit_min)
        ]

        if len(exit_trades) > 0:
            # Exit at target range: use actual price if in range, else use exit_min
            actual_price = exit_trades.iloc[0]['yes_price']
            if actual_price <= exit_max:
                # Price is in target range, exit at actual price
                exit_price = actual_price
            else:
                # Price jumped above target range, assume we exited at exit_min
                # (conservative - in reality we'd have seen bid/ask cross target)
                exit_price = exit_min

            raw_pnl = exit_price - entry_price
            pnl = raw_pnl * position_size
            status = 'exited_in_window'
        else:
            # Deep dip track: Check if we should hold to outcome
            if entry_price <= 35:
                # Check for second leg down or if still ≤40 after 30 minutes
                has_second_leg = check_second_leg_down(price_moves, entry_time, entry_price, 30)

                # Check price after 30 minutes
                future_moves = price_moves[price_moves['timestamp'] > entry_time + timedelta(minutes=30)]
                still_low = False
                if len(future_moves) > 0:
                    still_low = future_moves.iloc[0]['yes_price'] <= 40

                if has_second_leg or still_low:
                    # Hold to outcome
                    exit_price = 100 if outcome else 0
                    raw_pnl = exit_price - entry_price
                    pnl = raw_pnl * position_size
                    status = 'deep_dip_held_to_outcome'
                else:
                    # No favorable conditions, exit at market
                    exit_price = price_moves.iloc[-1]['yes_price']
                    raw_pnl = exit_price - entry_price
                    pnl = raw_pnl * position_size
                    status = 'exited_at_window_close'
            else:
                # Shallow/medium dip without exit signal - exit at window close
                exit_price = price_moves.iloc[-1]['yes_price']
                raw_pnl = exit_price - entry_price
                pnl = raw_pnl * position_size
                status = 'exited_at_window_close'

        results.append({
            'ticker': ticker,
            'opening_price': opening_price,
            'entry_price': entry_price,
            'exit_price': exit_price,
            'outcome': outcome,
            'position_size': position_size,
            'pnl': pnl,
            'pnl_1x': raw_pnl,  # Normalized to 1x for comparison
            'status': status
        })

    results_df = pd.DataFrame(results)
    return results_df


def calculate_performance(results_df):
    """Calculate strategy performance metrics."""
    print("\n" + "="*80)
    print("IMPROVED STRATEGY PERFORMANCE")
    print("="*80)

    # Filter to trades that actually entered
    trades = results_df[results_df['entry_price'].notna() & (results_df['position_size'] > 0)]

    if len(trades) == 0:
        print("\n⚠️  No trades executed")
        return

    print(f"\nTotal opportunities: {len(results_df)}")
    print(f"Trades executed: {len(trades)}")
    print(f"Entry rate: {len(trades) / len(results_df):.1%}")

    # P&L stats (with position sizing)
    total_pnl = trades['pnl'].sum()
    avg_pnl = trades['pnl'].mean()
    win_rate = (trades['pnl'] > 0).mean()

    # Also show 1x normalized P&L for comparison
    total_pnl_1x = trades['pnl_1x'].sum()
    avg_pnl_1x = trades['pnl_1x'].mean()

    print(f"\n{'-'*40}")
    print("P&L STATISTICS (WITH POSITION SIZING)")
    print(f"{'-'*40}")
    print(f"Total P&L: {total_pnl:.1f} cents (vs {total_pnl_1x:.1f} at 1x)")
    print(f"Average P&L per trade: {avg_pnl:.2f} cents (vs {avg_pnl_1x:.2f} at 1x)")
    print(f"Win rate: {win_rate:.1%}")
    print(f"Average winner: {trades[trades['pnl'] > 0]['pnl'].mean():.2f} cents")
    print(f"Average loser: {trades[trades['pnl'] < 0]['pnl'].mean():.2f} cents")

    # Position size distribution
    print(f"\n{'-'*40}")
    print("POSITION SIZE DISTRIBUTION")
    print(f"{'-'*40}")
    print(trades['position_size'].value_counts().sort_index())

    # Entry price distribution
    print(f"\n{'-'*40}")
    print("ENTRY PRICE DISTRIBUTION")
    print(f"{'-'*40}")
    entry_buckets = pd.cut(trades['entry_price'], bins=[0, 35, 40, 45, 50, 100])
    print(entry_buckets.value_counts().sort_index())

    # Exit analysis
    print(f"\n{'-'*40}")
    print("EXIT ANALYSIS")
    print(f"{'-'*40}")
    print(trades['status'].value_counts())

    # By entry depth
    print(f"\n{'-'*40}")
    print("PERFORMANCE BY ENTRY DEPTH")
    print(f"{'-'*40}")
    trades['entry_bucket'] = pd.cut(trades['entry_price'], bins=[0, 35, 40, 45, 50, 100],
                                      labels=['≤35', '36-40', '41-45', '46-50', '>50'])
    perf_by_depth = trades.groupby('entry_bucket', observed=True).agg({
        'pnl': ['count', 'mean', 'sum'],
        'position_size': 'mean'
    })
    print(perf_by_depth)

    return trades


def main():
    print("="*80)
    print("NHL MEAN REVERSION STRATEGY BACKTEST V2")
    print("="*80)

    # Load data
    print("\nLoading data...")
    data_dir = Path('../data')

    merged_df = pd.read_csv(data_dir / 'nhl_merged.csv')
    trades_df = pd.read_csv(data_dir / 'kalshi_nhl_trades.csv')

    print(f"✓ Loaded {len(merged_df)} markets")
    print(f"✓ Loaded {len(trades_df)} trades")

    # Identify favorites
    favorites_df = identify_favorites(merged_df)

    if len(favorites_df) == 0:
        print("\n⚠️  No favorites found")
        return

    # Simulate strategy
    results_df = simulate_strategy(favorites_df, trades_df)

    # Calculate performance
    trades = calculate_performance(results_df)

    # Save results
    output_file = data_dir / 'backtest_results_v2.csv'
    results_df.to_csv(output_file, index=False)
    print(f"\n✓ Saved results to {output_file}")

    # Sample trades
    if trades is not None and len(trades) > 0:
        print("\n" + "="*80)
        print("SAMPLE TRADES")
        print("="*80)
        sample_cols = ['ticker', 'entry_price', 'exit_price', 'position_size', 'pnl', 'status']
        print(trades[sample_cols].head(15).to_string(index=False))


if __name__ == "__main__":
    main()

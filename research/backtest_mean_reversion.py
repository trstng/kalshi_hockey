#!/usr/bin/env python3
"""
Backtest mean reversion strategy on NHL markets.

Strategy:
1. Identify favorites (>57% at market open)
2. Wait for price drop below 50% down to 20% in first 90 minutes after puck drop
3. Buy at that dip
4. Sell when price rebounds to 40-60% range

Since we only have YES prices, we need to:
- Use YES price directly for favorites
- Invert (100 - YES) for underdogs to get their implied probability
"""

import pandas as pd
import numpy as np
from datetime import datetime, timedelta
from pathlib import Path


def identify_favorites(merged_df):
    """
    Identify which team was the favorite for each game.

    For each matchup, compare the two markets to see which had higher opening price.
    """
    print("\nIdentifying favorites...")

    # Group by game (date + matchup)
    favorites = []

    for (date, matchup), group in merged_df.groupby(['date', 'matchup']):
        if len(group) != 2:
            continue  # Skip if not exactly 2 markets per game

        # Get both teams' prices
        team1 = group.iloc[0]
        team2 = group.iloc[1]

        # The team with higher last_price is the favorite
        if team1['last_price'] > team2['last_price']:
            favorite = team1
        else:
            favorite = team2

        # Only include if favorite >= 57%
        if favorite['last_price'] >= 57:
            favorites.append(favorite)

    favorites_df = pd.DataFrame(favorites)
    print(f"✓ Found {len(favorites_df)} favorites (>=57% opening price)")

    return favorites_df


def get_price_movements(ticker, puck_drop_time, trades_df):
    """
    Get price movements for a ticker during the first 90 minutes after puck drop.

    Returns:
        DataFrame with timestamp and yes_price in the 90-minute window
    """
    # Filter trades for this ticker
    ticker_trades = trades_df[trades_df['ticker'] == ticker].copy()

    # Convert timestamps - trade timestamps are in local time (PT), puck drop is UTC
    # Localize trade timestamps to Pacific time, then convert puck drop to Pacific for comparison
    ticker_trades['timestamp'] = pd.to_datetime(ticker_trades['timestamp']).dt.tz_localize('US/Pacific')
    puck_drop = pd.to_datetime(puck_drop_time).tz_convert('US/Pacific')

    # Filter to 90 minutes after puck drop
    window_end = puck_drop + timedelta(minutes=90)

    window_trades = ticker_trades[
        (ticker_trades['timestamp'] >= puck_drop) &
        (ticker_trades['timestamp'] <= window_end)
    ].copy()

    # Sort by timestamp
    window_trades = window_trades.sort_values('timestamp')

    return window_trades[['timestamp', 'yes_price']]


def simulate_strategy(favorites_df, trades_df, entry_range=(20, 50), exit_range=(40, 60)):
    """
    Simulate the mean reversion strategy.

    Args:
        favorites_df: DataFrame of favorite markets
        trades_df: DataFrame of all trades
        entry_range: (min, max) price range to enter position
        exit_range: (min, max) price range to exit position

    Returns:
        DataFrame with trade results
    """
    print(f"\nSimulating strategy...")
    print(f"  Entry range: {entry_range[0]}-{entry_range[1]}%")
    print(f"  Exit range: {exit_range[0]}-{exit_range[1]}%")

    results = []

    for idx, market in favorites_df.iterrows():
        ticker = market['ticker']
        puck_drop = market['start_time_utc']
        opening_price = market['last_price']
        outcome = market['settled_yes']

        # Get price movements in 90-minute window
        price_moves = get_price_movements(ticker, puck_drop, trades_df)

        if len(price_moves) == 0:
            # No trades in window, skip
            continue

        # Look for entry signal: price drops into entry range
        entry_trades = price_moves[
            (price_moves['yes_price'] >= entry_range[0]) &
            (price_moves['yes_price'] <= entry_range[1])
        ]

        if len(entry_trades) == 0:
            # No entry signal
            results.append({
                'ticker': ticker,
                'opening_price': opening_price,
                'entry_price': None,
                'exit_price': None,
                'outcome': outcome,
                'pnl': 0,
                'status': 'no_entry_signal'
            })
            continue

        # Take first entry signal
        entry_price = entry_trades.iloc[0]['yes_price']
        entry_time = entry_trades.iloc[0]['timestamp']

        # Look for exit signal after entry: price rebounds to exit range
        exit_trades = price_moves[
            (price_moves['timestamp'] > entry_time) &
            (price_moves['yes_price'] >= exit_range[0]) &
            (price_moves['yes_price'] <= exit_range[1])
        ]

        if len(exit_trades) > 0:
            # We exited in the window
            exit_price = exit_trades.iloc[0]['yes_price']
            pnl = exit_price - entry_price
            status = 'exited_in_window'
        else:
            # Held to outcome
            exit_price = 100 if outcome else 0
            pnl = exit_price - entry_price
            status = 'held_to_outcome'

        results.append({
            'ticker': ticker,
            'opening_price': opening_price,
            'entry_price': entry_price,
            'exit_price': exit_price,
            'outcome': outcome,
            'pnl': pnl,
            'status': status
        })

    results_df = pd.DataFrame(results)
    return results_df


def calculate_performance(results_df):
    """Calculate strategy performance metrics."""
    print("\n" + "="*80)
    print("STRATEGY PERFORMANCE")
    print("="*80)

    # Filter to trades that actually entered
    trades = results_df[results_df['entry_price'].notna()]

    if len(trades) == 0:
        print("\n⚠️  No trades executed")
        return

    print(f"\nTotal opportunities: {len(results_df)}")
    print(f"Trades executed: {len(trades)}")
    print(f"Entry rate: {len(trades) / len(results_df):.1%}")

    # P&L stats
    total_pnl = trades['pnl'].sum()
    avg_pnl = trades['pnl'].mean()
    win_rate = (trades['pnl'] > 0).mean()

    print(f"\n{'-'*40}")
    print("P&L STATISTICS")
    print(f"{'-'*40}")
    print(f"Total P&L: {total_pnl:.1f} cents")
    print(f"Average P&L per trade: {avg_pnl:.2f} cents")
    print(f"Win rate: {win_rate:.1%}")
    print(f"Average winner: {trades[trades['pnl'] > 0]['pnl'].mean():.2f} cents")
    print(f"Average loser: {trades[trades['pnl'] < 0]['pnl'].mean():.2f} cents")

    # Exit analysis
    print(f"\n{'-'*40}")
    print("EXIT ANALYSIS")
    print(f"{'-'*40}")
    print(trades['status'].value_counts())

    # Outcome analysis
    if 'outcome' in trades.columns:
        print(f"\n{'-'*40}")
        print("OUTCOME ANALYSIS")
        print(f"{'-'*40}")
        outcome_pnl = trades.groupby('outcome')['pnl'].agg(['count', 'mean', 'sum'])
        outcome_pnl.index = outcome_pnl.index.map({True: 'Favorite won', False: 'Favorite lost'})
        print(outcome_pnl)

    # Price distribution
    print(f"\n{'-'*40}")
    print("ENTRY PRICE DISTRIBUTION")
    print(f"{'-'*40}")
    print(trades['entry_price'].describe())

    return trades


def main():
    print("="*80)
    print("NHL MEAN REVERSION STRATEGY BACKTEST")
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
    results_df = simulate_strategy(
        favorites_df,
        trades_df,
        entry_range=(20, 50),
        exit_range=(40, 60)
    )

    # Calculate performance
    trades = calculate_performance(results_df)

    # Save results
    output_file = data_dir / 'backtest_results.csv'
    results_df.to_csv(output_file, index=False)
    print(f"\n✓ Saved results to {output_file}")

    # Sample trades
    if trades is not None and len(trades) > 0:
        print("\n" + "="*80)
        print("SAMPLE TRADES")
        print("="*80)
        print(trades[['ticker', 'opening_price', 'entry_price', 'exit_price', 'pnl', 'status']].head(10))


if __name__ == "__main__":
    main()

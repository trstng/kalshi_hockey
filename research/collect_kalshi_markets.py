#!/usr/bin/env python3
"""
Collect Kalshi NHL market data and trade history.

This script:
1. Fetches all KXNHLGAME markets from Kalshi
2. Parses game date and teams from ticker
3. Collects complete trade history for each market
4. Saves market metadata and trades to CSV

Usage:
    python collect_kalshi_markets.py --days 90
"""

import os
import sys
import argparse
from datetime import datetime, timedelta
from pathlib import Path
import pandas as pd
from tqdm import tqdm
import time

# Add parent directory to path to import kalshi_client
sys.path.insert(0, str(Path(__file__).parent.parent.parent / "src"))

from kalshi_nfl_research.kalshi_client import KalshiClient


def parse_nhl_ticker(ticker: str):
    """
    Extract game info from NHL ticker.

    Format: KXNHLGAME-25OCT20CARVGK-VGK
    - Date: 25OCT20 (October 20, 2025)
    - Matchup: CARVGK (Carolina vs Vegas Golden Knights)
    - Outcome: VGK (betting on Vegas to win)

    Returns:
        dict with date, matchup, team, opponent
    """
    parts = ticker.split('-')
    if len(parts) < 3:
        return None

    # Parse date (e.g., "25OCT20" → October 20, 2025)
    # Format: YYMMMDD where YY=year, MMM=month, DD=day
    date_matchup = parts[1]

    try:
        year = int('20' + date_matchup[:2])    # "25" → 2025
        month_str = date_matchup[2:5].upper()  # "OCT" → OCT
        day = int(date_matchup[5:7])           # "20" → 20

        month_map = {
            'JAN': 1, 'FEB': 2, 'MAR': 3, 'APR': 4,
            'MAY': 5, 'JUN': 6, 'JUL': 7, 'AUG': 8,
            'SEP': 9, 'OCT': 10, 'NOV': 11, 'DEC': 12
        }
        month = month_map.get(month_str)
        if not month:
            return None

        date = datetime(year, month, day).date()

        # Extract matchup (everything after date)
        matchup = date_matchup[7:]  # e.g., "CARVGK"

        # Extract team being bet on
        team = parts[2]  # e.g., "VGK"

        return {
            'date': date,
            'matchup': matchup,
            'team': team
        }

    except (ValueError, KeyError):
        return None


def collect_markets(client, days_back=90):
    """
    Collect all NHL markets from Kalshi.

    Args:
        client: KalshiClient instance
        days_back: How many days back to filter (default: 90)

    Returns:
        DataFrame with market metadata
    """
    print(f"\nFetching KXNHLGAME markets...")

    # Get all markets (API returns most recent first)
    all_markets = []
    limit = 200

    # Fetch in batches
    for i in range(10):  # Max 2000 markets
        markets = client.get_markets(
            series_ticker='KXNHLGAME',
            limit=limit
        )

        if not markets:
            break

        all_markets.extend(markets)
        print(f"  Fetched {len(all_markets)} markets so far...")

        # Check if we have enough
        if len(markets) < limit:
            break

    print(f"\n✓ Found {len(all_markets)} total markets")

    # Filter to finalized markets only (ignore date range, collect all historical)
    today = datetime.now().date()

    filtered_markets = []
    for m in all_markets:
        info = parse_nhl_ticker(m.ticker)
        # Only include finalized markets from the past
        if info and info['date'] < today and m.status == 'finalized':
            filtered_markets.append({
                'ticker': m.ticker,
                'title': m.title,
                'date': info['date'],
                'matchup': info['matchup'],
                'team': info['team'],
                'status': m.status,
                'last_price': m.last_price,
                'open_time': m.open_time,
                'close_time': m.close_time,
                'yes_bid': m.yes_bid,
                'yes_ask': m.yes_ask,
            })

    df = pd.DataFrame(filtered_markets)

    # Deduplicate by ticker (Kalshi API returns many duplicates)
    before_dedup = len(df)
    df = df.drop_duplicates(subset=['ticker'], keep='first')
    after_dedup = len(df)

    print(f"✓ Filtered to {before_dedup} finalized markets")
    print(f"✓ Removed {before_dedup - after_dedup} duplicates")
    print(f"✓ Final count: {after_dedup} unique markets")

    return df


def collect_trades(client, markets_df, rate_limit=0.1):
    """
    Collect complete trade history for each market.

    Args:
        client: KalshiClient instance
        markets_df: DataFrame with market info
        rate_limit: Seconds to wait between API calls

    Returns:
        DataFrame with all trades
    """
    print(f"\nFetching trade history for {len(markets_df)} markets...")

    all_trades = []

    for idx, row in tqdm(markets_df.iterrows(), total=len(markets_df)):
        ticker = row['ticker']

        try:
            # Get all trades for this market
            trades = client.get_trades(ticker=ticker, limit=500)

            for trade in trades:
                all_trades.append({
                    'ticker': ticker,
                    'timestamp': datetime.fromtimestamp(trade.created_time),
                    'yes_price': trade.yes_price,
                    'no_price': trade.no_price,
                    'count': trade.count,
                    'taker_side': trade.taker_side,
                })

            # Rate limiting
            time.sleep(rate_limit)

        except Exception as e:
            print(f"\n  Warning: Failed to fetch trades for {ticker}: {e}")
            continue

    df = pd.DataFrame(all_trades)
    print(f"\n✓ Collected {len(df)} trades")

    return df


def main():
    parser = argparse.ArgumentParser(description='Collect Kalshi NHL market data')
    parser.add_argument('--days', type=int, default=90,
                       help='Number of days to look back (default: 90)')
    parser.add_argument('--output-dir', type=str, default='../data',
                       help='Output directory')
    args = parser.parse_args()

    print("="*80)
    print("KALSHI NHL MARKET COLLECTOR")
    print("="*80)

    # Initialize client
    client = KalshiClient()
    print("✓ Connected to Kalshi API")

    # Collect markets
    markets_df = collect_markets(client, days_back=args.days)

    if len(markets_df) == 0:
        print("\n⚠️  No markets found in date range")
        return

    # Collect trades
    trades_df = collect_trades(client, markets_df)

    # Create output directory
    Path(args.output_dir).mkdir(parents=True, exist_ok=True)

    # Export to CSV
    markets_file = f"{args.output_dir}/kalshi_nhl_markets.csv"
    trades_file = f"{args.output_dir}/kalshi_nhl_trades.csv"

    markets_df.to_csv(markets_file, index=False)
    trades_df.to_csv(trades_file, index=False)

    print(f"\n✓ Exported to:")
    print(f"  Markets: {markets_file}")
    print(f"  Trades: {trades_file}")

    # Summary statistics
    print("\n" + "="*80)
    print("SUMMARY")
    print("="*80)
    print(f"\nMarkets collected: {len(markets_df)}")
    print(f"Date range: {markets_df['date'].min()} to {markets_df['date'].max()}")
    print(f"Unique dates: {markets_df['date'].nunique()}")

    print(f"\nStatus distribution:")
    print(markets_df['status'].value_counts())

    print(f"\nTotal trades: {len(trades_df)}")
    if len(trades_df) > 0:
        print(f"Avg trades per market: {len(trades_df) / len(markets_df):.1f}")

    # Sample markets
    print(f"\nSample markets:")
    print(markets_df[['date', 'matchup', 'team', 'status', 'last_price']].head(10).to_string(index=False))

    print("\n" + "="*80)


if __name__ == "__main__":
    main()

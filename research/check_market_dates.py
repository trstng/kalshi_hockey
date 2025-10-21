#!/usr/bin/env python3
"""
Quick diagnostic to check what dates are in the NHL markets.
"""

import sys
from datetime import datetime
from pathlib import Path
import pandas as pd

# Add parent directory to path
sys.path.insert(0, str(Path(__file__).parent.parent.parent / "src"))

from kalshi_nfl_research.kalshi_client import KalshiClient


def parse_nhl_ticker(ticker: str):
    """Extract date from NHL ticker."""
    parts = ticker.split('-')
    if len(parts) < 3:
        return None

    date_matchup = parts[1]

    try:
        day = int(date_matchup[:2])
        month_str = date_matchup[2:5].upper()
        year = int('20' + date_matchup[5:7])

        month_map = {
            'JAN': 1, 'FEB': 2, 'MAR': 3, 'APR': 4,
            'MAY': 5, 'JUN': 6, 'JUL': 7, 'AUG': 8,
            'SEP': 9, 'OCT': 10, 'NOV': 11, 'DEC': 12
        }
        month = month_map.get(month_str)
        if not month:
            return None

        date = datetime(year, month, day).date()
        matchup = date_matchup[7:]
        team = parts[2]

        return {'date': date, 'matchup': matchup, 'team': team}
    except (ValueError, KeyError):
        return None


def main():
    print("Checking NHL market dates...")

    client = KalshiClient()

    # Fetch markets
    all_markets = []
    limit = 200

    for i in range(20):  # Get a good sample
        markets = client.get_markets(
            series_ticker='KXNHLGAME',
            limit=limit
        )

        if not markets:
            break

        all_markets.extend(markets)

        if len(markets) < limit:
            break

    print(f"Fetched {len(all_markets)} markets")

    # Parse dates
    today = datetime.now().date()
    market_data = []

    for m in all_markets:
        info = parse_nhl_ticker(m.ticker)
        if info and m.status == 'finalized':
            market_data.append({
                'ticker': m.ticker,
                'date': info['date'],
                'status': m.status,
                'year': info['date'].year,
                'month': info['date'].month,
            })

    df = pd.DataFrame(market_data)

    print(f"\nFinalized markets: {len(df)}")
    print(f"\nDate range: {df['date'].min()} to {df['date'].max()}")

    print("\nMarkets by year:")
    print(df['year'].value_counts().sort_index())

    print("\nMarkets by month (recent):")
    print(df[df['year'] >= 2023].groupby(['year', 'month']).size())

    print("\nSample tickers:")
    print(df[['ticker', 'date']].head(20).to_string(index=False))


if __name__ == "__main__":
    main()

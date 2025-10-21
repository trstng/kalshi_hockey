#!/usr/bin/env python3
"""Quick check of market date distribution with FIXED parser."""

import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent.parent / "src"))

from kalshi_nfl_research.kalshi_client import KalshiClient


def parse_nhl_ticker_FIXED(ticker: str):
    """FIXED: Extract date from NHL ticker (YYMMMDD format)."""
    parts = ticker.split('-')
    if len(parts) < 3:
        return None

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
        return date
    except (ValueError, KeyError):
        return None


def main():
    print("Checking market dates with FIXED parser...")

    client = KalshiClient()

    # Get all markets
    all_markets = []
    for i in range(30):
        markets = client.get_markets(series_ticker='KXNHLGAME', limit=200)
        if not markets:
            break
        all_markets.extend(markets)
        if len(markets) < 200:
            break

    print(f"Fetched {len(all_markets)} total markets")

    # Parse and filter
    finalized_dates = []
    for m in all_markets:
        if m.status == 'finalized':
            date = parse_nhl_ticker_FIXED(m.ticker)
            if date:
                finalized_dates.append(date)

    print(f"Finalized markets: {len(finalized_dates)}")
    print(f"Date range: {min(finalized_dates)} to {max(finalized_dates)}")

    # Group by month
    from collections import Counter
    by_month = Counter()
    for d in finalized_dates:
        by_month[(d.year, d.month)] += 1

    print("\nMarkets by month:")
    for (year, month), count in sorted(by_month.items()):
        print(f"  {year}-{month:02d}: {count} markets")


if __name__ == "__main__":
    main()

#!/usr/bin/env python3
"""
Verify the actual ticker date format by comparing parsed dates with close_time.
"""

import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent.parent / "src"))

from kalshi_nfl_research.kalshi_client import KalshiClient


def parse_nhl_ticker_v1(ticker: str):
    """Current parsing: assumes DDMMMYY format."""
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
        return date
    except (ValueError, KeyError):
        return None


def main():
    print("Verifying ticker format...")

    client = KalshiClient()

    # Get some recent finalized markets
    markets = client.get_markets(series_ticker='KXNHLGAME', limit=10)

    print(f"\nChecking {len(markets)} markets:\n")
    print(f"{'Ticker':<40} {'Parsed Date':<15} {'Close Time (UTC)':<25} {'Status':<12}")
    print("=" * 95)

    for m in markets:
        parsed_date = parse_nhl_ticker_v1(m.ticker)

        # Convert close_time (Unix timestamp) to datetime
        if m.close_time:
            close_dt = datetime.fromtimestamp(m.close_time)
        else:
            close_dt = None

        print(f"{m.ticker:<40} {str(parsed_date):<15} {str(close_dt):<25} {m.status:<12}")


if __name__ == "__main__":
    main()

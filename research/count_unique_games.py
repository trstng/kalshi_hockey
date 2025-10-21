#!/usr/bin/env python3
"""Count unique games (not markets) in the data."""

import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent.parent / "src"))

from kalshi_nfl_research.kalshi_client import KalshiClient


def parse_nhl_ticker(ticker: str):
    """Parse ticker to get date and matchup."""
    parts = ticker.split('-')
    if len(parts) < 3:
        return None

    date_matchup = parts[1]

    try:
        year = int('20' + date_matchup[:2])
        month_str = date_matchup[2:5].upper()
        day = int(date_matchup[5:7])

        month_map = {
            'JAN': 1, 'FEB': 2, 'MAR': 3, 'APR': 4,
            'MAY': 5, 'JUN': 6, 'JUL': 7, 'AUG': 8,
            'SEP': 9, 'OCT': 10, 'NOV': 11, 'DEC': 12
        }
        month = month_map.get(month_str)
        if not month:
            return None

        date = datetime(year, month, day).date()
        matchup = date_matchup[7:]  # e.g., "CARVGK"

        return {
            'date': date,
            'matchup': matchup
        }
    except (ValueError, KeyError):
        return None


def main():
    print("Counting unique games...")

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

    print(f"Total markets: {len(all_markets)}")

    # Filter to finalized and parse
    unique_games = set()
    finalized_count = 0

    for m in all_markets:
        if m.status == 'finalized':
            finalized_count += 1
            info = parse_nhl_ticker(m.ticker)
            if info:
                # Create unique game identifier: date + matchup
                game_id = (info['date'], info['matchup'])
                unique_games.add(game_id)

    print(f"Finalized markets: {finalized_count}")
    print(f"Unique games: {len(unique_games)}")
    print(f"Markets per game (should be 2): {finalized_count / len(unique_games):.2f}")

    # Show date range
    dates = [game[0] for game in unique_games]
    print(f"\nDate range: {min(dates)} to {max(dates)}")

    # Count by month
    from collections import Counter
    by_month = Counter()
    for date, _ in unique_games:
        by_month[(date.year, date.month)] += 1

    print("\nUnique games by month:")
    for (year, month), count in sorted(by_month.items()):
        print(f"  {year}-{month:02d}: {count} games ({count * 2} markets)")

    print(f"\nTotal unique games: {len(unique_games)}")
    print(f"Total markets (should be 2x games): {len(unique_games) * 2}")


if __name__ == "__main__":
    main()

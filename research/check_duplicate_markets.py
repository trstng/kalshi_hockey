#!/usr/bin/env python3
"""Check why there are 60 markets per game."""

import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent.parent / "src"))

from kalshi_nfl_research.kalshi_client import KalshiClient


def main():
    print("Checking duplicate markets for a single game...")

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

    # Find one game with many markets
    # Let's look for any game from Oct 18 (should be finalized)
    oct18_markets = [m for m in all_markets if '25OCT18' in m.ticker and m.status == 'finalized']

    print(f"Found {len(oct18_markets)} markets for Oct 18 games")

    if oct18_markets:
        print(f"\nFirst 30 markets for Oct 18 games:\n")
        print(f"{'Ticker':<50} {'Title':<50} {'Open Time':<25} {'Close Time':<25}")
        print("=" * 155)

        for m in oct18_markets[:30]:
            open_dt = datetime.fromtimestamp(m.open_time) if m.open_time else None
            close_dt = datetime.fromtimestamp(m.close_time) if m.close_time else None
            print(f"{m.ticker:<50} {m.title[:48]:<50} {str(open_dt):<25} {str(close_dt):<25}")

        # Check if tickers are unique
        tickers = [m.ticker for m in oct18_markets]
        unique_tickers = set(tickers)
        print(f"\n\nTotal Oct 18 markets: {len(oct18_markets)}")
        print(f"Unique tickers: {len(unique_tickers)}")
        print(f"Duplicates: {len(oct18_markets) - len(unique_tickers)}")


if __name__ == "__main__":
    main()

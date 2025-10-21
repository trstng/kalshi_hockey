#!/usr/bin/env python3
"""Check what types of NHL markets we're collecting."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent.parent / "src"))

from kalshi_nfl_research.kalshi_client import KalshiClient


def main():
    print("Checking NHL market types...")

    client = KalshiClient()

    # Get sample markets
    markets = client.get_markets(series_ticker='KXNHLGAME', limit=100)

    print(f"\nSample of {len(markets)} markets:\n")
    print(f"{'Ticker':<45} {'Title':<80}")
    print("=" * 130)

    # Look for patterns in titles
    titles = set()
    for m in markets[:100]:
        print(f"{m.ticker:<45} {m.title[:80]:<80}")
        # Extract title pattern (remove team names)
        titles.add(m.title)

    print(f"\n\nUnique title patterns found: {len(titles)}")
    print("\nFirst 20 unique titles:")
    for i, title in enumerate(list(titles)[:20]):
        print(f"  {i+1}. {title}")


if __name__ == "__main__":
    main()

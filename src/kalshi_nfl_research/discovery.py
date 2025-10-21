"""
NFL event and market discovery logic.

Finds NFL series, games, and their corresponding WIN markets.
"""

import logging
import re
from datetime import datetime
from typing import Optional

from .data_models import EventInfo, MarketInfo, SeriesInfo
from .kalshi_client import KalshiClient

logger = logging.getLogger(__name__)


def discover_nfl_series(client: KalshiClient) -> list[SeriesInfo]:
    """
    Discover all NFL-related series for team WIN markets.

    Kalshi organizes NFL markets by individual series (e.g., KXNFLWINS-ATL, KXNFLWINS-HOU).

    Args:
        client: Kalshi API client.

    Returns:
        List of NFL SeriesInfo objects for team win markets.
    """
    logger.info("Fetching all series to find NFL team win markets...")
    all_series = client.get_series(limit=500)  # Fetch more series

    # Filter for NFL team WIN series specifically
    # Pattern: KXNFLWINS-{TEAM} or KXNFLEXACTWINS{TEAM}
    nfl_win_series = []
    for s in all_series:
        ticker_upper = s.series_ticker.upper()

        # Look for team win patterns
        if (ticker_upper.startswith("KXNFLWINS-") or
            ticker_upper.startswith("KXNFLEXACTWINS")):
            nfl_win_series.append(s)
            logger.debug(f"  Found NFL win series: {s.series_ticker} - {s.title}")

    logger.info(f"Discovered {len(nfl_win_series)} NFL team win series out of {len(all_series)} total")

    return nfl_win_series


def discover_nfl_events(
    client: KalshiClient,
    series_ticker: Optional[str] = None,
    start_date: Optional[str] = None,
    end_date: Optional[str] = None,
) -> list[EventInfo]:
    """
    Discover NFL game events, optionally filtered by date range.

    Args:
        client: Kalshi API client.
        series_ticker: Specific series to query (e.g., "NFL-2024").
        start_date: Start date YYYY-MM-DD (inclusive).
        end_date: End date YYYY-MM-DD (inclusive).

    Returns:
        List of EventInfo objects representing NFL games.
    """
    # Convert date strings to Unix timestamps for filtering
    start_ts = int(datetime.fromisoformat(start_date).timestamp()) if start_date else None
    end_ts = int(datetime.fromisoformat(end_date).replace(hour=23, minute=59).timestamp()) if end_date else None

    # Fetch events
    raw_events = client.get_events(series_ticker=series_ticker, limit=200)

    events = []
    for raw in raw_events:
        try:
            # Extract teams from title if possible
            teams = extract_teams_from_title(raw.get("title", ""))

            event = EventInfo(
                event_ticker=raw["event_ticker"],
                series_ticker=raw.get("series_ticker", ""),
                title=raw.get("title", ""),
                subtitle=raw.get("subtitle"),
                mutually_exclusive=raw.get("mutually_exclusive", True),
                strike_date=raw.get("strike_date"),
                category=raw.get("category"),
                teams=teams,
            )

            # Apply date filter
            if start_ts and event.strike_date and event.strike_date < start_ts:
                continue
            if end_ts and event.strike_date and event.strike_date > end_ts:
                continue

            events.append(event)

        except Exception as e:
            logger.warning(f"Failed to parse event {raw.get('event_ticker')}: {e}")

    logger.info(
        f"Discovered {len(events)} NFL events for series_ticker={series_ticker}, "
        f"start_date={start_date}, end_date={end_date}"
    )
    return events


def extract_teams_from_title(title: str) -> list[str]:
    """
    Extract team names from event title.

    Common formats:
    - "Team A vs Team B"
    - "Team A @ Team B"
    - "Team A vs. Team B"

    Args:
        title: Event title string.

    Returns:
        List of team names (up to 2).
    """
    # Regex to match common separators
    match = re.search(r"(.+?)\s+(?:vs\.?|@|versus)\s+(.+)", title, re.IGNORECASE)
    if match:
        team_a = match.group(1).strip()
        team_b = match.group(2).strip()
        return [team_a, team_b]

    logger.debug(f"Could not extract teams from title: {title}")
    return []


def find_win_market(client: KalshiClient, event_ticker: str) -> Optional[MarketInfo]:
    """
    Find the main WIN market for an NFL event.

    Searches for markets with "win" in the title/subtitle.

    Args:
        client: Kalshi API client.
        event_ticker: Event ticker to search.

    Returns:
        MarketInfo for the WIN market, or None if not found.
    """
    markets = client.get_markets(event_ticker=event_ticker)

    # Look for market with "win" in title or subtitle
    for market in markets:
        title_lower = market.title.lower()
        subtitle_lower = (market.subtitle or "").lower()

        if "win" in title_lower or "win" in subtitle_lower:
            logger.debug(f"Found WIN market: {market.ticker} for {event_ticker}")
            return market

    logger.warning(f"No WIN market found for event {event_ticker}")
    return None


def discover_games_with_markets(
    client: KalshiClient,
    series_ticker: Optional[str] = None,
    start_date: Optional[str] = None,
    end_date: Optional[str] = None,
) -> list[tuple[EventInfo, MarketInfo]]:
    """
    Discover NFL events and their corresponding WIN markets.

    Args:
        client: Kalshi API client.
        series_ticker: Specific series to query. If None, uses KXNFLGAME (individual games).
        start_date: Start date YYYY-MM-DD.
        end_date: End date YYYY-MM-DD.

    Returns:
        List of (EventInfo, MarketInfo) tuples.
    """
    # If no series specified, use the main NFL game series
    if series_ticker is None:
        series_ticker = "KXNFLGAME"  # Main series for individual NFL games
        logger.info(f"No series specified, using default: {series_ticker}")

    # Discover events from the series
    events = discover_nfl_events(client, series_ticker, start_date, end_date)
    logger.info(f"Total events discovered: {len(events)}")

    games_with_markets = []
    for event in events:
        market = find_win_market(client, event.event_ticker)
        if market:
            games_with_markets.append((event, market))
        else:
            logger.debug(f"Skipping event {event.event_ticker} (no WIN market found)")

    logger.info(f"Found {len(games_with_markets)} games with WIN markets")
    return games_with_markets

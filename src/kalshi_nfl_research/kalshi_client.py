"""
Kalshi API client with pagination, rate limiting, and robust error handling.

All endpoints are public and require no authentication.
"""

import logging
import os
import time
from typing import Any, Iterator, Optional
from urllib.parse import urljoin

import requests

from .data_models import Candle, MarketInfo, OrderbookSnapshot, SeriesInfo, Trade

logger = logging.getLogger(__name__)


class KalshiClient:
    """
    HTTP client for Kalshi public API.

    Features:
    - Configurable base URL via environment variable or constructor
    - Automatic pagination with cursor
    - Polite rate limiting
    - Typed response models
    """

    def __init__(
        self,
        base_url: Optional[str] = None,
        rate_limit_sleep_ms: int = 200,
        timeout: int = 30,
    ):
        """
        Initialize Kalshi API client.

        Args:
            base_url: API base URL. Defaults to KALSHI_BASE env var or fallback.
            rate_limit_sleep_ms: Milliseconds to sleep between requests.
            timeout: Request timeout in seconds.
        """
        self.base_url = (
            base_url
            or os.getenv("KALSHI_BASE", "https://api.elections.kalshi.com/trade-api/v2")
        ).rstrip("/")
        self.rate_limit_sleep_ms = rate_limit_sleep_ms
        self.timeout = timeout
        self.session = requests.Session()
        self.session.headers.update({"Accept": "application/json"})
        logger.info(f"Initialized KalshiClient with base_url={self.base_url}")

    def _get(self, endpoint: str, params: Optional[dict[str, Any]] = None) -> dict[str, Any]:
        """
        Execute GET request with rate limiting and error handling.

        Args:
            endpoint: API endpoint path (e.g., "/series").
            params: Query parameters.

        Returns:
            JSON response as dict.

        Raises:
            requests.HTTPError: On HTTP error status.
        """
        url = urljoin(self.base_url + "/", endpoint.lstrip("/"))
        logger.debug(f"GET {url} with params={params}")

        time.sleep(self.rate_limit_sleep_ms / 1000.0)

        try:
            response = self.session.get(url, params=params, timeout=self.timeout)
            response.raise_for_status()
            return response.json()
        except requests.exceptions.HTTPError as e:
            logger.error(f"HTTP error {e.response.status_code} for {url}: {e.response.text}")
            raise
        except requests.exceptions.RequestException as e:
            logger.error(f"Request failed for {url}: {e}")
            raise

    def _paginate(
        self, endpoint: str, params: Optional[dict[str, Any]] = None, data_key: str = "series"
    ) -> Iterator[dict[str, Any]]:
        """
        Paginate through API results using cursor.

        Args:
            endpoint: API endpoint.
            params: Base query parameters.
            data_key: Key in response containing the data list.

        Yields:
            Individual items from paginated results.
        """
        params = params or {}
        cursor = None

        while True:
            if cursor:
                params["cursor"] = cursor

            response = self._get(endpoint, params)
            items = response.get(data_key, [])

            if not items:
                logger.debug(f"No more items for {endpoint}")
                break

            for item in items:
                yield item

            # Check for next cursor
            cursor = response.get("cursor")
            if not cursor:
                logger.debug(f"No cursor found, pagination complete for {endpoint}")
                break

            logger.debug(f"Fetched {len(items)} items, next cursor={cursor[:20]}...")

    # -------------------------------------------------------------------------
    # Series Endpoints
    # -------------------------------------------------------------------------

    def get_series(self, limit: int = 100, with_nested_markets: bool = False) -> list[SeriesInfo]:
        """
        Fetch all series with pagination.

        Args:
            limit: Results per page.
            with_nested_markets: Include nested market data.

        Returns:
            List of SeriesInfo objects.
        """
        params = {"limit": limit, "with_nested_markets": str(with_nested_markets).lower()}
        series_list = []

        for item in self._paginate("/series", params=params, data_key="series"):
            try:
                series_list.append(SeriesInfo(**item))
            except Exception as e:
                logger.warning(f"Failed to parse series {item.get('series_ticker')}: {e}")

        logger.info(f"Fetched {len(series_list)} series")
        return series_list

    # -------------------------------------------------------------------------
    # Events Endpoints
    # -------------------------------------------------------------------------

    def get_events(
        self,
        series_ticker: Optional[str] = None,
        status: Optional[str] = None,
        limit: int = 200,
    ) -> list[dict[str, Any]]:
        """
        Fetch events (games) with optional filtering.

        Args:
            series_ticker: Filter by series (e.g., NFL-2024).
            status: Filter by status (e.g., "settled", "finalized").
            limit: Results per page.

        Returns:
            List of raw event dictionaries.
        """
        params: dict[str, Any] = {"limit": limit}
        if series_ticker:
            params["series_ticker"] = series_ticker
        if status:
            params["status"] = status

        events = list(self._paginate("/events", params=params, data_key="events"))
        logger.info(
            f"Fetched {len(events)} events for series_ticker={series_ticker}, status={status}"
        )
        return events

    # -------------------------------------------------------------------------
    # Markets Endpoints
    # -------------------------------------------------------------------------

    def get_markets(
        self, event_ticker: Optional[str] = None, series_ticker: Optional[str] = None, limit: int = 200
    ) -> list[MarketInfo]:
        """
        Fetch markets with optional filtering.

        Args:
            event_ticker: Filter by event.
            series_ticker: Filter by series.
            limit: Results per page.

        Returns:
            List of MarketInfo objects.
        """
        params: dict[str, Any] = {"limit": limit}
        if event_ticker:
            params["event_ticker"] = event_ticker
        if series_ticker:
            params["series_ticker"] = series_ticker

        markets = []
        for item in self._paginate("/markets", params=params, data_key="markets"):
            try:
                markets.append(MarketInfo(**item))
            except Exception as e:
                logger.warning(f"Failed to parse market {item.get('ticker')}: {e}")

        logger.info(
            f"Fetched {len(markets)} markets for event_ticker={event_ticker}, series_ticker={series_ticker}"
        )
        return markets

    def get_market(self, ticker: str) -> Optional[MarketInfo]:
        """
        Fetch a single market by ticker.

        Args:
            ticker: Market ticker.

        Returns:
            MarketInfo or None if not found.
        """
        try:
            response = self._get(f"/markets/{ticker}")
            market_data = response.get("market")
            if market_data:
                return MarketInfo(**market_data)
        except Exception as e:
            logger.warning(f"Failed to fetch market {ticker}: {e}")
        return None

    def get_orderbook(self, ticker: str) -> Optional[OrderbookSnapshot]:
        """
        Fetch current orderbook snapshot.

        Args:
            ticker: Market ticker.

        Returns:
            OrderbookSnapshot or None.
        """
        try:
            response = self._get(f"/markets/{ticker}/orderbook")
            orderbook = response.get("orderbook", {})

            # Parse yes side
            yes_bids = orderbook.get("yes", [])
            yes_asks = orderbook.get("no", [])  # No side acts as yes asks

            # Handle list format [price_cents, count]
            best_yes_bid = yes_bids[0] if yes_bids else None
            best_yes_ask = yes_asks[0] if yes_asks else None

            # Extract price and count from list format
            yes_bid_price = best_yes_bid[0] if best_yes_bid else None
            yes_ask_price = best_yes_ask[0] if best_yes_ask else None
            yes_bid_count = best_yes_bid[1] if best_yes_bid else None
            yes_ask_count = best_yes_ask[1] if best_yes_ask else None

            return OrderbookSnapshot(
                ticker=ticker,
                ts=int(time.time()),
                yes_bid=yes_bid_price,
                yes_ask=100 - yes_ask_price if yes_ask_price else None,
                yes_bid_size=yes_bid_count,
                yes_ask_size=yes_ask_count,
            )
        except Exception as e:
            logger.warning(f"Failed to fetch orderbook for {ticker}: {e}")
            return None

    # -------------------------------------------------------------------------
    # Trades Endpoints
    # -------------------------------------------------------------------------

    def get_trades(
        self,
        ticker: Optional[str] = None,
        min_ts: Optional[int] = None,
        max_ts: Optional[int] = None,
        limit: int = 500,
    ) -> list[Trade]:
        """
        Fetch trades (tape) with pagination.

        Args:
            ticker: Filter by market ticker.
            min_ts: Minimum timestamp (Unix seconds).
            max_ts: Maximum timestamp (Unix seconds).
            limit: Results per page.

        Returns:
            List of Trade objects sorted by timestamp.
        """
        params: dict[str, Any] = {"limit": limit}
        if ticker:
            params["ticker"] = ticker
        if min_ts:
            params["min_ts"] = min_ts
        if max_ts:
            params["max_ts"] = max_ts

        trades = []
        for item in self._paginate("/markets/trades", params=params, data_key="trades"):
            try:
                trades.append(Trade(**item))
            except Exception as e:
                logger.warning(f"Failed to parse trade: {e}")

        # Sort by timestamp for deterministic ordering
        trades.sort(key=lambda t: t.created_time)

        logger.info(
            f"Fetched {len(trades)} trades for ticker={ticker}, min_ts={min_ts}, max_ts={max_ts}"
        )
        return trades

    # -------------------------------------------------------------------------
    # Candlesticks Endpoints
    # -------------------------------------------------------------------------

    def get_candlesticks(
        self,
        series_ticker: str,
        event_ticker: str,
        interval: str = "1m",
        start_ts: Optional[int] = None,
        end_ts: Optional[int] = None,
    ) -> list[Candle]:
        """
        Fetch candlestick data for an event.

        Args:
            series_ticker: Series ticker (e.g., "NFL-2024").
            event_ticker: Event ticker.
            interval: Candle interval (1m, 5m, 15m, 1h, 1d).
            start_ts: Start timestamp (Unix seconds).
            end_ts: End timestamp (Unix seconds).

        Returns:
            List of Candle objects sorted by timestamp.
        """
        endpoint = f"/series/{series_ticker}/events/{event_ticker}/candlesticks"
        params: dict[str, Any] = {"period_interval": interval}  # API expects 'period_interval' not 'interval'
        if start_ts:
            params["start_ts"] = start_ts
        if end_ts:
            params["end_ts"] = end_ts

        try:
            response = self._get(endpoint, params=params)
            candles_data = response.get("candles", [])

            candles = []
            for item in candles_data:
                try:
                    # Map API response to our model
                    candle = Candle(
                        start_ts=item["start_period_ts"],
                        open_cents=item["open"],
                        high_cents=item["high"],
                        low_cents=item["low"],
                        close_cents=item["close"],
                        volume=item.get("volume", 0),
                    )
                    candles.append(candle)
                except Exception as e:
                    logger.warning(f"Failed to parse candle: {e}")

            candles.sort(key=lambda c: c.start_ts)
            logger.info(
                f"Fetched {len(candles)} candles for {series_ticker}/{event_ticker}, interval={interval}"
            )
            return candles

        except Exception as e:
            logger.warning(
                f"Failed to fetch candlesticks for {series_ticker}/{event_ticker}: {e}"
            )
            return []

    def close(self) -> None:
        """Close the underlying session."""
        self.session.close()
        logger.debug("KalshiClient session closed")

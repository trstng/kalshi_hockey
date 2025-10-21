"""
Data fetching and aggregation for individual games.

Handles pulling candles, trades, and orderbook snapshots for a specific event.
"""

import logging
from typing import Optional

from .data_models import Candle, EventInfo, MarketInfo, OrderbookSnapshot, Trade
from .kalshi_client import KalshiClient

logger = logging.getLogger(__name__)


class GameData:
    """
    Container for all market data for a single game.
    """

    def __init__(
        self,
        event: EventInfo,
        market: MarketInfo,
        candles: list[Candle],
        trades: list[Trade],
        orderbook: Optional[OrderbookSnapshot] = None,
    ):
        self.event = event
        self.market = market
        self.candles = candles
        self.trades = trades
        self.orderbook = orderbook

    def __repr__(self) -> str:
        return (
            f"GameData(event={self.event.event_ticker}, market={self.market.ticker}, "
            f"candles={len(self.candles)}, trades={len(self.trades)})"
        )


def fetch_game_data(
    client: KalshiClient,
    event: EventInfo,
    market: MarketInfo,
    pregame_window_sec: int = 900,
    first_half_sec: int = 1800,
    candle_interval: str = "1m",
    fetch_orderbook: bool = False,
) -> Optional[GameData]:
    """
    Fetch all relevant data for a single game.

    Args:
        client: Kalshi API client.
        event: Event information.
        market: Market information.
        pregame_window_sec: Seconds before kickoff to include.
        first_half_sec: Duration of first half (seconds).
        candle_interval: Candle interval (e.g., "1m").
        fetch_orderbook: Whether to fetch current orderbook snapshot.

    Returns:
        GameData object, or None if critical data is missing.
    """
    if not event.strike_date:
        logger.warning(f"Event {event.event_ticker} has no strike_date; skipping")
        return None

    kickoff_ts = event.strike_date
    start_ts = kickoff_ts - pregame_window_sec
    end_ts = kickoff_ts + first_half_sec

    logger.info(
        f"Fetching data for {event.event_ticker} ({market.ticker}): "
        f"kickoff={kickoff_ts}, window=[{start_ts}, {end_ts}]"
    )

    # Fetch candlesticks
    candles = client.get_candlesticks(
        series_ticker=event.series_ticker,
        event_ticker=event.event_ticker,
        interval=candle_interval,
        start_ts=start_ts,
        end_ts=end_ts,
    )

    # Fetch trades
    trades = client.get_trades(
        ticker=market.ticker,
        min_ts=start_ts,
        max_ts=end_ts,
    )

    # Optionally fetch orderbook
    orderbook = None
    if fetch_orderbook:
        orderbook = client.get_orderbook(market.ticker)

    # Validate we have usable data
    if not candles and not trades:
        logger.warning(f"No candles or trades found for {event.event_ticker}; skipping")
        return None

    logger.info(
        f"Fetched {len(candles)} candles and {len(trades)} trades for {event.event_ticker}"
    )

    return GameData(
        event=event,
        market=market,
        candles=candles,
        trades=trades,
        orderbook=orderbook,
    )


def compute_pregame_probability(
    game_data: GameData,
    kickoff_ts: int,
    pregame_window_sec: int = 900,
) -> Optional[float]:
    """
    Compute pregame implied probability for the favorite.

    Uses last candle close before kickoff, or VWAP from trades if candles unavailable.

    Args:
        game_data: Game data container.
        kickoff_ts: Kickoff timestamp.
        pregame_window_sec: Window before kickoff to consider.

    Returns:
        Pregame implied probability (0-1), or None if insufficient data.
    """
    pregame_start = kickoff_ts - pregame_window_sec

    # Try candles first
    pregame_candles = [c for c in game_data.candles if pregame_start <= c.start_ts < kickoff_ts]
    if pregame_candles:
        last_candle = pregame_candles[-1]
        prob = last_candle.close_cents / 100.0
        logger.debug(
            f"Pregame prob from candle: {prob:.3f} (close={last_candle.close_cents} cents)"
        )
        return prob

    # Fallback to trades VWAP
    pregame_trades = [t for t in game_data.trades if pregame_start <= t.created_time < kickoff_ts]
    if pregame_trades:
        total_volume = sum(t.count for t in pregame_trades)
        if total_volume > 0:
            vwap = sum(t.yes_price * t.count for t in pregame_trades) / total_volume
            prob = vwap / 100.0
            logger.debug(f"Pregame prob from trades VWAP: {prob:.3f} (n={len(pregame_trades)})")
            return prob

    logger.warning(f"No pregame data available for {game_data.event.event_ticker}")
    return None


def detect_trigger_time(
    game_data: GameData,
    kickoff_ts: int,
    halftime_ts: int,
    trigger_threshold: float = 0.50,
) -> Optional[int]:
    """
    Detect the first time implied probability crosses below the trigger threshold during first half.

    Uses candles if available, otherwise falls back to trades.

    Args:
        game_data: Game data container.
        kickoff_ts: Kickoff timestamp.
        halftime_ts: Halftime timestamp.
        trigger_threshold: Probability threshold (e.g., 0.50).

    Returns:
        Unix timestamp of trigger, or None if no cross.
    """
    # Try candles first for quick detection
    first_half_candles = [
        c for c in game_data.candles if kickoff_ts <= c.start_ts < halftime_ts
    ]

    if first_half_candles:
        for candle in first_half_candles:
            prob = candle.low_cents / 100.0  # Use low to be conservative
            if prob < trigger_threshold:
                logger.debug(
                    f"Trigger detected (candles) at {candle.start_ts} (prob={prob:.3f}, threshold={trigger_threshold})"
                )
                return candle.start_ts
        logger.debug(f"No trigger found in candles for {game_data.event.event_ticker}")
        return None

    # Fallback to trades if no candles available
    first_half_trades = [
        t for t in game_data.trades if kickoff_ts <= t.created_time < halftime_ts
    ]

    if not first_half_trades:
        logger.debug(f"No first-half trades available for {game_data.event.event_ticker}")
        return None

    for trade in first_half_trades:
        prob = trade.yes_price / 100.0
        if prob < trigger_threshold:
            logger.debug(
                f"Trigger detected (trades) at {trade.created_time} (prob={prob:.3f}, threshold={trigger_threshold})"
            )
            return trade.created_time

    logger.debug(f"No trigger found in trades for {game_data.event.event_ticker}")
    return None


def find_fill_trade(
    game_data: GameData,
    trigger_ts: int,
    grace_sec: int = 15,
) -> Optional[Trade]:
    """
    Find the first trade at or after trigger time within grace window.

    Args:
        game_data: Game data container.
        trigger_ts: Trigger timestamp.
        grace_sec: Grace period to find a trade.

    Returns:
        First valid Trade, or None if unfillable.
    """
    grace_end = trigger_ts + grace_sec
    candidate_trades = [
        t for t in game_data.trades if trigger_ts <= t.created_time <= grace_end
    ]

    if candidate_trades:
        fill_trade = candidate_trades[0]
        logger.debug(
            f"Fill trade found at {fill_trade.created_time} (price={fill_trade.yes_price} cents)"
        )
        return fill_trade

    logger.debug(f"No fill trade within grace window for trigger_ts={trigger_ts}")
    return None

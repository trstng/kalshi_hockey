"""
NHL Live Trading Bot - Mean Reversion Strategy

Monitors NHL game markets and executes mean reversion trades:
- Identifies favorites (≥57% at open)
- Waits for dips to ≤40%
- Enters with tiered position sizing
- Exits at target profits or holds to outcome

Polling schedule: 6hr, 3hr, 30min before puck drop
"""

import logging
import os
import time
import requests
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional
from dataclasses import dataclass, asdict
from dotenv import load_dotenv

import sys
sys.path.insert(0, str(Path(__file__).parent / "src"))

from kalshi_nfl_research.kalshi_client import KalshiClient
from kalshi_nfl_research.trading_client import KalshiTradingClient
from supabase_logger import SupabaseLogger
from nhl_strategy import (
    should_enter_position,
    get_position_size,
    get_exit_targets,
    should_exit_position,
    get_strategy_summary
)

# Load environment variables
load_dotenv()

# Create logs directory
Path("logs").mkdir(exist_ok=True)

# Setup logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler('logs/nhl_trading.log'),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger(__name__)


@dataclass
class NHLGame:
    """Represents an NHL game being monitored."""
    game_id: str
    date: str
    start_time_utc: str
    away_team: str
    home_team: str

    # Market tracking
    away_ticker: Optional[str] = None
    home_ticker: Optional[str] = None

    # Opening prices (first seen at 6hr checkpoint)
    away_opening_price: Optional[float] = None
    home_opening_price: Optional[float] = None

    # Favorite tracking
    favorite_team: Optional[str] = None
    favorite_ticker: Optional[str] = None
    favorite_opening_price: Optional[float] = None

    # Volume tracking (at 30min checkpoint)
    volume_30m: Optional[int] = None
    is_qualified: bool = False  # ≥57% favorite AND ≥50k volume

    # Checkpoints (Unix timestamps)
    poll_6h: Optional[int] = None
    poll_3h: Optional[int] = None
    poll_30m: Optional[int] = None

    # Game state
    game_started: bool = False
    monitoring_window_end: Optional[int] = None  # Puck drop + 90 minutes

    def get_puck_drop_timestamp(self) -> int:
        """Convert start_time_utc to Unix timestamp."""
        dt = datetime.fromisoformat(self.start_time_utc.replace('Z', '+00:00'))
        return int(dt.timestamp())

    def is_in_monitoring_window(self) -> bool:
        """Check if we're in the 90-minute monitoring window."""
        if not self.game_started or not self.monitoring_window_end:
            return False
        return int(time.time()) < self.monitoring_window_end


@dataclass
class Position:
    """Represents an open trading position."""
    ticker: str
    game_id: str
    entry_price: float
    entry_time: int  # Unix timestamp
    position_size: float  # Dollar value
    num_contracts: int
    exit_min: float
    exit_max: float
    order_id: Optional[str] = None

    def time_in_position_minutes(self) -> int:
        """Calculate how long we've been in this position."""
        return int((time.time() - self.entry_time) / 60)


class NHLTradingBot:
    """Main trading bot for NHL mean reversion strategy."""

    def __init__(self):
        """Initialize the trading bot."""
        # Kalshi clients
        self.client = KalshiClient()

        # Initialize trading client with API credentials from environment
        api_key_id = os.getenv('KALSHI_API_KEY_ID')
        private_key = os.getenv('KALSHI_PRIVATE_KEY')

        if not api_key_id or not private_key:
            logger.error("KALSHI_API_KEY_ID and KALSHI_PRIVATE_KEY must be set in environment")
            raise ValueError("Missing Kalshi API credentials")

        self.trading_client = KalshiTradingClient(
            api_key=api_key_id,
            api_secret=private_key
        )

        # Supabase logger
        try:
            self.logger = SupabaseLogger()
        except Exception as e:
            logger.warning(f"Supabase logger initialization failed: {e}")
            self.logger = None

        # Configuration from environment
        self.bankroll = float(os.getenv('TRADING_BANKROLL', 1000))
        self.max_exposure_pct = float(os.getenv('MAX_EXPOSURE_PCT', 0.5))
        self.dry_run = os.getenv('DRY_RUN', 'true').lower() == 'true'
        self.position_multiplier = float(os.getenv('POSITION_SIZE_MULTIPLIER', 1.0))

        # State
        self.games: Dict[str, NHLGame] = {}
        self.positions: Dict[str, Position] = {}
        self.kalshi_markets_cache: List[dict] = []  # Cache all NHL markets at startup

        logger.info("="*80)
        logger.info("NHL TRADING BOT INITIALIZED")
        logger.info("="*80)
        logger.info(f"Bankroll: ${self.bankroll:,.2f}")
        logger.info(f"Max Exposure: {self.max_exposure_pct:.0%}")
        logger.info(f"Dry Run: {self.dry_run}")
        logger.info(f"Position Multiplier: {self.position_multiplier}x")
        logger.info(get_strategy_summary())

        # Log initial bankroll to Supabase
        if self.logger:
            self.logger.log_bankroll_change(
                timestamp=int(time.time()),
                new_amount=self.bankroll,
                change=0,
                description="Bot initialized"
            )

    def fetch_nhl_schedule(self, date: str) -> List[dict]:
        """
        Fetch NHL schedule for a given date.

        Args:
            date: Date in YYYY-MM-DD format

        Returns:
            List of games
        """
        url = f"https://api-web.nhle.com/v1/schedule/{date}"
        try:
            response = requests.get(url, timeout=10)
            response.raise_for_status()
            data = response.json()

            games = []
            for week in data.get('gameWeek', []):
                for day in week.get('games', []):
                    games.append(day)

            return games
        except Exception as e:
            logger.error(f"Failed to fetch NHL schedule: {e}")
            return []

    def load_kalshi_markets_cache(self):
        """Fetch all NHL markets once at startup and cache them."""
        try:
            logger.info("📥 Fetching all Kalshi NHL markets (one-time)...")
            self.kalshi_markets_cache = self.client.get_markets(
                series_ticker='KXNHLGAME',
                limit=500
            )
            logger.info(f"✓ Cached {len(self.kalshi_markets_cache)} NHL markets")
        except Exception as e:
            logger.error(f"Failed to cache Kalshi markets: {e}")
            self.kalshi_markets_cache = []

    def find_market_for_team(self, game_date: str, team_abbrev: str, opponent_abbrev: str = None) -> Optional[dict]:
        """
        Find the Kalshi market for a specific team from cached markets.

        Args:
            game_date: Date in YYYY-MM-DD format
            team_abbrev: Team abbreviation (e.g., 'TOR', 'VGK')
            opponent_abbrev: Opponent team abbreviation (optional, for verification)

        Returns:
            Market dict or None
        """
        try:
            # Convert date format: 2025-10-21 -> 25OCT21
            from datetime import datetime
            dt = datetime.strptime(game_date, '%Y-%m-%d')
            date_str = dt.strftime('%y%b%d').upper()  # e.g., "25OCT21"

            # Search cached markets (NO API CALL)
            # Ticker format: KXNHLGAME-25OCT21EDMOTT-EDM
            for market in self.kalshi_markets_cache:
                ticker = market.ticker
                if date_str in ticker and ticker.endswith(f'-{team_abbrev}'):
                    # If opponent is specified, verify it's in the matchup
                    if opponent_abbrev:
                        # Extract matchup from ticker (e.g., "EDMOTT" from "KXNHLGAME-25OCT21EDMOTT-EDM")
                        parts = ticker.split('-')
                        if len(parts) >= 2:
                            matchup = parts[1][len(date_str):]  # Remove date prefix
                            # Check if opponent abbreviation is in the matchup
                            if opponent_abbrev.upper() not in matchup.upper():
                                continue  # Skip this market, opponent doesn't match

                    return market

            logger.debug(f"No cached market found for {team_abbrev} vs {opponent_abbrev} on {date_str}")
            return None
        except Exception as e:
            logger.error(f"Failed to find market for {team_abbrev}: {e}")
            return None

    def load_todays_games(self):
        """Load today's and yesterday's NHL games (to catch early UTC games)."""
        from datetime import timedelta

        today = datetime.now()
        yesterday = today - timedelta(days=1)

        # Load both today and yesterday to catch games that span midnight UTC
        today_str = today.strftime('%Y-%m-%d')
        yesterday_str = yesterday.strftime('%Y-%m-%d')

        games_today = self.fetch_nhl_schedule(today_str)
        games_yesterday = self.fetch_nhl_schedule(yesterday_str)

        # Combine and deduplicate
        all_games = games_yesterday + games_today
        seen_ids = set()
        games = []
        for game in all_games:
            game_id = str(game.get('id'))
            if game_id not in seen_ids:
                seen_ids.add(game_id)
                games.append(game)

        logger.info(f"\nFetched {len(games)} NHL games (yesterday + today)")

        for game_data in games:
            game_id = str(game_data.get('id'))
            start_time = game_data.get('startTimeUTC')
            away_abbrev = game_data.get('awayTeam', {}).get('abbrev')
            home_abbrev = game_data.get('homeTeam', {}).get('abbrev')

            if not all([game_id, start_time, away_abbrev, home_abbrev]):
                continue

            # Extract the actual game date from start_time_utc (e.g., "2025-10-23T00:30:00Z" -> "2025-10-23")
            game_date = start_time.split('T')[0] if 'T' in start_time else today_str

            game = NHLGame(
                game_id=game_id,
                date=game_date,  # Use actual game date from start_time_utc
                start_time_utc=start_time,
                away_team=away_abbrev,
                home_team=home_abbrev
            )

            # Calculate poll times
            puck_drop = game.get_puck_drop_timestamp()

            # Skip games that have already started (no point searching for markets)
            now = int(time.time())
            if puck_drop < now - 3600:  # Game started more than 1 hour ago
                logger.debug(f"  Skipping {away_abbrev} @ {home_abbrev} - already started")
                continue

            game.poll_6h = puck_drop - (6 * 3600)
            game.poll_3h = puck_drop - (3 * 3600)
            game.poll_30m = puck_drop - (30 * 60)

            self.games[game_id] = game

            logger.info(f"  {away_abbrev} @ {home_abbrev} - Puck drop: {start_time}")

        # Now find Kalshi markets and log games to Supabase
        if self.logger:
            self._log_games_to_supabase()

    def _log_games_to_supabase(self):
        """Find Kalshi markets for loaded games and log them to Supabase."""
        logger.info("\n🔍 Finding markets and logging games to Supabase...")

        for game in self.games.values():
            # Try to find markets for both teams (pass opponent to verify matchup)
            away_market = self.find_market_for_team(game.date, game.away_team, game.home_team)
            home_market = self.find_market_for_team(game.date, game.home_team, game.away_team)

            if not away_market and not home_market:
                logger.debug(f"  No markets found for {game.away_team} @ {game.home_team}")
                continue

            # Use the market we found (prefer home team market for consistency)
            market = home_market if home_market else away_market

            # Store market tickers
            if away_market:
                game.away_ticker = away_market.ticker
            if home_market:
                game.home_ticker = home_market.ticker

            # Determine favorite and get current odds
            away_price = away_market.last_price if away_market else 0
            home_price = home_market.last_price if home_market else 0

            if away_price > home_price:
                favorite_ticker = away_market.ticker
                favorite_price = away_price
                game.favorite_team = game.away_team
                game.favorite_ticker = away_market.ticker
                game.favorite_opening_price = away_price
            else:
                favorite_ticker = home_market.ticker
                favorite_price = home_price
                game.favorite_team = game.home_team
                game.favorite_ticker = home_market.ticker
                game.favorite_opening_price = home_price

            # Log to Supabase with current odds
            try:
                now = int(time.time())
                puck_drop = game.get_puck_drop_timestamp()
                time_until_game = (puck_drop - now) / 3600  # hours until game

                game_log_data = {
                    'market_ticker': favorite_ticker,
                    'event_ticker': market.event_ticker if hasattr(market, 'event_ticker') else None,
                    'market_title': market.title if hasattr(market, 'title') else f"{game.away_team} @ {game.home_team}",
                    'yes_subtitle': market.yes_sub_title if hasattr(market, 'yes_sub_title') else None,
                    'kickoff_ts': puck_drop,
                    'status': 'monitoring'
                }

                # Add current odds to the appropriate checkpoint field based on time until game
                current_odds = favorite_price / 100
                if time_until_game >= 5.5:  # More than 5.5 hours away - log as 6h checkpoint
                    game_log_data['odds_6h'] = current_odds
                    game_log_data['checkpoint_6h_ts'] = now
                    logger.info(f"  ✓ Logged: {game.away_team} @ {game.home_team} ({favorite_ticker}) - {game.favorite_team} @ {favorite_price}% (6h checkpoint)")
                elif time_until_game >= 2.5:  # 2.5-5.5 hours away - log as 3h checkpoint
                    game_log_data['odds_3h'] = current_odds
                    game_log_data['checkpoint_3h_ts'] = now
                    logger.info(f"  ✓ Logged: {game.away_team} @ {game.home_team} ({favorite_ticker}) - {game.favorite_team} @ {favorite_price}% (3h checkpoint)")
                elif time_until_game >= 0:  # 0-2.5 hours away - log as 30m checkpoint
                    game_log_data['odds_30m'] = current_odds
                    game_log_data['checkpoint_30m_ts'] = now
                    logger.info(f"  ✓ Logged: {game.away_team} @ {game.home_team} ({favorite_ticker}) - {game.favorite_team} @ {favorite_price}% (30m checkpoint)")
                else:
                    # Game already started
                    logger.info(f"  ✓ Logged: {game.away_team} @ {game.home_team} ({favorite_ticker}) - {game.favorite_team} @ {favorite_price}% (already started)")

                self.logger.log_game(game_log_data)

                # Immediately check eligibility if we have 30m checkpoint data
                if 'odds_30m' in game_log_data and favorite_price >= 57.0:
                    game.is_qualified = True
                    self.logger.update_game_eligibility(
                        market_ticker=favorite_ticker,
                        is_eligible=True
                    )
                    logger.info(f"     ✅ QUALIFIED for trading (favorite ≥57%)")

                    # If game hasn't started yet, place limit orders now
                    if time_until_game > 0:
                        self.place_tiered_limit_orders(game)
                elif 'odds_30m' in game_log_data:
                    self.logger.update_game_eligibility(
                        market_ticker=favorite_ticker,
                        is_eligible=False
                    )
                    logger.info(f"     ❌ NOT QUALIFIED (favorite only {favorite_price}%, need ≥57%)")

            except Exception as e:
                logger.error(f"  ✗ Failed to log game {game.away_team} @ {game.home_team}: {e}")

    def poll_game_markets(self, game: NHLGame, checkpoint: str):
        """
        Poll markets for a game at a specific checkpoint.

        Args:
            game: NHL game to poll
            checkpoint: '6h', '3h', or '30m'
        """

        # Find markets for both teams (game.date is already a string in YYYY-MM-DD format)
        away_market = self.find_market_for_team(game.date, game.away_team, game.home_team)
        home_market = self.find_market_for_team(game.date, game.home_team, game.away_team)

        if not away_market or not home_market:
            logger.warning(f"Markets not found for {game.away_team} @ {game.home_team}")
            return

        # Update game with market info
        game.away_ticker = away_market.ticker
        game.home_ticker = home_market.ticker

        # At 6hr checkpoint, record opening prices
        if checkpoint == '6h':
            game.away_opening_price = away_market.last_price
            game.home_opening_price = home_market.last_price

            # Determine favorite
            if away_market.last_price > home_market.last_price:
                game.favorite_team = game.away_team
                game.favorite_ticker = away_market.ticker
                game.favorite_opening_price = away_market.last_price
            else:
                game.favorite_team = game.home_team
                game.favorite_ticker = home_market.ticker
                game.favorite_opening_price = home_market.last_price

            logger.info(f"\n[{checkpoint.upper()}] {game.away_team} @ {game.home_team}")
            logger.info(f"  {game.away_team}: {away_market.last_price}%")
            logger.info(f"  {game.home_team}: {home_market.last_price}%")
            logger.info(f"  Favorite: {game.favorite_team} ({game.favorite_opening_price}%)")

            # Log checkpoint to Supabase
            if self.logger and game.favorite_ticker:
                self.logger.update_game_checkpoint(
                    market_ticker=game.favorite_ticker,
                    field_name='odds_6h',
                    odds=game.favorite_opening_price / 100,
                    timestamp=int(time.time())
                )

        # At 3hr checkpoint, record odds
        elif checkpoint == '3h':
            if game.favorite_ticker:
                # Get current market price for favorite
                fav_market = self.client.get_market(game.favorite_ticker)
                if fav_market:
                    logger.info(f"\n[{checkpoint.upper()}] {game.away_team} @ {game.home_team}")
                    logger.info(f"  Favorite {game.favorite_team}: {fav_market.last_price}%")

                    # Log checkpoint to Supabase
                    if self.logger:
                        self.logger.update_game_checkpoint(
                            market_ticker=game.favorite_ticker,
                            field_name='odds_3h',
                            odds=fav_market.last_price / 100,
                            timestamp=int(time.time())
                        )

        # At 30min checkpoint, check qualification and place limit orders
        elif checkpoint == '30m':
            if game.favorite_ticker and game.favorite_opening_price:
                # Get current market price
                fav_market = self.client.get_market(game.favorite_ticker)
                current_price = fav_market.last_price if fav_market else game.favorite_opening_price

                # Log 30m checkpoint to Supabase
                if self.logger:
                    self.logger.update_game_checkpoint(
                        market_ticker=game.favorite_ticker,
                        field_name='odds_30m',
                        odds=current_price / 100,
                        timestamp=int(time.time())
                    )

                # Check if favorite still qualifies (≥57%)
                if game.favorite_opening_price >= 57.0:
                    # TODO: Add volume check here when API access is available
                    # For now, assume qualified if ≥57%
                    game.is_qualified = True

                    logger.info(f"\n[{checkpoint.upper()}] {game.away_team} @ {game.home_team}")
                    logger.info(f"  ✅ QUALIFIED: Favorite {game.favorite_team} @ {game.favorite_opening_price}%")
                    logger.info(f"  📊 Placing limit orders for in-game dips <45¢")

                    # Update eligibility in Supabase
                    if self.logger:
                        self.logger.update_game_eligibility(
                            market_ticker=game.favorite_ticker,
                            is_eligible=True
                        )

                    # Place tiered limit orders at different price levels
                    self.place_tiered_limit_orders(game)
                else:
                    logger.info(f"\n[{checkpoint.upper()}] {game.away_team} @ {game.home_team}")
                    logger.info(f"  ❌ NOT QUALIFIED: Favorite only {game.favorite_opening_price}% (need ≥57%)")

                    # Update eligibility in Supabase
                    if self.logger:
                        self.logger.update_game_eligibility(
                            market_ticker=game.favorite_ticker,
                            is_eligible=False
                        )

        # Check exits on open positions
        self.check_exit_signals(game)

    def place_tiered_limit_orders(self, game: NHLGame):
        """
        Place tiered limit orders at 30-minute checkpoint.
        Orders will fill if price drops during the game.
        """
        if not game.favorite_ticker:
            return

        # Check if already have orders placed for this game
        if game.favorite_ticker in self.positions:
            logger.info(f"  ⚠️  Already have position/orders for {game.favorite_ticker}")
            return

        base_size = self.bankroll * 0.1  # Base 10% of bankroll per trade

        # Define price tiers for limit orders (below 45¢)
        # Tier 1: 40-44¢ (0.5x sizing)
        # Tier 2: 36-39¢ (1.0x sizing)
        # Tier 3: ≤35¢ (1.5x sizing)
        tiers = [
            {'price': 42, 'label': 'shallow', 'multiplier': 0.5},
            {'price': 38, 'label': 'medium', 'multiplier': 1.0},
            {'price': 34, 'label': 'deep', 'multiplier': 1.5},
        ]

        for tier in tiers:
            price = tier['price']
            position_value = base_size * tier['multiplier'] * self.position_multiplier

            # Check exposure limits
            current_exposure = sum(p.position_size for p in self.positions.values())
            max_exposure = self.bankroll * self.max_exposure_pct

            if current_exposure + position_value > max_exposure:
                logger.warning(f"  ⚠️  Skipping {tier['label']} tier - would exceed max exposure")
                continue

            num_contracts = int(position_value / (price / 100))
            exit_min, exit_max = get_exit_targets(price)

            logger.info(f"  📝 {tier['label'].capitalize()} tier: {num_contracts} contracts @ {price}¢ (exit {exit_min}-{exit_max}¢)")

            # Place limit order
            order_id = None
            if not self.dry_run:
                try:
                    order = self.trading_client.place_order(
                        ticker=game.favorite_ticker,
                        action='buy',
                        side='yes',
                        count=num_contracts,
                        price=price,
                        order_type='limit'
                    )
                    order_id = order.order_id
                    logger.info(f"     ✅ Limit order placed: {order_id}")

                    # Log order to Supabase
                    if self.logger:
                        self.logger.log_order(
                            market_ticker=game.favorite_ticker,
                            order_id=order_id,
                            price=price,
                            size=num_contracts,
                            side='buy'
                        )

                except Exception as e:
                    logger.error(f"     ❌ Order failed: {e}")
                    continue
            else:
                logger.info(f"     [DRY RUN] Would place limit order")

            # Create position (will be filled when price reaches level)
            position = Position(
                ticker=game.favorite_ticker,
                game_id=game.game_id,
                entry_price=price,
                entry_time=int(time.time()),
                position_size=position_value,
                num_contracts=num_contracts,
                exit_min=exit_min,
                exit_max=exit_max,
                order_id=order_id
            )

            # Use unique key for each tier
            position_key = f"{game.favorite_ticker}_{price}"
            self.positions[position_key] = position

    def monitor_order_fills(self):
        """
        Monitor pending orders for fills and update positions accordingly.
        This should be called regularly during the trading window.
        """
        if self.dry_run or not self.logger:
            return

        # Get all pending orders from Supabase
        for position_key, position in list(self.positions.items()):
            if not position.order_id:
                continue

            try:
                # Check order status with Kalshi
                order_status = self.trading_client.get_order_status(position.order_id)

                # If order returns None, it's been executed/cancelled
                if order_status is None:
                    # Check fills to see if it executed
                    fills = self.trading_client.get_fills(order_id=position.order_id)

                    if fills:
                        # Order filled! Update our records
                        total_filled = sum(fill['count'] for fill in fills)
                        avg_fill_price = sum(fill['yes_price'] or fill['no_price'] or 0 for fill in fills) / len(fills) if fills else position.entry_price

                        logger.info(f"\n✅ ORDER FILLED: {position.order_id}")
                        logger.info(f"   {total_filled} contracts @ {avg_fill_price}¢")

                        # Update order status in Supabase
                        self.logger.update_order_status(
                            order_id=position.order_id,
                            status='filled',
                            filled_size=total_filled
                        )

                        # Log position entry to Supabase
                        position_data = {
                            'market_ticker': position.ticker,
                            'entry_price': int(avg_fill_price),
                            'size': total_filled,
                            'entry_time': int(time.time()),
                            'order_id': position.order_id,
                            'status': 'open'
                        }
                        self.logger.log_position_entry(position_data)

                        # Update position with actual fill data
                        position.entry_price = avg_fill_price
                        position.num_contracts = total_filled

                        # Log bankroll change
                        cost = (avg_fill_price / 100) * total_filled
                        self.bankroll -= cost
                        self.logger.log_bankroll_change(
                            timestamp=int(time.time()),
                            new_amount=self.bankroll,
                            change=-cost,
                            description=f"Opened position: {total_filled} contracts @ {avg_fill_price}¢"
                        )

                    else:
                        # Order was cancelled
                        logger.info(f"⚠️  Order {position.order_id} cancelled/expired")
                        self.logger.update_order_status(
                            order_id=position.order_id,
                            status='cancelled'
                        )
                        # Remove from tracking
                        del self.positions[position_key]

                elif order_status and 'order' in order_status:
                    order = order_status['order']
                    filled_count = order.get('filled_count', 0)

                    # Check for partial fills
                    if filled_count > 0 and filled_count < order.get('count', 0):
                        logger.info(f"📊 Partial fill: {position.order_id} - {filled_count}/{order.get('count')} filled")
                        self.logger.update_order_status(
                            order_id=position.order_id,
                            status='partially_filled',
                            filled_size=filled_count
                        )

            except Exception as e:
                logger.error(f"Error monitoring order {position.order_id}: {e}")

    def check_exit_signals(self, game: NHLGame):
        """Check if we should exit any positions for this game."""
        for ticker in [game.away_ticker, game.home_ticker]:
            if not ticker or ticker not in self.positions:
                continue

            position = self.positions[ticker]

            # Get current market price
            market = self.client.get_market(ticker)
            if not market:
                continue

            current_price = market.last_price
            time_in_position = position.time_in_position_minutes()

            # Check exit strategy
            should_exit, reason = should_exit_position(
                position.entry_price,
                current_price,
                time_in_position
            )

            if should_exit:
                pnl = (current_price - position.entry_price) * position.num_contracts / 100

                logger.info(f"\n🚪 EXIT SIGNAL: {ticker}")
                logger.info(f"  Entry: {position.entry_price}¢ → Current: {current_price}¢")
                logger.info(f"  P&L: ${pnl:.2f}")
                logger.info(f"  Reason: {reason}")

                # Place exit order
                if not self.dry_run and position.order_id:
                    try:
                        exit_order = self.trading_client.place_order(
                            ticker=ticker,
                            action='sell',
                            side='yes',
                            count=position.num_contracts,
                            price=int(current_price),
                            order_type='limit'
                        )
                        logger.info(f"  ✅ Exit order placed: {exit_order.order_id}")

                        # Log exit order to Supabase
                        if self.logger:
                            self.logger.log_order(
                                market_ticker=ticker,
                                order_id=exit_order.order_id,
                                price=int(current_price),
                                size=position.num_contracts,
                                side='sell'
                            )

                            # Update position as closed in Supabase
                            self.logger.log_position_exit(
                                market_ticker=ticker,
                                exit_price=int(current_price),
                                exit_time=int(time.time()),
                                pnl=pnl
                            )

                            # Log bankroll change
                            proceeds = (current_price / 100) * position.num_contracts
                            self.bankroll += proceeds
                            self.logger.log_bankroll_change(
                                timestamp=int(time.time()),
                                new_amount=self.bankroll,
                                change=proceeds,
                                description=f"Closed position: {position.num_contracts} contracts @ {current_price}¢ (P&L: ${pnl:.2f})"
                            )

                    except Exception as e:
                        logger.error(f"  ❌ Exit order failed: {e}")
                        return  # Don't remove position if exit failed
                else:
                    logger.info(f"  [DRY RUN] Would exit position")

                # Remove position from tracking
                del self.positions[ticker]

    def force_close_positions(self, game: NHLGame):
        """Force close all positions for a game at 90-minute window close."""
        positions_to_close = []

        # Find all positions for this game
        for position_key, position in list(self.positions.items()):
            if position.game_id == game.game_id:
                positions_to_close.append((position_key, position))

        if not positions_to_close:
            logger.info(f"  No open positions to close")
            return

        for position_key, position in positions_to_close:
            # Get current market price
            market = self.client.get_market(position.ticker)
            if not market:
                logger.warning(f"  ⚠️  Could not fetch market for {position.ticker}")
                continue

            current_price = market.last_price
            pnl = (current_price - position.entry_price) * position.num_contracts / 100

            logger.info(f"\n  📤 FORCE CLOSING: {position.ticker}")
            logger.info(f"     Entry: {position.entry_price}¢ → Current: {current_price}¢")
            logger.info(f"     P&L: ${pnl:.2f}")

            # Place exit order
            if not self.dry_run and position.order_id:
                try:
                    exit_order = self.trading_client.place_order(
                        ticker=position.ticker,
                        action='sell',
                        side='yes',
                        count=position.num_contracts,
                        price=int(current_price),
                        order_type='limit'
                    )
                    logger.info(f"     ✅ Exit order placed: {exit_order.order_id}")

                    # Log exit order to Supabase
                    if self.logger:
                        self.logger.log_order(
                            market_ticker=position.ticker,
                            order_id=exit_order.order_id,
                            price=int(current_price),
                            size=position.num_contracts,
                            side='sell'
                        )

                        # Update position as closed in Supabase
                        self.logger.log_position_exit(
                            market_ticker=position.ticker,
                            exit_price=int(current_price),
                            exit_time=int(time.time()),
                            pnl=pnl
                        )

                        # Log bankroll change
                        proceeds = (current_price / 100) * position.num_contracts
                        self.bankroll += proceeds
                        self.logger.log_bankroll_change(
                            timestamp=int(time.time()),
                            new_amount=self.bankroll,
                            change=proceeds,
                            description=f"Force closed position: {position.num_contracts} contracts @ {current_price}¢ (P&L: ${pnl:.2f})"
                        )

                except Exception as e:
                    logger.error(f"     ❌ Exit order failed: {e}")
            else:
                logger.info(f"     [DRY RUN] Would exit position")

            # Remove position
            del self.positions[position_key]

    def run_polling_cycle(self):
        """Run one polling cycle across all games."""
        now = int(time.time())

        # Monitor order fills periodically (even if game hasn't started)
        if not self.dry_run:
            self.monitor_order_fills()

        for game in self.games.values():
            puck_drop = game.get_puck_drop_timestamp()

            # Check if it's time for 6hr poll
            if game.poll_6h and abs(now - game.poll_6h) < 300:  # Within 5min
                self.poll_game_markets(game, '6h')
                game.poll_6h = None  # Mark as done

            # Check for 3hr poll
            elif game.poll_3h and abs(now - game.poll_3h) < 300:
                self.poll_game_markets(game, '3h')
                game.poll_3h = None

            # Check for 30min poll
            elif game.poll_30m and abs(now - game.poll_30m) < 300:
                self.poll_game_markets(game, '30m')
                game.poll_30m = None

            # Check if game has started (puck drop)
            if not game.game_started and now >= puck_drop:
                game.game_started = True
                game.monitoring_window_end = puck_drop + (90 * 60)  # 90 minutes
                logger.info(f"\n🏒 PUCK DROP: {game.away_team} @ {game.home_team}")
                logger.info(f"  📊 Monitoring window: 90 minutes (until {datetime.fromtimestamp(game.monitoring_window_end).strftime('%H:%M')})")

            # Monitor positions during the 90-minute window
            if game.game_started and game.is_qualified:
                if game.is_in_monitoring_window():
                    # Monitor order fills
                    self.monitor_order_fills()

                    # Check exits for this game's positions
                    self.check_exit_signals(game)
                elif game.monitoring_window_end and now >= game.monitoring_window_end:
                    # Force close any remaining positions at window close
                    logger.info(f"\n⏰ WINDOW CLOSED: {game.away_team} @ {game.home_team}")
                    self.force_close_positions(game)

    def run(self):
        """Main bot loop."""
        logger.info("\n" + "="*80)
        logger.info("STARTING NHL TRADING BOT")
        logger.info("="*80)

        # Cache all Kalshi markets ONCE at startup
        self.load_kalshi_markets_cache()

        # Load today's schedule
        self.load_todays_games()

        # Main loop
        poll_interval = 60  # Check every minute

        while True:
            try:
                self.run_polling_cycle()
                time.sleep(poll_interval)
            except KeyboardInterrupt:
                logger.info("\n\nShutting down gracefully...")
                break
            except Exception as e:
                logger.error(f"Error in main loop: {e}", exc_info=True)
                time.sleep(poll_interval)


if __name__ == "__main__":
    bot = NHLTradingBot()
    bot.run()

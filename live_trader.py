"""
NHL Live Trading Bot - Mean Reversion Strategy

Monitors NHL game markets and executes mean reversion trades:
- Identifies favorites (≥59% at open)
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
    is_qualified: bool = False  # ≥59% favorite AND ≥50k volume

    # Order tracking (pending orders that haven't filled yet)
    pending_order_ids: List[str] = None

    # Checkpoints (Unix timestamps)
    poll_6h: Optional[int] = None
    poll_3h: Optional[int] = None
    poll_30m: Optional[int] = None

    # Game state
    game_started: bool = False
    monitoring_window_end: Optional[int] = None  # Puck drop + 90 minutes

    def __post_init__(self):
        """Initialize mutable default fields."""
        if self.pending_order_ids is None:
            self.pending_order_ids = []

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
    exit_target: int  # Measured-move exit price (computed once at entry)
    order_id: Optional[str] = None
    exit_order_id: Optional[str] = None  # Exit order placed via measured move

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
        self.position_multiplier = float(os.getenv('POSITION_SIZE_MULTIPLIER', 1.0))
        self.revert_fraction = float(os.getenv('REVERT_FRACTION', 0.50))  # Measured move exit fraction

        # State
        self.games: Dict[str, NHLGame] = {}
        self.positions: Dict[str, Position] = {}
        self.kalshi_markets_cache: List[dict] = []  # Cache all NHL markets at startup

        logger.info("="*80)
        logger.info("NHL TRADING BOT INITIALIZED")
        logger.info("="*80)
        logger.info(f"Bankroll: ${self.bankroll:,.2f}")
        logger.info(f"Max Exposure: {self.max_exposure_pct:.0%}")
        logger.info(f"Position Multiplier: {self.position_multiplier}x")
        logger.info(f"Revert Fraction: {self.revert_fraction:.0%}")
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
            # Map NHL API abbreviations to Kalshi ticker abbreviations
            # NHL API uses 3-letter codes, Kalshi sometimes uses 2-letter codes
            abbrev_map = {
                'NJD': 'NJ',   # New Jersey Devils
                'LAK': 'LA',   # Los Angeles Kings (sometimes)
                'SJS': 'SJ',   # San Jose Sharks
                'TBL': 'TB',   # Tampa Bay Lightning (sometimes)
                'VGK': 'VGK',  # Vegas Golden Knights (keep as-is)
            }

            # Normalize team abbreviations for Kalshi
            team_abbrev_kalshi = abbrev_map.get(team_abbrev, team_abbrev)
            opponent_abbrev_kalshi = abbrev_map.get(opponent_abbrev, opponent_abbrev) if opponent_abbrev else None

            # Convert date format: 2025-10-21 -> 25OCT21
            from datetime import datetime
            dt = datetime.strptime(game_date, '%Y-%m-%d')
            date_str = dt.strftime('%y%b%d').upper()  # e.g., "25OCT21"

            # Search cached markets (NO API CALL)
            # Ticker format: KXNHLGAME-25OCT21EDMOTT-EDM
            for market in self.kalshi_markets_cache:
                ticker = market.ticker
                if date_str in ticker and ticker.endswith(f'-{team_abbrev_kalshi}'):
                    # If opponent is specified, verify it's in the matchup
                    if opponent_abbrev_kalshi:
                        # Extract matchup from ticker (e.g., "EDMOTT" from "KXNHLGAME-25OCT21EDMOTT-EDM")
                        parts = ticker.split('-')
                        if len(parts) >= 2:
                            matchup = parts[1][len(date_str):]  # Remove date prefix
                            # Check if opponent abbreviation is in the matchup
                            if opponent_abbrev_kalshi.upper() not in matchup.upper():
                                continue  # Skip this market, opponent doesn't match

                    return market

            logger.debug(f"No cached market found for {team_abbrev} vs {opponent_abbrev} on {date_str}")
            return None
        except Exception as e:
            logger.error(f"Failed to find market for {team_abbrev}: {e}")
            return None

    def load_todays_games(self):
        """Load today's and yesterday's NHL games (to catch early UTC games)."""
        from datetime import datetime, timedelta

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

            # Convert UTC time to Eastern time to match Kalshi's date convention
            # Kalshi uses the local date, not UTC date
            # e.g., "2025-10-23T00:30:00Z" (12:30 AM UTC Oct 23) -> Oct 22 Eastern
            try:
                dt_utc = datetime.fromisoformat(start_time.replace('Z', '+00:00'))
                # Convert to Eastern (UTC-5 or UTC-4 depending on DST)
                # Approximate by subtracting 5 hours
                dt_eastern = dt_utc - timedelta(hours=5)
                game_date = dt_eastern.strftime('%Y-%m-%d')
            except:
                # Fallback to extracting date from UTC string
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

            # Store market tickers
            if away_market:
                game.away_ticker = away_market.ticker
            if home_market:
                game.home_ticker = home_market.ticker

            # Determine favorite BEFORE logging (so we log with correct ticker)
            if away_market and home_market:
                if away_market.last_price > home_market.last_price:
                    game.favorite_team = game.away_team
                    game.favorite_ticker = away_market.ticker
                    game.favorite_opening_price = away_market.last_price
                    favorite_market = away_market
                else:
                    game.favorite_team = game.home_team
                    game.favorite_ticker = home_market.ticker
                    game.favorite_opening_price = home_market.last_price
                    favorite_market = home_market
            else:
                # Only one market found, use it
                favorite_market = away_market if away_market else home_market
                game.favorite_ticker = favorite_market.ticker

            # Log the game to Supabase using the FAVORITE's ticker (consistency)
            try:
                game_log_data = {
                    'market_ticker': game.favorite_ticker,
                    'event_ticker': favorite_market.event_ticker if hasattr(favorite_market, 'event_ticker') else None,
                    'market_title': favorite_market.title if hasattr(favorite_market, 'title') else f"{game.away_team} @ {game.home_team}",
                    'yes_subtitle': favorite_market.yes_sub_title if hasattr(favorite_market, 'yes_sub_title') else None,
                    'kickoff_ts': game.get_puck_drop_timestamp(),
                    'status': 'monitoring'
                }

                self.logger.log_game(game_log_data)
                logger.info(f"  ✓ Logged: {game.away_team} @ {game.home_team} ({game.favorite_ticker})")

                # If checkpoints have already passed, capture odds immediately
                now = int(time.time())

                # Capture 6h odds if that checkpoint is in the past
                # (Favorite already determined above)
                if game.poll_6h and game.poll_6h < now and game.favorite_ticker:
                    # Log to Supabase
                    self.logger.update_game_checkpoint(
                        market_ticker=game.favorite_ticker,
                        field_name='odds_6h',
                        odds=game.favorite_opening_price / 100,
                        timestamp=now
                    )
                    logger.info(f"    Captured 6h odds (retroactive): {game.favorite_team} @ {game.favorite_opening_price}%")
                    game.poll_6h = None  # Mark as captured

                # Capture 3h odds if that checkpoint is in the past
                if game.poll_3h and game.poll_3h < now and game.favorite_ticker:
                    fav_market = self.client.get_market(game.favorite_ticker)
                    if fav_market:
                        self.logger.update_game_checkpoint(
                            market_ticker=game.favorite_ticker,
                            field_name='odds_3h',
                            odds=fav_market.last_price / 100,
                            timestamp=now
                        )
                        logger.info(f"    Captured 3h odds (retroactive): {game.favorite_team} @ {fav_market.last_price}%")
                        game.poll_3h = None  # Mark as captured

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

                # Check if favorite still qualifies (≥59%)
                if game.favorite_opening_price >= 59.0:
                    # TODO: Add volume check here when API access is available
                    # For now, assume qualified if ≥59%
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
                    logger.info(f"  ❌ NOT QUALIFIED: Favorite only {game.favorite_opening_price}% (need ≥59%)")

                    # Update eligibility in Supabase
                    if self.logger:
                        self.logger.update_game_eligibility(
                            market_ticker=game.favorite_ticker,
                            is_eligible=False
                        )

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

        # Max exposure per game (e.g., $200 for $1000 bankroll at 20%)
        max_exposure_per_game = self.bankroll * self.max_exposure_pct

        # Define price tiers for limit orders (below 45¢)
        # Tier 1: 42¢ (0.25x weight)
        # Tier 2: 38¢ (1.0x weight)
        # Tier 3: 34¢ (2.0x weight)
        # Tier 4: 32¢ (2.5x weight)
        # Total weight: 0.25 + 1.0 + 2.0 + 2.5 = 5.75
        tiers = [
            {'price': 42, 'label': 'shallow', 'weight': 0.25},
            {'price': 38, 'label': 'medium', 'weight': 1.0},
            {'price': 34, 'label': 'deep', 'weight': 2.0},
            {'price': 32, 'label': 'very_deep', 'weight': 2.5},
        ]

        total_weight = sum(t['weight'] for t in tiers)

        for tier in tiers:
            price = tier['price']
            # Allocate portion of max exposure based on tier weight
            # e.g., Tier 1: $200 × (0.5/3.0) = $33.33
            #       Tier 2: $200 × (1.0/3.0) = $66.67
            #       Tier 3: $200 × (1.5/3.0) = $100.00
            position_value = (max_exposure_per_game * tier['weight'] / total_weight) * self.position_multiplier

            num_contracts = int(position_value / (price / 100))

            # Skip zero-contract orders
            if num_contracts < 1:
                logger.info(f"  Skipping {tier['label']} tier @ {price}¢ (size=0)")
                continue

            exit_min, exit_max = get_exit_targets(price)

            # Compute measured-move exit target ONCE at position creation
            # Get opening odds (from 6h checkpoint, or fallback to 65¢)
            opening_price = game.favorite_opening_price if game.favorite_opening_price else 65

            # Calculate measured-move exit target
            drop_cents = opening_price - price  # how far it dropped from open
            revert_cents = int(drop_cents * self.revert_fraction)  # expect partial revert
            exit_target = price + revert_cents
            exit_target = max(1, min(99, exit_target))  # clamp to valid range

            logger.info(f"  📝 {tier['label'].capitalize()} tier: {num_contracts} contracts @ {price}¢ (exit target: {exit_target}¢)")

            # Place limit order
            order_id = None
            try:
                order = self.trading_client.place_order(
                    market_ticker=game.favorite_ticker,
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
                exit_target=exit_target,  # Store computed target
                order_id=order_id
            )

            # Use unique key for each tier
            position_key = f"{game.favorite_ticker}_{price}"
            self.positions[position_key] = position

    def _place_exit_order(self, game: NHLGame, position: Position):
        """
        Place exit order using measured move strategy.
        Exit price = entry + (opening_price - entry) * revert_fraction

        The position.exit_target is already calculated at entry time, so we use that.
        """
        if not position.exit_target:
            logger.warning("No exit target available for position, cannot place exit order")
            return

        exit_price_cents = position.exit_target

        logger.info(f"  → Placing exit order: {position.num_contracts} contracts @ {exit_price_cents}¢")
        logger.info(f"     Measured move: {int(position.entry_price)}¢ entry → {exit_price_cents}¢ target ({self.revert_fraction:.0%} revert)")

        try:
            exit_order = self.trading_client.place_order(
                market_ticker=position.ticker,
                side='yes',
                action='sell',
                count=position.num_contracts,
                price=exit_price_cents,
                order_type='limit'
            )
            position.exit_order_id = exit_order.order_id
            logger.info(f"    ✓ Exit order placed: {exit_order.order_id}")

            # Log to Supabase
            if self.logger:
                try:
                    self.logger.log_order(
                        market_ticker=position.ticker,
                        order_id=exit_order.order_id,
                        price=exit_price_cents,
                        size=position.num_contracts,
                        side='sell'
                    )
                except Exception as db_error:
                    logger.error(f"    ✗ Failed to log exit order to Supabase: {db_error}")

        except Exception as e:
            logger.error(f"    ✗ Error placing exit order: {e}")

    def monitor_order_fills(self):
        """
        Monitor pending orders for fills and update positions accordingly.
        This should be called regularly during the trading window.
        """
        # NO KILL SWITCH - monitoring always runs

        for position_key, position in list(self.positions.items()):
            if not position.order_id or position.exit_order_id:
                continue  # Skip if no pending buy order, or exit already placed

            try:
                # Check order status with Kalshi
                order_status = self.trading_client.get_order_status(position.order_id)

                # Handle "executed but filled_count=0" (Kalshi quirk)
                order = (order_status or {}).get('order', {})
                status = order.get('status', 'pending')
                filled_count = order.get('filled_count', 0)

                # Special handling for "executed" status with 0 filled_count
                if status == 'executed' and filled_count == 0:
                    # Try to get fills from API
                    fills = self.trading_client.get_fills(order_id=position.order_id) or []
                    filled_count = sum(f.get('count', 0) for f in fills)

                    # Update position entry price from actual fills
                    if filled_count > 0 and fills:
                        got_prices = [p for f in fills
                                     for p in [f.get('yes_price'), f.get('no_price')]
                                     if p is not None]
                        if got_prices:
                            position.entry_price = int(round(sum(got_prices)/len(got_prices)))

                # If order returns None (404), query fills to confirm execution
                if order_status is None:
                    fills = self.trading_client.get_fills(order_id=position.order_id)
                    if fills:
                        filled_count = sum(fill.get('count', 0) for fill in fills)
                        # Get average fill price
                        got_prices = [p for f in fills
                                     for p in [f.get('yes_price'), f.get('no_price')]
                                     if p is not None]
                        if got_prices:
                            position.entry_price = int(round(sum(got_prices)/len(got_prices)))
                        status = 'filled'
                    else:
                        # Order was cancelled
                        logger.info(f"⚠️  Order {position.order_id} cancelled/expired")
                        if self.logger:
                            self.logger.update_order_status(
                                order_id=position.order_id,
                                status='cancelled'
                            )
                        # Remove from tracking
                        del self.positions[position_key]
                        continue

                # If filled, place exit NOW using stored measured-move target
                if (status in ('filled', 'executed') or filled_count > 0) and not position.exit_order_id:
                    count = filled_count or position.num_contracts

                    if count > 0:
                        logger.info(f"\n✅ ORDER FILLED: {position.order_id}")
                        logger.info(f"   {count} contracts @ {int(position.entry_price)}¢")

                        # Update order status in Supabase (optional)
                        if self.logger:
                            try:
                                self.logger.update_order_status(
                                    order_id=position.order_id,
                                    status='filled',
                                    filled_size=count
                                )

                                # Log position entry
                                position_data = {
                                    'market_ticker': position.ticker,
                                    'entry_price': int(position.entry_price),
                                    'size': count,
                                    'entry_time': int(time.time()),
                                    'order_id': position.order_id,
                                    'status': 'open'
                                }
                                self.logger.log_position_entry(position_data)

                                # Log bankroll change
                                cost = (position.entry_price / 100) * count
                                self.bankroll -= cost
                                self.logger.log_bankroll_change(
                                    timestamp=int(time.time()),
                                    new_amount=self.bankroll,
                                    change=-cost,
                                    description=f"Opened position: {count} contracts @ {int(position.entry_price)}¢"
                                )
                            except Exception as e:
                                logger.debug(f"Supabase logging failed (non-critical): {e}")

                        # Update position with actual fill count
                        position.num_contracts = count

                        # IMMEDIATELY place exit order using measured-move target (bracket strategy)
                        # Find the game for this position
                        game = None
                        for g in self.games.values():
                            if g.game_id == position.game_id:
                                game = g
                                break

                        if game:
                            self._place_exit_order(game, position)
                        else:
                            logger.warning(f"   ⚠️  Game not found for position {position.ticker} - cannot place exit order")

                elif status == 'pending' and filled_count > 0 and filled_count < order.get('count', 0):
                    # Partial fill
                    logger.info(f"📊 Partial fill: {position.order_id} - {filled_count}/{order.get('count')} filled")
                    if self.logger:
                        try:
                            self.logger.update_order_status(
                                order_id=position.order_id,
                                status='partially_filled',
                                filled_size=filled_count
                            )
                        except Exception as e:
                            logger.debug(f"Supabase log failed: {e}")

            except Exception as e:
                logger.error(f"Error monitoring order {position.order_id}: {e}")

    def monitor_exit_order_fills(self):
        """
        Monitor pending exit orders for fills and close positions accordingly.
        This should be called regularly during the trading window.
        """
        # NO KILL SWITCH - monitoring always runs

        for position_key, position in list(self.positions.items()):
            # Skip positions without exit orders
            if not position.exit_order_id:
                continue

            try:
                # Check exit order status with Kalshi
                order_status = self.trading_client.get_order_status(position.exit_order_id)
                order = (order_status or {}).get('order', {})
                status = order.get('status', 'pending')

                # If order returns None (404) or status is filled/executed
                if order_status is None or status in ('filled', 'executed'):
                    # Check fills to see if it executed
                    fills = self.trading_client.get_fills(order_id=position.exit_order_id)

                    if fills:
                        # Exit order filled! Calculate P&L and close position
                        total_filled = sum(fill.get('count', 0) for fill in fills)
                        got_prices = [p for f in fills
                                     for p in [f.get('yes_price'), f.get('no_price')]
                                     if p is not None]
                        avg_exit_price = sum(got_prices) / len(got_prices) if got_prices else position.entry_price

                        pnl = ((avg_exit_price - position.entry_price) / 100) * total_filled

                        logger.info(f"\n✅ EXIT ORDER FILLED: {position.exit_order_id}")
                        logger.info(f"   {total_filled} contracts @ {int(avg_exit_price)}¢")
                        logger.info(f"   Entry: {int(position.entry_price)}¢ → Exit: {int(avg_exit_price)}¢")
                        logger.info(f"   P&L: ${pnl:+.2f}")

                        # Update Supabase (optional)
                        if self.logger:
                            try:
                                self.logger.update_order_status(
                                    order_id=position.exit_order_id,
                                    status='filled',
                                    filled_size=total_filled
                                )

                                # Log position exit
                                self.logger.log_position_exit(
                                    market_ticker=position.ticker,
                                    exit_price=int(avg_exit_price),
                                    exit_time=int(time.time()),
                                    pnl=pnl
                                )

                                # Log bankroll change
                                proceeds = (avg_exit_price / 100) * total_filled
                                self.bankroll += proceeds
                                self.logger.log_bankroll_change(
                                    timestamp=int(time.time()),
                                    new_amount=self.bankroll,
                                    change=proceeds,
                                    description=f"Closed position: {total_filled} contracts @ {int(avg_exit_price)}¢ (P&L: ${pnl:+.2f})"
                                )
                            except Exception as e:
                                logger.debug(f"Supabase log failed: {e}")

                        # Remove position from tracking
                        del self.positions[position_key]

                    else:
                        # Exit order was cancelled - keep exit_order_id to prevent duplicate placement
                        logger.info(f"⚠️  Exit order {position.exit_order_id} cancelled/expired - keeping ID to prevent re-placement")
                        # DO NOT clear exit_order_id - would cause duplicate exit orders every cycle

                elif status == 'pending':
                    # Still waiting
                    filled_count = order.get('filled_count', 0)

                    # Check for partial fills on exit
                    if filled_count > 0 and filled_count < order.get('count', 0):
                        logger.info(f"📊 Partial exit: {position.exit_order_id} - {filled_count}/{order.get('count')} filled")
                        if self.logger:
                            try:
                                self.logger.update_order_status(
                                    order_id=position.exit_order_id,
                                    status='partially_filled',
                                    filled_size=filled_count
                                )
                            except Exception as e:
                                logger.debug(f"Supabase log failed: {e}")

                else:
                    # Cancelled/expired - keep exit_order_id to prevent duplicate placement
                    logger.info(f"⚠️  Exit order {position.exit_order_id} in unexpected state ({status}) - keeping ID to prevent re-placement")
                    # DO NOT clear exit_order_id - would cause duplicate exit orders every cycle

            except Exception as e:
                logger.error(f"Error monitoring exit order {position.exit_order_id}: {e}")

    def run_polling_cycle(self):
        """Run one polling cycle across all games."""
        now = int(time.time())

        # Monitor order fills periodically (even if game hasn't started)
        self.monitor_order_fills()
        # Also monitor exit order fills
        self.monitor_exit_order_fills()

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
                    # Monitoring happens at line 937 for ALL games (no need to call again here)
                    pass
                elif game.monitoring_window_end and now >= game.monitoring_window_end:
                    # Window closed - exit orders already placed via bracket strategy
                    logger.info(f"\n⏰ WINDOW CLOSED: {game.away_team} @ {game.home_team}")
                    logger.info(f"   Exit orders already in place via measured-move strategy")

    def run(self):
        """Main bot loop."""
        logger.info("\n" + "="*80)
        logger.info("STARTING NHL TRADING BOT")
        logger.info("="*80)

        # Cache all Kalshi markets ONCE at startup
        self.load_kalshi_markets_cache()

        # Load today's schedule
        self.load_todays_games()

        # Track last reload date
        last_reload_date = datetime.now().date()

        # Main loop
        poll_interval = 60  # Check every minute

        while True:
            try:
                # Check if we need to reload games for new day (at midnight)
                current_date = datetime.now().date()
                if current_date > last_reload_date:
                    logger.info(f"\n📅 NEW DAY: Reloading games for {current_date}")
                    # Refresh Kalshi markets cache to discover new games
                    self.load_kalshi_markets_cache()
                    # Then reload today's schedule
                    self.load_todays_games()
                    last_reload_date = current_date

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

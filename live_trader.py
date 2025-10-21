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
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Dict, List, Optional, Tuple
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

    # Checkpoints (Unix timestamps)
    poll_6h: Optional[int] = None
    poll_3h: Optional[int] = None
    poll_30m: Optional[int] = None

    def get_puck_drop_timestamp(self) -> int:
        """Convert start_time_utc to Unix timestamp."""
        dt = datetime.fromisoformat(self.start_time_utc.replace('Z', '+00:00'))
        return int(dt.timestamp())


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
        self.trading_client = KalshiTradingClient()

        # Supabase logger
        try:
            self.logger = SupabaseLogger(
                table_name='nhl_positions',
                snapshot_table='nhl_market_snapshots'
            )
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

        logger.info("="*80)
        logger.info("NHL TRADING BOT INITIALIZED")
        logger.info("="*80)
        logger.info(f"Bankroll: ${self.bankroll:,.2f}")
        logger.info(f"Max Exposure: {self.max_exposure_pct:.0%}")
        logger.info(f"Dry Run: {self.dry_run}")
        logger.info(f"Position Multiplier: {self.position_multiplier}x")
        logger.info(get_strategy_summary())

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

    def find_market_for_team(self, game_date: str, team_abbrev: str) -> Optional[dict]:
        """
        Find the Kalshi market for a specific team.

        Args:
            game_date: Date in YYYY-MM-DD format
            team_abbrev: Team abbreviation (e.g., 'TOR', 'VGK')

        Returns:
            Market dict or None
        """
        try:
            # Search for KXNHLGAME markets on this date
            markets = self.client.get_markets(
                series_ticker='KXNHLGAME',
                status='open',
                limit=200
            )

            # Filter to markets for this team and date
            for market in markets:
                # Ticker format: KXNHLGAME-25OCT21EDMOTT-EDM
                if team_abbrev in market.ticker and game_date.replace('-', '') in market.ticker:
                    return market

            return None
        except Exception as e:
            logger.error(f"Failed to find market for {team_abbrev}: {e}")
            return None

    def load_todays_games(self):
        """Load today's NHL games and set up monitoring."""
        today = datetime.now().strftime('%Y-%m-%d')
        games = self.fetch_nhl_schedule(today)

        logger.info(f"\nFetched {len(games)} NHL games for {today}")

        for game_data in games:
            game_id = str(game_data.get('id'))
            start_time = game_data.get('startTimeUTC')
            away_abbrev = game_data.get('awayTeam', {}).get('abbrev')
            home_abbrev = game_data.get('homeTeam', {}).get('abbrev')

            if not all([game_id, start_time, away_abbrev, home_abbrev]):
                continue

            game = NHLGame(
                game_id=game_id,
                date=today,
                start_time_utc=start_time,
                away_team=away_abbrev,
                home_team=home_abbrev
            )

            # Calculate poll times
            puck_drop = game.get_puck_drop_timestamp()
            game.poll_6h = puck_drop - (6 * 3600)
            game.poll_3h = puck_drop - (3 * 3600)
            game.poll_30m = puck_drop - (30 * 60)

            self.games[game_id] = game

            logger.info(f"  {away_abbrev} @ {home_abbrev} - Puck drop: {start_time}")

    def poll_game_markets(self, game: NHLGame, checkpoint: str):
        """
        Poll markets for a game at a specific checkpoint.

        Args:
            game: NHL game to poll
            checkpoint: '6h', '3h', or '30m'
        """
        now = int(time.time())

        # Find markets for both teams
        away_market = self.find_market_for_team(game.date, game.away_team)
        home_market = self.find_market_for_team(game.date, game.home_team)

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
            else:
                game.favorite_team = game.home_team
                game.favorite_ticker = home_market.ticker

            logger.info(f"\n[{checkpoint.upper()}] {game.away_team} @ {game.home_team}")
            logger.info(f"  {game.away_team}: {away_market.last_price}%")
            logger.info(f"  {game.home_team}: {home_market.last_price}%")
            logger.info(f"  Favorite: {game.favorite_team}")

        # Check for entry signals on favorite
        if game.favorite_ticker:
            self.check_entry_signal(game, checkpoint)

        # Check exits on open positions
        self.check_exit_signals(game)

    def check_entry_signal(self, game: NHLGame, checkpoint: str):
        """Check if we should enter a position on the favorite."""
        if game.favorite_ticker in self.positions:
            # Already have a position
            return

        # Get current market
        market = self.client.get_market(game.favorite_ticker)
        if not market:
            return

        current_price = market.last_price
        opening_price = (game.away_opening_price if game.favorite_team == game.away_team
                        else game.home_opening_price)

        # Check strategy entry criteria
        if not should_enter_position(current_price, opening_price):
            return

        logger.info(f"\n🎯 ENTRY SIGNAL: {game.favorite_team}")
        logger.info(f"  Opening: {opening_price}% → Current: {current_price}%")

        # Calculate position size
        base_size = self.bankroll * 0.1  # Base 10% of bankroll per trade
        position_value = get_position_size(current_price, base_size)

        # Check exposure limits
        current_exposure = sum(p.position_size for p in self.positions.values())
        max_exposure = self.bankroll * self.max_exposure_pct

        if current_exposure + position_value > max_exposure:
            logger.warning(f"  ⚠️  Would exceed max exposure (${max_exposure:,.2f})")
            return

        # Calculate contracts
        num_contracts = int(position_value / (current_price / 100))

        # Get exit targets
        exit_min, exit_max = get_exit_targets(current_price)

        logger.info(f"  Position Size: ${position_value:,.2f} ({num_contracts} contracts)")
        logger.info(f"  Exit Target: {exit_min}-{exit_max}%")

        # Place order
        order_id = None
        if not self.dry_run:
            try:
                order_id = self.trading_client.place_order(
                    ticker=game.favorite_ticker,
                    action='buy',
                    side='yes',
                    count=num_contracts,
                    limit_price=int(current_price)
                )
                logger.info(f"  ✅ Order placed: {order_id}")
            except Exception as e:
                logger.error(f"  ❌ Order failed: {e}")
                return
        else:
            logger.info(f"  [DRY RUN] Would place order for {num_contracts} contracts")

        # Create position
        position = Position(
            ticker=game.favorite_ticker,
            game_id=game.game_id,
            entry_price=current_price,
            entry_time=int(time.time()),
            position_size=position_value,
            num_contracts=num_contracts,
            exit_min=exit_min,
            exit_max=exit_max,
            order_id=order_id
        )

        self.positions[game.favorite_ticker] = position

        # Log to Supabase
        if self.logger:
            try:
                self.logger.log_position(asdict(position))
            except Exception as e:
                logger.error(f"Failed to log position to Supabase: {e}")

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
                logger.info(f"\n🚪 EXIT SIGNAL: {ticker}")
                logger.info(f"  Entry: {position.entry_price}% → Current: {current_price}%")
                logger.info(f"  P&L: {(current_price - position.entry_price) * position.num_contracts / 100:.2f}¢")
                logger.info(f"  Reason: {reason}")

                # Place exit order
                if not self.dry_run and position.order_id:
                    try:
                        self.trading_client.place_order(
                            ticker=ticker,
                            action='sell',
                            side='yes',
                            count=position.num_contracts,
                            limit_price=int(current_price)
                        )
                        logger.info(f"  ✅ Exit order placed")
                    except Exception as e:
                        logger.error(f"  ❌ Exit order failed: {e}")
                else:
                    logger.info(f"  [DRY RUN] Would exit position")

                # Remove position
                del self.positions[ticker]

    def run_polling_cycle(self):
        """Run one polling cycle across all games."""
        now = int(time.time())

        for game_id, game in self.games.items():
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

    def run(self):
        """Main bot loop."""
        logger.info("\n" + "="*80)
        logger.info("STARTING NHL TRADING BOT")
        logger.info("="*80)

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

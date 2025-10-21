"""
Supabase Logger - Writes trading bot data to Supabase for dashboard visualization
"""
import os
import logging
import time
from typing import Optional
from functools import wraps
from supabase import create_client, Client

logger = logging.getLogger(__name__)


def retry_on_failure(max_retries=3, delay=1):
    """Retry database operations with exponential backoff."""
    def decorator(func):
        @wraps(func)
        def wrapper(*args, **kwargs):
            for attempt in range(max_retries):
                try:
                    return func(*args, **kwargs)
                except Exception as e:
                    if attempt < max_retries - 1:
                        wait_time = delay * (2 ** attempt)
                        logger.warning(f"Database update failed (attempt {attempt + 1}/{max_retries}), "
                                      f"retrying in {wait_time}s: {e}")
                        time.sleep(wait_time)
                    else:
                        logger.error(f"Database update failed after {max_retries} attempts: {e}")
                        raise
            return None
        return wrapper
    return decorator


class SupabaseLogger:
    """Handles all Supabase writes for the trading bot."""

    def __init__(self):
        """Initialize Supabase client from environment variables."""
        url = os.getenv("SUPABASE_URL")
        key = os.getenv("SUPABASE_ANON_KEY")

        if not url or not key:
            logger.warning("⚠️  Supabase credentials not found. Dashboard will not update.")
            logger.warning("   Set SUPABASE_URL and SUPABASE_ANON_KEY environment variables.")
            self.client = None
            return

        try:
            self.client: Client = create_client(url, key)
            logger.info("✓ Connected to Supabase")
        except Exception as e:
            logger.error(f"Failed to connect to Supabase: {e}")
            self.client = None

    def log_game(self, game_data: dict) -> Optional[str]:
        """
        Log a new game to the database.

        Args:
            game_data: Dict with keys: market_ticker, event_ticker, market_title,
                      yes_subtitle, kickoff_ts, halftime_ts, pregame_prob, status

        Returns:
            Game ID if successful, None otherwise
        """
        if not self.client:
            return None

        try:
            # Check if game already exists
            existing = self.client.table('games').select('id').eq('market_ticker', game_data['market_ticker']).execute()

            if existing.data:
                return existing.data[0]['id']

            # Insert new game
            result = self.client.table('games').insert(game_data).execute()

            if result.data:
                game_id = result.data[0]['id']
                logger.debug(f"Logged game to Supabase: {game_data['market_ticker']}")
                return game_id

        except Exception as e:
            logger.error(f"Error logging game to Supabase: {e}")

        return None

    def update_game_status(self, market_ticker: str, status: str, pregame_prob: Optional[float] = None):
        """Update game status (monitoring, triggered, completed, timeout)."""
        if not self.client:
            return

        try:
            update_data = {'status': status, 'updated_at': 'now()'}
            if pregame_prob is not None:
                update_data['pregame_prob'] = pregame_prob

            self.client.table('games').update(update_data).eq('market_ticker', market_ticker).execute()
            logger.debug(f"Updated game status: {market_ticker} -> {status}")
        except Exception as e:
            logger.error(f"Error updating game status: {e}")

    def update_game_checkpoint(self, market_ticker: str, field_name: str, odds: float, timestamp: int):
        """
        Update checkpoint odds for a game.

        Args:
            market_ticker: Market ticker to identify the game
            field_name: Name of the checkpoint field ('odds_6h', 'odds_3h', 'odds_30m')
            odds: The odds value (0-1 probability)
            timestamp: Unix timestamp when the checkpoint was captured
        """
        if not self.client:
            return

        try:
            # Map field names to their timestamp fields
            timestamp_field = field_name.replace('odds', 'checkpoint') + '_ts'

            update_data = {
                field_name: odds,
                timestamp_field: timestamp,
                'updated_at': 'now()'
            }

            self.client.table('games').update(update_data).eq('market_ticker', market_ticker).execute()
            logger.debug(f"Updated {field_name}: {market_ticker} -> {odds:.0%}")
        except Exception as e:
            logger.error(f"Error updating checkpoint {field_name}: {e}")

    def update_game_eligibility(self, market_ticker: str, is_eligible: bool):
        """
        Update the eligibility status for a game.

        Args:
            market_ticker: Market ticker to identify the game
            is_eligible: Whether the game is eligible for trading based on checkpoint rules
        """
        if not self.client:
            return

        try:
            update_data = {
                'is_eligible': is_eligible,
                'updated_at': 'now()'
            }

            self.client.table('games').update(update_data).eq('market_ticker', market_ticker).execute()
            logger.debug(f"Updated eligibility: {market_ticker} -> {is_eligible}")
        except Exception as e:
            logger.error(f"Error updating eligibility: {e}")

    @retry_on_failure(max_retries=3, delay=1)
    def log_position_entry(self, position_data: dict) -> Optional[str]:
        """
        Log a new position entry.

        Args:
            position_data: Dict with keys: market_ticker, entry_price, size,
                          entry_time, order_id, status='open'

        Returns:
            Position ID if successful, None otherwise
        """
        if not self.client:
            return None

        # Get game_id from market_ticker
        game = self.client.table('games').select('id').eq('market_ticker', position_data['market_ticker']).execute()

        if not game.data:
            logger.warning(f"Game not found for position: {position_data['market_ticker']}")
            return None

        position_data['game_id'] = game.data[0]['id']
        position_data['status'] = 'open'

        result = self.client.table('positions').insert(position_data).execute()

        if result.data:
            position_id = result.data[0]['id']
            logger.debug(f"Logged position entry: {position_data['size']} @ {position_data['entry_price']}¢")
            return position_id

        return None

    @retry_on_failure(max_retries=3, delay=1)
    def log_position_exit(self, market_ticker: str, exit_price: int, exit_time: int, pnl: float):
        """Update position with exit details and calculate P&L."""
        if not self.client:
            return

        # Update all open positions for this market
        update_data = {
            'exit_price': exit_price,
            'exit_time': exit_time,
            'pnl': pnl,
            'status': 'closed',
            'updated_at': 'now()'
        }

        self.client.table('positions').update(update_data).eq('market_ticker', market_ticker).eq('status', 'open').execute()
        logger.debug(f"Logged position exit: {market_ticker} P&L=${pnl:+.2f}")

    @retry_on_failure(max_retries=3, delay=1)
    def update_position_status(self, order_id: str, status: str, exit_price: Optional[int] = None, pnl: Optional[float] = None):
        """
        Update position status in database.

        Args:
            order_id: The original buy order ID
            status: New status ('open' or 'closed')
            exit_price: Exit price in cents (if closing)
            pnl: Realized P&L (if closing)
        """
        if not self.client:
            return

        update_data = {
            'status': status,
            'updated_at': 'now()'
        }

        if status == 'closed':
            update_data['exit_price'] = exit_price
            update_data['pnl'] = pnl
            update_data['exit_time'] = int(time.time())

        self.client.table('positions').update(update_data).eq('order_id', order_id).execute()

        logger.info(f"✓ Updated position status: {order_id} → {status}")

    def log_bankroll_change(self, timestamp: int, new_amount: float, change: float,
                           game_id: Optional[str] = None, description: Optional[str] = None):
        """Log a bankroll change to the history table."""
        if not self.client:
            return

        try:
            data = {
                'timestamp': timestamp,
                'amount': new_amount,
                'change': change,
                'game_id': game_id,
                'description': description
            }

            self.client.table('bankroll_history').insert(data).execute()
            logger.debug(f"Logged bankroll: ${new_amount:.2f} ({change:+.2f})")

        except Exception as e:
            logger.error(f"Error logging bankroll change: {e}")

    def log_price_tick(self, market_ticker: str, timestamp: int, favorite_price: float,
                       yes_ask: Optional[int] = None, no_ask: Optional[int] = None):
        """
        Log a price tick for historical data collection.

        Args:
            market_ticker: Market ticker to identify the game
            timestamp: Unix timestamp when price was captured
            favorite_price: Favorite's probability (0-1)
            yes_ask: Price to buy YES in cents
            no_ask: Price to buy NO in cents
        """
        if not self.client:
            return

        try:
            # Get game_id from market_ticker
            game = self.client.table('games').select('id').eq('market_ticker', market_ticker).execute()

            game_id = game.data[0]['id'] if game.data else None

            data = {
                'market_ticker': market_ticker,
                'game_id': game_id,
                'timestamp': timestamp,
                'favorite_price': favorite_price,
                'yes_ask': yes_ask,
                'no_ask': no_ask
            }

            self.client.table('market_ticks').insert(data).execute()
            # Only log at debug level to avoid spam (happens every 10 seconds)
            logger.debug(f"Logged price tick: {market_ticker} @ {favorite_price:.0%}")

        except Exception as e:
            logger.error(f"Error logging price tick: {e}")

    def log_order(self, market_ticker: str, order_id: str, price: int, size: int, side: str = 'buy') -> Optional[str]:
        """
        Log a new order placement.

        Args:
            market_ticker: Market ticker to identify the game
            order_id: Kalshi order ID
            price: Order price in cents
            size: Number of contracts
            side: 'buy' or 'sell'

        Returns:
            Order record ID if successful, None otherwise
        """
        if not self.client:
            return None

        try:
            # Get game_id from market_ticker
            game = self.client.table('games').select('id').eq('market_ticker', market_ticker).execute()

            if not game.data:
                logger.warning(f"Game not found for order: {market_ticker}")
                return None

            order_data = {
                'game_id': game.data[0]['id'],
                'market_ticker': market_ticker,
                'order_id': order_id,
                'price': price,
                'size': size,
                'filled_size': 0,
                'status': 'pending',
                'side': side
            }

            result = self.client.table('orders').insert(order_data).execute()

            if result.data:
                logger.debug(f"Logged order: {order_id} - {size} @ {price}¢ ({side})")
                return result.data[0]['id']

        except Exception as e:
            logger.error(f"Error logging order: {e}")

        return None

    @retry_on_failure(max_retries=3, delay=1)
    def update_order_status(self, order_id: str, status: str, filled_size: int = 0):
        """
        Update order status (e.g., when order fills or gets cancelled).

        Args:
            order_id: Kalshi order ID
            status: 'pending', 'filled', 'partially_filled', 'cancelled'
            filled_size: Number of contracts filled
        """
        if not self.client:
            return

        update_data = {
            'status': status,
            'filled_size': filled_size,
            'updated_at': 'now()'
        }

        self.client.table('orders').update(update_data).eq('order_id', order_id).execute()
        logger.debug(f"Updated order {order_id}: {status} ({filled_size} filled)")

    def get_pending_orders(self, market_ticker: str) -> list:
        """
        Get all pending orders for a market.

        Args:
            market_ticker: Market ticker to identify the game

        Returns:
            List of pending order records
        """
        if not self.client:
            return []

        try:
            result = self.client.table('orders').select('*').eq('market_ticker', market_ticker).eq('status', 'pending').execute()
            return result.data if result.data else []

        except Exception as e:
            logger.error(f"Error getting pending orders: {e}")
            return []

    def get_order(self, order_id: str) -> Optional[dict]:
        """
        Get order details by order_id.

        Args:
            order_id: Kalshi order ID

        Returns:
            Order record dict or None
        """
        if not self.client:
            return None

        try:
            result = self.client.table('orders').select('*').eq('order_id', order_id).execute()
            return result.data[0] if result.data else None

        except Exception as e:
            logger.error(f"Error getting order {order_id}: {e}")
            return None

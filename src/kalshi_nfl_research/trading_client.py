"""
Kalshi Trading API Client for placing real orders.
"""
import logging
import time
import uuid
from typing import Optional, Literal
from dataclasses import dataclass

from kalshi_python import Configuration, KalshiClient as OfficialKalshiClient, PortfolioApi

logger = logging.getLogger(__name__)


@dataclass
class Order:
    """Represents a Kalshi order."""
    order_id: str
    market_ticker: str
    side: Literal["yes", "no"]
    action: Literal["buy", "sell"]
    count: int
    price: int  # In cents
    status: str


class KalshiTradingClient:
    """
    Client for Kalshi Trading API.
    Handles authentication and order placement using the official kalshi-python library.
    """

    def __init__(
        self,
        email: Optional[str] = None,
        password: Optional[str] = None,
        api_key: Optional[str] = None,
        api_secret: Optional[str] = None,
    ):
        # Convert literal \n in private key to actual newlines
        if api_secret and '\\n' in api_secret:
            api_secret = api_secret.replace('\\n', '\n')

        # Use official Kalshi client
        if api_key and api_secret:
            config = Configuration(
                host="https://api.elections.kalshi.com/trade-api/v2"
            )
            config.api_key_id = api_key
            config.private_key_pem = api_secret
            self.client = OfficialKalshiClient(config)
            self.portfolio_api = PortfolioApi(self.client)
            logger.info("Successfully authenticated with API key")
        elif email and password:
            # Official client doesn't support email/password, fall back to basic auth
            raise NotImplementedError(
                "Email/password authentication is deprecated. "
                "Please use API key authentication instead."
            )
        else:
            raise ValueError("Must provide (api_key, api_secret)")

    def get_balance(self) -> int:
        """Get account balance in cents."""
        response = self.portfolio_api.get_balance()
        return response.balance

    def get_positions(self) -> list[dict]:
        """Get all open positions."""
        response = self.portfolio_api.get_positions()
        # Convert response to list of dicts for compatibility
        if hasattr(response, 'positions') and response.positions is not None:
            return [pos.to_dict() if hasattr(pos, 'to_dict') else pos for pos in response.positions]
        return []

    def place_order(
        self,
        market_ticker: str,
        side: Literal["yes", "no"],
        action: Literal["buy", "sell"],
        count: int,
        price: int,  # In cents
        order_type: Literal["limit", "market"] = "limit",
    ) -> Order:
        """
        Place a limit or market order.

        Args:
            market_ticker: Market ticker (e.g., "KXNFLGAME-25OCT13BUFATL")
            side: "yes" or "no"
            action: "buy" or "sell"
            count: int
            price: Price in cents (0-100)
            order_type: "limit" or "market"

        Returns:
            Order object with order details
        """
        logger.info(
            f"Placing order: {action} {count} {side} @ {price}¢ on {market_ticker}"
        )

        # Generate unique client order ID
        client_order_id = f"order_{int(time.time())}_{str(uuid.uuid4())[:8]}"

        # Place order through official API (pass as keyword arguments)
        response = self.portfolio_api.create_order(
            ticker=market_ticker,
            client_order_id=client_order_id,
            side=side,
            action=action,
            count=count,
            type=order_type,
            yes_price=price if side == "yes" else None,
            no_price=price if side == "no" else None,
        )

        # Convert response to our Order dataclass
        order = Order(
            order_id=response.order.order_id,
            market_ticker=market_ticker,
            side=side,
            action=action,
            count=count,
            price=price,
            status=response.order.status if hasattr(response.order, 'status') else "pending",
        )

        logger.info(f"Order placed successfully: {order.order_id}")
        return order

    def cancel_order(self, order_id: str) -> bool:
        """Cancel a pending order."""
        self.portfolio_api.cancel_order(order_id=order_id)
        logger.info(f"Order {order_id} cancelled")
        return True

    def get_order_status(self, order_id: str) -> dict:
        """
        Get status of an order.

        Note: Kalshi returns 404 for executed orders (they're removed from active orders).
        In that case, we return None to indicate the order is no longer queryable.
        """
        try:
            response = self.portfolio_api.get_order(order_id=order_id)
            # Convert response to dict for compatibility
            if hasattr(response, 'order'):
                order = response.order
                return {
                    "order": {
                        "order_id": order.order_id if hasattr(order, 'order_id') else order_id,
                        "status": order.status if hasattr(order, 'status') else "unknown",
                        "ticker": order.ticker if hasattr(order, 'ticker') else None,
                        "side": order.side if hasattr(order, 'side') else None,
                        "action": order.action if hasattr(order, 'action') else None,
                        "count": order.count if hasattr(order, 'count') else None,
                        "filled_count": order.filled_count if hasattr(order, 'filled_count') else 0,
                        "yes_price": order.yes_price if hasattr(order, 'yes_price') else None,
                        "no_price": order.no_price if hasattr(order, 'no_price') else None,
                    }
                }
            return {}
        except Exception as e:
            # 404 means order was executed/cancelled and removed from active orders
            if "404" in str(e) or "not_found" in str(e).lower():
                logger.debug(f"Order {order_id} not found (likely executed/cancelled)")
                return None  # None indicates order is not queryable
            # Other errors should be raised
            logger.error(f"Error getting order status for {order_id}: {e}")
            raise

    def close(self):
        """Close the client."""
        # Official client handles cleanup automatically
        pass

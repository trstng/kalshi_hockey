"""
Pydantic data models for Kalshi API responses and backtest outputs.

All timestamps are Unix seconds (UTC). Prices are in cents unless noted.
"""

from typing import Any, Literal, Optional
from datetime import datetime
from pydantic import BaseModel, Field, field_validator


class SeriesInfo(BaseModel):
    """Represents a Kalshi series (e.g., NFL season)."""

    series_ticker: str = Field(alias="ticker")
    title: str
    frequency: Optional[str] = None
    category: Optional[str] = None
    tags: Optional[list[str]] = Field(default_factory=list)

    class Config:
        frozen = True
        populate_by_name = True


class EventInfo(BaseModel):
    """Represents a Kalshi event (e.g., specific NFL game)."""

    event_ticker: str
    series_ticker: str
    title: str
    subtitle: Optional[str] = None
    mutually_exclusive: bool = True
    strike_date: Optional[int] = None  # Unix timestamp
    category: Optional[str] = None
    # For NFL: teams extracted from title
    teams: list[str] = Field(default_factory=list)

    @field_validator("strike_date", mode="before")
    @classmethod
    def convert_timestamp(cls, v: Any) -> Optional[int]:
        """Convert ISO 8601 string to Unix timestamp."""
        if v is None:
            return None
        if isinstance(v, str):
            dt = datetime.fromisoformat(v.replace('Z', '+00:00'))
            return int(dt.timestamp())
        return v

    class Config:
        frozen = True


class MarketInfo(BaseModel):
    """Represents a Kalshi market (e.g., Team A to win)."""

    ticker: str
    event_ticker: str
    market_type: str
    title: str
    subtitle: Optional[str] = None
    open_time: Optional[int] = None
    close_time: Optional[int] = None
    expiration_time: Optional[int] = None
    status: Optional[str] = None
    yes_sub_title: Optional[str] = None
    no_sub_title: Optional[str] = None

    # Price data (in cents)
    yes_bid: Optional[int] = None
    yes_ask: Optional[int] = None
    no_bid: Optional[int] = None
    no_ask: Optional[int] = None
    last_price: Optional[int] = None

    @field_validator("open_time", "close_time", "expiration_time", mode="before")
    @classmethod
    def convert_timestamp(cls, v: Any) -> Optional[int]:
        """Convert ISO 8601 string to Unix timestamp."""
        if v is None:
            return None
        if isinstance(v, str):
            dt = datetime.fromisoformat(v.replace('Z', '+00:00'))
            return int(dt.timestamp())
        return v

    class Config:
        frozen = True


class Trade(BaseModel):
    """Represents an executed trade."""

    trade_id: Optional[str] = None
    ticker: str
    created_time: int  # Unix timestamp
    count: int = 1
    yes_price: int  # Cents
    no_price: Optional[int] = None  # Cents
    taker_side: Optional[Literal["yes", "no"]] = None

    @field_validator("created_time", mode="before")
    @classmethod
    def convert_timestamp(cls, v: Any) -> int:
        """Convert ISO 8601 string to Unix timestamp."""
        if isinstance(v, str):
            dt = datetime.fromisoformat(v.replace('Z', '+00:00'))
            return int(dt.timestamp())
        return v

    @field_validator("yes_price", mode="before")
    @classmethod
    def validate_price(cls, v: Any) -> int:
        """Ensure price is integer cents."""
        if isinstance(v, float):
            return int(v)
        return v

    @property
    def price_cents(self) -> int:
        """Alias for yes_price for consistency."""
        return self.yes_price

    @property
    def ts(self) -> int:
        """Alias for created_time."""
        return self.created_time

    class Config:
        frozen = True


class Candle(BaseModel):
    """Represents a candlestick bar."""

    start_ts: int  # Unix timestamp
    open_cents: int
    high_cents: int
    low_cents: int
    close_cents: int
    volume: int = 0

    @field_validator("open_cents", "high_cents", "low_cents", "close_cents", mode="before")
    @classmethod
    def validate_price(cls, v: Any) -> int:
        """Ensure price is integer cents."""
        if isinstance(v, float):
            return int(v)
        return v

    @property
    def ts(self) -> int:
        """Alias for start_ts."""
        return self.start_ts

    class Config:
        frozen = True


class OrderbookSnapshot(BaseModel):
    """Represents orderbook depth at a point in time."""

    ticker: str
    ts: int  # Unix timestamp when snapshot was taken
    yes_bid: Optional[int] = None  # Best yes bid in cents
    yes_ask: Optional[int] = None  # Best yes ask in cents
    yes_bid_size: Optional[int] = None
    yes_ask_size: Optional[int] = None

    class Config:
        frozen = True


class EntryExit(BaseModel):
    """Represents a single simulated trade (entry + exit)."""

    event_ticker: str
    favorite_side: Literal["yes", "no"]
    pregame_prob: float  # Implied probability (0-1)
    kickoff_ts: int
    halftime_ts: int

    # Entry
    trigger_ts: int
    trigger_prob: float
    entry_ts: int
    entry_prob: float
    entry_price_cents: int
    entry_fill_source: Literal["trade", "ask", "trade_with_slippage"]

    # Exit
    exit_ts: int
    exit_prob: float
    exit_price_cents: int
    exit_fill_source: Literal["trade", "bid", "trade_with_slippage", "timeout"]
    exit_reason: Literal["revert_band", "timeout", "mae_stop"]
    band_hit: Optional[float] = None  # Which revert band was hit (if any)

    # P&L
    pnl_gross_cents: int
    pnl_net_cents: int  # After fees & slippage
    fees_paid_cents: int
    slippage_cents: int

    # Risk Metrics
    mae: Optional[float] = None  # Max adverse excursion (in probability)
    mfe: Optional[float] = None  # Max favorable excursion (in probability)
    max_drawdown_cents: Optional[int] = None

    # Duration
    hold_time_sec: int

    class Config:
        frozen = False  # Allow mutation for backtest aggregations


class BacktestConfig(BaseModel):
    """Configuration for a backtest run."""

    kalshi_base: str
    start_date: str  # YYYY-MM-DD
    end_date: str  # YYYY-MM-DD
    pregame_favorite_threshold: float
    trigger_threshold: float
    revert_bands: list[float]
    per_contract_fee: float
    extra_slippage: float
    mae_stop_prob: Optional[float]
    timeout: Literal["halftime", "full"]
    grace_sec_for_fill: int
    rate_limit_sleep_ms: int

    class Config:
        frozen = True


class BandMetrics(BaseModel):
    """Performance metrics for a single revert band."""

    band: float
    num_trades: int
    hit_rate: float  # Fraction that hit this band
    avg_pnl_cents: float
    median_pnl_cents: float
    std_pnl_cents: float
    win_pct: float  # Fraction with positive P&L
    total_pnl_cents: float
    sharpe_ratio: Optional[float] = None
    ev_per_trade_cents: float

    class Config:
        frozen = True


class BacktestSummary(BaseModel):
    """High-level backtest results."""

    config: BacktestConfig
    num_events_analyzed: int
    num_events_qualified: int  # Had pregame favorite > threshold
    num_trades_triggered: int  # Dipped below 50%
    num_trades_filled: int  # Actually entered after grace period check
    total_pnl_gross_cents: int
    total_pnl_net_cents: int
    overall_win_rate: float
    avg_hold_time_sec: float
    band_metrics: list[BandMetrics]

    class Config:
        frozen = True

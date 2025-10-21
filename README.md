# NHL Trading Bot - Mean Reversion Strategy

Automated trading bot for NHL game winner markets on Kalshi, implementing a backtested mean reversion strategy with 95% historical win rate.

## Strategy Overview

**Entry Criteria:**
- Identify favorites (≥57% at market open)
- Wait for price drop to ≤40%
- Skip 46-50% range (poor performance in backtest)

**Position Sizing:**
- 41-45%: 0.5x base size
- 36-40%: 1.0x base size
- ≤35%: 1.5x base size (best performance)

**Exit Strategy:**
- Shallow dips (40-45%): Quick exit at +3 to +6 cents
- Deep dips (≤35%): Target +10 to +15 cents or hold to outcome

**Historical Performance:**
- 95% win rate
- +7.88 cents average P&L per trade
- Deep dips (≤35%) averaged +24.60 cents profit

## Project Structure

```
kalshi-nhl-trader/
├── live_trader.py              # Main trading bot
├── src/
│   ├── nhl_strategy.py         # Strategy logic
│   ├── supabase_logger.py      # Supabase logging
│   └── kalshi_nfl_research/    # Kalshi API clients
├── research/                    # Backtesting scripts
├── logs/                        # Trading logs
└── data/                        # Market data
```

## Configuration

All configuration is managed via environment variables:

### Required Variables
```bash
# Kalshi API
KALSHI_EMAIL=your_email@example.com
KALSHI_PASSWORD=your_password
KALSHI_API_KEY=your_api_key

# Supabase
SUPABASE_URL=your_supabase_url
SUPABASE_KEY=your_supabase_key
```

### Optional Variables
```bash
# Trading settings
TRADING_BANKROLL=1000           # Total bankroll
MAX_EXPOSURE_PCT=0.5            # Max % of bankroll at risk
POSITION_SIZE_MULTIPLIER=1.0    # Scale all positions
DRY_RUN=true                    # Paper trading mode
```

## Setup

1. **Install dependencies:**
```bash
pip install -r requirements.txt
```

2. **Configure environment:**
```bash
cp .env.example .env
# Edit .env with your credentials
```

3. **Test in dry run mode:**
```bash
export DRY_RUN=true
python3 live_trader.py
```

## Deployment

### Railway (Recommended)

1. Push to GitHub
2. Create new Railway project
3. Connect GitHub repository
4. Set environment variables in Railway dashboard
5. Deploy

The bot will:
- Poll markets at 6hr, 3hr, and 30min before puck drop
- Identify favorites at the 6hr checkpoint
- Monitor for entry signals (price ≤40%)
- Execute orders with tiered position sizing
- Track positions and check exit signals
- Log all activity to Supabase

## Market Polling Schedule

- **6 hours before:** Record opening prices, identify favorites
- **3 hours before:** Check for entry signals
- **30 minutes before:** Final check for entry signals
- **During game:** Monitor positions for exit signals

## Database Schema

### nhl_positions
- Position tracking (entry/exit, P&L)
- Linked to Supabase project (shared with NFL bot)

### nhl_market_snapshots
- Market state snapshots at checkpoints
- Price history for analysis

## Research Scripts

Located in `research/`:
- `backtest_mean_reversion_v2.py` - Final backtest with refined strategy
- `collect_kalshi_markets.py` - Fetch NHL markets from Kalshi
- `collect_nhl_schedule.py` - Fetch NHL schedule from official API
- `merge_data.py` - Match markets to games with outcomes

## Safety Features

- Exposure limits (default 50% of bankroll)
- Dry run mode for testing
- Comprehensive logging
- Position tracking
- Error handling and recovery

## Support

For issues or questions, refer to the backtest analysis in `research/`.

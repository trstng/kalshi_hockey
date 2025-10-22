# NHL Trading Bot - Strategy Update Summary

## ✅ Implementation Complete

The bot has been updated to correctly implement the **in-game mean reversion strategy** that exploits order-flow overreactions during the first 90 minutes of NHL games.

---

## 🔄 Major Changes Made

### 1. Entry Threshold: 40¢ → <45¢
**File:** `src/nhl_strategy.py:25`
- Changed from `max_entry_price: float = 40.0` to `45.0`
- Now accepts entries at 44¢ and below (not 45¢+)

### 2. Position Sizing Adjusted
**File:** `src/nhl_strategy.py:71-79`
- **Old:** 46-50 skip, 41-45 (0.5x), 36-40 (1.0x), ≤35 (1.5x)
- **New:** 40-44¢ (0.5x), 36-39¢ (1.0x), ≤35¢ (1.5x)

### 3. Tiered Limit Orders at 30-Min Checkpoint
**File:** `live_trader.py:326-410`
- **New function:** `place_tiered_limit_orders()`
- Places 3 limit orders pregame:
  - **Shallow tier:** 42¢ @ 0.5x sizing
  - **Medium tier:** 38¢ @ 1.0x sizing
  - **Deep tier:** 34¢ @ 1.5x sizing
- Orders sit in the book and fill automatically if price drops during game

### 4. 90-Minute Monitoring Window
**File:** `live_trader.py:55-99` (NHLGame dataclass)
- Added `game_started`, `monitoring_window_end`, `is_in_monitoring_window()`
- Tracks game state: pregame → in-game → finished
- Window = puck drop + 90 minutes

**File:** `live_trader.py:482-497` (main polling loop)
- Detects puck drop
- Starts 90-minute monitoring window
- Force closes all positions at window end

### 5. Force Close at Window End
**File:** `live_trader.py:460-505`
- **New function:** `force_close_positions()`
- At 90-minute mark, automatically closes all open positions
- No holding to outcome - exit regardless of P&L

### 6. Updated Exit Logic
**File:** `src/nhl_strategy.py:104-151`
- Removed "hold to outcome" logic
- All exits happen within 90-minute window
- **Shallow (40-44¢):** Exit at +3¢ bounce
- **Deep (≤35¢):** Hold for ≥45¢ strong bounce or force close at window end

### 7. Game Qualification at 30-Min
**File:** `live_trader.py:304-321`
- Checks favorite ≥57% at 30-min checkpoint
- TODO: Volume check ($50k+) - placeholder added
- Only qualified games get limit orders placed

### 8. Removed Old Entry Logic
**File:** `live_trader.py`
- Removed `check_entry_signal()` function (was entering pregame)
- No longer enters during 6hr/3hr checkpoints
- Only action at 30min is placing limit orders

---

## 📊 Strategy Flow (Correct Implementation)

```
T-6hr:  Record opening prices, identify favorite
T-3hr:  Re-check prices (logging only)
T-30m:  ✅ QUALIFY (≥57% + $50k vol) → PLACE LIMIT ORDERS
T-0:    🏒 PUCK DROP → Start 90-min window
T+1-90: Monitor positions, exit on bounces
T+90:   ⏰ FORCE CLOSE all positions
```

---

## 🎯 What This Exploits

**Order-Flow Overreaction:**
- Favorite gets scored on → panic selling drives price down
- Liquidity providers widen spreads → temporary mispricing
- Price mean-reverts as:
  - Scoring pressure equalizes
  - Market makers tighten spreads
  - Late money comes in

**Path > Outcome:**
- You're trading the **bounce**, not who wins
- Can profit even if favorite loses the game
- All exits happen in first 90 minutes

---

## ⚠️ Known Limitations

1. **Volume filter not yet implemented** - TODO at `live_trader.py:309`
   - Need to add API call to check market volume
   - Currently qualifies all ≥57% favorites

2. **YES vs NO handling** - Assumed YES side for favorites
   - May need adjustment depending on Kalshi market structure

3. **Order fill monitoring** - Currently assumes fills happen
   - Should add logic to check order status
   - Cancel unfilled orders at window close

4. **Multiple tiers per game** - Uses position_key suffix
   - `{ticker}_42`, `{ticker}_38`, `{ticker}_34`
   - May need better tracking

---

## 📝 Files Modified

### Core Logic
- ✅ `src/nhl_strategy.py` - Entry/exit thresholds, position sizing
- ✅ `live_trader.py` - Limit orders, 90-min window, force close

### Documentation
- ✅ `README.md` - Updated strategy description, polling schedule
- ✅ `STRATEGY_UPDATE_SUMMARY.md` - This file

### Not Modified (Library Code)
- ❌ `src/kalshi_nfl_research/*` - Intentionally left unchanged

---

## 🧪 Testing Checklist

Before going live, test:

1. **Dry run mode** - Verify limit orders would be placed correctly
2. **Puck drop detection** - Check 90-min window starts properly
3. **Force close** - Confirm positions exit at T+90min
4. **Multiple games** - Test with 3-5 concurrent games
5. **Exposure limits** - Verify doesn't exceed 50% bankroll
6. **Exit signals** - Confirm bounces trigger exits correctly

---

## 🚀 Next Steps

1. **Test in dry run** - Run bot during live NHL games today
2. **Add volume check** - Implement $50k volume filter
3. **Monitor fills** - Add order status checking
4. **Cancel unfilled** - Cancel limit orders at window close
5. **Live trading** - Start with small bankroll once verified

---

## 💡 Future Enhancements

- **Dynamic pricing** - Adjust limit order prices based on current odds
- **Multiple tiers** - More granular ladder (e.g., 42¢, 40¢, 38¢, 36¢, 34¢)
- **Partial exits** - Scale out at different profit targets
- **MAE stops** - Max adverse excursion stops for risk management
- **Session filters** - Differentiate regular season vs playoffs
- **Correlation limits** - Limit concurrent positions across correlated games

---

**Strategy now correctly implements in-game mean reversion!** 🏒📈

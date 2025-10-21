"""
Backtest engine for NFL in-game reversion strategy.

Simulates:
1. Pregame favorite identification (>60% implied)
2. First-half dip detection (<50%)
3. Entry with conservative fills (at ask or trade + slippage)
4. Exit on reversion bands or timeout
5. P&L with fees and slippage
"""

import logging
from typing import Literal, Optional

import numpy as np
import pandas as pd

from .data_models import (
    BacktestConfig,
    BacktestSummary,
    BandMetrics,
    EntryExit,
)
from .fetch import (
    GameData,
    compute_pregame_probability,
    detect_trigger_time,
    find_fill_trade,
)

logger = logging.getLogger(__name__)


def simulate_trade(
    game_data: GameData,
    config: BacktestConfig,
) -> Optional[EntryExit]:
    """
    Simulate a single trade for a game.

    Args:
        game_data: Game data for the event.
        config: Backtest configuration.

    Returns:
        EntryExit record if trade executed, else None.
    """
    if not game_data.event.strike_date:
        logger.debug(f"Skipping {game_data.event.event_ticker} (no strike_date)")
        return None

    kickoff_ts = game_data.event.strike_date
    # NFL first half is 30min game time, but ~90-120min real time
    # due to clock stoppages, timeouts, commercials, etc.
    halftime_ts = kickoff_ts + 5400  # 90 minutes real time

    # Step 1: Compute pregame probability
    pregame_prob = compute_pregame_probability(game_data, kickoff_ts, pregame_window_sec=900)
    if pregame_prob is None:
        logger.debug(f"Skipping {game_data.event.event_ticker} (no pregame data)")
        return None

    # Step 2: Filter by pregame favorite threshold
    if pregame_prob <= config.pregame_favorite_threshold:
        logger.debug(
            f"Skipping {game_data.event.event_ticker} (pregame_prob={pregame_prob:.3f} "
            f"<= threshold={config.pregame_favorite_threshold})"
        )
        return None

    logger.info(
        f"Event {game_data.event.event_ticker} qualifies (pregame_prob={pregame_prob:.3f})"
    )

    # Step 3: Detect trigger (first cross below 50%)
    trigger_ts = detect_trigger_time(
        game_data, kickoff_ts, halftime_ts, trigger_threshold=config.trigger_threshold
    )
    if trigger_ts is None:
        logger.debug(f"No trigger for {game_data.event.event_ticker}")
        return None

    logger.info(f"Trigger detected at {trigger_ts} for {game_data.event.event_ticker}")

    # Step 4: Find fill trade
    fill_trade = find_fill_trade(game_data, trigger_ts, grace_sec=config.grace_sec_for_fill)
    if fill_trade is None:
        logger.debug(f"Unfillable (no trade within grace) for {game_data.event.event_ticker}")
        return None

    # Entry price: use trade price + slippage (conservative)
    entry_price_cents = fill_trade.yes_price + int(config.extra_slippage * 100)
    entry_prob = entry_price_cents / 100.0
    entry_ts = fill_trade.created_time

    logger.info(
        f"Entering at {entry_ts} (price={entry_price_cents} cents, prob={entry_prob:.3f})"
    )

    # Step 5: Simulate exit
    exit_result = simulate_exit(
        game_data=game_data,
        entry_ts=entry_ts,
        entry_price_cents=entry_price_cents,
        halftime_ts=halftime_ts,
        revert_bands=config.revert_bands,
        mae_stop_prob=config.mae_stop_prob,
        extra_slippage=config.extra_slippage,
        timeout_mode=config.timeout,
    )

    if exit_result is None:
        logger.warning(f"Failed to simulate exit for {game_data.event.event_ticker}")
        return None

    exit_ts, exit_price_cents, exit_prob, exit_reason, band_hit, exit_fill_source = exit_result

    # Step 6: Calculate P&L
    pnl_gross_cents = exit_price_cents - entry_price_cents
    fees_cents = int(config.per_contract_fee * 100) * 2  # Entry + exit
    slippage_cents = int(config.extra_slippage * 100) * 2  # Applied both sides
    pnl_net_cents = pnl_gross_cents - fees_cents

    # Step 7: Calculate risk metrics (MAE, MFE)
    mae, mfe = calculate_mae_mfe(
        game_data=game_data,
        entry_ts=entry_ts,
        exit_ts=exit_ts,
        entry_prob=entry_prob,
    )

    hold_time_sec = exit_ts - entry_ts

    logger.info(
        f"Exit at {exit_ts} (price={exit_price_cents} cents, prob={exit_prob:.3f}, "
        f"reason={exit_reason}, pnl_net={pnl_net_cents} cents)"
    )

    return EntryExit(
        event_ticker=game_data.event.event_ticker,
        favorite_side="yes",  # Assume we're always on the yes side (favorite)
        pregame_prob=pregame_prob,
        kickoff_ts=kickoff_ts,
        halftime_ts=halftime_ts,
        trigger_ts=trigger_ts,
        trigger_prob=config.trigger_threshold,
        entry_ts=entry_ts,
        entry_prob=entry_prob,
        entry_price_cents=entry_price_cents,
        entry_fill_source="trade_with_slippage",
        exit_ts=exit_ts,
        exit_prob=exit_prob,
        exit_price_cents=exit_price_cents,
        exit_fill_source=exit_fill_source,
        exit_reason=exit_reason,
        band_hit=band_hit,
        pnl_gross_cents=pnl_gross_cents,
        pnl_net_cents=pnl_net_cents,
        fees_paid_cents=fees_cents,
        slippage_cents=slippage_cents,
        mae=mae,
        mfe=mfe,
        max_drawdown_cents=None,
        hold_time_sec=hold_time_sec,
    )


def simulate_exit(
    game_data: GameData,
    entry_ts: int,
    entry_price_cents: int,
    halftime_ts: int,
    revert_bands: list[float],
    mae_stop_prob: Optional[float],
    extra_slippage: float,
    timeout_mode: Literal["halftime", "full"],
) -> Optional[tuple[int, int, float, Literal["revert_band", "timeout", "mae_stop"], Optional[float], str]]:
    """
    Simulate exit logic.

    Args:
        game_data: Game data.
        entry_ts: Entry timestamp.
        entry_price_cents: Entry price.
        halftime_ts: Halftime timestamp.
        revert_bands: List of reversion bands (e.g., [0.55, 0.60, 0.65, 0.70]).
        mae_stop_prob: Max adverse excursion stop (optional).
        extra_slippage: Slippage to apply at exit.
        timeout_mode: "halftime" or "full".

    Returns:
        Tuple of (exit_ts, exit_price_cents, exit_prob, exit_reason, band_hit, exit_fill_source)
        or None if no valid exit.
    """
    # Determine timeout
    if timeout_mode == "halftime":
        timeout_ts = halftime_ts
    else:
        # Full game timeout (assume 2 hours after kickoff)
        timeout_ts = halftime_ts + 6000  # ~100 minutes total

    # Sort bands ascending
    sorted_bands = sorted(revert_bands)

    # Get trades after entry
    exit_candidates = [t for t in game_data.trades if entry_ts < t.created_time <= timeout_ts]

    if not exit_candidates:
        logger.debug(f"No trades after entry; timeout exit at halftime")
        # Timeout: use last available price before timeout
        last_trade = [t for t in game_data.trades if t.created_time <= timeout_ts]
        if last_trade:
            last_price = last_trade[-1].yes_price - int(extra_slippage * 100)  # Exit at bid
            return (
                timeout_ts,
                max(0, last_price),
                last_price / 100.0,
                "timeout",
                None,
                "trade_with_slippage",
            )
        else:
            # No data at all
            return None

    # Check each trade for band hits or MAE stop
    for trade in exit_candidates:
        trade_prob = trade.yes_price / 100.0

        # Check MAE stop
        if mae_stop_prob is not None:
            entry_prob = entry_price_cents / 100.0
            if trade_prob < (entry_prob - mae_stop_prob):
                exit_price = trade.yes_price - int(extra_slippage * 100)
                return (
                    trade.created_time,
                    max(0, exit_price),
                    trade_prob,
                    "mae_stop",
                    None,
                    "trade_with_slippage",
                )

        # Check reversion bands
        for band in sorted_bands:
            if trade_prob >= band:
                exit_price = trade.yes_price - int(extra_slippage * 100)  # Conservative: bid
                return (
                    trade.created_time,
                    max(0, exit_price),
                    trade_prob,
                    "revert_band",
                    band,
                    "trade_with_slippage",
                )

    # If no band hit, timeout
    last_trade = exit_candidates[-1]
    exit_price = last_trade.yes_price - int(extra_slippage * 100)
    return (
        timeout_ts,
        max(0, exit_price),
        last_trade.yes_price / 100.0,
        "timeout",
        None,
        "trade_with_slippage",
    )


def calculate_mae_mfe(
    game_data: GameData,
    entry_ts: int,
    exit_ts: int,
    entry_prob: float,
) -> tuple[Optional[float], Optional[float]]:
    """
    Calculate Max Adverse Excursion and Max Favorable Excursion.

    Args:
        game_data: Game data.
        entry_ts: Entry timestamp.
        exit_ts: Exit timestamp.
        entry_prob: Entry probability.

    Returns:
        (MAE, MFE) as probability deltas.
    """
    hold_trades = [t for t in game_data.trades if entry_ts < t.created_time <= exit_ts]

    if not hold_trades:
        return None, None

    probs = [t.yes_price / 100.0 for t in hold_trades]
    min_prob = min(probs)
    max_prob = max(probs)

    mae = entry_prob - min_prob  # How far down it went (adverse)
    mfe = max_prob - entry_prob  # How far up it went (favorable)

    return mae, mfe


def run_backtest(
    game_data_list: list[GameData],
    config: BacktestConfig,
) -> tuple[list[EntryExit], BacktestSummary]:
    """
    Run full backtest across multiple games.

    Args:
        game_data_list: List of GameData objects.
        config: Backtest configuration.

    Returns:
        (list of EntryExit records, BacktestSummary).
    """
    logger.info(f"Starting backtest with {len(game_data_list)} games")

    trades: list[EntryExit] = []
    num_qualified = 0

    for game_data in game_data_list:
        # Attempt to simulate trade
        entry_exit = simulate_trade(game_data, config)

        # Track qualification
        if entry_exit or (
            compute_pregame_probability(game_data, game_data.event.strike_date or 0)
            and compute_pregame_probability(game_data, game_data.event.strike_date or 0)
            > config.pregame_favorite_threshold
        ):
            num_qualified += 1

        if entry_exit:
            trades.append(entry_exit)

    logger.info(
        f"Backtest complete: {len(trades)} trades from {num_qualified} qualified games "
        f"out of {len(game_data_list)} analyzed"
    )

    # Compute summary metrics
    summary = compute_summary(trades, config, len(game_data_list), num_qualified)

    return trades, summary


def compute_summary(
    trades: list[EntryExit],
    config: BacktestConfig,
    num_analyzed: int,
    num_qualified: int,
) -> BacktestSummary:
    """
    Compute backtest summary statistics.

    Args:
        trades: List of executed trades.
        config: Backtest configuration.
        num_analyzed: Total events analyzed.
        num_qualified: Events that met pregame threshold.

    Returns:
        BacktestSummary object.
    """
    if not trades:
        logger.warning("No trades executed; returning empty summary")
        return BacktestSummary(
            config=config,
            num_events_analyzed=num_analyzed,
            num_events_qualified=num_qualified,
            num_trades_triggered=0,
            num_trades_filled=0,
            total_pnl_gross_cents=0,
            total_pnl_net_cents=0,
            overall_win_rate=0.0,
            avg_hold_time_sec=0.0,
            band_metrics=[],
        )

    df = pd.DataFrame([t.model_dump() for t in trades])

    total_pnl_gross = df["pnl_gross_cents"].sum()
    total_pnl_net = df["pnl_net_cents"].sum()
    overall_win_rate = (df["pnl_net_cents"] > 0).mean()
    avg_hold_time = df["hold_time_sec"].mean()

    # Compute per-band metrics
    band_metrics = []
    for band in sorted(config.revert_bands):
        band_df = df[df["band_hit"] == band]
        n = len(band_df)

        if n == 0:
            # No hits for this band
            band_metrics.append(
                BandMetrics(
                    band=band,
                    num_trades=0,
                    hit_rate=0.0,
                    avg_pnl_cents=0.0,
                    median_pnl_cents=0.0,
                    std_pnl_cents=0.0,
                    win_pct=0.0,
                    total_pnl_cents=0.0,
                    sharpe_ratio=None,
                    ev_per_trade_cents=0.0,
                )
            )
            continue

        hit_rate = n / len(trades)
        avg_pnl = band_df["pnl_net_cents"].mean()
        median_pnl = band_df["pnl_net_cents"].median()
        std_pnl = band_df["pnl_net_cents"].std()
        win_pct = (band_df["pnl_net_cents"] > 0).mean()
        total_pnl = band_df["pnl_net_cents"].sum()

        # Sharpe ratio (simple)
        sharpe = avg_pnl / std_pnl if std_pnl > 0 else None

        # EV per trade (across all trades, not just band hits)
        ev_per_trade = total_pnl / len(trades)

        band_metrics.append(
            BandMetrics(
                band=band,
                num_trades=n,
                hit_rate=hit_rate,
                avg_pnl_cents=avg_pnl,
                median_pnl_cents=median_pnl,
                std_pnl_cents=std_pnl,
                win_pct=win_pct,
                total_pnl_cents=total_pnl,
                sharpe_ratio=sharpe,
                ev_per_trade_cents=ev_per_trade,
            )
        )

    return BacktestSummary(
        config=config,
        num_events_analyzed=num_analyzed,
        num_events_qualified=num_qualified,
        num_trades_triggered=len(trades),  # Triggered = filled in our case
        num_trades_filled=len(trades),
        total_pnl_gross_cents=int(total_pnl_gross),
        total_pnl_net_cents=int(total_pnl_net),
        overall_win_rate=overall_win_rate,
        avg_hold_time_sec=avg_hold_time,
        band_metrics=band_metrics,
    )

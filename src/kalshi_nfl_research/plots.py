"""
Plotting utilities for backtest visualizations.

Generates:
- Equity curves
- P&L distributions
- Example game timelines with entry/exit markers
"""

import logging
import random
from datetime import datetime
from pathlib import Path
from typing import Optional

import matplotlib.pyplot as plt
import numpy as np
import pandas as pd

from .data_models import EntryExit
from .fetch import GameData

logger = logging.getLogger(__name__)


def plot_equity_curve(trades: list[EntryExit], output_dir: Path) -> Path:
    """
    Plot cumulative P&L over time (equity curve).

    Args:
        trades: List of trades.
        output_dir: Output directory.

    Returns:
        Path to saved plot.
    """
    if not trades:
        logger.warning("No trades to plot equity curve")
        return output_dir / "equity_curve.png"

    df = pd.DataFrame([t.model_dump() for t in trades])
    df = df.sort_values("exit_ts")
    df["cumulative_pnl_cents"] = df["pnl_net_cents"].cumsum()
    df["exit_dt"] = pd.to_datetime(df["exit_ts"], unit="s", utc=True)

    fig, ax = plt.subplots(figsize=(12, 6))
    ax.plot(df["exit_dt"], df["cumulative_pnl_cents"] / 100, marker="o", linewidth=2)
    ax.axhline(0, color="red", linestyle="--", alpha=0.5)
    ax.set_xlabel("Exit Time (UTC)", fontsize=12)
    ax.set_ylabel("Cumulative P&L (Dollars)", fontsize=12)
    ax.set_title("Equity Curve (Net P&L)", fontsize=14, fontweight="bold")
    ax.grid(True, alpha=0.3)
    plt.xticks(rotation=45)
    plt.tight_layout()

    output_path = output_dir / "equity_curve.png"
    plt.savefig(output_path, dpi=150)
    plt.close()
    logger.info(f"Saved equity curve to {output_path}")

    return output_path


def plot_pnl_distribution(trades: list[EntryExit], output_dir: Path) -> Path:
    """
    Plot histogram of P&L distribution.

    Args:
        trades: List of trades.
        output_dir: Output directory.

    Returns:
        Path to saved plot.
    """
    if not trades:
        logger.warning("No trades to plot P&L distribution")
        return output_dir / "pnl_distribution.png"

    df = pd.DataFrame([t.model_dump() for t in trades])
    pnl_dollars = df["pnl_net_cents"] / 100

    fig, ax = plt.subplots(figsize=(10, 6))
    ax.hist(pnl_dollars, bins=30, edgecolor="black", alpha=0.7)
    ax.axvline(pnl_dollars.mean(), color="red", linestyle="--", linewidth=2, label=f"Mean: ${pnl_dollars.mean():.2f}")
    ax.axvline(pnl_dollars.median(), color="green", linestyle="--", linewidth=2, label=f"Median: ${pnl_dollars.median():.2f}")
    ax.set_xlabel("Net P&L (Dollars)", fontsize=12)
    ax.set_ylabel("Frequency", fontsize=12)
    ax.set_title("P&L Distribution", fontsize=14, fontweight="bold")
    ax.legend()
    ax.grid(True, alpha=0.3, axis="y")
    plt.tight_layout()

    output_path = output_dir / "pnl_distribution.png"
    plt.savefig(output_path, dpi=150)
    plt.close()
    logger.info(f"Saved P&L distribution to {output_path}")

    return output_path


def plot_game_timeline(
    game_data: GameData,
    entry_exit: Optional[EntryExit],
    output_dir: Path,
    filename: str,
) -> Path:
    """
    Plot price action timeline for a single game with entry/exit markers.

    Args:
        game_data: Game data.
        entry_exit: Trade entry/exit info (if traded).
        output_dir: Output directory.
        filename: Output filename.

    Returns:
        Path to saved plot.
    """
    if not game_data.trades:
        logger.warning(f"No trades to plot for {game_data.event.event_ticker}")
        return output_dir / filename

    # Build time series from trades
    df = pd.DataFrame([{"ts": t.created_time, "prob": t.yes_price / 100.0} for t in game_data.trades])
    df["dt"] = pd.to_datetime(df["ts"], unit="s", utc=True)

    fig, ax = plt.subplots(figsize=(14, 7))
    ax.plot(df["dt"], df["prob"], marker=".", markersize=4, linewidth=1, label="Market Price")

    # Mark kickoff
    if game_data.event.strike_date:
        kickoff_dt = pd.to_datetime(game_data.event.strike_date, unit="s", utc=True)
        ax.axvline(kickoff_dt, color="blue", linestyle="--", linewidth=1.5, label="Kickoff", alpha=0.7)

    # Mark halftime
    if game_data.event.strike_date:
        halftime_dt = pd.to_datetime(game_data.event.strike_date + 1800, unit="s", utc=True)
        ax.axvline(halftime_dt, color="orange", linestyle="--", linewidth=1.5, label="Halftime", alpha=0.7)

    # Mark trigger threshold
    ax.axhline(0.50, color="red", linestyle=":", linewidth=1, label="Trigger (50%)", alpha=0.5)

    # Mark entry/exit if traded
    if entry_exit:
        entry_dt = pd.to_datetime(entry_exit.entry_ts, unit="s", utc=True)
        exit_dt = pd.to_datetime(entry_exit.exit_ts, unit="s", utc=True)

        ax.scatter([entry_dt], [entry_exit.entry_prob], color="green", s=200, zorder=5, marker="^", label="Entry", edgecolors="black", linewidths=1.5)
        ax.scatter([exit_dt], [entry_exit.exit_prob], color="red", s=200, zorder=5, marker="v", label="Exit", edgecolors="black", linewidths=1.5)

        # Annotate P&L
        pnl_text = f"P&L: {entry_exit.pnl_net_cents}¢"
        ax.text(
            exit_dt,
            entry_exit.exit_prob + 0.02,
            pnl_text,
            fontsize=10,
            color="black",
            bbox=dict(facecolor="white", alpha=0.8, boxstyle="round,pad=0.3"),
        )

    ax.set_xlabel("Time (UTC)", fontsize=12)
    ax.set_ylabel("Implied Probability", fontsize=12)
    ax.set_title(f"Game Timeline: {game_data.event.event_ticker}", fontsize=14, fontweight="bold")
    ax.set_ylim(0, 1)
    ax.legend(loc="best")
    ax.grid(True, alpha=0.3)
    plt.xticks(rotation=45)
    plt.tight_layout()

    output_path = output_dir / filename
    plt.savefig(output_path, dpi=150)
    plt.close()
    logger.info(f"Saved game timeline to {output_path}")

    return output_path


def plot_sample_games(
    game_data_list: list[GameData],
    trades: list[EntryExit],
    output_dir: Path,
    num_samples: int = 3,
) -> list[Path]:
    """
    Plot timelines for a random sample of games.

    Args:
        game_data_list: List of game data.
        trades: List of trades.
        output_dir: Output directory.
        num_samples: Number of samples to plot.

    Returns:
        List of paths to saved plots.
    """
    # Create mapping of event_ticker -> trade
    trade_map = {t.event_ticker: t for t in trades}

    # Filter games that had trades
    traded_games = [gd for gd in game_data_list if gd.event.event_ticker in trade_map]

    if not traded_games:
        logger.warning("No traded games to plot")
        return []

    # Sample randomly
    sample_size = min(num_samples, len(traded_games))
    sampled_games = random.sample(traded_games, sample_size)

    paths = []
    for i, game_data in enumerate(sampled_games, 1):
        entry_exit = trade_map.get(game_data.event.event_ticker)
        filename = f"sample_game_{i}_{game_data.event.event_ticker}.png"
        path = plot_game_timeline(game_data, entry_exit, output_dir, filename)
        paths.append(path)

    return paths


def plot_mae_mfe_scatter(trades: list[EntryExit], output_dir: Path) -> Path:
    """
    Plot MAE vs MFE scatter to analyze drawdown/runup patterns.

    Args:
        trades: List of trades.
        output_dir: Output directory.

    Returns:
        Path to saved plot.
    """
    if not trades:
        logger.warning("No trades to plot MAE/MFE")
        return output_dir / "mae_mfe_scatter.png"

    df = pd.DataFrame([t.model_dump() for t in trades])
    df = df.dropna(subset=["mae", "mfe"])

    if df.empty:
        logger.warning("No MAE/MFE data available")
        return output_dir / "mae_mfe_scatter.png"

    fig, ax = plt.subplots(figsize=(10, 8))

    # Color by win/loss
    colors = ["green" if pnl > 0 else "red" for pnl in df["pnl_net_cents"]]

    ax.scatter(df["mae"], df["mfe"], c=colors, alpha=0.6, s=50, edgecolors="black", linewidths=0.5)
    ax.set_xlabel("Max Adverse Excursion (MAE)", fontsize=12)
    ax.set_ylabel("Max Favorable Excursion (MFE)", fontsize=12)
    ax.set_title("MAE vs MFE (Green=Win, Red=Loss)", fontsize=14, fontweight="bold")
    ax.grid(True, alpha=0.3)
    plt.tight_layout()

    output_path = output_dir / "mae_mfe_scatter.png"
    plt.savefig(output_path, dpi=150)
    plt.close()
    logger.info(f"Saved MAE/MFE scatter to {output_path}")

    return output_path


def generate_all_plots(
    trades: list[EntryExit],
    game_data_list: list[GameData],
    output_dir: Path,
) -> None:
    """
    Generate all standard plots.

    Args:
        trades: List of trades.
        game_data_list: List of game data.
        output_dir: Output directory.
    """
    logger.info("Generating plots...")

    plot_equity_curve(trades, output_dir)
    plot_pnl_distribution(trades, output_dir)
    plot_mae_mfe_scatter(trades, output_dir)
    plot_sample_games(game_data_list, trades, output_dir, num_samples=3)

    logger.info("All plots generated successfully")

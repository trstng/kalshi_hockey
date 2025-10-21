"""
I/O utilities for saving backtest results to CSV, Parquet, and markdown.
"""

import logging
from datetime import datetime
from pathlib import Path
from typing import Any

import pandas as pd

from .data_models import BacktestSummary, EntryExit

logger = logging.getLogger(__name__)


def create_output_dir(base_dir: str = "./artifacts") -> Path:
    """
    Create timestamped output directory.

    Args:
        base_dir: Base artifacts directory.

    Returns:
        Path to created directory.
    """
    timestamp = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
    output_dir = Path(base_dir) / timestamp
    output_dir.mkdir(parents=True, exist_ok=True)
    logger.info(f"Created output directory: {output_dir}")
    return output_dir


def save_trades_csv(trades: list[EntryExit], output_dir: Path) -> Path:
    """
    Save trade-level results to CSV.

    Args:
        trades: List of EntryExit records.
        output_dir: Output directory.

    Returns:
        Path to saved CSV file.
    """
    if not trades:
        logger.warning("No trades to save")
        return output_dir / "trades.csv"

    df = pd.DataFrame([t.model_dump() for t in trades])

    # Convert timestamps to human-readable
    for col in ["kickoff_ts", "halftime_ts", "trigger_ts", "entry_ts", "exit_ts"]:
        if col in df.columns:
            df[f"{col}_utc"] = pd.to_datetime(df[col], unit="s", utc=True)

    # Reorder columns for readability
    priority_cols = [
        "event_ticker",
        "entry_ts_utc",
        "entry_prob",
        "entry_price_cents",
        "exit_ts_utc",
        "exit_prob",
        "exit_price_cents",
        "exit_reason",
        "band_hit",
        "pnl_gross_cents",
        "pnl_net_cents",
        "hold_time_sec",
        "mae",
        "mfe",
    ]
    remaining_cols = [c for c in df.columns if c not in priority_cols]
    ordered_cols = [c for c in priority_cols if c in df.columns] + remaining_cols
    df = df[ordered_cols]

    output_path = output_dir / "trades.csv"
    df.to_csv(output_path, index=False)
    logger.info(f"Saved {len(df)} trades to {output_path}")

    return output_path


def save_by_event_csv(trades: list[EntryExit], output_dir: Path) -> Path:
    """
    Save event-level aggregates to CSV.

    Args:
        trades: List of EntryExit records.
        output_dir: Output directory.

    Returns:
        Path to saved CSV file.
    """
    if not trades:
        logger.warning("No trades to aggregate by event")
        return output_dir / "by_event.csv"

    df = pd.DataFrame([t.model_dump() for t in trades])

    # Aggregate by event
    event_agg = df.groupby("event_ticker").agg(
        {
            "pregame_prob": "first",
            "kickoff_ts": "first",
            "entry_ts": "first",
            "exit_ts": "first",
            "pnl_gross_cents": "sum",
            "pnl_net_cents": "sum",
            "hold_time_sec": "mean",
            "mae": "max",
            "mfe": "max",
        }
    ).reset_index()

    event_agg["kickoff_utc"] = pd.to_datetime(event_agg["kickoff_ts"], unit="s", utc=True)

    output_path = output_dir / "by_event.csv"
    event_agg.to_csv(output_path, index=False)
    logger.info(f"Saved event aggregates to {output_path}")

    return output_path


def save_band_metrics_csv(summary: BacktestSummary, output_dir: Path) -> Path:
    """
    Save per-band metrics to CSV.

    Args:
        summary: Backtest summary.
        output_dir: Output directory.

    Returns:
        Path to saved CSV file.
    """
    if not summary.band_metrics:
        logger.warning("No band metrics to save")
        return output_dir / "band_metrics.csv"

    df = pd.DataFrame([m.model_dump() for m in summary.band_metrics])

    output_path = output_dir / "band_metrics.csv"
    df.to_csv(output_path, index=False)
    logger.info(f"Saved band metrics to {output_path}")

    return output_path


def save_summary_markdown(
    summary: BacktestSummary,
    trades: list[EntryExit],
    output_dir: Path,
    command_line: str = "",
) -> Path:
    """
    Generate human-readable summary markdown.

    Args:
        summary: Backtest summary.
        trades: List of trades.
        output_dir: Output directory.
        command_line: Command line used to run backtest.

    Returns:
        Path to saved markdown file.
    """
    md_lines = [
        "# Kalshi NFL Backtest Summary",
        "",
        f"**Generated**: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
        "",
        "## Configuration",
        "",
        f"- **Date Range**: {summary.config.start_date} to {summary.config.end_date}",
        f"- **Pregame Favorite Threshold**: {summary.config.pregame_favorite_threshold:.1%}",
        f"- **Trigger Threshold**: {summary.config.trigger_threshold:.1%}",
        f"- **Revert Bands**: {summary.config.revert_bands}",
        f"- **Per-Contract Fee**: ${summary.config.per_contract_fee:.2f}",
        f"- **Extra Slippage**: ${summary.config.extra_slippage:.3f}",
        f"- **Timeout Mode**: {summary.config.timeout}",
        f"- **Grace Period**: {summary.config.grace_sec_for_fill}s",
        "",
        "## Command Line",
        "",
        f"```bash",
        f"{command_line}",
        f"```",
        "",
        "## Overall Results",
        "",
        f"- **Events Analyzed**: {summary.num_events_analyzed}",
        f"- **Events Qualified** (pregame favorite > {summary.config.pregame_favorite_threshold:.0%}): {summary.num_events_qualified}",
        f"- **Trades Triggered**: {summary.num_trades_triggered}",
        f"- **Trades Filled**: {summary.num_trades_filled}",
        "",
        f"- **Total P&L (Gross)**: {summary.total_pnl_gross_cents / 100:.2f} cents",
        f"- **Total P&L (Net)**: {summary.total_pnl_net_cents / 100:.2f} cents",
        f"- **Overall Win Rate**: {summary.overall_win_rate:.1%}",
        f"- **Avg Hold Time**: {summary.avg_hold_time_sec / 60:.1f} minutes",
        "",
        "## Per-Band Metrics",
        "",
        "| Band | Num Trades | Hit Rate | Avg P&L (¢) | Median P&L (¢) | Std (¢) | Win % | Total P&L (¢) | Sharpe | EV/Trade (¢) |",
        "|------|-----------|----------|-------------|----------------|---------|-------|---------------|--------|--------------|",
    ]

    for band_metric in summary.band_metrics:
        sharpe_str = f"{band_metric.sharpe_ratio:.2f}" if band_metric.sharpe_ratio else "N/A"
        md_lines.append(
            f"| {band_metric.band:.2f} | {band_metric.num_trades} | "
            f"{band_metric.hit_rate:.1%} | {band_metric.avg_pnl_cents:.2f} | "
            f"{band_metric.median_pnl_cents:.2f} | {band_metric.std_pnl_cents:.2f} | "
            f"{band_metric.win_pct:.1%} | {band_metric.total_pnl_cents:.2f} | "
            f"{sharpe_str} | {band_metric.ev_per_trade_cents:.2f} |"
        )

    md_lines.extend([
        "",
        "## Sample Trades",
        "",
    ])

    # Show first 5 trades
    if trades:
        sample_trades = trades[:5]
        for i, trade in enumerate(sample_trades, 1):
            entry_dt = datetime.fromtimestamp(trade.entry_ts).strftime("%Y-%m-%d %H:%M:%S")
            exit_dt = datetime.fromtimestamp(trade.exit_ts).strftime("%Y-%m-%d %H:%M:%S")
            md_lines.extend([
                f"### Trade {i}: {trade.event_ticker}",
                "",
                f"- **Entry**: {entry_dt} UTC @ {trade.entry_prob:.1%} ({trade.entry_price_cents}¢)",
                f"- **Exit**: {exit_dt} UTC @ {trade.exit_prob:.1%} ({trade.exit_price_cents}¢)",
                f"- **Exit Reason**: {trade.exit_reason} (band={trade.band_hit})",
                f"- **P&L (Net)**: {trade.pnl_net_cents}¢",
                f"- **Hold Time**: {trade.hold_time_sec / 60:.1f} min",
                f"- **MAE/MFE**: {trade.mae:.2%} / {trade.mfe:.2%}" if trade.mae and trade.mfe else "",
                "",
            ])

    md_lines.extend([
        "## Caveats & Limitations",
        "",
        "- **Fill Model**: Conservative (entry at ask + slippage, exit at bid - slippage)",
        "- **Data Quality**: Assumes API data is complete and accurate",
        "- **Slippage**: Fixed slippage may underestimate real execution costs in thin markets",
        "- **Survivorship Bias**: Only includes games with available market data",
        "- **No Look-Ahead**: Strategy uses only data available at decision time",
        "",
        "## Next Steps",
        "",
        "- Analyze MAE/MFE distributions to optimize stop placement",
        "- Test sensitivity to entry/exit thresholds",
        "- Consider Kelly sizing based on EV estimates",
        "- Explore full-game timeout vs halftime exits",
        "- Add live execution simulation with orderbook depth",
        "",
    ])

    output_path = output_dir / "summary.md"
    with open(output_path, "w") as f:
        f.write("\n".join(md_lines))

    logger.info(f"Saved summary markdown to {output_path}")
    return output_path


def save_parquet(trades: list[EntryExit], output_dir: Path) -> Path:
    """
    Save trades to Parquet format for efficient storage.

    Args:
        trades: List of trades.
        output_dir: Output directory.

    Returns:
        Path to saved Parquet file.
    """
    if not trades:
        logger.warning("No trades to save to Parquet")
        return output_dir / "trades.parquet"

    df = pd.DataFrame([t.model_dump() for t in trades])

    output_path = output_dir / "trades.parquet"
    df.to_parquet(output_path, index=False)
    logger.info(f"Saved {len(df)} trades to {output_path}")

    return output_path

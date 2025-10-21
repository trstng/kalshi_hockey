"""
CLI interface for Kalshi NFL research toolchain.

Commands:
- discover-nfl: Discover NFL events and markets
- pull-game: Fetch data for a specific game
- backtest: Run full backtest
"""

import logging
import sys
from pathlib import Path
from typing import Optional

import click
import yaml

from .backtest import run_backtest
from .data_models import BacktestConfig
from .discovery import discover_games_with_markets, discover_nfl_series
from .fetch import fetch_game_data
from .io_utils import (
    create_output_dir,
    save_band_metrics_csv,
    save_by_event_csv,
    save_parquet,
    save_summary_markdown,
    save_trades_csv,
)
from .kalshi_client import KalshiClient
from .plots import generate_all_plots

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
)
logger = logging.getLogger(__name__)


def load_config(config_path: str = "config.yaml") -> dict:
    """Load configuration from YAML file."""
    try:
        with open(config_path, "r") as f:
            config = yaml.safe_load(f)
        logger.info(f"Loaded configuration from {config_path}")
        return config
    except FileNotFoundError:
        logger.warning(f"Config file {config_path} not found; using defaults")
        return {}


@click.group()
def cli():
    """Kalshi NFL Research CLI - Production-ready backtesting toolchain."""
    pass


@cli.command("discover-nfl")
@click.option("--out", default="./artifacts/discovered_events.csv", help="Output CSV path")
@click.option("--series", default=None, help="Specific series ticker (e.g., NFL-2024)")
@click.option("--start-date", default=None, help="Start date YYYY-MM-DD")
@click.option("--end-date", default=None, help="End date YYYY-MM-DD")
@click.option("--config", default="config.yaml", help="Config file path")
def discover_nfl(out: str, series: Optional[str], start_date: Optional[str], end_date: Optional[str], config: str):
    """
    Discover NFL events and WIN markets, save to CSV.
    """
    logger.info("Starting NFL discovery...")

    # Load config
    cfg = load_config(config)
    kalshi_base = cfg.get("kalshi_base", "https://api.elections.kalshi.com/trade-api/v2")
    rate_limit_ms = cfg.get("rate_limit_sleep_ms", 200)

    # Initialize client
    client = KalshiClient(base_url=kalshi_base, rate_limit_sleep_ms=rate_limit_ms)

    try:
        # Discover series if not specified
        if not series:
            logger.info("Discovering NFL series...")
            nfl_series = discover_nfl_series(client)
            if nfl_series:
                series = nfl_series[0].series_ticker
                logger.info(f"Using series: {series}")
            else:
                logger.error("No NFL series found")
                return

        # Discover games with markets
        games_with_markets = discover_games_with_markets(
            client,
            series_ticker=series,
            start_date=start_date,
            end_date=end_date,
        )

        if not games_with_markets:
            logger.warning("No games with WIN markets found")
            return

        # Prepare output
        import pandas as pd

        rows = []
        for event, market in games_with_markets:
            rows.append({
                "event_ticker": event.event_ticker,
                "series_ticker": event.series_ticker,
                "title": event.title,
                "strike_date": event.strike_date,
                "market_ticker": market.ticker,
                "market_title": market.title,
            })

        df = pd.DataFrame(rows)

        # Ensure output directory exists
        output_path = Path(out)
        output_path.parent.mkdir(parents=True, exist_ok=True)

        df.to_csv(output_path, index=False)
        logger.info(f"Discovered {len(df)} games; saved to {output_path}")

    finally:
        client.close()


@cli.command("pull-game")
@click.option("--event", required=True, help="Event ticker (e.g., GAME-NFL-2024-SEA-SF)")
@click.option("--series", default=None, help="Series ticker (e.g., NFL-2024)")
@click.option("--out", default="./artifacts", help="Output directory")
@click.option("--config", default="config.yaml", help="Config file path")
def pull_game(event: str, series: Optional[str], out: str, config: str):
    """
    Pull candles, trades, and orderbook for a specific game.
    """
    logger.info(f"Pulling data for event: {event}")

    # Load config
    cfg = load_config(config)
    kalshi_base = cfg.get("kalshi_base", "https://api.elections.kalshi.com/trade-api/v2")
    rate_limit_ms = cfg.get("rate_limit_sleep_ms", 200)

    # Initialize client
    client = KalshiClient(base_url=kalshi_base, rate_limit_sleep_ms=rate_limit_ms)

    try:
        # Discover event and market
        from .discovery import find_win_market
        from .data_models import EventInfo

        # Fetch event details
        events = client.get_events()
        event_data = next((e for e in events if e.get("event_ticker") == event), None)

        if not event_data:
            logger.error(f"Event {event} not found")
            return

        event_info = EventInfo(
            event_ticker=event_data["event_ticker"],
            series_ticker=event_data.get("series_ticker", series or ""),
            title=event_data.get("title", ""),
            strike_date=event_data.get("strike_date"),
        )

        # Find WIN market
        market = find_win_market(client, event)
        if not market:
            logger.error(f"No WIN market found for {event}")
            return

        # Fetch game data
        game_data = fetch_game_data(
            client,
            event_info,
            market,
            pregame_window_sec=cfg.get("pregame_window_sec", 900),
            first_half_sec=cfg.get("first_half_sec", 1800),
            candle_interval=cfg.get("candle_interval", "1m"),
            fetch_orderbook=True,
        )

        if not game_data:
            logger.warning(f"No data available for {event}")
            return

        # Save outputs
        import pandas as pd

        output_dir = Path(out) / event
        output_dir.mkdir(parents=True, exist_ok=True)

        # Save candles
        if game_data.candles:
            candles_df = pd.DataFrame([c.model_dump() for c in game_data.candles])
            candles_df.to_csv(output_dir / "candles.csv", index=False)
            logger.info(f"Saved {len(candles_df)} candles")

        # Save trades
        if game_data.trades:
            trades_df = pd.DataFrame([t.model_dump() for t in game_data.trades])
            trades_df.to_csv(output_dir / "trades.csv", index=False)
            logger.info(f"Saved {len(trades_df)} trades")

        # Save orderbook snapshot
        if game_data.orderbook:
            ob_df = pd.DataFrame([game_data.orderbook.model_dump()])
            ob_df.to_csv(output_dir / "orderbook.csv", index=False)
            logger.info(f"Saved orderbook snapshot")

        logger.info(f"Data saved to {output_dir}")

    finally:
        client.close()


@cli.command("backtest")
@click.option("--from", "start_date", required=True, help="Start date YYYY-MM-DD")
@click.option("--to", "end_date", required=True, help="End date YYYY-MM-DD")
@click.option("--series", default=None, help="Series ticker (e.g., NFL-2024)")
@click.option("--revert-bands", default=None, help="Comma-separated revert bands (e.g., 0.55,0.60,0.65,0.70)")
@click.option("--fees", type=float, default=None, help="Per-contract fee (dollars)")
@click.option("--slippage", type=float, default=None, help="Extra slippage (dollars)")
@click.option("--timeout", default=None, help="Timeout mode: halftime or full")
@click.option("--grace-sec", type=int, default=None, help="Grace period for fills (seconds)")
@click.option("--mae-stop", type=float, default=None, help="MAE stop threshold (e.g., 0.12)")
@click.option("--config", default="config.yaml", help="Config file path")
@click.option("--out", default="./artifacts", help="Output directory")
def backtest(
    start_date: str,
    end_date: str,
    series: Optional[str],
    revert_bands: Optional[str],
    fees: Optional[float],
    slippage: Optional[float],
    timeout: Optional[str],
    grace_sec: Optional[int],
    mae_stop: Optional[float],
    config: str,
    out: str,
):
    """
    Run full backtest across date range.
    """
    logger.info(f"Starting backtest: {start_date} to {end_date}")

    # Load config
    cfg = load_config(config)

    # Override with CLI args
    kalshi_base = cfg.get("kalshi_base", "https://api.elections.kalshi.com/trade-api/v2")
    rate_limit_ms = cfg.get("rate_limit_sleep_ms", 200)
    pregame_threshold = cfg.get("pregame_favorite_threshold", 0.60)
    trigger_threshold = cfg.get("trigger_threshold", 0.50)

    if revert_bands:
        bands = [float(b.strip()) for b in revert_bands.split(",")]
    else:
        bands = cfg.get("revert_bands", [0.55, 0.60, 0.65, 0.70])

    per_contract_fee = fees if fees is not None else cfg.get("per_contract_fee", 0.01)
    extra_slippage = slippage if slippage is not None else cfg.get("extra_slippage", 0.005)
    timeout_mode = timeout or cfg.get("timeout", "halftime")
    grace_period = grace_sec if grace_sec is not None else cfg.get("grace_sec_for_fill", 15)
    mae_stop_prob = mae_stop or cfg.get("mae_stop_prob")

    # Build config
    backtest_config = BacktestConfig(
        kalshi_base=kalshi_base,
        start_date=start_date,
        end_date=end_date,
        pregame_favorite_threshold=pregame_threshold,
        trigger_threshold=trigger_threshold,
        revert_bands=bands,
        per_contract_fee=per_contract_fee,
        extra_slippage=extra_slippage,
        mae_stop_prob=mae_stop_prob,
        timeout=timeout_mode,
        grace_sec_for_fill=grace_period,
        rate_limit_sleep_ms=rate_limit_ms,
    )

    logger.info(f"Config: {backtest_config.model_dump()}")

    # Initialize client
    client = KalshiClient(base_url=kalshi_base, rate_limit_sleep_ms=rate_limit_ms)

    try:
        # Discover games
        logger.info("Discovering NFL games...")
        games_with_markets = discover_games_with_markets(
            client,
            series_ticker=series,
            start_date=start_date,
            end_date=end_date,
        )

        if not games_with_markets:
            logger.error("No games found in date range")
            return

        logger.info(f"Found {len(games_with_markets)} games")

        # Fetch data for each game
        logger.info("Fetching game data...")
        game_data_list = []
        for event, market in games_with_markets:
            game_data = fetch_game_data(
                client,
                event,
                market,
                pregame_window_sec=cfg.get("pregame_window_sec", 900),
                first_half_sec=cfg.get("first_half_sec", 1800),
                candle_interval=cfg.get("candle_interval", "1m"),
            )
            if game_data:
                game_data_list.append(game_data)

        logger.info(f"Fetched data for {len(game_data_list)} games")

        # Run backtest
        logger.info("Running backtest...")
        trades, summary = run_backtest(game_data_list, backtest_config)

        # Create output directory
        output_dir = create_output_dir(base_dir=out)

        # Save results
        logger.info("Saving results...")
        save_trades_csv(trades, output_dir)
        save_by_event_csv(trades, output_dir)
        save_band_metrics_csv(summary, output_dir)
        save_parquet(trades, output_dir)

        # Build command line string for summary
        command_line = f"python -m kalshi_nfl_research backtest --from {start_date} --to {end_date}"
        if series:
            command_line += f" --series {series}"
        if revert_bands:
            command_line += f" --revert-bands {revert_bands}"

        save_summary_markdown(summary, trades, output_dir, command_line=command_line)

        # Generate plots
        logger.info("Generating plots...")
        generate_all_plots(trades, game_data_list, output_dir)

        logger.info(f"Backtest complete! Results saved to {output_dir}")

        # Print summary
        print("\n" + "=" * 60)
        print("BACKTEST SUMMARY")
        print("=" * 60)
        print(f"Events Analyzed:     {summary.num_events_analyzed}")
        print(f"Events Qualified:    {summary.num_events_qualified}")
        print(f"Trades Executed:     {summary.num_trades_filled}")
        print(f"Total P&L (Net):     ${summary.total_pnl_net_cents / 100:.2f}")
        print(f"Win Rate:            {summary.overall_win_rate:.1%}")
        print(f"Avg Hold Time:       {summary.avg_hold_time_sec / 60:.1f} min")
        print("=" * 60)
        print(f"\nDetailed results: {output_dir}")

    finally:
        client.close()


@cli.command("__main__")
def main_entry():
    """
    Entry point when module is run as python -m kalshi_nfl_research.
    """
    cli()


if __name__ == "__main__":
    cli()

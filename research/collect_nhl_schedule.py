#!/usr/bin/env python3
"""
Collect NHL game schedule and results from the official NHL API.

This script fetches:
- Game dates and puck drop times
- Team matchups (home/away)
- Final scores (for completed games)
- Game IDs for matching with Kalshi markets

API: https://api-web.nhle.com/v1/schedule/{date}

Usage:
    python collect_nhl_schedule.py --start-date 2025-09-01 --end-date 2025-10-21
"""

import argparse
import requests
import pandas as pd
from datetime import datetime, timedelta
from pathlib import Path
import time


def get_nhl_schedule(date_str):
    """
    Fetch NHL schedule for a specific date.

    Args:
        date_str: Date in YYYY-MM-DD format

    Returns:
        List of game dictionaries
    """
    url = f"https://api-web.nhle.com/v1/schedule/{date_str}"

    try:
        response = requests.get(url, timeout=10)
        response.raise_for_status()
        data = response.json()

        games = []

        # Extract games from game week
        if 'gameWeek' in data:
            for day in data['gameWeek']:
                if 'games' in day:
                    for game in day['games']:
                        games.append({
                            'game_id': game['id'],
                            'date': day['date'],
                            'start_time_utc': game['startTimeUTC'],
                            'away_team': game['awayTeam']['abbrev'],
                            'away_team_name': game['awayTeam']['placeName']['default'],
                            'home_team': game['homeTeam']['abbrev'],
                            'home_team_name': game['homeTeam']['placeName']['default'],
                            'away_score': game['awayTeam'].get('score'),
                            'home_score': game['homeTeam'].get('score'),
                            'game_state': game['gameState'],
                            'venue': game['venue']['default'],
                        })

        return games

    except requests.exceptions.RequestException as e:
        print(f"Error fetching schedule for {date_str}: {e}")
        return []


def collect_schedule_range(start_date, end_date, output_dir='../data'):
    """
    Collect NHL schedule for a date range.

    Args:
        start_date: Start date (datetime.date)
        end_date: End date (datetime.date)
        output_dir: Directory to save CSV
    """
    print(f"Collecting NHL schedule from {start_date} to {end_date}...")

    all_games = []
    current_date = start_date

    while current_date <= end_date:
        date_str = current_date.strftime('%Y-%m-%d')
        print(f"  Fetching {date_str}...")

        games = get_nhl_schedule(date_str)
        all_games.extend(games)

        # Rate limiting - be nice to the API
        time.sleep(0.5)

        current_date += timedelta(days=1)

    print(f"\n✓ Collected {len(all_games)} games")

    # Convert to DataFrame
    df = pd.DataFrame(all_games)

    if len(df) > 0:
        # Parse start time to datetime
        df['start_time_utc'] = pd.to_datetime(df['start_time_utc'])

        # Sort by date and time
        df = df.sort_values(['date', 'start_time_utc'])

        # Create output directory
        Path(output_dir).mkdir(parents=True, exist_ok=True)

        # Save to CSV
        output_file = f"{output_dir}/nhl_schedule.csv"
        df.to_csv(output_file, index=False)
        print(f"✓ Saved to {output_file}")

        # Print summary
        print("\n" + "="*80)
        print("SUMMARY")
        print("="*80)
        print(f"\nTotal games: {len(df)}")
        print(f"Date range: {df['date'].min()} to {df['date'].max()}")

        # Count by game state
        print("\nGame states:")
        print(df['game_state'].value_counts())

        # Count completed games
        completed = df[df['game_state'].isin(['OFF', 'FINAL'])]
        print(f"\nCompleted games: {len(completed)}")

        # Show sample
        print("\nSample games:")
        print(df[['date', 'start_time_utc', 'away_team', 'home_team', 'away_score', 'home_score']].head(10).to_string(index=False))

    else:
        print("\n⚠️  No games found in date range")

    return df


def main():
    parser = argparse.ArgumentParser(description='Collect NHL schedule data')
    parser.add_argument('--start-date', type=str, required=True,
                       help='Start date (YYYY-MM-DD)')
    parser.add_argument('--end-date', type=str, required=True,
                       help='End date (YYYY-MM-DD)')
    parser.add_argument('--output-dir', type=str, default='../data',
                       help='Output directory for CSV')
    args = parser.parse_args()

    # Parse dates
    start_date = datetime.strptime(args.start_date, '%Y-%m-%d').date()
    end_date = datetime.strptime(args.end_date, '%Y-%m-%d').date()

    print("="*80)
    print("NHL SCHEDULE COLLECTOR")
    print("="*80)
    print()

    # Collect schedule
    df = collect_schedule_range(start_date, end_date, args.output_dir)

    print("\n" + "="*80)
    print("COLLECTION COMPLETE")
    print("="*80)


if __name__ == "__main__":
    main()

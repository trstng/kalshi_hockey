#!/usr/bin/env python3
"""
Merge Kalshi NHL markets with actual NHL game results.

This script:
1. Loads Kalshi market data and NHL schedule data
2. Matches markets to games by date and teams
3. Determines market outcomes (YES/NO) based on actual winners
4. Calculates market accuracy and profitability metrics

Usage:
    python merge_data.py
"""

import pandas as pd
import numpy as np
from datetime import datetime
from pathlib import Path


def normalize_team_name(team):
    """
    Normalize team abbreviations for matching.

    Kalshi uses 2-letter abbreviations for some teams, while NHL API uses 3-letter codes.
    """
    # Map Kalshi's 2-letter codes to NHL API's 3-letter codes
    mappings = {
        # 2-letter Kalshi codes → 3-letter NHL codes
        'SJ': 'SJS',   # San Jose Sharks
        'LA': 'LAK',   # Los Angeles Kings
        'TB': 'TBL',   # Tampa Bay Lightning
        'NJ': 'NJD',   # New Jersey Devils
        'NS': 'NSH',   # Nashville Predators
        'CO': 'COL',   # Colorado Avalanche
        'FL': 'FLA',   # Florida Panthers
        'ED': 'EDM',   # Edmonton Oilers
        'OT': 'OTT',   # Ottawa Senators
        'PI': 'PIT',   # Pittsburgh Penguins
        'WS': 'WSH',   # Washington Capitals
        'VG': 'VGK',   # Vegas Golden Knights
        'AN': 'ANA',   # Anaheim Ducks
        'CA': 'CAR',   # Carolina Hurricanes
    }

    return mappings.get(team, team)


def parse_matchup(matchup_str):
    """
    Parse matchup string to extract away and home teams.

    Kalshi formats:
    - 6 chars: 3+3 (e.g., "CARVGK" -> CAR vs VGK)
    - 5 chars: 2+3 or 3+2 (e.g., "PITSJ" -> PIT vs SJ, "SJVGK" -> SJ vs VGK)
    - 4 chars: 2+2 (e.g., "NJTB" -> NJ vs TB)
    - 7 chars: 3+4 or 4+3
    - 8 chars: 4+4

    Returns:
        tuple: (away_team, home_team) or None if can't parse
    """
    matchup_str = str(matchup_str).upper()

    # 2-letter team codes used by Kalshi
    two_letter_codes = {'SJ', 'LA', 'TB', 'NJ', 'NS', 'CO', 'FL', 'ED',
                       'OT', 'PI', 'WS', 'VG', 'AN', 'CA'}

    if len(matchup_str) == 4:
        # 2+2 format (both teams use 2-letter codes)
        away = matchup_str[:2]
        home = matchup_str[2:4]
        return (away, home)

    elif len(matchup_str) == 5:
        # Could be 2+3 or 3+2 - try both and pick the best match

        # Option 1: 2+3
        away_2 = matchup_str[:2]
        home_3 = matchup_str[2:5]

        # Option 2: 3+2
        away_3 = matchup_str[:3]
        home_2 = matchup_str[3:5]

        # Prefer the option where the 2-letter code is in the known set
        # If both away and home are in two_letter_codes, prefer 3+2 (more specific)
        if home_2 in two_letter_codes and away_3 not in two_letter_codes:
            return (away_3, home_2)
        elif away_2 in two_letter_codes and home_3 not in two_letter_codes:
            return (away_2, home_3)
        elif home_2 in two_letter_codes:
            # Both could work, prefer 3+2 for away team
            return (away_3, home_2)
        else:
            # Default to 2+3
            return (away_2, home_3)

    elif len(matchup_str) == 6:
        # Standard 3+3 format
        away = matchup_str[:3]
        home = matchup_str[3:6]
        return (away, home)

    elif len(matchup_str) == 7:
        # 3+4 or 4+3 format (for teams with 4-char abbrev)
        # Try 3+4 first
        away = matchup_str[:3]
        home = matchup_str[3:7]
        return (away, home)

    elif len(matchup_str) == 8:
        # 4+4 format (both teams have 4-char abbrev)
        away = matchup_str[:4]
        home = matchup_str[4:8]
        return (away, home)

    else:
        # Unknown format
        return None


def match_markets_to_games(markets_df, schedule_df):
    """
    Match Kalshi markets to actual NHL games.

    Args:
        markets_df: DataFrame with Kalshi market data
        schedule_df: DataFrame with NHL schedule and results

    Returns:
        DataFrame with merged data including outcomes
    """
    print("\nMatching Kalshi markets to NHL games...")

    # Parse matchup to get teams
    markets_df['parsed_matchup'] = markets_df['matchup'].apply(parse_matchup)
    markets_df['away_team_parsed'] = markets_df['parsed_matchup'].apply(
        lambda x: x[0] if x else None
    )
    markets_df['home_team_parsed'] = markets_df['parsed_matchup'].apply(
        lambda x: x[1] if x else None
    )

    # Convert date columns to datetime.date for matching
    markets_df['date'] = pd.to_datetime(markets_df['date']).dt.date
    schedule_df['date'] = pd.to_datetime(schedule_df['date']).dt.date

    # Merge on date first
    print(f"  Markets: {len(markets_df)} records")
    print(f"  Schedule: {len(schedule_df)} games")

    merged = []

    for _, market in markets_df.iterrows():
        # Find matching game by date and teams
        date_matches = schedule_df[schedule_df['date'] == market['date']]

        if len(date_matches) == 0:
            # No games on this date
            continue

        # Try to match by teams
        away_parsed = market['away_team_parsed']
        home_parsed = market['home_team_parsed']

        if not away_parsed or not home_parsed:
            continue

        # Look for matching game
        game_match = date_matches[
            ((date_matches['away_team'] == away_parsed) |
             (date_matches['away_team'] == normalize_team_name(away_parsed))) &
            ((date_matches['home_team'] == home_parsed) |
             (date_matches['home_team'] == normalize_team_name(home_parsed)))
        ]

        if len(game_match) > 0:
            game = game_match.iloc[0]

            # Determine if market settled YES or NO
            team_bet = market['team']

            # Check if the team bet won
            if pd.notna(game['away_score']) and pd.notna(game['home_score']):
                if game['away_score'] > game['home_score']:
                    winner = game['away_team']
                elif game['home_score'] > game['away_score']:
                    winner = game['home_team']
                else:
                    # Tie (shouldn't happen in NHL after OT/SO)
                    winner = None

                market_settled_yes = (team_bet == winner) if winner else None

                merged.append({
                    # Market info
                    'ticker': market['ticker'],
                    'date': market['date'],
                    'matchup': market['matchup'],
                    'team_bet': team_bet,
                    'last_price': market['last_price'],
                    'yes_bid': market['yes_bid'],
                    'yes_ask': market['yes_ask'],
                    'status': market['status'],
                    'open_time': market['open_time'],
                    'close_time': market['close_time'],

                    # Game info
                    'game_id': game['game_id'],
                    'away_team': game['away_team'],
                    'home_team': game['home_team'],
                    'away_score': game['away_score'],
                    'home_score': game['home_score'],
                    'winner': winner,
                    'start_time_utc': game['start_time_utc'],

                    # Outcome
                    'settled_yes': market_settled_yes,
                    'implied_prob': market['last_price'] / 100 if market['last_price'] else None,
                })

    result_df = pd.DataFrame(merged)

    print(f"✓ Matched {len(result_df)} markets to games")
    print(f"  Unmatched: {len(markets_df) - len(result_df)}")

    return result_df


def calculate_metrics(merged_df):
    """
    Calculate market accuracy and profitability metrics.

    Args:
        merged_df: DataFrame with matched markets and outcomes

    Returns:
        dict with summary metrics
    """
    print("\nCalculating metrics...")

    # Filter to markets with outcomes
    df = merged_df[merged_df['settled_yes'].notna()].copy()

    if len(df) == 0:
        print("⚠️  No markets with outcomes to analyze")
        return {}

    # Overall accuracy
    total_markets = len(df)

    # Calibration by price bucket
    df['price_bucket'] = pd.cut(
        df['last_price'],
        bins=[0, 10, 20, 30, 40, 50, 60, 70, 80, 90, 100],
        labels=['0-10', '10-20', '20-30', '30-40', '40-50',
                '50-60', '60-70', '70-80', '80-90', '90-100']
    )

    calibration = df.groupby('price_bucket', observed=True).agg({
        'settled_yes': ['mean', 'count'],
        'last_price': 'mean'
    }).reset_index()

    calibration.columns = ['price_bucket', 'actual_rate', 'count', 'avg_price']
    calibration['actual_rate'] = calibration['actual_rate'] * 100

    # Brier score
    df['brier'] = (df['implied_prob'] - df['settled_yes'].astype(int)) ** 2
    brier_score = df['brier'].mean()

    # Favorite vs underdog performance
    df['is_favorite'] = df['last_price'] >= 50
    favorite_accuracy = df[df['is_favorite']]['settled_yes'].mean()
    underdog_accuracy = df[~df['is_favorite']]['settled_yes'].mean()

    metrics = {
        'total_markets': total_markets,
        'calibration': calibration,
        'brier_score': brier_score,
        'favorite_win_rate': favorite_accuracy,
        'underdog_win_rate': underdog_accuracy,
    }

    print(f"\n  Total markets analyzed: {total_markets}")
    print(f"  Brier score: {brier_score:.4f}")
    print(f"  Favorite win rate: {favorite_accuracy:.1%}")
    print(f"  Underdog win rate: {underdog_accuracy:.1%}")

    return metrics


def main():
    print("="*80)
    print("NHL DATA MERGER")
    print("="*80)

    # Load data
    print("\nLoading data...")
    data_dir = Path('../data')

    try:
        markets_df = pd.read_csv(data_dir / 'kalshi_nhl_markets.csv')
        schedule_df = pd.read_csv(data_dir / 'nhl_schedule.csv')

        print(f"✓ Loaded {len(markets_df)} Kalshi markets")
        print(f"✓ Loaded {len(schedule_df)} NHL games")

    except FileNotFoundError as e:
        print(f"✗ Error: {e}")
        print("\nMake sure you've run:")
        print("  1. collect_kalshi_markets.py")
        print("  2. collect_nhl_schedule.py")
        return

    # Match markets to games
    merged_df = match_markets_to_games(markets_df, schedule_df)

    if len(merged_df) == 0:
        print("\n⚠️  No markets matched to games")
        return

    # Calculate metrics
    metrics = calculate_metrics(merged_df)

    # Save merged data
    output_file = data_dir / 'nhl_merged.csv'
    merged_df.to_csv(output_file, index=False)
    print(f"\n✓ Saved merged data to {output_file}")

    # Print summary
    print("\n" + "="*80)
    print("SUMMARY")
    print("="*80)
    print(f"\nDate range: {merged_df['date'].min()} to {merged_df['date'].max()}")
    print(f"Total matched markets: {len(merged_df)}")
    print(f"Markets with outcomes: {merged_df['settled_yes'].notna().sum()}")

    if 'calibration' in metrics:
        print("\nCalibration:")
        print(metrics['calibration'].to_string(index=False))

    # Sample data
    print("\nSample matched markets:")
    sample_cols = ['date', 'matchup', 'team_bet', 'last_price', 'winner', 'settled_yes']
    print(merged_df[sample_cols].head(10).to_string(index=False))

    print("\n" + "="*80)


if __name__ == "__main__":
    main()

"""
Entry point for running kalshi_nfl_research as a module.

Usage: python -m kalshi_nfl_research <command> [args...]
"""

from .cli import cli

if __name__ == "__main__":
    cli()

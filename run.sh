#!/bin/bash
# NHL Trading Bot Startup Script

cd "$(dirname "$0")"

# Check if venv exists
if [ ! -d "venv" ]; then
    echo "Virtual environment not found. Creating..."
    python3 -m venv venv
    source venv/bin/activate
    echo "Installing dependencies..."
    pip install -r requirements.txt
else
    source venv/bin/activate
fi

# Run the bot
echo "Starting NHL Trading Bot..."
python3 live_trader.py

#!/bin/bash

# Script to stop the Nutrios project services

# Find and kill processes
echo "Stopping services..."

# Kill API
API_PID=$(pgrep -f "python run_api.py")
if [ ! -z "$API_PID" ]; then
    kill $API_PID
    echo "API stopped (PID: $API_PID)"
else
    echo "API not running"
fi

# Kill Bot
BOT_PID=$(pgrep -f "python main.py")
if [ ! -z "$BOT_PID" ]; then
    kill $BOT_PID
    echo "Bot stopped (PID: $BOT_PID)"
else
    echo "Bot not running"
fi

# Kill Dashboard
DASH_PID=$(pgrep -f "streamlit run dashboard/app.py")
if [ ! -z "$DASH_PID" ]; then
    kill $DASH_PID
    echo "Dashboard stopped (PID: $DASH_PID)"
else
    echo "Dashboard not running"
fi

echo "All services stopped."

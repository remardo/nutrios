#!/bin/bash

# Script to run the Nutrios project on Linux VPS
# Assumes Python venv is set up and dependencies are installed

# Activate virtual environment
source .venv/bin/activate

# Set environment variables if needed
export ADMIN_API_KEY="supersecret"
export ADMIN_API_BASE="http://localhost:8000"

# Start API server in background
echo "Starting API server..."
nohup python run_api.py > api.log 2>&1 &
API_PID=$!
echo "API started with PID $API_PID"

# Wait a bit for API to start
sleep 5

# Start Telegram bot in background
echo "Starting Telegram bot..."
nohup python bot/main.py > bot.log 2>&1 &
BOT_PID=$!
echo "Bot started with PID $BOT_PID"

# Start Streamlit dashboard in background
echo "Starting Streamlit dashboard..."
nohup streamlit run dashboard/app.py --server.port 8501 --server.address 0.0.0.0 > dashboard.log 2>&1 &
DASH_PID=$!
echo "Dashboard started with PID $DASH_PID"

echo "All services started!"
echo "API: http://localhost:8000"
echo "Dashboard: http://localhost:8501"
echo "Bot is running in background"
echo ""
echo "To stop services, use: kill $API_PID $BOT_PID $DASH_PID"
echo "Or check logs: api.log, bot.log, dashboard.log"

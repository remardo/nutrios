#!/bin/bash

# Quick deployment script using screen for process management

echo "🚀 Quick Nutrios deployment..."

# Install dependencies
sudo apt update
sudo apt install -y python3 python3-pip python3-venv screen

# Setup project
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

# Start services in screen
screen -dmS nutrios-api bash -c "source .venv/bin/activate && python run_api.py"
screen -dmS nutrios-bot bash -c "source .venv/bin/activate && python main.py"
screen -dmS nutrios-dashboard bash -c "source .venv/bin/activate && streamlit run dashboard/app.py --server.port 8501 --server.address 0.0.0.0"

echo "Services started in screen sessions:"
echo "- API: screen -r nutrios-api"
echo "- Bot: screen -r nutrios-bot"
echo "- Dashboard: screen -r nutrios-dashboard"
echo ""
echo "To stop: screen -X -S nutrios-api quit"

#!/bin/bash

# Full setup script for Nutrios project on Linux VPS

echo "Setting up Nutrios project..."

# Update system
sudo apt update && sudo apt upgrade -y

# Install dependencies
sudo apt install -y python3 python3-pip python3-venv sqlite3 git

# Create virtual environment
python3 -m venv .venv
source .venv/bin/activate

# Install Python packages
pip install --upgrade pip
pip install -r requirements.txt

# Make scripts executable
chmod +x start.sh stop.sh

echo "Setup complete!"
echo "Edit .env file with your credentials"
echo "Then run: ./start.sh"

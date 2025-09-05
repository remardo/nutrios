#!/bin/bash

# Nutrios Project Deployment Script for Clean VPS
# This script sets up the entire project from scratch

set -e  # Exit on any error

echo "🚀 Starting Nutrios deployment on clean VPS..."

# Colors for output
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m' # No Color

# Function to print colored output
print_status() {
    echo -e "${GREEN}[INFO]${NC} $1"
}

print_warning() {
    echo -e "${YELLOW}[WARNING]${NC} $1"
}

print_error() {
    echo -e "${RED}[ERROR]${NC} $1"
}

# Check if running as root
if [[ $EUID -eq 0 ]]; then
   print_warning "Running as root. This is not recommended for security reasons."
   print_warning "Consider creating a non-root user with sudo access."
   print_status "Continuing deployment as root..."
   sleep 2
else
   print_status "Running as regular user - good!"
fi

# Update system
print_status "Updating system packages..."
sudo apt update && sudo apt upgrade -y

# Install system dependencies
print_status "Installing system dependencies..."
sudo apt install -y \
    python3 \
    python3-pip \
    python3-venv \
    python3-dev \
    sqlite3 \
    git \
    curl \
    wget \
    build-essential \
    libssl-dev \
    libffi-dev \
    python3-setuptools

# Install Node.js for any frontend dependencies (if needed)
curl -fsSL https://deb.nodesource.com/setup_18.x | sudo -E bash -
sudo apt-get install -y nodejs

# Create project directory
PROJECT_DIR="$HOME/nutrios"
if [ -d "$PROJECT_DIR" ]; then
    print_warning "Project directory already exists. Backing up..."
    mv "$PROJECT_DIR" "${PROJECT_DIR}_backup_$(date +%Y%m%d_%H%M%S)"
fi

# Clone repository (you'll need to replace with actual repo URL)
REPO_URL="https://github.com/remardo/nutrios.git"  # Replace with actual repo
print_status "Cloning repository..."
git clone "$REPO_URL" "$PROJECT_DIR"

cd "$PROJECT_DIR"

# Create virtual environment
print_status "Creating Python virtual environment..."
python3 -m venv .venv
source .venv/bin/activate

# Upgrade pip
pip install --upgrade pip

# Install Python dependencies
print_status "Installing Python dependencies..."
pip install -r requirements.txt

# Create .env file template
print_status "Creating .env template..."
cat > .env << EOF
# Telegram Bot Configuration
TELEGRAM_BOT_TOKEN=your_telegram_bot_token_here
OPENAI_API_KEY=your_openai_api_key_here

# OpenAI Models
OPENAI_VISION_MODEL=gpt-4o-mini
OPENAI_TEXT_MODEL=gpt-4o-mini

# Admin API Configuration
ADMIN_DB_URL=sqlite:///./nutrios.db
ADMIN_API_KEY=supersecret
ADMIN_API_BASE=http://localhost:8000

# Optional: Database (if using PostgreSQL instead of SQLite)
# DATABASE_URL=postgresql://user:password@localhost/nutrios
EOF

print_warning "Please edit .env file with your actual credentials!"
print_warning "Run: nano .env"

# Make scripts executable
print_status "Making scripts executable..."
chmod +x start.sh stop.sh setup.sh

# Initialize database
print_status "Initializing database..."
python3 -c "
from admin.db import init_db, Base
init_db(Base)
print('Database initialized successfully')
"

# Create systemd services
print_status "Creating systemd services..."

# Get current user (works for both root and regular users)
CURRENT_USER=$(whoami)

# API Service
sudo tee /etc/systemd/system/nutrios-api.service > /dev/null << EOF
[Unit]
Description=Nutrios API Server
After=network.target

[Service]
User=$CURRENT_USER
WorkingDirectory=$PROJECT_DIR
Environment=PATH=$PROJECT_DIR/.venv/bin
ExecStart=$PROJECT_DIR/.venv/bin/python run_api.py
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
EOF

# Bot Service
sudo tee /etc/systemd/system/nutrios-bot.service > /dev/null << EOF
[Unit]
Description=Nutrios Telegram Bot
After=network.target nutrios-api.service

[Service]
User=$CURRENT_USER
WorkingDirectory=$PROJECT_DIR
Environment=PATH=$PROJECT_DIR/.venv/bin
ExecStart=$PROJECT_DIR/.venv/bin/python main.py
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
EOF

# Dashboard Service
sudo tee /etc/systemd/system/nutrios-dashboard.service > /dev/null << EOF
[Unit]
Description=Nutrios Streamlit Dashboard
After=network.target nutrios-api.service

[Service]
User=$CURRENT_USER
WorkingDirectory=$PROJECT_DIR
Environment=PATH=$PROJECT_DIR/.venv/bin
ExecStart=$PROJECT_DIR/.venv/bin/streamlit run dashboard/app.py --server.port 8501 --server.address 0.0.0.0
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
EOF

# Reload systemd
sudo systemctl daemon-reload

print_status "Services created. To start them:"
echo "  sudo systemctl start nutrios-api"
echo "  sudo systemctl start nutrios-bot"
echo "  sudo systemctl start nutrios-dashboard"
echo ""
echo "To enable auto-start:"
echo "  sudo systemctl enable nutrios-api nutrios-bot nutrios-dashboard"

# Create Nginx configuration
print_status "Setting up Nginx (optional)..."
sudo apt install -y nginx

sudo tee /etc/nginx/sites-available/nutrios > /dev/null << EOF
server {
    listen 80;
    server_name _;

    # API
    location /api/ {
        proxy_pass http://localhost:8000/;
        proxy_set_header Host \$host;
        proxy_set_header X-Real-IP \$remote_addr;
        proxy_set_header X-Forwarded-For \$proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto \$scheme;
    }

    # Dashboard
    location / {
        proxy_pass http://localhost:8501/;
        proxy_set_header Host \$host;
        proxy_set_header X-Real-IP \$remote_addr;
        proxy_set_header X-Forwarded-For \$proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto \$scheme;
    }
}
EOF

sudo ln -sf /etc/nginx/sites-available/nutrios /etc/nginx/sites-enabled/
sudo nginx -t && sudo systemctl reload nginx

# Setup firewall
print_status "Configuring firewall..."
sudo apt install -y ufw
sudo ufw allow ssh
sudo ufw allow 80
sudo ufw allow 8501
sudo ufw --force enable

# Final instructions
print_status "🎉 Deployment completed!"
echo ""
echo "Next steps:"
echo "1. Edit .env file: nano $PROJECT_DIR/.env"
echo "2. Start services:"
echo "   sudo systemctl start nutrios-api nutrios-bot nutrios-dashboard"
echo "3. Enable auto-start:"
echo "   sudo systemctl enable nutrios-api nutrios-bot nutrios-dashboard"
echo "4. Check status:"
echo "   sudo systemctl status nutrios-api"
echo ""
echo "Access points:"
echo "- API: http://your-vps-ip/api/"
echo "- Dashboard: http://your-vps-ip/"
echo "- Direct dashboard: http://your-vps-ip:8501"
echo ""
echo "Logs:"
echo "- API: sudo journalctl -u nutrios-api -f"
echo "- Bot: sudo journalctl -u nutrios-bot -f"
echo "- Dashboard: sudo journalctl -u nutrios-dashboard -f"

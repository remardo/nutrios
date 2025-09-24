# Nutrios Project - VPS Deployment Guide

## Quick Deployment Options

### Option 1: Full Automated Deployment
For a complete setup with systemd services and Nginx:

```bash
# Download and run deployment script
wget https://raw.githubusercontent.com/remardo/nutrios/master/deploy_vps.sh
chmod +x deploy_vps.sh
./deploy_vps.sh
```

### Option 2: Quick Manual Setup
For testing or simple deployment:

```bash
# Download and run quick setup
wget https://raw.githubusercontent.com/remardo/nutrios/master/quick_deploy.sh
chmod +x quick_deploy.sh
./quick_deploy.sh
```

### Option 3: Manual Setup
Follow the step-by-step guide below.

## Prerequisites
- Ubuntu/Debian Linux VPS (20.04+ recommended)
- Python 3.8+
- Git
- Root or sudo access

## Automated Deployment (Recommended)

1. **Run the deployment script:**
   ```bash
   wget https://raw.githubusercontent.com/remardo/nutrios/master/deploy_vps.sh
   chmod +x deploy_vps.sh
   ./deploy_vps.sh
   ```

2. **Edit environment variables:**
   ```bash
   nano .env
   ```

3. **Start services:**
   ```bash
   sudo systemctl start nutrios-api nutrios-bot nutrios-dashboard
   sudo systemctl enable nutrios-api nutrios-bot nutrios-dashboard
   ```

## Manual Setup

### 1. System Preparation
```bash
# Update system
sudo apt update && sudo apt upgrade -y

# Install dependencies
sudo apt install -y python3 python3-pip python3-venv sqlite3 git curl wget
```

### 2. Project Setup
```bash
# Clone repository
git clone <your-repo-url>
cd nutrios

# Create virtual environment
python3 -m venv .venv
source .venv/bin/activate

# Install dependencies
pip install -r requirements.txt
```

### 3. Environment Configuration
```bash
# Copy and edit .env
cp .env.example .env
nano .env
```

### 4. Database Setup
```bash
# Initialize database
python3 -c "from admin.db import init_db, Base; init_db(Base)"
```

### 5. Service Management

#### Using systemd (Production)
```bash
# Copy service files
sudo cp systemd/*.service /etc/systemd/system/

# Reload and start
sudo systemctl daemon-reload
sudo systemctl start nutrios-api nutrios-bot nutrios-dashboard
sudo systemctl enable nutrios-api nutrios-bot nutrios-dashboard
```

#### Using screen (Development/Testing)
```bash
# Install screen
sudo apt install -y screen

# Start services
screen -dmS api bash -c "cd /path/to/nutrios && source .venv/bin/activate && python run_api.py"
screen -dmS bot bash -c "cd /path/to/nutrios && source .venv/bin/activate && python bot/main.py"
screen -dmS dashboard bash -c "cd /path/to/nutrios && source .venv/bin/activate && streamlit run dashboard/app.py --server.port 8501 --server.address 0.0.0.0"

# Attach to sessions
screen -r api
screen -r bot
screen -r dashboard
```

## Manual Daily Metrics & Events Logging

До полноценной автоматизации нутрициолог может фиксировать ежедневные отметки и события вручную через REST API админки.

### Ежедневные метрики

Эндпоинт: `PUT /clients/{client_id}/metrics/daily`

Тело запроса — объект (или список объектов) с датой и нужными полями. Идемпотентность обеспечивается сочетанием `(client_id, date)`.

```bash
curl -X PUT \
  -H "x-api-key: $ADMIN_API_KEY" \
  -H "Content-Type: application/json" \
  "${ADMIN_API_BASE}/clients/42/metrics/daily" \
  -d '{
        "date": "2024-06-15",
        "water_goal_met": true,
        "steps": 8700,
        "dinner_logged": true,
        "new_recipe_logged": false,
        "protein_goal_met": true,
        "fiber_goal_met": false
      }'
```

Доступные поля:

- `water_goal_met` — достигнута ли цель по воде за день.
- `steps` — количество шагов (целое число).
- `protein_goal_met`, `fiber_goal_met` — выполнены ли цели по белку/клетчатке.
- `breakfast_logged_before_10` — был ли завтрак до 10:00.
- `dinner_logged` — ужин зафиксирован.
- `new_recipe_logged` — пробовали новое блюдо.

Любые отсутствующие поля не изменяют текущее состояние. Можно отправлять несколько объектов одним запросом.

### События клиента

Эндпоинт: `POST /clients/{client_id}/events`

Эндпоинт принимает объект (или список), идемпотентность по сочетанию `(client_id, date, type)`. Если событие уже есть за этот день, оно обновится.

```bash
curl -X POST \
  -H "x-api-key: $ADMIN_API_KEY" \
  -H "Content-Type: application/json" \
  "${ADMIN_API_BASE}/clients/42/events" \
  -d '{
        "type": "challenge_completed",
        "date": "2024-06-15",
        "payload": {"title": "10к шагов ежедневно"}
      }'
```

Поддерживаемые типы событий: `portion_adjusted`, `shared_progress`, `challenge_completed`, `streak_resumed` (можно расширять по необходимости). Поле `payload` — произвольный JSON с деталями события.

### Быстрые отметки

- В боте доступны команды `/water`, `/steps <число>`, `/dinner`, `/newrecipe` — они обновляют текущие метрики.
- В мини-приложении появился блок «Ежедневные отметки» (вода, шаги, ужин, новый рецепт) и секция «Челленджи и события» для фиксации прогресса, завершённых челленджей, восстановления серии и шеринга.

## Nginx Configuration

### Install and Configure
```bash
# Install Nginx
sudo apt install -y nginx

# Create site configuration
sudo nano /etc/nginx/sites-available/nutrios
```

Add this configuration:
```nginx
server {
    listen 80;
    server_name your-domain.com;

    # SSL configuration (optional)
    # listen 443 ssl;
    # ssl_certificate /path/to/cert.pem;
    # ssl_certificate_key /path/to/key.pem;

    # API proxy
    location /api/ {
        proxy_pass http://localhost:8000/;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
    }

    # Dashboard proxy
    location / {
        proxy_pass http://localhost:8501/;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
    }
}
```

### Enable Site
```bash
# Enable site
sudo ln -s /etc/nginx/sites-available/nutrios /etc/nginx/sites-enabled/

# Test configuration
sudo nginx -t

# Reload Nginx
sudo systemctl reload nginx
```

## SSL Certificate (Let's Encrypt)

```bash
# Install certbot
sudo apt install -y certbot python3-certbot-nginx

# Get certificate
sudo certbot --nginx -d your-domain.com

# Auto-renewal is enabled by default
```

## Firewall Configuration

```bash
# Install ufw
sudo apt install -y ufw

# Allow necessary ports
sudo ufw allow ssh
sudo ufw allow 80
sudo ufw allow 443
sudo ufw allow 8501  # Direct dashboard access

# Enable firewall
sudo ufw --force enable
```

## Monitoring and Logs

### Systemd Services
```bash
# Check status
sudo systemctl status nutrios-api

# View logs
sudo journalctl -u nutrios-api -f

# Restart service
sudo systemctl restart nutrios-api
```

### Screen Sessions
```bash
# List sessions
screen -ls

# Attach to session
screen -r nutrios-api

# Detach: Ctrl+A, D
```

## Backup Strategy

### Database Backup
```bash
# SQLite backup (daily)
crontab -e
# Add: 0 2 * * * sqlite3 /path/to/nutrios/nutrios.db ".backup /path/to/backup/nutrios_$(date +\%Y\%m\%d).db"
```

### File Backup
```bash
# Backup uploads and config
rsync -av /path/to/nutrios/uploads/ /path/to/backup/uploads/
rsync -av /path/to/nutrios/.env /path/to/backup/
```

## Troubleshooting

### Common Issues

1. **Port already in use:**
   ```bash
   sudo lsof -i :8000
   sudo kill -9 <PID>
   ```

2. **Permission denied:**
   ```bash
   sudo chown -R $USER:$USER /path/to/nutrios
   ```

3. **Service won't start:**
   ```bash
   sudo journalctl -u nutrios-api -n 50
   ```

4. **Database connection failed:**
   ```bash
   python3 -c "import sqlite3; conn = sqlite3.connect('nutrios.db'); print('OK')"
   ```

### Performance Tuning

1. **Increase file limits:**
   ```bash
   echo "fs.file-max = 65536" | sudo tee -a /etc/sysctl.conf
   sudo sysctl -p
   ```

2. **Python optimization:**
   ```bash
   export PYTHONOPTIMIZE=1
   ```

## Access URLs

- **API**: http://your-vps-ip/api/ or https://your-domain.com/api/
- **Dashboard**: http://your-vps-ip/ or https://your-domain.com/
- **Direct Dashboard**: http://your-vps-ip:8501

## Security Considerations

1. **Change default API key** in `.env`
2. **Use strong passwords** for all services
3. **Enable SSL/TLS** for production
4. **Regular updates**: `sudo apt update && sudo apt upgrade`
5. **Monitor logs** for suspicious activity
6. **Use firewall** to restrict access
7. **Backup regularly** important data

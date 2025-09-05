# Nutrios VPS Deployment

## Files Created:
- `deploy_vps.sh` - Full automated deployment script
- `quick_deploy.sh` - Quick deployment with screen
- `README_VPS.md` - Detailed deployment guide
- `.env.example` - Environment template
- `systemd/` - Systemd service files

## Quick Start:

### Option 1: Automated (Recommended)
```bash
wget https://raw.githubusercontent.com/your-repo/main/deploy_vps.sh
chmod +x deploy_vps.sh
./deploy_vps.sh
```

### Option 2: Quick Manual
```bash
wget https://raw.githubusercontent.com/your-repo/main/quick_deploy.sh
chmod +x quick_deploy.sh
./quick_deploy.sh
```

## What the scripts do:
1. Install system dependencies (Python, pip, git, etc.)
2. Clone repository
3. Setup virtual environment
4. Install Python packages
5. Configure environment variables
6. Setup systemd services (automated) or screen sessions (quick)
7. Configure Nginx reverse proxy
8. Setup firewall
9. Initialize database

## After deployment:
1. Edit `.env` with your actual credentials
2. Start services: `sudo systemctl start nutrios-api nutrios-bot nutrios-dashboard`
3. Access dashboard at http://your-vps-ip/

## Services:
- API: http://localhost:8000
- Dashboard: http://localhost:8501
- Bot: Running in background

## Logs:
- Systemd: `sudo journalctl -u nutrios-api -f`
- Screen: `screen -r nutrios-api`

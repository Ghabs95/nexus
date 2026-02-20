# Nexus Deployment Guide

Complete guide for deploying and maintaining the Nexus automation system in production.

## Prerequisites

### System Requirements
- Ubuntu 20.04+ (or Debian-based Linux)
- Python 3.8+
- 2GB RAM minimum (4GB recommended)
- 10GB disk space
- systemd (for service management)

### Required Accounts & Credentials
- GitHub account with CLI (`gh`) authenticated
- Telegram Bot Token (from [@BotFather](https://t.me/botfather))
- Google Gemini API key
- Access to agent repositories

## Initial Setup

### 1. Install System Dependencies

```bash
# Update system
sudo apt update && sudo apt upgrade -y

# Install Python and FFmpeg
sudo apt install -y python3 python3-pip python3-venv ffmpeg

# Install GitHub CLI
type -p curl >/dev/null || sudo apt install curl -y
curl -fsSL https://cli.github.com/packages/githubcli-archive-keyring.gpg | sudo dd of=/usr/share/keyrings/githubcli-archive-keyring.gpg \
&& sudo chmod go+r /usr/share/keyrings/githubcli-archive-keyring.gpg \
&& echo "deb [arch=$(dpkg --print-architecture) signed-by=/usr/share/keyrings/githubcli-archive-keyring.gpg] https://cli.github.com/packages stable main" | sudo tee /etc/apt/sources.list.d/github-cli.list > /dev/null \
&& sudo apt update \
&& sudo apt install gh -y

# Authenticate GitHub CLI
gh auth login
```

### 2. Clone Repository and Setup Environment

```bash
# Clone the repository
cd /home/ubuntu/git/ghabs
git clone <repository-url> nexus
cd nexus

# Create virtual environment
python3 -m venv venv
source venv/bin/activate

# Install Python dependencies
pip install -r requirements.txt
```

### 3. Configure Environment Variables

Create and edit `vars.secret`:

```bash
cp vars.secret.example vars.secret
nano vars.secret
```

Required variables:
```bash
# Telegram Configuration
TELEGRAM_TOKEN=your_bot_token_here
TELEGRAM_CHAT_ID=your_chat_id_here
ALLOWED_USER=your_telegram_user_id

# Google Gemini Configuration
AI_API_KEY=your_google_gemini_api_key
AI_MODEL=gemini-2.0-flash

# GitHub Configuration
PROJECT_CONFIG_PATH=path/to/your_project_config.yaml  # Per-project git_repo/git_repos settings

# Optional: Feature Flags
ENABLE_SCHEDULED_REPORTS=true
WEEKLY_SUMMARY_ENABLED=true
ENABLE_ALERTING=true

# Optional: Thresholds
ERROR_RATE_THRESHOLD=10
STUCK_WORKFLOW_HOURS=2
AGENT_FAILURE_THRESHOLD=3
ALERT_COOLDOWN_MINUTES=30
```

**Security Note**: Never commit `vars.secret` to version control. It's included in `.gitignore`.

### 4. Initialize Data Directories

```bash
# Create required directories
mkdir -p data logs

# Set proper permissions
chmod 755 data logs
```

### 5. Run Tests

```bash
# Verify installation
venv/bin/pytest -v

# Expected output: 115 passed
```

## Service Installation

### 1. Install as Systemd Services

```bash
# Copy service files
sudo cp nexus-bot.service /etc/systemd/system/
sudo cp nexus-processor.service /etc/systemd/system/
sudo cp nexus-health.service /etc/systemd/system/  # If using health check

# Reload systemd
sudo systemctl daemon-reload

# Enable services (start on boot)
sudo systemctl enable nexus-bot
sudo systemctl enable nexus-processor
sudo systemctl enable nexus-health

# Start services
sudo systemctl start nexus-bot
sudo systemctl start nexus-processor
sudo systemctl start nexus-health
```

### 2. Verify Services are Running

```bash
# Check status
systemctl status nexus-bot
systemctl status nexus-processor
systemctl status nexus-health

# All should show: Active: active (running)
```

### 3. Setup Log Rotation

```bash
# Install logrotate configuration
sudo cp logrotate.conf /etc/logrotate.d/nexus

# Test configuration
sudo logrotate -d /etc/logrotate.d/nexus

# Force rotate (optional)
sudo logrotate -f /etc/logrotate.d/nexus
```

## Monitoring & Maintenance

### Health Checks

The health check endpoint runs on `http://localhost:8080`:

```bash
# Check overall health
curl http://localhost:8080/health

# Get detailed status
curl http://localhost:8080/status | python3 -m json.tool

# View metrics
curl http://localhost:8080/metrics | python3 -m json.tool
```

### Log Monitoring

```bash
# Real-time logs
sudo journalctl -u nexus-bot -f
sudo journalctl -u nexus-processor -f

# Last 50 lines
sudo journalctl -u nexus-bot -n 50
sudo journalctl -u nexus-processor -n 50

# Logs since specific time
sudo journalctl -u nexus-bot --since "1 hour ago"
sudo journalctl -u nexus-bot --since "2026-02-16 10:00:00"

# Filter by priority
sudo journalctl -u nexus-bot -p err  # Errors only
```

### Service Management

```bash
# Restart services
sudo systemctl restart nexus-bot
sudo systemctl restart nexus-processor

# Stop services
sudo systemctl stop nexus-bot
sudo systemctl stop nexus-processor

# View service status
systemctl status nexus-bot --no-pager -l
```

### Database Backups

```bash
# Backup state files
#!/bin/bash
BACKUP_DIR="/home/ubuntu/nexus-backups/$(date +%Y%m%d-%H%M%S)"
mkdir -p "$BACKUP_DIR"

# Backup data directory
cp -r /home/ubuntu/git/ghabs/nexus/data/* "$BACKUP_DIR/"

# Backup logs (optional)
cp -r /home/ubuntu/git/ghabs/nexus/logs/* "$BACKUP_DIR/logs/"

echo "Backup created: $BACKUP_DIR"
```

**Recommended**: Create a cron job for daily backups.

## Troubleshooting

### Service Won't Start

```bash
# Check logs for errors
sudo journalctl -u nexus-bot -n 100 --no-pager

# Common issues:
# 1. Missing environment variables
source /home/ubuntu/git/ghabs/nexus/vars.secret
env | grep TELEGRAM

# 2. Permission issues
ls -la /home/ubuntu/git/ghabs/nexus/data/
chmod 755 /home/ubuntu/git/ghabs/nexus/data/

# 3. Python import errors
cd /home/ubuntu/git/ghabs/nexus/src
python3 -c "import config, state_manager, agent_monitor"
```

### High Memory Usage

```bash
# Check process memory
ps aux | grep python | grep nexus

# Kill stuck processes
pkill -f "copilot.*issues/"

# Restart services
sudo systemctl restart nexus-bot nexus-processor
```

### Rate Limiting Issues

```bash
# Check rate limiter state
cat /home/ubuntu/git/ghabs/nexus/data/rate_limits.json | python3 -m json.tool

# Reset rate limits (if needed)
rm /home/ubuntu/git/ghabs/nexus/data/rate_limits.json
sudo systemctl restart nexus-bot
```

### GitHub CLI Authentication Expired

```bash
# Re-authenticate
gh auth login

# Verify
gh auth status

# Restart services
sudo systemctl restart nexus-bot nexus-processor
```

## Performance Tuning

### Adjust Monitoring Intervals

Edit `src/config.py`:

```python
# Reduce CPU usage (slower response)
SLEEP_INTERVAL = 30  # Check every 30 seconds (default: 15)

# Increase for faster response (more CPU)
SLEEP_INTERVAL = 5   # Check every 5 seconds
```

### Optimize Rate Limits

Edit rate limit thresholds in `src/rate_limiter.py`:

```python
# More permissive (allows more requests)
"user_global": {"limit": 100, "window": 60}  # 100/min

# More restrictive (fewer requests)
"user_global": {"limit": 10, "window": 60}   # 10/min
```

## Security Best Practices

1. **Environment Variables**
   - Never commit `vars.secret`
   - Use restrictive permissions: `chmod 600 vars.secret`
   - Rotate tokens periodically

2. **Service User**
   - Run services as non-root user
   - Create dedicated service account (optional):
     ```bash
     sudo useradd -r -s /bin/false nexus
     sudo chown -R nexus:nexus /home/ubuntu/git/ghabs/nexus
     ```

3. **Firewall**
   - Health check endpoint is localhost-only (safe)
   - No ports exposed to internet

4. **GitHub Permissions**
   - Use fine-grained access tokens
   - Limit repo access to only required repositories

## Upgrading

### Update to Latest Version

```bash
cd /home/ubuntu/git/ghabs/nexus

# Stop services
sudo systemctl stop nexus-bot nexus-processor

# Backup current state
cp -r data data.backup.$(date +%Y%m%d)

# Pull latest changes
git pull

# Update dependencies
source venv/bin/activate
pip install -r requirements.txt --upgrade

# Run tests
venv/bin/pytest -v

# Restart services
sudo systemctl start nexus-bot nexus-processor

# Verify
systemctl status nexus-bot nexus-processor
```

### Rollback (if needed)

```bash
# Stop services
sudo systemctl stop nexus-bot nexus-processor

# Restore previous version
git reset --hard <previous-commit-hash>

# Restore data
rm -rf data
mv data.backup.20260216 data

# Restart services
sudo systemctl start nexus-bot nexus-processor
```

## Monitoring Dashboard (Optional)

### Using systemd Journal

```bash
# Install journal viewer
sudo apt install gnome-logs  # GUI
# or
journalctl --follow --unit nexus-*  # CLI
```

### Integration with External Monitoring

Export metrics via health endpoint:

```bash
# Prometheus scrape config
curl http://localhost:8080/metrics

# Grafana dashboard can visualize:
# - Workflow completion rates
# - Agent performance
# - Error rates
# - Rate limiter usage
```

## Production Checklist

- [ ] All environment variables configured
- [ ] GitHub CLI authenticated
- [ ] Services enabled and running
- [ ] Log rotation configured
- [ ] Health checks passing
- [ ] Tests passing (115/115)
- [ ] Backup automation configured
- [ ] Monitoring alerts configured
- [ ] Documentation reviewed
- [ ] Team trained on Telegram commands

## Support

For issues or questions:
1. Check logs: `sudo journalctl -u nexus-bot -n 100`
2. Run diagnostics: `venv/bin/pytest -v`
3. Review [README.md](README.md) for command reference
4. Check [ARCHITECTURE.md](ARCHITECTURE.md) for system design

## Next Steps

After deployment:
1. Test workflow with a sample issue
2. Monitor logs for 24 hours
3. Adjust rate limits based on usage
4. Configure daily digest schedule
5. Set up backup automation

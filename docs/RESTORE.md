# Nexus System Restore & Replication Guide

Complete guide to restore or replicate the entire Nexus system from scratch, including infrastructure, services, and security configuration.

## Prerequisites

- Ubuntu 24.04 (or similar Linux distro)
- Access to OCI (or your cloud provider)
- GitHub CLI (`gh`) pre-installed and authenticated
- Terraform installed locally (for infrastructure)
- Root/sudo access

## Part 1: Infrastructure Setup (Local, via Terraform)

### 1.1 OCI Security Group Rules

Update [vsc-server-infra/main.tf](../vsc-server-infra/main.tf) with webhook allowlist:

```hcl
locals {
  webhook_allowed_cidrs = [
    "95.248.208.163/32",        # Your public IP (temp for testing)
    "192.30.252.0/22",          # GitHub webhooks IPv4
    "185.199.108.0/22",         # GitHub webhooks IPv4
    "140.82.112.0/20",          # GitHub webhooks IPv4
    "143.55.64.0/20",           # GitHub webhooks IPv4
    "2a0a:a440::/29",           # GitHub webhooks IPv6
    "2606:50c0::/32",           # GitHub webhooks IPv6
  ]
}

resource "oci_core_network_security_group_security_rule" "allow_webhook" {
  for_each                    = length(data.oci_core_network_security_groups.nsg.network_security_groups) > 0 ? toset(local.webhook_allowed_cidrs) : toset([])
  network_security_group_id   = data.oci_core_network_security_groups.nsg.network_security_groups[0].id
  direction                   = "INGRESS"
  protocol                    = "6" # TCP
  source                      = each.key
  destination_port_range {
    min = 8081
    max = 8081
  }
  description = "Allow webhook server from GitHub + verifier IP"
}
```

Then apply locally:
```bash
cd vsc-server-infra
terraform init
terraform plan
terraform apply  # Use your local credentials/tfvars
```

**Note:** Save your `terraform.tfvars` securely — it contains sensitive OCI keys.

## Part 2: VM Setup (On Remote Instance)

### 2.1 System Dependencies

```bash
# Update system
sudo apt update && sudo apt upgrade -y

# Install Python, FFmpeg, Git
sudo apt install -y python3 python3-pip python3-venv ffmpeg git

# Install GitHub CLI
type -p curl >/dev/null || sudo apt install curl -y
curl -fsSL https://cli.github.com/packages/githubcli-archive-keyring.gpg | sudo dd of=/usr/share/keyrings/githubcli-archive-keyring.gpg
sudo chmod go+r /usr/share/keyrings/githubcli-archive-keyring.gpg
echo "deb [arch=$(dpkg --print-architecture) signed-by=/usr/share/keyrings/githubcli-archive-keyring.gpg] https://cli.github.com/packages stable main" | sudo tee /etc/apt/sources.list.d/github-cli.list > /dev/null
sudo apt update
sudo apt install gh -y

# Authenticate GitHub
gh auth login
```

### 2.2 Clone Repositories

```bash
mkdir -p /home/ubuntu/git/ghabs
cd /home/ubuntu/git/ghabs

# Clone main repos
git clone https://github.com/Ghabs95/nexus.git
git clone https://github.com/Ghabs95/agents.git
git clone https://github.com/Ghabs95/vsc-server-infra.git
```

### 2.3 Setup Python Virtual Environment

```bash
cd /home/ubuntu/git/ghabs/nexus

# Create venv
python3 -m venv venv

# Activate and install dependencies
source venv/bin/activate
pip install -r requirements.txt

# Verify installation
python3 -c "from config import WORKFLOW_CHAIN; print('✅ Imports OK')"
```

### 2.4 Initialize Data & Logs Directories

```bash
cd /home/ubuntu/git/ghabs/nexus

# Create directories
mkdir -p data logs

# Create data files (empty)
touch data/tracked_issues.json
echo "{}" > data/tracked_issues.json

touch data/launched_agents.json
echo "{}" > data/launched_agents.json

touch data/workflow_state.json
echo "{}" > data/workflow_state.json

# Set permissions
chmod 755 data logs
sudo chown -R ubuntu:ubuntu data logs
```

### 2.5 Configure Environment Variables

**Critical:** Never commit `vars.secret` to git.

Create `/home/ubuntu/git/ghabs/nexus/vars.secret`:

```bash
TELEGRAM_TOKEN=<your_telegram_bot_token>
ALLOWED_USER=<your_user_id>
BASE_DIR=/home/ubuntu/git
PROJECT_CONFIG_PATH=config/project_config.yaml
GITHUB_TOKEN=<your_github_pat>
WEBHOOK_SECRET=$(openssl rand -base64 32)
WEBHOOK_PORT=8081
```

**Generate WEBHOOK_SECRET securely:**
```bash
openssl rand -base64 32
```

Set proper permissions:
```bash
chmod 600 /home/ubuntu/git/ghabs/nexus/vars.secret
```

### 2.6 Create Project Workspace Structure

Each agent project needs task directories:

```bash
for project in bm-agents casit-agents wlbl-agents; do
  mkdir -p /home/ubuntu/git/ghabs/agents/$project/.github/inbox
  mkdir -p /home/ubuntu/git/ghabs/agents/$project/.github/tasks/active
  mkdir -p /home/ubuntu/git/ghabs/agents/$project/.github/tasks/logs
done
```

Verify:
```bash
find /home/ubuntu/git/ghabs/agents -type d -name inbox -o -type d -name active
```

## Part 3: Service Installation

### 3.1 Install Systemd Service Files

```bash
cd /home/ubuntu/git/ghabs/nexus

# Copy service files
sudo cp nexus-bot.service /etc/systemd/system/
sudo cp nexus-health.service /etc/systemd/system/
sudo cp nexus-processor.service /etc/systemd/system/
sudo cp nexus-webhook.service /etc/systemd/system/

# Reload daemon
sudo systemctl daemon-reload

# Enable services (start on boot)
sudo systemctl enable nexus-bot.service
sudo systemctl enable nexus-health.service
sudo systemctl enable nexus-processor.service
sudo systemctl enable nexus-webhook.service
```

### 3.2 Start Services

```bash
sudo systemctl start nexus-bot.service
sudo systemctl start nexus-health.service
sudo systemctl start nexus-processor.service
sudo systemctl start nexus-webhook.service

# Verify all running
systemctl status nexus-bot.service nexus-health.service nexus-processor.service nexus-webhook.service --no-pager
```

### 3.3 Setup Log Rotation

```bash
sudo cp /home/ubuntu/git/ghabs/nexus/logrotate.conf /etc/logrotate.d/nexus

# Test config
sudo logrotate -d /etc/logrotate.d/nexus
```

## Part 4: Firewall Security (Host-Level)

### 4.1 Configure iptables for Webhook Access

Allow only GitHub hook IPs + your verification IP to port 8081:

```bash
# Fetch GitHub webhook IP ranges
GITHUB_HOOKS=$(python3 - <<'PY'
import json
import urllib.request
with urllib.request.urlopen("https://api.github.com/meta", timeout=10) as resp:
    data = json.load(resp)
for c in data.get("hooks", []):
    print(c)
PY
)

# Add rules for IPv4
for cidr in $GITHUB_HOOKS; do
  if [[ ! "$cidr" =~ : ]]; then
    sudo iptables -C INPUT -p tcp -s "$cidr" --dport 8081 -j ACCEPT 2>/dev/null \
      || sudo iptables -I INPUT 5 -p tcp -s "$cidr" --dport 8081 -m comment --comment "nexus-webhook" -j ACCEPT
  fi
done

# Add rules for IPv6
for cidr in $GITHUB_HOOKS; do
  if [[ "$cidr" =~ : ]]; then
    sudo ip6tables -C INPUT -p tcp -s "$cidr" --dport 8081 -j ACCEPT 2>/dev/null \
      || sudo ip6tables -I INPUT 1 -p tcp -s "$cidr" --dport 8081 -m comment --comment "nexus-webhook" -j ACCEPT
  fi
done

# Add your public IP (temporary for testing)
sudo iptables -C INPUT -p tcp -s YOUR_PUBLIC_IP --dport 8081 -j ACCEPT 2>/dev/null \
  || sudo iptables -I INPUT 5 -p tcp -s YOUR_PUBLIC_IP --dport 8081 -m comment --comment "nexus-webhook" -j ACCEPT

# Persist rules
sudo netfilter-persistent save
```

### 4.2 Verify Firewall Rules

```bash
# List all port 8081 rules
sudo iptables -S INPUT | grep 'dport 8081'
sudo ip6tables -S INPUT | grep 'dport 8081'
```

## Part 5: Configuration Fixes

### 5.1 Fix Telegram Bot Warning

Edit `/home/ubuntu/git/ghabs/nexus/src/telegram_bot.py` around line 2283:

Change:
```python
conv_handler = ConversationHandler(
    entry_points=[CommandHandler("new", start_selection)],
    states={...},
    fallbacks=[...],
    per_message=False  # ❌ Causes warning
)
```

To:
```python
conv_handler = ConversationHandler(
    entry_points=[CommandHandler("new", start_selection)],
    states={...},
    fallbacks=[...],
    per_message=True  # ✅ Fixes warning
)
```

Then restart:
```bash
sudo systemctl restart nexus-bot.service
```

### 5.2 Fix Log File Permissions

Ensure the `ubuntu` user can write to logs:

```bash
sudo chown -R ubuntu:ubuntu /home/ubuntu/git/ghabs/nexus/logs
chmod 755 /home/ubuntu/git/ghabs/nexus/logs
```

## Part 6: Testing

### 6.1 Health Check

```bash
curl http://127.0.0.1:8080/health
# Expected: {"status":"healthy",...}
```

### 6.2 Webhook Signature Test (Local)

```bash
bash
WEBHOOK_SECRET=$(grep WEBHOOK_SECRET /home/ubuntu/git/ghabs/nexus/vars.secret | cut -d= -f2)

payload='{"zen":"Keep it logically awesome.","hook_id":123}'
sig=$(printf "%s" "$payload" | openssl dgst -sha256 -hmac "$WEBHOOK_SECRET" | awk '{print $2}')

curl -X POST http://127.0.0.1:8081/webhook \
  -H "Content-Type: application/json" \
  -H "X-GitHub-Event: ping" \
  -H "X-GitHub-Delivery: local-test" \
  -H "X-Hub-Signature-256: sha256=$sig" \
  -d "$payload"
# Expected: {"status":"pong"}
```

### 6.3 Remote Test (From Your Local Machine)

```bash
# From your local machine (replace 158.180.233.4 with your server IP)
curl -s http://158.180.233.4:8081/health
# Should work if firewall allows your IP
```

## Part 7: GitHub Webhook Configuration

1. Go to https://github.com/Ghabs95/agents
2. **Settings → Webhooks → Add webhook**
3. Fill in:
   - **Payload URL:** `http://<YOUR_SERVER_IP>:8081/webhook`
   - **Content type:** `application/json`
   - **Secret:** (paste your WEBHOOK_SECRET from vars.secret)
   - **Events:** Select "Let me select individual events" → check:
     - `Issue comments` (workflow completion)
     - `Pull requests` (PR notifications)
     - `Pull request reviews` (review notifications)

4. **Test delivery** to verify signature verification works

## Monitoring & Verification

### Service Logs

```bash
# Telegram bot
sudo journalctl -u nexus-bot.service -f

# Webhook server
sudo journalctl -u nexus-webhook.service -f

# Processor
sudo journalctl -u nexus-processor.service -f
```

### Data Persistence

State files are stored in `data/`:
- `tracked_issues.json` — user issue subscriptions
- `launched_agents.json` — recent agent launches
- `workflow_state.json` — pause/resume/stop state

Backup daily:
```bash
cp -r /home/ubuntu/git/ghabs/nexus/data /backups/nexus-data.$(date +%Y%m%d)
```

## Troubleshooting

### Webhook Not Receiving Events

```bash
# 1. Verify firewall allows GitHub IPs
sudo iptables -S INPUT | grep 8081

# 2. Check webhook logs
sudo journalctl -u nexus-webhook.service -n 100

# 3. Test signature verification
# (see "Webhook Signature Test" above)

# 4. Verify secret matches in GitHub webhook settings
grep WEBHOOK_SECRET /home/ubuntu/git/ghabs/nexus/vars.secret
```

### Services Won't Start

```bash
# 1. Check venv exists
ls -la /home/ubuntu/git/ghabs/nexus/venv/bin/python

# 2. Verify requirements installed
source /home/ubuntu/git/ghabs/nexus/venv/bin/activate
python3 -c "import telegram; import flask"

# 3. Check vars.secret is readable
cat /home/ubuntu/git/ghabs/nexus/vars.secret

# 4. Review service logs
sudo systemctl status nexus-bot.service --no-pager -l
sudo journalctl -u nexus-bot.service -n 100
```

### GitHub CLI Not Authenticated

```bash
gh auth login
gh auth status
```

## Automated GitHub IP Range Refresh (Optional)

GitHub webhook IP ranges can change. Create a monthly cron job to refresh:

```bash
sudo tee /usr/local/bin/refresh-webhook-ips.sh > /dev/null <<'EOF'
#!/bin/bash
set -e

python3 - <<'PY'
import json
import urllib.request

with urllib.request.urlopen("https://api.github.com/meta", timeout=10) as resp:
    data = json.load(resp)

print(" ".join(data.get("hooks", [])))
PY
EOF

sudo chmod +x /usr/local/bin/refresh-webhook-ips.sh

# Add to crontab
sudo crontab -e
# Add: 0 2 1 * * /usr/local/bin/refresh-webhook-ips.sh >> /var/log/webhook-ips-refresh.log 2>&1
```

## Production Checklist

- [ ] Terraform infrastructure applied
- [ ] System dependencies installed
- [ ] Python venv created and dependencies installed
- [ ] Data/logs directories created with correct permissions
- [ ] vars.secret configured with all required keys
- [ ] Project workspace directories (.github/inbox, tasks/) created
- [ ] Service files installed and enabled
- [ ] All 4 services running without errors
- [ ] Log rotation configured
- [ ] Firewall rules (iptables + OCI NSG) applied
- [ ] GitHub CLI authenticated
- [ ] Webhook secret generated and stored securely
- [ ] Health checks passing
- [ ] GitHub webhook configured with correct URL + secret
- [ ] Webhook signature verification tested
- [ ] Remote IP firewall test passing (from your local machine)
- [ ] Telegram bot warnings cleared (per_message=True)
- [ ] Log file permissions verified

## Next Steps

1. Test with sample tasks through Telegram bot
2. Monitor logs for 24 hours
3. Configure GitHub webhook delivery retries if needed
4. Set up automated backups of `data/` directory
5. Document any customizations made to your environment

## Support

For issues:
1. Check logs: `sudo journalctl -u nexus-* -n 100`
2. Review [DEPLOYMENT.md](DEPLOYMENT.md) for operations guide
3. Check [ARCHITECTURE.md](ARCHITECTURE.md) for system design
4. Verify firewall rules: `sudo iptables -S INPUT | grep 8081`


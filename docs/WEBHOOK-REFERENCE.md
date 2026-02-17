# Webhook Server Quick Reference

## Service Management

```bash
# Check service status
sudo systemctl status nexus-webhook

# View logs (real-time)
tail -f /home/ubuntu/git/ghabs/nexus/logs/webhook.log

# View system logs
sudo journalctl -u nexus-webhook -f

# Restart service
sudo systemctl restart nexus-webhook

# Stop service
sudo systemctl stop nexus-webhook

# Start service
sudo systemctl start nexus-webhook
```

## Health Check

```bash
# Check if webhook server is responding
curl http://localhost:8081/health

# Expected response:
# {"service":"nexus-webhook","status":"healthy","version":"1.0.0"}
```

## GitHub Webhook Configuration

1. Go to: https://github.com/Ghabs95/agents/settings/hooks
2. Click "Add webhook"
3. Configure:
   - **Payload URL**: `http://<your-server-ip>:8081/webhook`
   - **Content type**: `application/json`
   - **Secret**: (use value from vars.secret: `WEBHOOK_SECRET`)
   - **Events**: Select individual events:
     - ✓ Issue comments
     - ✓ Pull requests  
     - ✓ Pull request reviews
4. Click "Add webhook"

## Testing

### Local Test (ping event)
```bash
curl -X POST http://localhost:8081/webhook \
  -H "X-GitHub-Event: ping" \
  -H "Content-Type: application/json" \
  -d '{"zen": "test"}'
```

### Check Recent Events
```bash
# View last 50 log entries
tail -n 50 /home/ubuntu/git/ghabs/nexus/logs/webhook.log

# Search for specific issue
grep "issue #123" /home/ubuntu/git/ghabs/nexus/logs/webhook.log
```

## Troubleshooting

### Service won't start
```bash
# Check for errors
sudo journalctl -u nexus-webhook -n 100

# Check if port is already in use
sudo lsof -i :8081

# Verify configuration
cat /home/ubuntu/git/ghabs/nexus/vars.secret | grep WEBHOOK
```

### Webhooks not being received
```bash
# 1. Check if service is running
sudo systemctl status nexus-webhook

# 2. Check if port is accessible from outside
curl http://<server-ip>:8081/health

# 3. Check GitHub webhook deliveries
# Go to: https://github.com/Ghabs95/agents/settings/hooks
# Click on your webhook → "Recent Deliveries"
# Check for failed delivery attempts

# 4. Check firewall
sudo ufw status
# If blocked, allow port: sudo ufw allow 8081/tcp
```

### Signature verification failing
```bash
# Check if WEBHOOK_SECRET matches GitHub configuration
grep WEBHOOK_SECRET /home/ubuntu/git/ghabs/nexus/vars.secret

# Verify in GitHub webhook settings
# Settings → Webhooks → Edit → Secret field
```

## Production Deployment

For production, use nginx reverse proxy with SSL:

```nginx
# /etc/nginx/sites-available/nexus-webhook
server {
    listen 443 ssl;
    server_name webhooks.yourdomain.com;
    
    ssl_certificate /etc/letsencrypt/live/webhooks.yourdomain.com/fullchain.pem;
    ssl_certificate_key /etc/letsencrypt/live/webhooks.yourdomain.com/privkey.pem;
    
    location /webhook {
        proxy_pass http://localhost:8081/webhook;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
    }
}
```

Then update GitHub webhook URL to: `https://webhooks.yourdomain.com/webhook`

## Event Processing Flow

1. **GitHub Event** → Webhook POST to `/webhook`
2. **Signature Verification** → Validate HMAC-SHA256
3. **Event Routing**:
   - `issue_comment` → Detect workflow completion or next agent
   - `pull_request` → Log PR events
   - `pull_request_review` → Log review events
4. **Agent Launch** → If next agent mentioned, invoke copilot
5. **Notification** → Send Telegram notification

## Performance

- **Latency**: < 2 seconds from GitHub event to agent launch
- **Memory**: ~20-30 MB per process
- **CPU**: Minimal (spikes during event processing)

## Security

- **Signature Verification**: All requests verified with WEBHOOK_SECRET
- **Rate Limiting**: Consider adding nginx rate limiting
- **Firewall**: Restrict port 8081 to GitHub IP ranges for production

GitHub webhook IPs: https://api.github.com/meta (check `hooks` array)

Example UFW rule:
```bash
# Allow only from specific IP ranges
sudo ufw allow from 140.82.112.0/20 to any port 8081
sudo ufw allow from 143.55.64.0/20 to any port 8081
```

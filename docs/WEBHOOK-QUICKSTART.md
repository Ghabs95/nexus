# Quick Start: Enable GitHub Webhooks

## TL;DR

Your webhook server is **already running** on port 8081! Just configure GitHub to send events to it.

## Step 1: Get Your Webhook Secret

```bash
grep WEBHOOK_SECRET /home/ubuntu/git/ghabs/nexus/vars.secret
```

**Result**: `b6fa2513cbf3d41cdbddbac4b6ff774ae7b6ac2e632ba988a06dcc2eff22edc1`

## Step 2: Configure GitHub Webhook

1. Go to: **https://github.com/Ghabs95/agents/settings/hooks**
2. Click **"Add webhook"**
3. Fill in:

```
Payload URL:   http://<YOUR-SERVER-IP>:8081/webhook
Content type:  application/json
Secret:        b6fa2513cbf3d41cdbddbac4b6ff774ae7b6ac2e632ba988a06dcc2eff22edc1
```

4. Select **individual events**:
   - ✅ Issue comments
   - ✅ Pull requests
   - ✅ Pull request reviews

5. Ensure **"Active"** is checked
6. Click **"Add webhook"**

## Step 3: Test It!

### Option A: Create Test Issue
```bash
# Create a test issue
gh issue create --repo Ghabs95/agents \
  --title "Test webhook integration" \
  --body "Testing real-time agent chaining" \
  --label "workflow:fast-track,project:casit"

# Add a comment mentioning next agent
gh issue comment <ISSUE#> --repo Ghabs95/agents \
  --body "Initial test complete. Ready for @Atlas"
```

### Option B: Use Existing Issue
1. Go to any open issue in Ghabs95/agents
2. Add a comment: "Test complete. Ready for @Atlas"
3. Watch webhook logs:

```bash
tail -f /home/ubuntu/git/ghabs/nexus/logs/webhook.log
```

## Step 4: Verify It Works

### Check Webhook Received Event
```bash
# Should see entries like:
# INFO - 📨 Webhook received: issue_comment (delivery: abc123)
# INFO - 🔗 Chaining to @Atlas for issue #123
# INFO - ✅ Successfully launched @Atlas for issue #123
```

### Check GitHub Webhook Deliveries
1. Go to: https://github.com/Ghabs95/agents/settings/hooks
2. Click on your webhook
3. Click **"Recent Deliveries"** tab
4. Should see successful deliveries (green checkmarks)

### Check Agent Launched
```bash
# Check for running copilot process
pgrep -af copilot

# Check recent logs
ls -lt /home/ubuntu/git/case_italia/.github/tasks/logs/ | head -5
```

## Expected Timeline

| Action | Time | How to Verify |
|--------|------|---------------|
| GitHub sends webhook | ~100ms | GitHub → Recent Deliveries |
| Webhook server receives | ~10ms | Check webhook.log |
| Agent launches | ~1s | Check copilot process |
| **Total latency** | **<2s** | Compare to old 15-30s! |

## Troubleshooting

### Webhook not receiving events
```bash
# 1. Check service is running
sudo systemctl status nexus-webhook

# 2. Check if port is accessible
curl http://localhost:8081/health

# 3. Check from outside (replace with your IP)
curl http://<YOUR-SERVER-IP>:8081/health

# 4. Check firewall
sudo ufw status
# If blocked: sudo ufw allow 8081/tcp
```

### GitHub shows delivery failed
1. Check **"Recent Deliveries"** for error message
2. Common issues:
   - ❌ Connection timeout → Check firewall
   - ❌ 403 Forbidden → Signature mismatch (check secret)
   - ❌ Connection refused → Service not running

### Events received but agent not launching
```bash
# Check webhook logs for errors
tail -n 100 /home/ubuntu/git/ghabs/nexus/logs/webhook.log | grep ERROR

# Check if duplicate prevention is blocking
grep "already running\|recently launched\|Recent log file" logs/webhook.log
```

## Production Checklist

Before going live:

- [ ] GitHub webhook configured and tested
- [ ] Webhook secret verified (matches vars.secret)
- [ ] Firewall allows port 8081 (or use nginx reverse proxy)
- [ ] Service auto-starts on boot: `sudo systemctl is-enabled nexus-webhook`
- [ ] Logs rotating properly (check logrotate.conf)
- [ ] Consider nginx reverse proxy with SSL for production

## Monitoring

```bash
# Real-time webhook logs
tail -f /home/ubuntu/git/ghabs/nexus/logs/webhook.log

# Service status
watch -n 5 'sudo systemctl status nexus-webhook --no-pager'

# Check webhook is responding
watch -n 10 'curl -s http://localhost:8081/health | jq .'
```

## What Happens Next?

Once configured, your workflow becomes:

1. **Issue created** → Agent launches automatically
2. **Agent posts comment** mentioning next agent → Next agent launches within 2 seconds
3. **Agent posts "workflow complete"** → PR detection & notification automatic
4. **You receive Telegram notification** → Click inline buttons to approve/review

All **fully automated** with **near-instant** response times!

## Need Help?

- **Operations guide**: WEBHOOK-REFERENCE.md
- **Implementation details**: WEBHOOK-PHASE1-COMPLETE.md
- **Full summary**: WEBHOOK-IMPLEMENTATION-SUMMARY.md
- **Architecture**: ARCHITECTURE.md

---

**Your Webhook Secret** (save this):
```
b6fa2513cbf3d41cdbddbac4b6ff774ae7b6ac2e632ba988a06dcc2eff22edc1
```

**Webhook URL**:
```
http://<YOUR-SERVER-IP>:8081/webhook
```

Replace `<YOUR-SERVER-IP>` with your actual server IP address.

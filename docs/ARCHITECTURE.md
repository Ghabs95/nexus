# Nexus System Architecture

High-level architecture and design documentation for the Nexus AI task automation system.

## System Overview

Nexus is a Telegram-based workflow automation system that orchestrates AI agents to complete software development tasks. It consists of three main services:

1. **Telegram Bot** - User interface and command handler
2. **Inbox Processor** - Workflow orchestration and agent management
3. **Health Check** - Monitoring and metrics endpoint

## Architecture Diagram

```
┌─────────────────────────────────────────────────────────────────┐
│                         User Layer                              │
└─────────────────────────────────────────────────────────────────┘
                              │
                    ┌─────────▼─────────┐
                    │  Telegram API     │
                    └─────────┬─────────┘
                              │
┌─────────────────────────────▼─────────────────────────────────┐
│                      Nexus Bot (telegram_bot.py)              │
│  - Command handlers (/new, /status, /pause, /logs, etc.)     │
│  - Inline keyboard callback handler                           │
│  - Rate limiting (15 limits, sliding window)                  │
│  - User authentication                                        │
│  - Per-project issue tracking                                 │
└───────────────┬───────────────────────────────┬───────────────┘
                │                               │
        ┌───────▼────────┐             ┌────────▼────────┐
        │  State Manager │             │  User Manager   │
        │  - Workflow    │             │  - Tracking     │
        │  - Agents      │             │  - Projects     │
        │  - Audit log   │             │  - Stats        │
        └────────────────┘             └─────────────────┘
                │
┌───────────────▼───────────────────────────────────────────────┐
│              Inbox Processor (inbox_processor.py)             │
│  - Monitor inbox files                                        │
│  - Create GitHub issues with SOP checklists                   │
│  - Launch Copilot CLI agents                                  │
│  - Auto-chain agents based on completion markers              │
│  - Detect and kill stuck agents (timeout: 60s)                │
│  - Retry failed agents (max: 3 attempts)                      │
│  - Check for workflow completion & linked PRs                 │
└───┬───────────────────────────────────────┬───────────────────┘
    │                                       │
    │                              ┌────────▼────────┐
    │                              │ Agent Monitor   │
    │                              │ - Timeout track │
    │                              │ - Retry logic   │
    │                              │ - PID tracking  │
    │                              └─────────────────┘
    │
┌───▼──────────────────────────────────────────────────────────┐
│                    GitHub API (via gh CLI)                    │
│  - Create issues with workflow labels                        │
│  - Monitor comments for completion markers                   │
│  - Search for linked PRs                                     │
│  - Post agent updates and handoffs                           │
└───┬──────────────────────────────────────────────────────────┘
    │
┌───▼──────────────────────────────────────────────────────────┐
│              Copilot CLI Agents (subprocess)                  │
│  - @ProjectLead - Triage and routing                         │
│  - @Architect - Design and ADR                               │
│  - @ProductDesigner - UX wireframes                          │
│  - Tier 2 Leads - Implementation                             │
│  - @QAGuard - Quality assurance                              │
│  - @OpsCommander - Deployment                                │
│  - @Scribe - Documentation                                   │
└──────────────────────────────────────────────────────────────┘
```

## Data Flow

### 1. Task Submission Flow

```
User Voice Note → Telegram → Bot Transcribes (Gemini)
                                  ↓
                          Auto-route to Project
                                  ↓
                          Save to Inbox File
                                  ↓
                    Inbox Processor Detects File
                                  ↓
                      Create GitHub Issue
                                  ↓
                    Launch @ProjectLead Agent
```

### 2. Workflow Execution Flow

```
Agent Starts → Posts to GitHub
                     ↓
              Completes Work
                     ↓
          Posts "Ready for @NextAgent"
                     ↓
    Inbox Processor Detects Comment
                     ↓
         Auto-chains to Next Agent
                     ↓
              [Repeat until done]
                     ↓
         Final Agent Completes
                     ↓
    Search for Linked PR
                     ↓
  Notify User with Review Buttons
```

### 3. Notification Flow

```
Event Occurs → Check Rate Limits
                     ↓
            Build Inline Keyboard
                     ↓
         Send to Telegram API
                     ↓
     User Clicks Button → Callback
                     ↓
        Parse action_issuenum
                     ↓
     Execute Handler Function
```

## Core Components

### 1. Telegram Bot (`src/telegram_bot.py`)

**Responsibilities:**
- Handle user commands
- Manage conversation state
- Send notifications
- Process inline keyboard callbacks
- Enforce rate limits

**Key Functions:**
- `start_handler()` - Welcome message
- `new_handler()` - Start workflow wizard
- `status_handler()` - Show pending tasks
- `logs_handler()` - View agent logs
- `pause_handler()` - Pause workflow
- `inline_keyboard_handler()` - Process button clicks

**State Files:**
- `data/tracked_issues.json` - User subscriptions
- `data/rate_limits.json` - Rate limiter state

### 2. Inbox Processor (`src/inbox_processor.py`)

**Responsibilities:**
- Monitor inbox directories
- Create GitHub issues
- Launch and monitor agents
- Detect completions
- Auto-chain agents
- Handle timeouts and retries

**Key Functions:**
- `process_file()` - Create issue from inbox file
- `check_completed_agents()` - Detect agent completions
- `check_stuck_agents()` - Timeout detection
- `invoke_copilot_agent()` - Launch agent subprocess
- `check_and_notify_pr()` - Find linked PRs

**Monitoring Loop:**
```python
while True:
    check_stuck_agents()        # Kill timeouts
    check_completed_agents()    # Auto-chain
    process_inbox_files()       # New tasks
    sleep(15)                   # Wait 15s
```

### 3. State Manager (`src/state_manager.py`)

**Responsibilities:**
- Persist workflow state
- Track launched agents
- Manage issue tracking
- Audit logging

**Key Methods:**
- `StateManager.load_workflow_state()` - Load paused/stopped issues
- `StateManager.set_workflow_state()` - Update state
- `StateManager.register_launched_agent()` - Track PIDs
- `StateManager.audit_log()` - Log events

**Storage:**
- `data/workflow_state.json` - Pause/resume/stop state
- `data/launched_agents.json` - Recent agent PIDs (2-min TTL)
- `data/tracked_issues.json` - User subscriptions
- `logs/audit.log` - Append-only event log

### 4. Agent Monitor (`src/agent_monitor.py`)

**Responsibilities:**
- Track agent timeouts
- Manage retry logic
- Kill stuck processes
- Route workflows by tier

**Key Classes:**
- `AgentMonitor` - Timeout and retry tracking
- `WorkflowRouter` - Tier detection (full/shortened/fast-track)

**Retry Logic:**
- Max 3 attempts per agent per issue
- Exponential backoff between retries
- Per-issue, per-agent counters
- Reset on successful completion

### 5. Notifications (`src/notifications.py`)

**Responsibilities:**
- Build inline keyboards
- Send rich notifications
- Provide context-aware buttons

**Key Classes:**
- `InlineKeyboard` - Fluent API builder

**Notification Types:**
- `notify_agent_needs_input()` - Agent blocked
- `notify_workflow_started()` - New workflow
- `notify_agent_completed()` - Agent done
- `notify_agent_timeout()` - Stuck/failed
- `notify_workflow_completed()` - With PR link
- `notify_implementation_requested()` - Approval needed

### 6. Rate Limiter (`src/rate_limiter.py`)

**Responsibilities:**
- Enforce usage limits
- Sliding window algorithm
- Per-user tracking

**Limits:**
- Global: 30 requests/min
- Logs: 5 requests/min
- Stats: 10 requests/min
- Direct: 3 requests/min
- GitHub API: 100 requests/hour

### 7. User Manager (`src/user_manager.py`)

**Responsibilities:**
- Track user subscriptions
- Per-project issue lists
- User statistics

**Key Methods:**
- `track_issue()` - Subscribe to issue
- `untrack_issue()` - Unsubscribe
- `get_user_tracked_issues()` - List subscriptions
- `get_all_users_stats()` - Engagement metrics

## Workflow Tiers

### Full SOP (9 steps)
- Feature requests
- Complete design process
- All quality gates

**Agents:** ProjectLead → Atlas → Architect → ProductDesigner → Tier2Lead → QAGuard → Privacy → OpsCommander → Scribe

### Shortened SOP (6 steps)
- Bug fixes
- Skip Vision/UX steps
- Essential quality gates

**Agents:** ProjectLead → Atlas → Architect → Tier2Lead → QAGuard → OpsCommander

### Fast-Track (4 steps)
- Hotfixes, chores
- Direct to Copilot
- Minimal overhead

**Agents:** ProjectLead → Tier2Lead → QAGuard → OpsCommander

## PR Merge Approval Policy

The system enforces human review gates to prevent @OpsCommander from auto-merging pull requests without oversight.

### Two-Level Configuration

**1. Project-Level Policy** (`config/project_config.yaml`)

Controls the global enforcement level:

```yaml
require_human_merge_approval: always  # Options: always | workflow-based | never
```

- `always` (Recommended) - Human approval REQUIRED for all PRs, overrides workflow settings
- `workflow-based` - Workflow YAML controls per-workflow/per-step behavior
- `never` - Allow auto-merge (NOT recommended for production)

**2. Workflow-Level Preference** (`agents/workflows/ghabs_org_workflow.yaml`)

Only applies when project policy is `workflow-based`:

```yaml
monitoring:
  require_human_merge_approval: true  # Workflow preference
```

### Precedence Rules

| Project Config | Workflow Config | Result |
|----------------|-----------------|---------|
| `always` | [ignored] | Human approval REQUIRED |
| `workflow-based` | `true` | Human approval required |
| `workflow-based` | `false` | Auto-merge allowed |
| `never` | [ignored] | Auto-merge ALWAYS allowed |

### @OpsCommander Behavior

When human approval is required, @OpsCommander:
1. Posts deployment readiness comment: 🚀 Deployment ready. PR requires human review before merge
2. Waits for explicit human approval
3. NEVER executes `github:merge_pr` tool automatically

See skill definitions:
- [wlbl-agents/.agent/skills/ops_commander/SKILL.md](../agents/wlbl-agents/.agent/skills/ops_commander/SKILL.md)
- [casit-agents/.agent/skills/ops_commander/SKILL.md](../agents/casit-agents/.agent/skills/ops_commander/SKILL.md)
- [bm-agents/.agent/skills/ops_commander/SKILL.md](../agents/bm-agents/.agent/skills/ops_commander/SKILL.md)

### Rationale

Prevents accidental deployments from:
- Unreviewed code changes
- Security vulnerabilities
- Breaking changes without human oversight
- Compliance violations

Maintains human-in-the-loop for critical decision points while preserving automation for routine tasks.

## Configuration

### Environment Variables (`vars.secret`)
```bash
TELEGRAM_TOKEN          # Bot authentication
TELEGRAM_CHAT_ID        # Target chat
ALLOWED_USER            # Authorized user ID
AI_API_KEY              # Google Gemini key
PROJECT_CONFIG_PATH     # Per-project git_repo/git_repos settings
```

### Project Configuration (`src/config.py`)
```python
PROJECT_CONFIG = {
    "casit": {
        "workspace": "../casit-agents/workspace",
        "agents_dir": "../casit-agents/agents"
    },
    # ... more projects
}

WORKFLOW_CHAIN = {
    "full": [("ProjectLead", "Triage"), ...],
    "shortened": [("ProjectLead", "Triage"), ...],
    "fast-track": [("ProjectLead", "Triage"), ...]
}
```

## Persistence & Recovery

### State Persistence
All state is persisted to JSON files in `data/`:
- Survives service restarts
- Manual editing possible (caution!)
- Backed up via separate script

### Crash Recovery
Services auto-restart via systemd:
```ini
[Service]
Restart=always
RestartSec=10
```

State is loaded from disk on startup:
- Paused workflows remain paused
- Stopped workflows remain stopped
- Active workflows resume monitoring

### Audit Trail
All events logged to `logs/audit.log`:
- Agent launches with PID
- State changes (pause/resume/stop)
- Timeouts and kills
- Retries and failures
- Workflow completions

Query via `/audit <issue#>` command.

## Security Model

### Authentication
- Telegram: Single authorized user (`ALLOWED_USER`)
- GitHub: CLI authenticated with user credentials
- No public API endpoints

### Authorization
- All commands restricted to authorized user
- Rate limits prevent abuse
- No write access to sensitive files from bot

### Data Privacy
- All data stored locally
- No external analytics
- Logs contain issue numbers (not content)
- State files contain metadata only

## Performance Characteristics

### Resource Usage
- **CPU**: Low (< 5% idle, < 30% peak)
- **Memory**: ~100MB per service
- **Disk**: ~50MB code, ~10MB state/logs
- **Network**: Minimal (polling intervals)

### Scalability Limits
- **Users**: Single user (by design)
- **Concurrent Workflows**: ~10-20 (limited by agent processes)
- **Issues Tracked**: Unlimited (JSON scales to thousands)
- **Rate Limits**: Configurable per endpoint

### Latency
- **Command Response**: < 1s (Telegram API)
- **Agent Launch**: 2-5s (subprocess spawn)
- **Completion Detection**: 15-30s (polling interval)
- **Notification Delivery**: < 2s (Telegram API)

## Extension Points

### Adding New Commands
```python
# In telegram_bot.py
async def my_command_handler(update, context):
    # Your logic here
    pass

# Register in main()
application.add_handler(CommandHandler("mycommand", my_command_handler))
```

### Adding New Notifications
```python
# In notifications.py
def notify_my_event(issue_number: str) -> bool:
    keyboard = InlineKeyboard()
    # Build keyboard...
    return send_notification(message, keyboard=keyboard)
```

### Adding New Agents
```python
# In config.py
WORKFLOW_CHAIN = {
    "full": [
        ("ProjectLead", "Triage"),
        ("MyNewAgent", "Custom step"),  # Add here
        # ...
    ]
}
```

### Custom Integrations
- Slack: Replace Telegram handlers with Slack client
- Discord: Use discord.py instead of python-telegram-bot
- Web UI: Add Flask routes alongside health_check.py
- CI/CD: Trigger workflows from GitHub Actions

## Testing Strategy

### Unit Tests (115 total)
- `tests/test_error_handling.py` - Retry logic, validation
- `tests/test_analytics.py` - Metrics parsing
- `tests/test_agent_monitor.py` - Timeout detection
- `tests/test_rate_limiter.py` - Rate limiting
- `tests/test_user_manager.py` - User tracking
- `tests/test_notifications.py` - Keyboard building
- `tests/test_state_manager.py` - State persistence

### Integration Testing
Manual workflow tests:
1. Submit task via Telegram
2. Verify GitHub issue created
3. Check agent launch
4. Simulate completion
5. Verify auto-chain
6. Check notification delivery

### Monitoring in Production
- Health check endpoint (`/health`, `/status`, `/metrics`)
- Systemd journal logs
- Audit log analysis
- Rate limiter tracking

## Known Limitations

1. **Single User**: By design, not multi-tenant
2. **GitHub CLI Dependency**: Requires `gh` authentication
3. **Polling-Based**: 15s latency between checks (webhooks would improve)
4. **No Database**: JSON files (simple but not scalable to 1000s of issues)
5. **Local Only**: No remote API access

## Future Enhancements

See [WEBHOOKS-TODO.md](WEBHOOKS-TODO.md) for webhook implementation plan.

Other potential improvements:
- Multi-user support with RBAC
- Web dashboard for monitoring
- Database backend (PostgreSQL)
- Docker containerization
- Kubernetes deployment
- Prometheus metrics export
- Sentry error tracking
- Real-time WebSocket updates

## Troubleshooting Reference

| Symptom | Likely Cause | Solution |
|---------|--------------|----------|
| Service won't start | Missing env vars | Check `vars.secret`, verify with `env \| grep TELEGRAM` |
| No auto-chaining | Comment format wrong | Ensure `` Ready for `@AgentName` `` format |
| High memory | Stuck processes | `pkill -f "copilot.*issues/"` |
| Rate limit errors | Too many requests | Wait for window to reset or adjust limits |
| GitHub auth errors | Token expired | `gh auth login` |
| Notification silence | Telegram blocked | Check bot is not blocked in chat |
| Agent timeout | Long-running task | Increase `STUCK_AGENT_THRESHOLD` in config |

## References

- [README.md](README.md) - User guide and command reference
- [DEPLOYMENT.md](DEPLOYMENT.md) - Production deployment guide
- [requirements.txt](requirements.txt) - Python dependencies
- [tests/](tests/) - Unit test suite

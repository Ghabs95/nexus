# AI Orchestrator: Copilot CLI + Gemini CLI Integration

## Overview

The AI Orchestrator is a unified framework for managing two AI tools:
- **Copilot CLI**: For complex code generation, architecture, and reasoning tasks
- **Gemini CLI**: For fast analysis, content generation, and simple classification

The system intelligently routes tasks to the appropriate tool based on agent requirements and performance characteristics, with automatic fallback support when tools are unavailable or rate-limited.

## Architecture

### Core Components

```
┌──────────────────┬──────────────────────┐
│           AIOrchestrator                 │
│  - Tool Selection Logic                 │
│  - Rate Limit Management                │
│  - Fallback Orchestration               │
│  - Result Validation                    │
└──────────────────┬──────────────────────┘
         │
         ├─────────────────┬──────────────────┐
         │                 │                  │
   ┌────▼────┐    ┌──────▼─────┐
   │ Copilot │    │   Gemini   │
   │   CLI   │    │    CLI     │
   └─────────┘    └────────────┘
         │              │
    code-gen       fast-routing
    complex         lightweight
    reasoning       analysis
```

### Tool Capabilities

| Tool | Strengths | Weaknesses | Best For |
|------|-----------|-----------|----------|
| **Copilot CLI** | • Deep codebase understanding<br>• Multi-file refactoring<br>• Architecture decisions<br>• Complex reasoning | • Slower execution<br>• Higher resource usage<br>• May hit Pro+ limits during heavy usage | Code generation, architecture, complex implementations |
| **Gemini CLI** | • Fast execution<br>• Lightweight<br>• Good for simple tasks<br>• Low resource usage | • Limited codebase context<br>• Not ideal for complex refactoring<br>• Less sophisticated reasoning | Task routing, content generation, quick analysis |

## Tool Selection Strategy

### Copilot Preferred Agents
```python
COPILOT_PREFERRED = {
    "ProjectLead":      "Complex task triage, architecture routing",
    "Atlas":            "Technical feasibility assessment",
    "Architect":        "System design, major refactoring",
    "Tier2Lead":        "Feature implementation, debugging",
    "FrontendLead":     "Complex UI implementations",
    "BackendLead":      "API design, database schema",
    "MobileLead":       "Flutter/app architecture",
}
```

### Gemini Preferred Agents
```python
GEMINI_PREFERRED = {
    "ProductDesigner":  "Quick design feedback, low complexity",
    "QAGuard":          "Test validation, simple verification",
    "Scribe":           "Documentation writing, content gen",
    "OpsCommander":     "Simple deployment, ops tasks",
    "Privacy":          "Policy review, compliance checks",
}
```

## Configuration

### Environment Variables

```bash
# AI Orchestrator settings
GEMINI_CLI_PATH="gemini"              # Path to gemini-cli
COPILOT_CLI_PATH="copilot"            # Path to copilot-cli
AI_FALLBACK_ENABLED="true"            # Enable fallback support
AI_RATE_LIMIT_TTL="3600"              # Rate limit timeout (seconds)
AI_MAX_RETRIES="2"                    # Max retries before marking unavailable
```

### config.py Settings

```python
AI_TOOL_PREFERENCES = {
    "ProjectLead": "copilot",
    "Atlas": "copilot",
    "QAGuard": "gemini",
    # ... more agents
}

ORCHESTRATOR_CONFIG = {
    "gemini_cli_path": os.getenv("GEMINI_CLI_PATH", "gemini"),
    "copilot_cli_path": os.getenv("COPILOT_CLI_PATH", "copilot"),
    "tool_preferences": AI_TOOL_PREFERENCES,
    "fallback_enabled": True,
    "rate_limit_ttl": 3600,
    "max_retries": 2,
}
```

## Usage Patterns

### Agent Invocation (with auto-routing)

```python
from ai_orchestrator import get_orchestrator

orchestrator = get_orchestrator(ORCHESTRATOR_CONFIG)

# Primary: Copilot, Fallback: Gemini
pid, tool_used = orchestrator.invoke_agent(
    agent_prompt="You are @ProjectLead...",
    workspace_dir="/path/to/workspace",
    agents_dir="/path/to/agents",
    base_dir=BASE_DIR,
    agent_name="ProjectLead"  # Optional: uses config for tool selection
)

print(f"Agent launched with {tool_used.value}: PID {pid}")
```

### Force Gemini for Testing

```python
# Temporarily use Gemini for a specific agent
pid, tool_used = orchestrator.invoke_agent(
    agent_prompt="...",
    workspace_dir="...",
    agents_dir="...",
    base_dir=BASE_DIR,
    use_gemini=True  # Force Gemini (Copilot is fallback)
)
```

### Text Analysis with Fallback

```python
# Analyze text with automatic tool selection and fallback
result = orchestrator.run_text_to_speech_analysis(
    text="implement user authentication",
    task="classify",  # or "route", "generate_name"
    projects=["case-italia", "wallible", "biome"],
    types=["feature", "bug-fix", "improvement"]
)

# Returns: {"project": "case-italia", "type": "feature", "issue_name": "user-auth-impl"}
```

## Fallback & Rate Limiting

### Fallback Trigger Logic

Fallback activates when:
1. Primary tool is not available (CLI not found)
2. Primary tool times out (>30 sec for analysis, task-dependent for agents)
3. Primary tool returns error (failed to execute)
4. Primary tool is rate-limited (quota exceeded)

### Rate Limit Recovery

```
Time 0:00   - Copilot invoked (succeeds)
Time 1:05   - Copilot rate-limited → Gemini becomes primary for 1 hour
Time 1:10   - Copilot still rate-limited, additional retries use Gemini
Time 2:05   - Rate limit expires, Copilot reset to primary
```

### Manual Rate Limit Reset

```python
# If you manually raise limits on GitHub/Google:
orchestrator._rate_limits = {}
orchestrator._tool_available = {}
```

## Monitoring & Logging

### Log Output Example

```
🤖 Launching AI agent in /agents (mode: initial)
   Workspace: /workspace
   Workflow: /new_feature (tier: full)
✅ Copilot available
🚀 Copilot CLI launched (PID: 12345)
   Log: /workspace/.github/tasks/logs/copilot_20260217_101530.log

// Later: if Copilot times out
❌ Copilot invocation failed: Timeout
🔄 Attempting fallback from copilot → gemini
🤖 Launching Gemini CLI agent
✅ Gemini available
🚀 Gemini launched (PID: 12346)
✅ Fallback gemini succeeded
```

### Tool Usage Tracking

All tool invocations are logged with:
- Tool used (copilot/gemini)
- Issue number
- Agent name
- Timestamp
- Success/failure status
- Fallback indicators

View in:
- `data/launched_agents.json` - Recent launches
- `logs/audit.log` - Complete audit trail
- `.github/tasks/logs/` - Per-agent logs

## Performance Benchmarks

### Single Task Analysis (text → JSON)

| Task | Copilot | Gemini |
|------|---------|--------|
| Route task to agent | 15-25s | 3-8s |
| Classify issue type | 12-20s | 2-5s |
| Generate issue name | 8-15s | 1-3s |
| **Fallback overhead** | +2-3s | +2-3s |

### Agent Full Workflow (task → completion)

| Workflow | Copilot | Gemini | Notes |
|----------|---------|--------|-------|
| **fast-track** (4 steps) | 40-60 min | N/A*1 | Copilot better for code |
| **shortened** (6 steps) | 45-70 min | N/A*1 | Complex reasoning needed |
| **full** (9 steps) | 90-150 min | N/A*1 | Architecture depth needed |

*1 Gemini not recommended for full agent workflows due to limited codebase understanding.

## Evaluation Results

### When Copilot Excels

✅ **Architecture Review**: Understands system boundaries, identifies design issues
✅ **Code Generation**: Creates functional, idiomatic code with proper error handling
✅ **Refactoring**: Safely transforms large codebases while maintaining semantics
✅ **Debugging**: Traces issues across multiple files and contexts
✅ **API Design**: Creates coherent, scalable interfaces

❌ Gemini often produces incomplete code or misses architectural concerns

### When Gemini Excels

✅ **Classification**: Quickly categorizes issues into predefined categories
✅ **Content Writing**: Generates clear documentation and communications
✅ **Routing**: Maps tasks to appropriate agents without deep analysis
✅ **Simple Validation**: Confirms format, syntax, or basic requirements
✅ **Speed**: Completes in seconds vs. minutes for simple tasks

❌ Copilot is overkill and slower for these tasks

### Hybrid Approach Benefits

| Scenario | Approach | Benefit |
|----------|----------|---------|
| **Code Review** | Copilot routes → Gemini validates | 70% faster validation |
| **Task Intake** | Gemini classifies → Copilot architects | 40% faster overall |
| **Fallback** | Copilot down → Gemini continues | 100% uptime |
| **Load Peak** | Gemini handles non-code tasks | Copilot quota preserved |

## Troubleshooting

### "Tool not found" Error

```bash
# Check tool availability
which copilot
which gemini

# Install missing tools
# Copilot
brew install copilot-cli  # macOS
pip install copilot-cli    # or from npm: npm install -g copilot-cli

# Gemini
pip install google-generativeai-cli  # or: npm install -g @google/generative-ai-cli
```

### Rate Limiting Issues

```bash
# Check your limits
gh api rate_limit  # GitHub (for Copilot context)

# Temporary workaround: Use Gemini more
# Permanent: Upgrade subscriptions
# - Copilot Pro+ (higher quotas)
# - Google AI Studio Pro (higher quotas)
```

### Slow Fallback Activation

Fallback only triggers on actual failure, not on slowness. To force switching:

```python
# Manually record failure
orchestrator.record_failure(AIProvider.COPILOT)
# Will use Gemini as primary for next 1 hour

# Or force Gemini for next invocation
pid, tool = orchestrator.invoke_agent(..., use_gemini=True)
```

## Future Enhancements

### Planned Improvements

1. **Per-Agent Performance Tuning**
   - Track success rates per agent
   - Automatically adjust primary based on historical performance

2. **Predictive Fallback**
   - "If Copilot historical success < 80%, prep Gemini"
   - Load balancing during peak times

3. **Tool-Specific Prompting**
   - Optimize prompts for each tool's strengths
   - Copilot: emphasize architectural constraints
   - Gemini: emphasize speed and format requirements

4. **Batch Processing**
   - Send multiple tasks to both tools in parallel
   - Return whichever completes first

5. **Cost Optimization**
   - Track cost per tool
   - Route based on cost efficiency vs. quality trade-off

6. **Claude Integration**
   - Add Claude as third option forsimilar capabilities

### Contribution Areas

- Add new agent tool preferences to `AI_TOOL_PREFERENCES`
- Implement new analysis tasks in `orchestrator.run_text_to_speech_analysis()`
- Create tool-specific prompt variants
- Add monitoring dashboards

## Related Documentation

- [Agent Organization Playbook](../agents/AGENT-ORG-PLAYBOOK.md)
- [Workflow Architecture](./ARCHITECTURE.md)
- [Telegram Bot Commands](/docs/CONTRIBUTING.md)

## Support

Issues with the AI Orchestrator? 

1. Check logs: `tail -f logs/inbox_processor.log | grep orchestrator`
2. Test tool availability: `orchestrator.check_tool_available(AIProvider.COPILOT)`
3. Check subscriptions and quotas
4. Open issue with orchestrator version and error details

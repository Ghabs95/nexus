# Agent Completion Summary Format

Agents running within the Nexus framework should write a structured `completion_summary.json` file when their work completes. This enables the processor to post rich, semantic GitHub comments instead of raw log dumps.

## File Location

Write to: `.nexus/tasks/logs/completion_summary_{ISSUE_NUMBER}.json`

Example: `.nexus/tasks/logs/completion_summary_35.json`

## JSON Schema

```json
{
  "status": "complete",
  "summary": "Brief one-line summary of work completed",
  "key_findings": [
    "Finding or result 1",
    "Finding or result 2"
  ],
  "effort_breakdown": {
    "task_name": "duration or effort",
    "another_task": "3 hours"
  },
  "verdict": "Assessment of work quality (e.g., Ready to proceed)",
  "next_agent": "agent_type_for_next_step"
}
```

## Field Reference

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `status` | string | ✅ | Completion status: `complete`, `in-progress`, or `blocked` |
| `summary` | string | ✅ | One-line summary of what was accomplished |
| `key_findings` | array[string] | ❌ | Important discoveries, test results, or findings from the work |
| `effort_breakdown` | object | ❌ | Key-value pairs showing time/effort spent on major tasks |
| `verdict` | string | ❌ | Quality assessment or readiness statement |
| `next_agent` | string | ❌ | `agent_type` string for the next agent in the workflow (e.g., "architect", "code_reviewer", "triage") |

## Example: Feature Implementation

```json
{
  "status": "complete",
  "summary": "Conditional step execution feature fully implemented and tested",
  "key_findings": [
    "All 14 unit tests pass",
    "Edge case: context evaluation with missing fields handled correctly",
    "No breaking changes to public APIs",
    "Performance: 0.5ms overhead per step evaluation"
  ],
  "effort_breakdown": {
    "implementation": "4 hours",
    "unit testing": "2 hours",
    "integration testing": "1 hour"
  },
  "verdict": "✅ Implementation complete, correct, and ready for review",
  "next_agent": "code_reviewer"
}
```

## Example: Analysis/Investigation

```json
{
  "status": "complete",
  "summary": "Root cause analysis complete for race condition in webhook handler",
  "key_findings": [
    "Race condition occurs when two webhooks arrive within 500ms",
    "Root cause: Missing mutex lock on shared state dictionary",
    "Severity: High - affects concurrent deployments",
    "Frequency: Occurs in ~5% of multi-agent scenarios"
  ],
  "verdict": "✅ RCA complete, recommendations documented",
  "next_agent": "architect"
}
```

## Example: Design/Planning

```json
{
  "status": "complete",
  "summary": "Technical architecture designed for floor plan editor integration",
  "key_findings": [
    "Flutter-based UI package available (floor_plan_editor)",
    "Integration recommended via REST API (not direct SDK)",
    "Storage: Firebase recommended for consistency with existing stack",
    "Timeline estimate: 3-4 weeks including testing"
  ],
  "effort_breakdown": {
    "research": "6 hours",
    "design": "8 hours",
    "documentation": "2 hours"
  },
  "verdict": "✅ Design ready for implementation",
  "next_agent": "implementer"
}
```

## Posted Comment Format

When the Nexus processor detects a completed agent, it posts a GitHub comment like:

```markdown
### ✅ Agent Completed

**Summary:** Conditional step execution feature fully implemented and tested

**Key Findings:**
- All 14 unit tests pass
- Edge case: context evaluation with missing fields handled correctly
- No breaking changes to public APIs
- Performance: 0.5ms overhead per step evaluation

**Effort Breakdown:**
- implementation: 4 hours
- unit testing: 2 hours
- integration testing: 1 hour

**Verdict:** ✅ Implementation complete, correct, and ready for review

**Next:** Ready for `@code_reviewer`

_Automated comment from Nexus._
```

## Backward Compatibility

If a `completion_summary.json` file is not found, the processor falls back to basic pattern matching on log files. However, structured JSON output ensures better GitHub comments and enables future automation (e.g., automatic agent routing based on "next_agent" field).

## Implementation in Python

```python
import json
import os
from pathlib import Path

def write_completion_summary(issue_number: int, data: dict) -> None:
    """Write completion summary JSON for an issue."""
    log_dir = Path.home() / ".nexus" / "tasks" / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    
    summary_path = log_dir / f"completion_summary_{issue_number}.json"
    with open(summary_path, "w") as f:
        json.dump(data, f, indent=2)
    
    print(f"✅ Wrote completion summary to {summary_path}")

# Usage
completion_data = {
    "status": "complete",
    "summary": "Feature implementation complete",
    "key_findings": [
        "All tests pass",
        "Code review ready"
    ],
    "next_agent": "code_reviewer"
}
write_completion_summary(35, completion_data)
```

## Implementation in Shell

```bash
#!/bin/bash
ISSUE_NUMBER=35
LOG_DIR="$HOME/.nexus/tasks/logs"
mkdir -p "$LOG_DIR"

cat > "$LOG_DIR/completion_summary_${ISSUE_NUMBER}.json" <<EOF
{
  "status": "complete",
  "summary": "Feature implementation complete",
  "key_findings": [
    "All tests pass",
    "Code review ready"
  ],
  "next_agent": "code_reviewer"
}
EOF

echo "✅ Wrote completion summary"
```

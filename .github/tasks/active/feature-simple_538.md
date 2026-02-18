# ✨ Simple Feature (4-step fast-track)
**Project:** General Inbox (Nexus)
**Type:** feature-simple
**Issue Name:** workflow-approval-gate
**Status:** Pending

What: Allow workflows to pause and wait for human approval before proceeding (e.g., PR review, deploy confirmation)

Why it tests our changes:
• Uses YAML workflow definitions we just documented
• Demonstrates Git-native integration (PR approvals)
• Validates WorkflowDefinition.from_yaml() in real scenarios
• Production-ready feature (mentioned in comparisons)

Example YAML:
steps:
  - name: design
    agent: Architect
    approval_required: true
    approval_timeout: 86400  # 24 hours
    
  - name: deploy
    agent: OpsCommander
    approval_required: true
    approvers: ["tech-lead", "devops-team"]

**Issue:** https://github.com/Ghabs95/nexus-core/issues/1
**Agent PID:** 403138
**Agent Tool:** copilot

---

## Triage — @ProjectLead

**Severity:** Medium — new capability, no existing breakage; blocks production readiness mentioned in feature comparisons.

**Affected sub-repo(s):** `nexus` (this repo)

**Affected modules:**
- `src/orchestration.py` — workflow execution engine; approval gate logic belongs here
- `src/models.py` — `WorkflowDefinition` / step model; needs `approval_required`, `approval_timeout`, `approvers` fields
- `src/nexus_core_helpers.py` — YAML parsing helpers (`WorkflowDefinition.from_yaml()`)
- `src/state_manager.py` — must persist paused/waiting-for-approval state across restarts
- `src/telegram_bot.py` / `src/notifications.py` — notify approvers and accept approval responses

**Routing:** Tier 2 Lead (Atlas) for RCA + implementation.

**Status:** Triaged — ready for next step.

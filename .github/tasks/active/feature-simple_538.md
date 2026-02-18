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

---

## QA Verification — @QAGuard

**Branch:** `feat/workflow-approval-gate`
**PR:** https://github.com/Ghabs95/nexus/pull/1
**Commit tested:** `a04a1cb`

**Regression Suite Results:**
- ✅ **136/136 tests passed** on the committed branch state
- 21 new approval-gate tests in `tests/test_workflow_approval.py` — all pass
- All pre-existing tests remain green (no regressions)

**Code Review Notes:**
- `src/nexus_core_helpers.py`: `handle_approval_gate()` correctly persists state + notifies; sync wrapper provided
- `src/state_manager.py`: `set/get/clear_pending_approval()` use JSON persistence — consistent with existing patterns
- `src/notifications.py`: `notify_approval_required()` sends inline Approve/Deny Telegram buttons — correct approach
- `src/telegram_bot.py`: `wfapprove_` / `wfdeny_` callback handlers properly wire to engine
- `src/inbox_processor.py`: approval gate check injected into step execution loop — minimal impact

**⚠️ Advisory (non-blocking):** There are uncommitted local file modifications (not part of the PR commits). The `tests/conftest.py` change among them adds a `mock_audit_log` autouse fixture that would break 2 existing `TestAuditLog` tests if committed as-is. These changes should be reviewed before any follow-up commit.

**Verdict:** ✅ APPROVED — branch is clean, all tests pass, implementation is sound.

**Status:** QA complete — ready for deploy.

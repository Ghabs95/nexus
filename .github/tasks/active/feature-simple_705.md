# ✨ Simple Feature (4-step fast-track)
**Project:** Nexus Core
**Type:** feature-simple
**Issue Name:** conditional-step-execution-workflow-engine
**Status:** Triaged

Implement conditional step execution in the nexus-core workflow engine to enable flexible workflow control flow.

Requirements:
1. Add condition field support to WorkflowStep (already exists in model, but not enforced)
2. Implement condition evaluator in WorkflowEngine
   - Support Python expressions: result.tier == 'high', error is None, etc.
   - Access to previous step outputs via context
3. Update complete_step() to evaluate conditions before advancing
4. Skip step if condition evaluates to False, move to next pending step
5. Log skipped steps with reason in audit trail
6. Add tests for conditional logic

**Issue:** https://github.com/Ghabs95/nexus-core/issues/2

---

## Triage — @ProjectLead

**Severity:** Medium — new capability enhancing workflow flexibility; `condition` field already exists on `WorkflowStep` model but is not evaluated anywhere. No existing breakage; purely additive.

**Affected sub-repo(s):** `nexus-core`

**Affected modules:**
- `nexus/core/models.py` — `WorkflowStep.condition` field already declared (`Optional[str]`); no changes needed
- `nexus/core/orchestrator.py` — `complete_step()` / step-advance logic; condition evaluation must be injected here before step activation
- `nexus/core/workflow.py` — workflow state transitions; skipped-step handling needed
- `tests/` — new tests for conditional skip logic required

**Routing:** Atlas (Tier 2 Lead) for RCA + implementation.

**Status:** Triaged — ready for next step.

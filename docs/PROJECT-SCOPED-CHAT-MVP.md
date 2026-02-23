# Project-Scoped Chat MVP (C-Level Agent Routing)

## Decision

Yes, this is possible and strategically useful.

Recommended model:
- Make chat threads project-scoped.
- Route by `agent_type` intent (not hardcoded names).
- Allow a CEO persona as an orchestrator for C-level prompts only.
- Keep strict handoff rules to prevent noisy or uncontrolled delegation.

## What is already defined (confirmed)

Agent/workflow definitions already provide most governance inputs:

- Agent identity and role: `spec.agent_type`
- Agent capabilities: `requires_tools`
- Input contract: `inputs`
- Output contract: `outputs`
- Required result fields: `validation.required_outputs` (present in your agent YAMLs)
- Workflow routing graph: steps with `agent_type`, `on_success`, and router `routes`

Observed in current repo:
- CEO/business agents exist in `agents/workflows/agents/*.yaml`.
- Tiered workflow routing exists in `agents/workflows/ghabs_org_workflow.yaml`.
- Nexus Core enforces step-to-step next-agent consistency via workflow helpers (for example `resolve_next_agents` and `canonicalize_next_agent`).

## Important honesty check

Strict rules are defined by YAML, but enforcement is mixed today:

- Strict now:
  - Workflow step progression and valid next-agent canonicalization.
  - Agent resolution by `agent_type` (no hardcoded @mentions in framework path).
- Not fully strict yet:
  - Universal runtime validation of agent output against each agent YAML `outputs` + `validation.required_outputs`.
  - Universal runtime block on tools not listed in `requires_tools` for every chat turn.

So: you already have the right contract definitions, but full policy enforcement should be completed in chat routing/runtime.

## MVP data model

Extend existing chat records (Redis-backed chat metadata) with:

```json
{
  "id": "chat_uuid",
  "title": "Wallible Vision",
  "created_at": "...",
  "project_key": "wallible",
  "chat_mode": "strategy", 
  "primary_agent_type": "ceo",
  "allowed_agent_types": ["ceo", "business", "marketing"],
  "workflow_profile": "ghabs_org_workflow",
  "delegation_enabled": true
}
```

Notes:
- `chat_mode`: `strategy` or `execution`
- `primary_agent_type`: default responder for this thread
- `allowed_agent_types`: hard gate for delegation
- `workflow_profile`: optional mapping for future workflow-aware delegation

## MVP routing policy

For every incoming chat message:

1. Load active chat metadata.
2. Resolve project context from `project_key`.
3. Classify intent into one of:
   - `c_level_strategy`
   - `market_analysis`
   - `execution_task`
   - `general`
4. Apply routing:
   - If `chat_mode=strategy` and intent is C-level: route to `primary_agent_type` (usually `ceo`).
   - If specialist intent (business/marketing): route to matching specialist `agent_type` if present in `allowed_agent_types`.
   - If specialist missing: fallback to `primary_agent_type` with explicit note.
   - If no project context: ask user to choose project via menu.
5. If delegation is used:
   - Delegator must pick next agent from allowed set.
   - Save delegation trace in chat metadata/history.
   - Return final consolidated response in thread.

## Guardrails (must-have)

- Never route outside `allowed_agent_types`.
- Never use hardcoded agent names; use `agent_type` only.
- Attach project context to every prompt.
- Add max delegation depth for a single user turn (suggested: 1 for MVP).
- Log delegation chain for audit/debug.

## CEO orchestrator policy (recommended)

Use CEO mode only when:
- chat is marked `strategy`, and
- intent is C-level (vision, prioritization, positioning, go/no-go).

For normal execution/ops questions, route directly to specialist `agent_type`.
This avoids over-centralizing all responses through CEO and keeps latency/cost lower.

## Implementation order (small and safe)

1. Add chat metadata fields (project + mode + allowed agent types).
2. Add project selector when creating/switching chats.
3. Add router function that returns `agent_type` based on chat metadata + intent.
4. Add delegation guard (`allowed_agent_types`, depth limit).
5. Add response envelope with `routed_to`, optional `delegated_to`, and `reason`.
6. Add runtime checks for `required_outputs` and tool allowlist in chat runtime path.

## Why this is a good idea

- Better context quality (project-scoped memory).
- Cleaner governance (agent_type-based control).
- Real executive simulation possible without losing control.
- Incremental rollout with low migration risk.

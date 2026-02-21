"""Workflow control service helpers used by Telegram command handlers."""

from __future__ import annotations

import logging
import os
import re
import time
from typing import Any, Callable, Dict, List, Optional, Tuple

from integrations.git_platform_utils import build_issue_url, resolve_repo

logger = logging.getLogger(__name__)


def prepare_continue_context(
    *,
    issue_num: str,
    project_key: str,
    rest_tokens: List[str],
    base_dir: str,
    project_config: Dict[str, Dict[str, Any]],
    default_repo: str,
    find_task_file_by_issue: Callable[[str], Optional[str]],
    get_issue_details: Callable[[str, Optional[str]], Optional[Dict[str, Any]]],
    resolve_project_config_from_task: Callable[[str], Tuple[Optional[str], Optional[Dict[str, Any]]]],
    get_runtime_ops_plugin: Callable[..., Any],
    scan_for_completions: Callable[[str], List[Any]],
    normalize_agent_reference: Callable[[Optional[str]], Optional[str]],
    get_expected_running_agent_from_workflow: Callable[[str], Optional[str]],
    get_sop_tier_from_issue: Callable[[str, Optional[str]], Optional[str]],
    get_sop_tier: Callable[[str], Tuple[str, Any, Any]],
) -> Dict[str, Any]:
    """Build context for /continue and return either a terminal state or launch payload."""
    forced_agent = None
    filtered_rest: List[str] = []
    for token in (rest_tokens or []):
        if token.lower().startswith("from:"):
            forced_agent = token[5:].strip()
        else:
            filtered_rest.append(token)

    continuation_prompt = " ".join(filtered_rest) if filtered_rest else "Please continue with the next step."

    runtime_ops = get_runtime_ops_plugin(cache_key="runtime-ops:telegram")
    pid = runtime_ops.find_agent_pid_for_issue(issue_num) if runtime_ops else None
    if pid:
        return {
            "status": "already_running",
            "message": (
                f"⚠️ Agent is already running for issue #{issue_num} (PID: {pid}).\n\n"
                f"Use /kill {issue_num} first if you want to restart it."
            ),
        }

    task_file = find_task_file_by_issue(issue_num)
    details = None
    repo = None

    if not task_file:
        repo = resolve_repo(project_config.get(project_key), default_repo)
        details = get_issue_details(issue_num, repo)
        if not details:
            return {"status": "error", "message": f"❌ Could not load issue #{issue_num}."}
        body = details.get("body", "")
        match = re.search(r"Task File:\s*`([^`]+)`", body)
        task_file = match.group(1) if match else None

    if not task_file or not os.path.exists(task_file):
        return {"status": "error", "message": f"❌ Task file not found for issue #{issue_num}."}

    project_name, config = resolve_project_config_from_task(task_file)
    if not config or not config.get("agents_dir"):
        name = project_name or "unknown"
        return {"status": "error", "message": f"❌ No agents config for project '{name}'."}

    repo = resolve_repo(config, default_repo)
    if not details:
        details = get_issue_details(issue_num, repo)
        if not details:
            return {"status": "error", "message": f"❌ Could not load issue #{issue_num}."}

    if details.get("state", "").lower() == "closed":
        return {"status": "error", "message": f"⚠️ Issue #{issue_num} is closed."}

    with open(task_file, "r", encoding="utf-8") as handle:
        content = handle.read()

    type_match = re.search(r"\*\*Type:\*\*\s*(.+)", content)
    task_type = type_match.group(1).strip().lower() if type_match else "feature"

    agent_type = None
    resumed_from = None
    workflow_already_done = False

    try:
        completions = scan_for_completions(base_dir)
        issue_completions = [c for c in completions if c.issue_number == str(issue_num)]
        if issue_completions:
            latest = max(issue_completions, key=lambda c: os.path.getmtime(c.file_path))
            if getattr(latest.summary, "is_workflow_done", False):
                workflow_already_done = True
                resumed_from = latest.summary.agent_type
            else:
                raw_next = latest.summary.next_agent
                normalized = normalize_agent_reference(raw_next)
                if normalized and normalized.lower() not in {
                    "none",
                    "n/a",
                    "null",
                    "done",
                    "end",
                    "finish",
                    "complete",
                    "",
                }:
                    agent_type = normalized
                    resumed_from = latest.summary.agent_type
                    logger.info(
                        "Continue issue #%s: last step was %s, resuming with next_agent=%s",
                        issue_num,
                        resumed_from,
                        agent_type,
                    )
    except Exception as exc:
        logger.warning("Could not scan completions for issue #%s: %s", issue_num, exc)

    if forced_agent:
        agent_type = normalize_agent_reference(forced_agent) or forced_agent
        workflow_already_done = False
        logger.info("Continue issue #%s: overriding agent to %s (from: arg)", issue_num, agent_type)

    if workflow_already_done and not forced_agent:
        if details.get("state", "").lower() == "open":
            return {
                "status": "workflow_done_open",
                "repo": repo,
                "resumed_from": resumed_from,
                "project_name": project_name or project_key,
            }
        return {
            "status": "workflow_done_closed",
            "message": (
                f"✅ Workflow for issue #{issue_num} is already complete and closed.\n"
                f"Last agent: `{resumed_from}`\n\n"
                f"Use `/continue {project_key} {issue_num} from:<agent>` to re-run a specific step."
            ),
        }

    if not agent_type:
        agent_type_match = re.search(r"\*\*Agent Type:\*\*\s*(.+)", content)
        agent_type = agent_type_match.group(1).strip() if agent_type_match else "triage"
        logger.info(
            "Continue issue #%s: no prior completion found, starting with %s",
            issue_num,
            agent_type,
        )

    expected_running_agent = get_expected_running_agent_from_workflow(str(issue_num))
    normalized_expected = normalize_agent_reference(expected_running_agent) if expected_running_agent else None
    normalized_requested = normalize_agent_reference(agent_type) if agent_type else None
    if normalized_expected and normalized_requested and normalized_expected != normalized_requested:
        logger.warning(
            "Continue issue #%s: requested agent '%s' does not match workflow RUNNING step '%s'; "
            "blocking launch",
            issue_num,
            agent_type,
            expected_running_agent,
        )
        return {
            "status": "mismatch",
            "message": (
                f"⚠️ Workflow-state mismatch for issue #{issue_num}.\n\n"
                f"Requested next agent: `{normalized_requested}`\n"
                f"Workflow RUNNING step: `{normalized_expected}`\n\n"
                "Launch blocked to avoid routing drift. Reconcile workflow state first, "
                "then run /continue again."
            ),
        }

    label_tier = get_sop_tier_from_issue(issue_num, project_name or project_key)
    if label_tier:
        tier_name = label_tier
    else:
        tier_name, _, _ = get_sop_tier(task_type)

    issue_url = build_issue_url(repo, issue_num, config)
    log_subdir = project_name or project_key

    return {
        "status": "ready",
        "issue_num": issue_num,
        "repo": repo,
        "agent_type": agent_type,
        "resumed_from": resumed_from,
        "continuation_prompt": continuation_prompt,
        "agents_abs": os.path.join(base_dir, config["agents_dir"]),
        "workspace_abs": os.path.join(base_dir, config["workspace"]),
        "issue_url": issue_url,
        "tier_name": tier_name,
        "content": content,
        "log_subdir": log_subdir,
    }


def kill_issue_agent(*, issue_num: str, get_runtime_ops_plugin: Callable[..., Any]) -> Dict[str, Any]:
    """Kill a running issue agent and report outcome."""
    runtime_ops = get_runtime_ops_plugin(cache_key="runtime-ops:telegram")
    pid = runtime_ops.find_agent_pid_for_issue(issue_num) if runtime_ops else None

    if not pid:
        return {
            "status": "not_running",
            "message": f"⚠️ No running agent found for issue #{issue_num}.",
        }

    if not runtime_ops or not runtime_ops.kill_process(pid, force=False):
        return {"status": "error", "message": f"Failed to stop process {pid}", "pid": pid}

    time.sleep(1)
    new_pid = runtime_ops.find_agent_pid_for_issue(issue_num) if runtime_ops else None
    if new_pid:
        if not runtime_ops or not runtime_ops.kill_process(pid, force=True):
            return {"status": "error", "message": f"Failed to force kill process {pid}", "pid": pid}
        return {"status": "killed", "pid": pid}

    return {"status": "stopped", "pid": pid}

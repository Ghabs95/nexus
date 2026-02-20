import asyncio
import glob
import json
import logging
import os
import re
import shutil
import subprocess
import time
from typing import Any, Optional

# Nexus Core framework imports
from nexus.core.completion import (
    CompletionSummary,
    scan_for_completions,
)
from nexus.core.models import WorkflowState as NexusCoreWorkflowState

# Import centralized configuration
from config import (
    BASE_DIR, get_github_repo,
    SLEEP_INTERVAL, STUCK_AGENT_THRESHOLD,
    PROJECT_CONFIG, DATA_DIR, INBOX_PROCESSOR_LOG_FILE, ORCHESTRATOR_CONFIG,
    NEXUS_CORE_STORAGE_DIR, get_inbox_dir, get_tasks_active_dir, get_tasks_closed_dir,
    get_tasks_logs_dir, get_nexus_dir_name
)
from state_manager import StateManager
from models import WorkflowState
from agent_monitor import AgentMonitor, WorkflowRouter
from agent_launcher import invoke_copilot_agent, is_recent_launch, get_sop_tier_from_issue, clear_launch_guard, launch_next_agent
from ai_orchestrator import get_orchestrator
from nexus_core_helpers import get_git_platform, get_workflow_definition_path, complete_step_for_issue
from plugin_runtime import (
    get_github_workflow_policy_plugin,
    get_profiled_plugin,
    get_runtime_ops_plugin,
    get_workflow_policy_plugin,
    get_workflow_state_plugin,
)
from error_handling import (
    run_command_with_retry,
    RetryExhaustedError
)
from notifications import (
    notify_agent_needs_input,
    notify_agent_completed,
    notify_agent_timeout,
    notify_workflow_completed,
    send_telegram_alert
)

# Helper to get issue repo (currently defaults to nexus, should be extended for multi-project)
def get_issue_repo(project: str = "nexus") -> str:
    """Get the GitHub repo for issue operations.
    
    Args:
        project: Project name (currently unused, defaults to nexus)
        
    Returns:
        GitHub repo string
        
    Note: This should be extended to support per-project repos when multi-project
          issue tracking is implemented.
    """
    return get_github_repo("nexus")

# Initialize orchestrator (CLI-only)
orchestrator = get_orchestrator(ORCHESTRATOR_CONFIG)

# Track alerted agents to avoid spam
alerted_agents = set()
notified_comments = set()  # Track comment IDs we've already notified about
auto_chained_agents = {}  # Track issue -> log_file to avoid re-chaining same completion
POLLING_FAILURE_THRESHOLD = 3
polling_failure_counts: dict[str, int] = {}
_WORKFLOW_STATE_PLUGIN_KWARGS = {
    "storage_dir": NEXUS_CORE_STORAGE_DIR,
    "issue_to_workflow_id": StateManager.get_workflow_id_for_issue,
    "issue_to_workflow_map_setter": StateManager.map_issue_to_workflow,
    "workflow_definition_path_resolver": get_workflow_definition_path,
    "github_repo": get_github_repo("nexus"),
}


def _get_github_issue_plugin(repo: str, max_attempts: int = 3, timeout: int = 30):
    """Create a configured GitHub issue plugin instance for a repo."""
    return get_profiled_plugin(
        "github_inbox",
        overrides={
            "repo": repo,
            "max_attempts": max_attempts,
            "timeout": timeout,
        },
        cache_key=f"github:inbox:{repo}:{max_attempts}:{timeout}",
    )


def _resolve_tier_for_issue(
    issue_num: str,
    project_name: str,
    repo: str,
    *,
    context: str = "auto-chain",
) -> Optional[str]:
    """Resolve workflow tier for an issue, halt with alert when unknown.

    Resolution order:
      1. ``launched_agents`` tracker (persisted from the most recent launch)
      2. Issue ``workflow:`` labels on GitHub
      3. ``None`` — caller must halt the flow

    When the tier is found from the tracker but the issue has no
    ``workflow:`` label, the label is added automatically so that
    future reads succeed.

    Returns:
        Tier name (``"full"``, ``"shortened"``, ``"fast-track"``) or
        ``None`` when the tier cannot be determined.  When ``None`` is
        returned, a Telegram alert has already been sent.
    """
    tracker_tier = StateManager.get_last_tier_for_issue(issue_num)
    label_tier = get_sop_tier_from_issue(issue_num, project_name)

    if tracker_tier and label_tier:
        # Both sources available — prefer label (canonical), but warn on mismatch
        if tracker_tier != label_tier:
            logger.warning(
                f"Tier mismatch for issue #{issue_num}: "
                f"tracker={tracker_tier}, label={label_tier}. Using label."
            )
        return label_tier

    if label_tier:
        return label_tier

    if tracker_tier:
        # Label missing — backfill it so future reads work
        _ensure_workflow_label(issue_num, tracker_tier, repo)
        return tracker_tier

    # Neither source available — halt
    logger.error(
        f"Cannot determine workflow tier for issue #{issue_num} "
        f"({context}): no tracker entry and no workflow: label."
    )
    send_telegram_alert(
        f"⚠️ {context.title()} halted for issue #{issue_num}: "
        f"missing `workflow:` label and no prior launch data.\n"
        f"Add a label (e.g. `workflow:full`) to the issue and retry."
    )
    return None


def _ensure_workflow_label(issue_num: str, tier_name: str, repo: str) -> None:
    """Add `workflow:<tier>` label to an issue if missing."""
    label = f"workflow:{tier_name}"
    try:
        plugin = _get_github_issue_plugin(repo, max_attempts=2, timeout=10)
        plugin.add_label(str(issue_num), label)
        logger.info(f"Added missing label '{label}' to issue #{issue_num}")
    except Exception as e:
        logger.warning(f"Failed to add label '{label}' to issue #{issue_num}: {e}")


# Wrapper functions for backward compatibility - these now delegate to StateManager
def load_launched_agents():
    """Load recently launched agents from persistent storage."""
    return StateManager.load_launched_agents()

def save_launched_agents(data):
    """Save launched agents to persistent storage."""
    StateManager.save_launched_agents(data)

# Load persisted state
launched_agents_tracker = StateManager.load_launched_agents()
# PROJECT_CONFIG is now imported from config.py

# Failed task file lookup tracking (stop checking after 3 failures)
FAILED_LOOKUPS_FILE = os.path.join(DATA_DIR, "failed_task_lookups.json")
COMPLETION_COMMENTS_FILE = os.path.join(DATA_DIR, "completion_comments.json")

def load_failed_lookups():
    """Load failed task file lookup counters."""
    try:
        if os.path.exists(FAILED_LOOKUPS_FILE):
            with open(FAILED_LOOKUPS_FILE, 'r') as f:
                return json.load(f)
    except Exception as e:
        logger.error(f"Error loading failed lookups: {e}")
    return {}

def save_failed_lookups(lookups):
    """Save failed task file lookup counters."""
    try:
        with open(FAILED_LOOKUPS_FILE, 'w') as f:
            json.dump(lookups, f, indent=2)
    except Exception as e:
        logger.error(f"Error saving failed lookups: {e}")


def load_completion_comments():
    """Load completion comment tracking data."""
    try:
        if os.path.exists(COMPLETION_COMMENTS_FILE):
            with open(COMPLETION_COMMENTS_FILE, "r") as f:
                return json.load(f)
    except Exception as e:
        logger.warning(f"Failed to read completion comments file: {e}")
    return {}


def save_completion_comments(comments):
    """Save completion comment tracking data."""
    try:
        with open(COMPLETION_COMMENTS_FILE, "w") as f:
            json.dump(comments, f, indent=2)
    except Exception as e:
        logger.warning(f"Failed to save completion comments file: {e}")

failed_task_lookups = load_failed_lookups()
completion_comments = load_completion_comments()

# Logging — force=True overrides the root handler set by config.py at import time
logging.basicConfig(
    format='%(asctime)s - %(levelname)s - %(message)s',
    level=logging.INFO,
    force=True,
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler(INBOX_PROCESSOR_LOG_FILE)
    ]
)
logger = logging.getLogger("InboxProcessor")


def _record_polling_failure(scope: str, error: Exception) -> None:
    """Increment polling failure count and alert once threshold is reached."""
    count = polling_failure_counts.get(scope, 0) + 1
    polling_failure_counts[scope] = count
    if count != POLLING_FAILURE_THRESHOLD:
        return

    try:
        send_telegram_alert(
            "⚠️ **Polling Error Threshold Reached**\n\n"
            f"Scope: `{scope}`\n"
            f"Consecutive failures: {count}\n"
            f"Last error: `{error}`"
        )
    except Exception as notify_err:
        logger.error(f"Failed to send polling escalation alert for {scope}: {notify_err}")


def _clear_polling_failures(scope: str) -> None:
    """Reset polling failure count for a scope after a successful attempt."""
    if scope in polling_failure_counts:
        polling_failure_counts.pop(scope, None)


def slugify(text):
    """Converts text to a branch-friendly slug."""
    text = text.lower()
    text = re.sub(r'[^a-z0-9\s-]', '', text)
    text = re.sub(r'\s+', '-', text)
    return text[:50]


def _iter_project_configs():
    """Yield (project_name, config) pairs for dict-based project configs."""
    for project_name, cfg in PROJECT_CONFIG.items():
        if isinstance(cfg, dict) and "github_repo" in cfg:
            yield project_name, cfg


def _resolve_project_from_path(summary_path: str) -> str:
    """Resolve project name from a completion_summary file path.

    Matches the path against configured project workspaces.
    Returns project key or empty string if no match.
    """
    for key, cfg in _iter_project_configs():
        workspace = cfg.get("workspace")
        if not workspace:
            continue
        workspace_abs = os.path.join(BASE_DIR, workspace)
        if summary_path.startswith(workspace_abs):
            return key
    return ""


def _resolve_repo_for_issue(issue_num: str, default_project: str = "nexus") -> str:
    """Resolve repo for issue by reading task file location from issue body.

    Delegates to nexus-core's GitHubPlatform.get_issue() to fetch the body.
    """
    default_repo = get_github_repo(default_project)
    project_workspaces = {}
    project_repos = {}
    for key, cfg in _iter_project_configs():
        workspace = cfg.get("workspace")
        if not workspace:
            continue
        project_workspaces[key] = os.path.join(BASE_DIR, workspace)
        project_repos[key] = get_github_repo(key)

    policy = get_github_workflow_policy_plugin(
        get_issue=lambda **kwargs: asyncio.run(
            get_git_platform(kwargs["repo"]).get_issue(str(kwargs["issue_number"]))
        ),
        cache_key=None,
    )

    resolved_repo = policy.resolve_repo_for_issue(
        issue_number=str(issue_num),
        default_repo=default_repo,
        project_workspaces=project_workspaces,
        project_repos=project_repos,
    )

    if resolved_repo != default_repo:
        logger.debug(f"Issue #{issue_num} resolved to repo '{resolved_repo}'")
    return resolved_repo


def _normalize_agent_reference(agent_ref: str) -> str:
    """Normalize next-agent references emitted by completion summaries."""
    value = (agent_ref or "").strip()
    value = value.lstrip("@").strip()
    return value.strip("`").strip()


def _is_terminal_agent_reference(agent_ref: str) -> bool:
    """Return True when a next-agent reference means workflow completion."""
    return _normalize_agent_reference(agent_ref).lower() in {
        "none", "n/a", "null", "no", "end", "done", "finish", "complete", ""
    }


def _resolve_git_dir(project_name: str) -> Optional[str]:
    """Resolve the actual git repo directory for a project.

    Tries:
    1. workspace itself (e.g. /home/ubuntu/git/case_italia)
    2. workspace/repo_name (e.g. /home/ubuntu/git/ghabs/nexus-core)

    Returns absolute path or None.
    """
    proj_cfg = PROJECT_CONFIG.get(project_name, {})
    workspace = proj_cfg.get("workspace", "")
    github_repo = proj_cfg.get("github_repo", "")
    if not workspace:
        return None
    workspace_abs = os.path.join(BASE_DIR, workspace)

    if os.path.isdir(os.path.join(workspace_abs, ".git")):
        return workspace_abs
    if github_repo and "/" in github_repo:
        repo_name = github_repo.split("/")[-1]
        candidate = os.path.join(workspace_abs, repo_name)
        if os.path.isdir(os.path.join(candidate, ".git")):
            return candidate
    return None


def _finalize_workflow(issue_num: str, repo: str, last_agent: str, project_name: str) -> None:
    """Handle workflow completion: close issue, create PR if needed, send Telegram.

    Called when the last agent finishes (next_agent is 'none' or empty).
    Delegates PR creation and issue closing to nexus-core GitHubPlatform.
    """
    def _create_pr_from_changes(**kwargs):
        platform = get_git_platform(kwargs["repo"])
        pr_result = asyncio.run(
            platform.create_pr_from_changes(
                repo_dir=kwargs["repo_dir"],
                issue_number=kwargs["issue_number"],
                title=kwargs["title"],
                body=kwargs["body"],
            )
        )
        return pr_result.url if pr_result else None

    def _close_issue(**kwargs):
        platform = get_git_platform(kwargs["repo"])
        return bool(asyncio.run(platform.close_issue(kwargs["issue_number"], comment=kwargs["comment"])))

    workflow_policy = get_workflow_policy_plugin(
        resolve_git_dir=_resolve_git_dir,
        create_pr_from_changes=_create_pr_from_changes,
        close_issue=_close_issue,
        send_notification=send_telegram_alert,
        cache_key="workflow-policy:finalize",
    )

    result = workflow_policy.finalize_workflow(
        issue_number=str(issue_num),
        repo=repo,
        last_agent=last_agent,
        project_name=project_name,
    )

    if result.get("pr_url"):
        logger.info(f"🔀 Created PR for issue #{issue_num}: {result['pr_url']}")
    if result.get("issue_closed"):
        logger.info(f"🔒 Closed issue #{issue_num}")
        archived = _archive_closed_task_files(issue_num, project_name)
        if archived:
            logger.info(f"📦 Archived {archived} task file(s) for closed issue #{issue_num}")


def _archive_closed_task_files(issue_num: str, project_name: str = "") -> int:
    """Archive active task files for a closed issue into tasks/closed.

    Matches files by either:
    1) filename pattern ``issue_<issue_num>.md``
    2) issue URL metadata inside the file body
    """
    projects_to_scan = []
    if project_name and project_name in PROJECT_CONFIG:
        projects_to_scan.append(project_name)

    projects_to_scan.extend(
        key for key in PROJECT_CONFIG.keys()
        if key not in {"workflow_definition_path", "shared_agents_dir", "nexus_dir", "require_human_merge_approval", "github_issue_triage", "ai_tool_preferences"}
        and key not in projects_to_scan
    )

    archived_count = 0
    issue_pattern = re.compile(r"\*\*Issue:\*\*\s*https?://github\.com/[^/]+/[^/]+/issues/(\d+)")

    for project_key in projects_to_scan:
        project_cfg = PROJECT_CONFIG.get(project_key, {})
        if not isinstance(project_cfg, dict):
            continue

        workspace_rel = project_cfg.get("workspace")
        if not workspace_rel:
            continue

        project_root = os.path.join(BASE_DIR, workspace_rel)
        active_dir = get_tasks_active_dir(project_root, project_key)
        if not os.path.isdir(active_dir):
            continue

        closed_dir = get_tasks_closed_dir(project_root, project_key)

        for filename in os.listdir(active_dir):
            if not filename.endswith(".md"):
                continue

            source_path = os.path.join(active_dir, filename)
            matched = False

            if filename == f"issue_{issue_num}.md":
                matched = True
            else:
                try:
                    with open(source_path, "r") as f:
                        content = f.read()
                    match = issue_pattern.search(content)
                    matched = bool(match and match.group(1) == str(issue_num))
                except Exception as exc:
                    logger.warning(f"Could not inspect active task file {source_path}: {exc}")
                    continue

            if not matched:
                continue

            target_path = os.path.join(closed_dir, filename)
            if os.path.exists(target_path):
                stem, ext = os.path.splitext(filename)
                target_path = os.path.join(closed_dir, f"{stem}_{int(time.time())}{ext}")

            try:
                os.makedirs(closed_dir, exist_ok=True)
                os.replace(source_path, target_path)
                archived_count += 1
            except Exception as exc:
                logger.warning(f"Failed to archive task file {source_path}: {exc}")

    return archived_count


def _post_completion_comments_from_logs() -> None:
    """Detect agent completions and auto-chain to the next workflow step.

    Uses nexus-core's scan_for_completions() to detect completion files.
    Agents post their own rich GitHub comments, so this function only handles
    orchestration: dedup, finalization, Telegram alerts, and auto-chaining.
    """
    global completion_comments
    nexus_dir_name = get_nexus_dir_name()

    # Use framework scanner to find all completion summaries
    detected = scan_for_completions(BASE_DIR, nexus_dir=nexus_dir_name)

    for detection in detected:
        issue_num = detection.issue_number
        summary = detection.summary

        try:
            # Skip if agent for this issue is still running
            runtime_ops = get_runtime_ops_plugin(cache_key="runtime-ops:inbox")
            if runtime_ops.is_issue_process_running(issue_num):
                continue

            # Dedup using framework's dedup_key
            comment_key = detection.dedup_key
            if comment_key in completion_comments:
                continue

            project_name = _resolve_project_from_path(detection.file_path)
            if project_name:
                repo = get_github_repo(project_name)
            else:
                repo = _resolve_repo_for_issue(issue_num)

            completed_agent = summary.agent_type

            # Record completion (no GitHub comment — agents post their own)
            completion_comments[comment_key] = time.time()
            save_completion_comments(completion_comments)
            logger.info(f"📋 Agent completed for issue #{issue_num} ({completed_agent})")

            # --- Auto-chain: engine-driven routing (Phase 2) with manual fallback ---
            # Try to advance the workflow via WorkflowEngine.complete_step().
            # The engine handles router steps, conditional branches, and
            # review/develop loops automatically. Falls back to manual routing
            # when no engine workflow is mapped to this issue (legacy issues).
            engine_workflow = asyncio.run(
                complete_step_for_issue(
                    issue_number=issue_num,
                    completed_agent_type=completed_agent,
                    outputs=summary.to_dict(),
                )
            )

            tier_name = None
            next_agent = None

            if engine_workflow is not None:
                # Engine path: routing was determined by the WorkflowEngine
                if engine_workflow.state in (
                    NexusCoreWorkflowState.COMPLETED,
                    NexusCoreWorkflowState.FAILED,
                ):
                    reason = engine_workflow.state.value
                    logger.info(f"✅ Workflow {reason} for issue #{issue_num} (last agent: {completed_agent})")
                    _la = StateManager.load_launched_agents(recent_only=False)
                    if str(issue_num) in _la:
                        del _la[str(issue_num)]
                        save_launched_agents(_la)
                    _finalize_workflow(issue_num, repo, completed_agent, project_name)
                    continue

                next_agent = engine_workflow.active_agent_type
                if not next_agent:
                    logger.warning(
                        f"Engine returned no active agent for issue #{issue_num}; skipping auto-chain"
                    )
                    continue
                logger.info(f"🔀 Engine routed #{issue_num}: {completed_agent} → {next_agent}")

            else:
                # Fallback: manual routing (no engine workflow mapped to this issue)
                if summary.is_workflow_done:
                    logger.info(f"✅ Workflow complete for issue #{issue_num} (last agent: {completed_agent})")
                    _la = StateManager.load_launched_agents(recent_only=False)
                    if str(issue_num) in _la:
                        del _la[str(issue_num)]
                        save_launched_agents(_la)
                    _finalize_workflow(issue_num, repo, completed_agent, project_name)
                    continue

                next_agent = _normalize_agent_reference(summary.next_agent)

                workflow_path = get_workflow_definition_path(project_name)
                tier_name = _resolve_tier_for_issue(
                    issue_num, project_name, repo, context="auto-chain"
                )
                if not tier_name:
                    continue

                if workflow_path and os.path.exists(workflow_path):
                    try:
                        from nexus.core.workflow import WorkflowDefinition
                        canonical_next = WorkflowDefinition.canonicalize_next_agent(
                            workflow_path,
                            completed_agent,
                            summary.next_agent,
                            workflow_type=tier_name,
                        )
                        valid_next = WorkflowDefinition.resolve_next_agents(
                            workflow_path, completed_agent, workflow_type=tier_name
                        )
                        if canonical_next:
                            if canonical_next != next_agent:
                                logger.warning(
                                    f"Canonicalized next_agent '{next_agent}' → '{canonical_next}' "
                                    f"for '{completed_agent}'"
                                )
                            next_agent = canonical_next
                        elif valid_next and next_agent not in valid_next:
                            logger.error(
                                f"Invalid next_agent '{next_agent}' for '{completed_agent}'. "
                                f"Valid: {valid_next}. Skipping auto-chain."
                            )
                            send_telegram_alert(
                                f"❌ Auto-chain skipped for issue #{issue_num}: invalid next_agent "
                                f"`{next_agent}` after `{completed_agent}`. "
                                f"Valid options: {', '.join(valid_next)}"
                            )
                            continue
                    except Exception as e:
                        logger.debug(f"Could not validate next_agent: {e}")

                if _is_terminal_agent_reference(next_agent):
                    logger.info(
                        f"✅ Workflow complete for issue #{issue_num} "
                        f"(terminal next_agent after {completed_agent})"
                    )
                    _finalize_workflow(issue_num, repo, completed_agent, project_name)
                    continue

            # NOTE: no is_recent_launch() check here — dedup_key above already
            # guarantees each completion triggers auto-chain at most once.
            if not project_name:
                logger.warning(f"Cannot auto-chain issue #{issue_num}: could not resolve project")
                continue

            proj_cfg = PROJECT_CONFIG.get(project_name, {})
            agents_dir = proj_cfg.get("agents_dir", "")
            workspace = proj_cfg.get("workspace", "")
            if not agents_dir or not workspace:
                logger.warning(
                    f"Cannot auto-chain issue #{issue_num}: missing agents_dir/workspace for {project_name}"
                )
                continue

            agents_abs = os.path.join(BASE_DIR, agents_dir)
            workspace_abs = os.path.join(BASE_DIR, workspace)
            issue_url = f"https://github.com/{repo}/issues/{issue_num}"

            workflow_policy = get_workflow_policy_plugin(cache_key="workflow-policy:inbox")

            # Resolve tier lazily for engine path (not needed for routing, but
            # required by the agent launcher)
            if tier_name is None:
                tier_name = _resolve_tier_for_issue(
                    issue_num, project_name, repo, context="auto-chain"
                )
                if not tier_name:
                    continue

            send_telegram_alert(
                workflow_policy.build_transition_message(
                    issue_number=issue_num,
                    completed_agent=completed_agent,
                    next_agent=next_agent,
                    repo=repo,
                )
            )

            continuation_prompt = (
                f"You are a {next_agent} agent. The previous step ({completed_agent}) is complete.\n\n"
                f"Your task: Begin your step in the workflow.\n"
                f"Read recent GitHub comments to understand what's been completed.\n"
                f"Then perform your assigned work and post a status update."
            )

            pid, tool_used = invoke_copilot_agent(
                agents_dir=agents_abs,
                workspace_dir=workspace_abs,
                issue_url=issue_url,
                tier_name=tier_name,
                task_content="",
                continuation=True,
                continuation_prompt=continuation_prompt,
                log_subdir=project_name,
                agent_type=next_agent,
                project_name=project_name
            )

            if pid:
                logger.info(
                    f"🔗 Auto-chained {completed_agent} → {next_agent} "
                    f"for issue #{issue_num} (PID: {pid}, tool: {tool_used})"
                )
            else:
                logger.error(f"❌ Failed to auto-chain to {next_agent} for issue #{issue_num}")
                send_telegram_alert(
                    workflow_policy.build_autochain_failed_message(
                        issue_number=issue_num,
                        completed_agent=completed_agent,
                        next_agent=next_agent,
                        repo=repo,
                    )
                )
        except json.JSONDecodeError as e:
            logger.warning(f"Invalid completion_summary.json for issue #{detection.issue_number}: {e}")
        except Exception as e:
            logger.warning(f"Error processing completion summary for issue #{detection.issue_number}: {e}")


def _get_initial_agent_from_workflow(project_name: str, workflow_type: str = "") -> str:
    """Get the first agent/agent_type from a workflow YAML definition.

    Delegates to nexus-core's WorkflowDefinition.from_yaml() to parse the
    workflow, then reads the first step's agent name.

    Args:
        project_name: Project name to resolve workflow path.
        workflow_type: Tier name (full/shortened/fast-track) for multi-tier workflows.

    Returns empty string if workflow definition is missing or invalid.
    """
    from nexus.core.workflow import WorkflowDefinition

    path = get_workflow_definition_path(project_name)
    if not path:
        logger.error(f"Missing workflow_definition_path for project '{project_name}'")
        send_telegram_alert(
            f"Missing workflow_definition_path for project '{project_name}'."
        )
        return ""
    if not os.path.exists(path):
        logger.error(f"Workflow definition not found: {path}")
        send_telegram_alert(f"Workflow definition not found: {path}")
        return ""
    try:
        workflow = WorkflowDefinition.from_yaml(path, workflow_type=workflow_type)
        if not workflow.steps:
            logger.error(f"Workflow definition has no steps: {path}")
            send_telegram_alert(f"Workflow definition has no steps: {path}")
            return ""
        first_step = workflow.steps[0]
        return first_step.agent.name or first_step.agent.display_name or ""
    except Exception as e:
        logger.error(f"Failed to read workflow definition {path}: {e}")
        send_telegram_alert(f"Failed to read workflow definition {path}: {e}")
        return ""


# send_telegram_alert is now imported from notifications module


def _is_pid_alive(pid: int) -> bool:
    """Check whether a process is still running via kill(pid, 0)."""
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        # Process exists but we can't signal it — still alive
        return True
    except Exception:
        return False


def check_stuck_agents():
    """Monitor agent processes and handle timeouts with auto-kill and retry.

    Two detection strategies:
    1. Log-file scan: find stale log files with a still-running PID → kill + retry.
    2. Dead-process scan: iterate launched_agents.json, detect PIDs that exited
       without posting a completion → alert via Telegram.
    """
    scope = "stuck-agents:loop"
    try:
        # --- Strategy 1: stale log file detection (kills still-running agents) ---
        nexus_dir_name = get_nexus_dir_name()
        log_files = glob.glob(
            os.path.join(BASE_DIR, "**", nexus_dir_name, "tasks", "*", "logs", "*_*.log"),
            recursive=True
        )

        for log_file in log_files:
            # Extract issue number from filename: <tool>_<issue>_<timestamp>.log
            match = re.search(r"(?:copilot|gemini)_(\d+)_\d{8}_", os.path.basename(log_file))
            if not match:
                continue

            issue_num = match.group(1)

            # Get latest log file for this issue only (ignore old ones)
            all_logs_for_issue = sorted(
                [f for f in log_files if re.search(rf"(?:copilot|gemini)_{issue_num}_\d{{8}}_", os.path.basename(f))],
                key=os.path.getmtime,
                reverse=True
            )
            if all_logs_for_issue and log_file != all_logs_for_issue[0]:
                continue  # Skip old log files

            # Check for timeout
            timed_out, pid = AgentMonitor.check_timeout(issue_num, log_file)

            if timed_out and pid:
                # Kill the stuck agent
                killed = AgentMonitor.kill_agent(pid, issue_num)

                if killed:
                    launched_agents = load_launched_agents()
                    agent_data = launched_agents.get(str(issue_num), {})
                    agent_type = agent_data.get("agent_type", "unknown")

                    will_retry = AgentMonitor.should_retry(issue_num, agent_type)
                    notify_agent_timeout(issue_num, agent_type, will_retry, project="nexus")

                    if will_retry:
                        crashed_tool = agent_data.get("tool", "")
                        # Remove stale entry and immediately relaunch
                        launched_agents.pop(str(issue_num), None)
                        save_launched_agents(launched_agents)
                        clear_launch_guard(issue_num)
                        try:
                            launched = launch_next_agent(
                                issue_num, agent_type, trigger_source="timeout-retry",
                                exclude_tools=[crashed_tool] if crashed_tool else None
                            )
                            if not launched:
                                logger.error(
                                    f"Timeout retry failed to launch {agent_type} "
                                    f"for issue #{issue_num}"
                                )
                        except Exception as exc:
                            logger.error(
                                f"Exception during timeout retry for issue #{issue_num}: {exc}"
                            )

        # --- Strategy 2: dead-process detection (catches crashed agents) ---
        _check_dead_agents()

        _clear_polling_failures(scope)

    except Exception as e:
        logger.error(f"Error in check_stuck_agents: {e}")
        _record_polling_failure(scope, e)


# Track issues we've already alerted about to avoid repeat notifications
_dead_agent_alerted: set = set()


def _check_dead_agents() -> None:
    """Detect agents that exited without completing and send alerts.

    Reads launched_agents.json, checks each PID, and alerts if the process
    is dead but no completion was recorded (i.e. still in launched_agents).
    Gives a grace period of STUCK_AGENT_THRESHOLD seconds before alerting
    to allow normal completion scanning to detect the result first.
    """
    # recent_only=False: agents that crashed long ago are still detectable;
    # the AGENT_RECENT_WINDOW filter would silently hide them.
    launched_agents = StateManager.load_launched_agents(recent_only=False)
    if not launched_agents:
        return

    current_time = time.time()

    for issue_num, agent_data in list(launched_agents.items()):
        pid = agent_data.get("pid")
        launch_ts = agent_data.get("timestamp", 0)
        agent_type = agent_data.get("agent_type", "unknown")
        tier = agent_data.get("tier", "unknown")

        if not pid:
            continue

        # Grace period: let completion scanner run first
        age_seconds = current_time - launch_ts
        if age_seconds < STUCK_AGENT_THRESHOLD:
            continue

        # Check if process is still alive
        if _is_pid_alive(pid):
            continue  # Still running — Strategy 1 handles timeout kills

        # Skip if workflow was stopped/paused (e.g. via /stop command)
        wf_state = StateManager.get_workflow_state(str(issue_num))
        if wf_state in (WorkflowState.STOPPED, WorkflowState.PAUSED):
            logger.debug(
                f"Skipping dead agent check for issue #{issue_num}: "
                f"workflow state is {wf_state.value}"
            )
            continue

        # Process is dead. Check if completion scanner already handled it
        # (it would have removed from launched_agents or the dedup would fire).
        # If we get here, the agent exited without a detected completion.

        alert_key = f"{issue_num}:{pid}"
        if alert_key in _dead_agent_alerted:
            continue  # Already alerted for this specific launch

        logger.warning(
            f"💀 Dead agent detected: issue #{issue_num} "
            f"({agent_type}, PID {pid}, tier {tier}, age {age_seconds/60:.0f}min)"
        )

        StateManager.audit_log(
            int(issue_num),
            "AGENT_DEAD",
            f"Agent process PID {pid} ({agent_type}) exited without completion "
            f"after {age_seconds/60:.0f}min"
        )

        # Determine project for GitHub link
        project_name = _resolve_project_for_issue(issue_num)
        repo = get_github_repo(project_name) if project_name else get_github_repo()

        crashed_tool = agent_data.get("tool", "")
        will_retry = AgentMonitor.should_retry(issue_num, agent_type)
        if will_retry:
            alert_sent = send_telegram_alert(
                f"💀 **Agent Crashed → Retrying**\n\n"
                f"Issue: [#{issue_num}](https://github.com/{repo}/issues/{issue_num})\n"
                f"Agent: {agent_type} (PID {pid}, tool: {crashed_tool})\n"
                f"Tier: {tier}\n"
                f"Status: Process exited without completion, retry scheduled"
                + (" with Copilot" if crashed_tool == "gemini" else "")
            )
            if not alert_sent:
                logger.warning(
                    f"Failed to send dead-agent alert for issue #{issue_num}; "
                    "will retry notification on next poll"
                )
                continue

            _dead_agent_alerted.add(alert_key)
            # Remove from tracker before retrying so launch_next_agent's own
            # save writes the fresh entry cleanly (no post-loop clobber).
            del launched_agents[issue_num]
            save_launched_agents(launched_agents)

            # Actually relaunch — clear the LaunchGuard cooldown first so the
            # retry is not blocked by the now-dead previous launch record.
            # Exclude the tool that crashed so the next available tool is used.
            clear_launch_guard(issue_num)
            try:
                launched = launch_next_agent(
                    issue_num, agent_type, trigger_source="dead-agent-retry",
                    exclude_tools=[crashed_tool] if crashed_tool else None
                )
                if launched:
                    logger.info(
                        f"🔄 Dead-agent retry launched: {agent_type} for issue #{issue_num}"
                    )
                else:
                    logger.error(
                        f"Dead-agent retry failed to launch {agent_type} for issue #{issue_num}"
                    )
            except Exception as exc:
                logger.error(
                    f"Exception during dead-agent retry for issue #{issue_num}: {exc}"
                )
        else:
            alert_sent = send_telegram_alert(
                f"💀 **Agent Crashed → Manual Intervention**\n\n"
                f"Issue: [#{issue_num}](https://github.com/{repo}/issues/{issue_num})\n"
                f"Agent: {agent_type} (PID {pid})\n"
                f"Tier: {tier}\n"
                f"Status: Process exited without completion, max retries reached\n\n"
                f"Use /reprocess nexus {issue_num} to retry"
            )
            if not alert_sent:
                logger.warning(
                    f"Failed to send dead-agent alert for issue #{issue_num}; "
                    "will retry notification on next poll"
                )
                continue

            _dead_agent_alerted.add(alert_key)
            AgentMonitor.mark_failed(issue_num, agent_type, "Agent process exited without completion")
            del launched_agents[issue_num]
            save_launched_agents(launched_agents)


def _resolve_project_for_issue(issue_num: str) -> Optional[str]:
    """Best-effort project resolution from config for an issue number."""
    # Try to find which project this issue belongs to by checking agents data
    for project_name, _ in _iter_project_configs():
        return project_name
    return None


def check_agent_comments():
    """Monitor GitHub issues for agent comments requesting input across all projects."""
    loop_scope = "agent-comments:loop"
    try:
        # Query issues from all project repos
        all_issue_nums = []
        for project_name, _ in _iter_project_configs():
            repo = get_github_repo(project_name)
            list_scope = f"agent-comments:list-issues:{project_name}"
            try:
                issue_plugin = _get_github_issue_plugin(repo, max_attempts=3, timeout=10)
                monitor_policy = get_github_workflow_policy_plugin(
                    list_issues=lambda **kwargs: issue_plugin.list_issues(
                        state=kwargs["state"],
                        limit=kwargs["limit"],
                        fields=kwargs["fields"],
                    ),
                    cache_key=None,
                )
                workflow_labels = {
                    "workflow:full",
                    "workflow:shortened",
                    "workflow:fast-track",
                }
                issue_numbers = monitor_policy.list_workflow_issue_numbers(
                    repo=repo,
                    workflow_labels=workflow_labels,
                    limit=100,
                )
                for issue_number in issue_numbers:
                    all_issue_nums.append((issue_number, project_name, repo))
                _clear_polling_failures(list_scope)
            except Exception as e:
                logger.warning(f"GitHub issue list failed for project {project_name}: {e}")
                _record_polling_failure(list_scope, e)
                continue
        
        if not all_issue_nums:
            return
        
        for issue_num, project_name, repo in all_issue_nums:
            if not issue_num:
                continue
                
            # Get issue comments via framework
            comments_scope = f"agent-comments:get-comments:{project_name}"
            try:
                monitor_policy = get_github_workflow_policy_plugin(
                    get_comments=lambda **kwargs: asyncio.run(
                        get_git_platform(kwargs["repo"]).get_comments(str(kwargs["issue_number"]))
                    ),
                    cache_key=None,
                )
                bot_comments = monitor_policy.get_bot_comments(
                    repo=repo,
                    issue_number=str(issue_num),
                    bot_author="Ghabs95",
                )
                _clear_polling_failures(comments_scope)
            except Exception as e:
                logger.warning(f"Failed to fetch comments for issue #{issue_num}: {e}")
                _record_polling_failure(comments_scope, e)
                continue

            if not bot_comments:
                continue
            
            for comment in bot_comments:
                try:
                    comment_id = comment.id
                    body = comment.body or ""
                    
                    # Skip if we've already notified about this comment
                    if comment_id in notified_comments:
                        continue
                    
                    # Check if comment contains questions or blockers
                    needs_input = any(pattern in body.lower() for pattern in [
                        "questions for @ghabs",
                        "questions for `@ghabs",  # Escaped mention
                        "waiting for @ghabs",
                        "waiting for `@ghabs",  # Escaped mention
                        "need your input",
                        "please provide",
                        "owner:** @ghabs",
                        "owner:** `@ghabs",  # Escaped mention
                        "blocker:",
                        "your input to proceed"
                    ])
                    
                    if needs_input:
                        # Extract preview (first 200 chars)
                        preview = body[:200] + "..." if len(body) > 200 else body
                        
                        if notify_agent_needs_input(issue_num, "agent", preview, project=project_name):
                            logger.info(f"📨 Sent input request alert for issue #{issue_num}")
                            notified_comments.add(comment_id)
                        else:
                            logger.warning(f"Failed to send input alert for issue #{issue_num}")
                
                except Exception as e:
                    logger.error(f"Error processing comment for issue #{issue_num}: {e}")

        _clear_polling_failures(loop_scope)
                    
    except Exception as e:
        logger.error(f"Error in check_agent_comments: {e}")
        _record_polling_failure(loop_scope, e)


def check_and_notify_pr(issue_num, project):
    """
    Check if there's a PR linked to the issue and notify user for review.

    Delegates to nexus-core's GitHubPlatform.search_linked_prs().

    Args:
        issue_num: GitHub issue number
        project: Project name
    """
    try:
        repo = get_github_repo(project)
        monitor_policy = get_github_workflow_policy_plugin(
            search_linked_prs=lambda **kwargs: asyncio.run(
                get_git_platform(kwargs["repo"]).search_linked_prs(str(kwargs["issue_number"]))
            ),
            cache_key=None,
        )
        pr = monitor_policy.find_open_linked_pr(repo=repo, issue_number=str(issue_num))
        if pr:
            logger.info(f"✅ Found PR #{pr.number} for issue #{issue_num}")
            notify_workflow_completed(
                issue_num, project, pr_number=str(pr.number), pr_url=pr.url,
            )
            return

        logger.info(f"ℹ️ No open PR found for issue #{issue_num}")
        notify_workflow_completed(issue_num, project)
    
    except Exception as e:
        logger.error(f"Error checking for PR: {e}")
        # Still notify even if PR check fails
        notify_workflow_completed(issue_num, project)


def check_completed_agents():
    """Monitor for completed agent steps and auto-chain to next agent.

    Delegates to _post_completion_comments_from_logs()
    which uses the nexus-core framework for completion scanning and auto-chaining.
    """
    _post_completion_comments_from_logs()


# SOP Checklist Templates
SOP_FULL = """## SOP Checklist — New Feature
- [ ] 1. **Vision & Scope** — Define requirements
- [ ] 2. **Technical Feasibility** — Assess approach and timeline
- [ ] 3. **Architecture Design** — Create ADR + breakdown
- [ ] 4. **UX Design** — Design wireframes
- [ ] 5. **Implementation** — Write code + tests
- [ ] 6. **Quality Gate** — Verify coverage
- [ ] 7. **Compliance Gate** — PIA (if user data)
- [ ] 8. **Deployment** — Deploy to production
- [ ] 9. **Documentation** — Update changelog + docs"""

SOP_SHORTENED = """## SOP Checklist — Bug Fix
- [ ] 1. **Triage** — Severity + routing
- [ ] 2. **Root Cause Analysis** — Investigate issue
- [ ] 3. **Fix** — Code + regression test
- [ ] 4. **Verify** — Regression suite
- [ ] 5. **Deploy** — Deploy to production
- [ ] 6. **Document** — Update changelog"""

SOP_FAST_TRACK = """## SOP Checklist — Fast-Track
- [ ] 1. **Triage** — Route to repo
- [ ] 2. **Implementation** — Code + tests
- [ ] 3. **Verify** — Quick check
- [ ] 4. **Deploy** — Deploy changes"""


def get_sop_tier(task_type, title=None, body=None):
    """Returns (tier_name, sop_template, workflow_label) based on task type AND content.
    
    Now integrates WorkflowRouter for intelligent routing based on issue content.
    
    Workflow mapping:
    - hotfix, chore, feature-simple, improvement-simple → fast-track:
        Triage → Develop → Review → Deploy
    - bug → shortened:
        Triage → Debug → Develop → Review → Deploy → Close
    - feature, improvement, release → full:
        Triage → Design → Develop → Review → Compliance → Deploy → Close
    """
    # Try intelligent routing if title and body provided
    if title or body:
        try:
            suggested_label = WorkflowRouter.suggest_tier_label(title or "", body or "")
            if suggested_label:
                logger.info(f"🤖 WorkflowRouter suggestion: {suggested_label}")
                if "fast-track" in suggested_label:
                    return "fast-track", SOP_FAST_TRACK, "workflow:fast-track"
                elif "shortened" in suggested_label:
                    return "shortened", SOP_SHORTENED, "workflow:shortened"
                elif "full" in suggested_label:
                    return "full", SOP_FULL, "workflow:full"
        except Exception as e:
            logger.warning(f"WorkflowRouter suggestion failed: {e}, falling back to task_type")
    
    # Fallback: Original task_type-based routing
    if any(t in task_type for t in ["hotfix", "chore", "simple"]):
        return "fast-track", SOP_FAST_TRACK, "workflow:fast-track"
    elif "bug" in task_type:
        return "shortened", SOP_SHORTENED, "workflow:shortened"
    else:
        return "full", SOP_FULL, "workflow:full"


def create_github_issue(title, body, project, workflow_label, task_type, tier_name, github_repo):
    """Creates a GitHub Issue in the specified repo with SOP checklist."""
    type_label = f"type:{task_type}"
    project_label = f"project:{project}"
    labels = [project_label, type_label, workflow_label]

    creator = _get_github_issue_plugin(github_repo, max_attempts=3, timeout=30)

    try:
        issue_url = creator.create_issue(
            title=title,
            body=body,
            labels=labels,
        )
        if issue_url:
            logger.info("📋 Issue created via plugin")
            return issue_url

        raise RuntimeError("GitHub issue plugin returned no issue URL")
    except Exception as e:
        raise RuntimeError(f"GitHub issue plugin create failed: {e}") from e


def generate_issue_name(content, project_name):
    """Generate a concise issue name using orchestrator (CLI only).
    
    Returns a slugified name in format: "this-is-the-issue-name"
    Falls back to slugified content if AI tools are unavailable.
    """
    try:
        logger.info("Generating concise issue name with orchestrator...")
        result = orchestrator.run_text_to_speech_analysis(
            text=content[:500],
            task="generate_name",
            project_name=project_name
        )

        suggested_name = result.get("text", "").strip().strip('"`\'').strip()
        slug = slugify(suggested_name)

        if slug:
            logger.info(f"✨ Orchestrator suggested: {slug}")
            return slug
        
        raise ValueError("Empty slug from orchestrator")

    except Exception as e:
        logger.warning(f"Name generation failed: {e}, using fallback")
        body = re.sub(r'^#.*\n', '', content)
        body = re.sub(r'\*\*.*\*\*.*\n', '', body)
        return slugify(body.strip()) or "generic-task"



def process_file(filepath):
    """Processes a single task file."""
    logger.info(f"Processing: {filepath}")

    try:
        with open(filepath, "r") as f:
            content = f.read()

        # Parse Metadata
        type_match = re.search(r'\*\*Type:\*\*\s*(.+)', content)
        task_type = type_match.group(1).strip().lower() if type_match else "feature"

        # Determine project from filepath
        # filepath is .../workspace/.nexus/inbox/<project>/file.md
        nexus_dir_name = get_nexus_dir_name()
        marker = f"{os.sep}{nexus_dir_name}{os.sep}inbox{os.sep}"
        project_name = None
        config = None
        project_root = None

        if marker in filepath:
            prefix, suffix = filepath.split(marker, 1)
            project_name = suffix.split(os.sep, 1)[0] if suffix else None
            project_root = prefix

        if project_name and project_name in PROJECT_CONFIG:
            cfg = PROJECT_CONFIG.get(project_name)
            if isinstance(cfg, dict):
                config = cfg

        # Fallback: look up project by matching workspace path
        if not config:
            for key, cfg in _iter_project_configs():
                workspace = cfg.get("workspace")
                if not workspace:
                    continue
                workspace_abs = os.path.join(BASE_DIR, workspace)
                if filepath.startswith(workspace_abs + os.sep):
                    project_name = key
                    config = cfg
                    project_root = workspace_abs
                    break
        
        if not config:
            logger.warning(f"⚠️ No project config for workspace '{project_root}', skipping.")
            return

        logger.info(f"Project: {project_name}")

        # Check if this task came from webhook (already has GitHub issue)
        source_match = re.search(r'\*\*Source:\*\*\s*(.+)', content)
        source = source_match.group(1).strip().lower() if source_match else None
        
        # Extract issue number and URL if from webhook
        issue_num_match = re.search(r'\*\*Issue Number:\*\*\s*(.+)', content)
        issue_url_match = re.search(r'\*\*URL:\*\*\s*(.+)', content)
        agent_type_match = re.search(r'\*\*Agent Type:\*\*\s*(.+)', content)
        
        if source == "webhook":
            # This task is from a webhook - issue already exists, skip creation
            issue_number = issue_num_match.group(1).strip() if issue_num_match else None
            issue_url = issue_url_match.group(1).strip() if issue_url_match else None
            agent_type = agent_type_match.group(1).strip() if agent_type_match else "triage"
            
            if not issue_url or not issue_number:
                logger.error(f"⚠️ Webhook task missing issue URL or number, skipping: {filepath}")
                return
            
            logger.info(f"📌 Webhook task for existing issue #{issue_number}, launching agent directly")
            
            # Guard: skip if an agent was recently launched for this issue
            # (prevents double-launch when processor creates a GH issue and the
            # resulting webhook fires back into this path)
            if is_recent_launch(issue_number):
                logger.info(f"⏭️ Skipping webhook launch for issue #{issue_number} — agent recently launched")
                # Still move file to active so it's not re-processed
                active_dir = get_tasks_active_dir(project_root, project_name)
                os.makedirs(active_dir, exist_ok=True)
                new_filepath = os.path.join(active_dir, os.path.basename(filepath))
                shutil.move(filepath, new_filepath)
                return
            
            # Move file to project workspace active folder
            active_dir = get_tasks_active_dir(project_root, project_name)
            os.makedirs(active_dir, exist_ok=True)
            new_filepath = os.path.join(active_dir, os.path.basename(filepath))
            logger.info(f"Moving task to active: {new_filepath}")
            shutil.move(filepath, new_filepath)
            
            # Launch agent directly for existing GitHub issue
            agents_dir_val = config.get("agents_dir")
            if agents_dir_val and issue_url:
                agents_abs = os.path.join(BASE_DIR, agents_dir_val)
                workspace_abs = os.path.join(BASE_DIR, config["workspace"])
                
                # Use specified agent type or get from workflow
                if not agent_type or agent_type == "triage":
                    agent_type = _get_initial_agent_from_workflow(project_name)
                    if not agent_type:
                        logger.error(f"Stopping launch: missing workflow definition for {project_name}")
                        send_telegram_alert(f"Stopping launch: missing workflow for {project_name}")
                        return
                
                # Resolve tier (halt if unknown — prevents wrong workflow execution)
                repo_for_tier = config.get("github_repo", get_github_repo("nexus"))
                tier_name = _resolve_tier_for_issue(
                    issue_number, project_name, repo_for_tier, context="webhook launch"
                )
                if not tier_name:
                    return

                pid, tool_used = invoke_copilot_agent(
                    agents_dir=agents_abs,
                    workspace_dir=workspace_abs,
                    issue_url=issue_url,
                    tier_name=tier_name,
                    task_content=content,
                    log_subdir=project_name,
                    agent_type=agent_type,
                    project_name=project_name
                )
                
                if pid:
                    try:
                        with open(new_filepath, 'a') as f:
                            f.write(f"\n**Agent PID:** {pid}\n")
                            f.write(f"**Agent Tool:** {tool_used}\n")
                    except Exception as e:
                        logger.error(f"Failed to append PID: {e}")
                
                logger.info(f"✅ Launched {agent_type} agent for webhook issue #{issue_number}")
            else:
                logger.info(f"ℹ️ No agents directory for {project_name}, skipping agent launch.")
            
            return  # Done processing webhook task
        
        # Standard task processing (create new GitHub issue)
        # Check if issue name was already generated (in telegram_bot)
        issue_name_match = re.search(r'\*\*Issue Name:\*\*\s*(.+)', content)
        if issue_name_match:
            slug = slugify(issue_name_match.group(1).strip())
            logger.info(f"✅ Using pre-generated issue name: {slug}")
        else:
            # Fallback: Generate concise issue name using Gemini AI
            slug = generate_issue_name(content, project_name)

        # Determine SOP tier using intelligent routing (pass content for WorkflowRouter analysis)
        tier_name, sop_template, workflow_label = get_sop_tier(
            task_type=task_type,
            title=slug,  # Use slug as preliminary title
            body=content  # Pass full content for intelligent routing
        )
        sop_checklist = sop_template

        # Move file to project workspace active folder
        active_dir = get_tasks_active_dir(project_root, project_name)
        os.makedirs(active_dir, exist_ok=True)
        new_filepath = os.path.join(active_dir, os.path.basename(filepath))
        logger.info(f"Moving task to active: {new_filepath}")
        shutil.move(filepath, new_filepath)

        # Create GitHub Issue with SOP checklist
        # Build type prefix for issue title
        type_prefixes = {
            "feature": "feat",
            "feature-simple": "feat",
            "bug": "fix",
            "hotfix": "hotfix",
            "chore": "chore",
            "refactor": "refactor",
            "improvement": "feat",
            "improvement-simple": "feat",
        }
        prefix = type_prefixes.get(task_type, task_type.split("-")[0] if "-" in task_type else task_type)
        issue_title = f"[{project_name}] {prefix}/{slug}"
        
        # Determine target branch name
        branch_name = f"{prefix}/{slug}"
        
        issue_body = f"""## Task
{content}

---

{sop_checklist}

---

**Project:** {project_name}
**Tier:** {tier_name}
**Target Branch:** `{branch_name}`
**Task File:** `{new_filepath}`"""

        issue_url = create_github_issue(
            title=issue_title,
            body=issue_body,
            project=project_name,
            workflow_label=workflow_label,
            task_type=task_type,
            tier_name=tier_name,
            github_repo=config["github_repo"]
        )

        if issue_url:
            # Rename task file from {task_type}_{telegram_msg_id}.md to
            # {task_type}_{issue_num}.md so the GitHub issue number is visible
            # in the filename instead of a random Telegram message ID.
            issue_num = issue_url.split('/')[-1]
            old_basename = os.path.basename(new_filepath)
            new_basename = re.sub(r'_(\d+)\.md$', f'_{issue_num}.md', old_basename)
            if new_basename != old_basename:
                renamed_path = os.path.join(os.path.dirname(new_filepath), new_basename)
                try:
                    os.rename(new_filepath, renamed_path)
                    logger.info(f"Renamed task file: {old_basename} → {new_basename}")
                    # Keep the issue body consistent — update the Task File path
                    corrected_body = issue_body.replace(new_filepath, renamed_path)
                    subprocess.run(
                        ["gh", "issue", "edit", issue_num,
                         "--body", corrected_body,
                         "--repo", config["github_repo"]],
                        capture_output=True, timeout=15
                    )
                    new_filepath = renamed_path
                except Exception as e:
                    logger.error(f"Failed to rename task file to issue-number name: {e}")

            # Append issue URL to the task file
            try:
                with open(new_filepath, 'a') as f:
                    f.write(f"\n\n**Issue:** {issue_url}\n")
            except Exception as e:
                logger.error(f"Failed to append issue URL: {e}")
            
            # Create nexus-core workflow
            workflow_plugin = get_workflow_state_plugin(
                **_WORKFLOW_STATE_PLUGIN_KWARGS,
                cache_key="workflow:state-engine",
            )
            workflow_id = asyncio.run(
                workflow_plugin.create_workflow_for_issue(
                    issue_number=issue_num,
                    issue_title=slug,
                    project_name=project_name,
                    tier_name=tier_name,
                    task_type=task_type,
                    description=content,
                )
            )
            if workflow_id:
                logger.info(f"✅ Created nexus-core workflow: {workflow_id}")
                try:
                    with open(new_filepath, 'a') as f:
                        f.write(f"**Workflow ID:** {workflow_id}\n")
                except Exception as e:
                    logger.error(f"Failed to append workflow ID: {e}")

        # Invoke Copilot CLI agent (if agents_dir is configured)
        agents_dir_val = config["agents_dir"]
        if agents_dir_val is not None and issue_url:
            agents_abs = os.path.join(BASE_DIR, agents_dir_val)
            workspace_abs = os.path.join(BASE_DIR, config["workspace"])
            initial_agent = _get_initial_agent_from_workflow(project_name)
            if not initial_agent:
                logger.error(
                    f"Stopping launch: missing workflow definition for {project_name}"
                )
                send_telegram_alert(
                    f"Stopping launch: missing workflow definition for {project_name}"
                )
                return

            pid, tool_used = invoke_copilot_agent(
                agents_dir=agents_abs,
                workspace_dir=workspace_abs,
                issue_url=issue_url,
                tier_name=tier_name,
                task_content=content,
                log_subdir=project_name,
                agent_type=initial_agent,
                project_name=project_name
            )

            if pid:
                # Log PID for tracking
                try:
                    with open(new_filepath, 'a') as f:
                        f.write(f"**Agent PID:** {pid}\n")
                        f.write(f"**Agent Tool:** {tool_used}\n")
                except Exception as e:
                    logger.error(f"Failed to append PID: {e}")
        else:
            logger.info(f"ℹ️ No agents directory for {project_name}, skipping Copilot CLI invocation.")

        logger.info(f"✅ Dispatch complete for [{project_name}] {slug} (Tier: {tier_name})")

    except Exception as e:
        logger.error(f"Failed to process {filepath}: {e}")


def main():
    logger.info(f"Inbox Processor started on {BASE_DIR}")
    logger.info(f"Stuck agent monitoring enabled (threshold: {STUCK_AGENT_THRESHOLD}s)")
    logger.info(f"Agent comment monitoring enabled")
    last_check = time.time()
    check_interval = 60  # Check for stuck agents and comments every 60 seconds
    
    while True:
        # Scan for md files in project/{nexus_dir}/inbox/<project>/*.md
        nexus_dir_name = get_nexus_dir_name()
        pattern = os.path.join(BASE_DIR, "**", nexus_dir_name, "inbox", "*", "*.md")
        files = glob.glob(pattern, recursive=True)

        for filepath in files:
            process_file(filepath)
        
        # Periodically check for stuck agents and agent comments
        current_time = time.time()
        if current_time - last_check >= check_interval:
            check_stuck_agents()
            check_agent_comments()
            check_completed_agents()
            last_check = current_time

        time.sleep(SLEEP_INTERVAL)


if __name__ == "__main__":
    main()

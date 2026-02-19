import asyncio
import glob
import json
import logging
import os
import re
import shutil
import subprocess
import time
from typing import Optional

import yaml

# Nexus Core framework imports
from nexus.core.completion import (
    CompletionSummary,
    build_completion_comment,
    scan_for_completions,
)

# Import centralized configuration
from config import (
    BASE_DIR, get_github_repo,
    SLEEP_INTERVAL, STUCK_AGENT_THRESHOLD,
    PROJECT_CONFIG, DATA_DIR, INBOX_PROCESSOR_LOG_FILE, ORCHESTRATOR_CONFIG,
    USE_NEXUS_CORE, get_inbox_dir, get_tasks_active_dir, get_tasks_logs_dir, get_nexus_dir_name
)
from state_manager import StateManager
from models import WorkflowState
from agent_monitor import AgentMonitor, WorkflowRouter
from agent_launcher import invoke_copilot_agent, is_recent_launch
from ai_orchestrator import get_orchestrator
from nexus_core_helpers import create_workflow_for_issue_sync, get_git_platform, get_workflow_definition_path
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

# Wrapper functions for backward compatibility - these now delegate to StateManager
def load_launched_agents():
    """Load recently launched agents from persistent storage."""
    return StateManager.load_launched_agents()

def save_launched_agents(data):
    """Save launched agents to persistent storage."""
    StateManager.save_launched_agents(data)

def load_workflow_state():
    """Load workflow state (paused/stopped issues) from persistent storage."""
    return StateManager.load_workflow_state()

def save_workflow_state(data):
    """Save workflow state to persistent storage."""
    StateManager.save_workflow_state(data)

def set_workflow_state(issue_num, state):
    """Set workflow state for an issue (paused, stopped, or active)."""
    # Convert string to enum if needed
    if isinstance(state, str):
        try:
            state = WorkflowState[state.upper()]
        except KeyError:
            state = WorkflowState.ACTIVE
    StateManager.set_workflow_state(str(issue_num), state)

def get_workflow_state(issue_num):
    """Get workflow state for an issue. Returns 'active', 'paused', 'stopped', or None."""
    state = StateManager.get_workflow_state(str(issue_num))
    return state.value  # Return string value for backward compatibility

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

# Logging
logging.basicConfig(
    format='%(asctime)s - %(levelname)s - %(message)s',
    level=logging.INFO,
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler(INBOX_PROCESSOR_LOG_FILE)
    ]
)
logger = logging.getLogger("InboxProcessor")


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
    try:
        platform = get_git_platform(get_github_repo(default_project))
        issue = asyncio.run(platform.get_issue(str(issue_num)))
        if not issue:
            return get_github_repo(default_project)

        body = issue.body or ""
        task_file_match = re.search(r"\*\*Task File:\*\*\s*`([^`]+)`", body)
        if not task_file_match:
            logger.debug(f"No task file in issue #{issue_num} body, returning default repo")
            return get_github_repo(default_project)

        task_file = task_file_match.group(1)
        for key, cfg in _iter_project_configs():
            workspace = cfg.get("workspace")
            if not workspace:
                continue
            workspace_abs = os.path.join(BASE_DIR, workspace)
            if task_file.startswith(workspace_abs):
                logger.debug(f"Issue #{issue_num} resolved to project '{key}'")
                return get_github_repo(key)
        logger.debug(f"Issue #{issue_num} task file doesn't match any project workspace")
    except Exception as e:
        logger.debug(f"Could not resolve repo for issue #{issue_num} (will use default): {e}")

    return get_github_repo(default_project)


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
    pr_url = None

    # --- Create branch + PR if there are uncommitted changes ---
    if project_name:
        git_dir = _resolve_git_dir(project_name)
        if not git_dir:
            logger.info(f"No git repo found for {project_name} — skipping PR creation")
        else:
            try:
                platform = get_git_platform(repo)
                pr_body = (
                    f"Automated PR for issue #{issue_num}.\n\n"
                    f"Workflow completed by Nexus agent chain.\n"
                    f"Last agent: `{last_agent}`\n\n"
                    f"Closes #{issue_num}"
                )
                pr_result = asyncio.run(platform.create_pr_from_changes(
                    repo_dir=git_dir,
                    issue_number=str(issue_num),
                    title=f"fix: resolve #{issue_num}",
                    body=pr_body,
                ))
                if pr_result:
                    pr_url = pr_result.url
                    logger.info(f"🔀 Created PR for issue #{issue_num}: {pr_url}")
                else:
                    logger.info(f"No code changes for issue #{issue_num} — skipping PR creation")
            except Exception as e:
                logger.warning(f"Error creating PR for issue #{issue_num}: {e}")

    # --- Close the issue ---
    try:
        platform = get_git_platform(repo)
        close_comment = (
            f"✅ Workflow completed. All agent steps finished successfully.\n"
            f"Last agent: `{last_agent}`"
        )
        if pr_url:
            close_comment += f"\nPR: {pr_url}"

        asyncio.run(platform.close_issue(str(issue_num), comment=close_comment))
        logger.info(f"🔒 Closed issue #{issue_num}")
    except Exception as e:
        logger.warning(f"Error closing issue #{issue_num}: {e}")

    # --- Telegram notification ---
    parts = [
        f"✅ **Workflow Complete**\n\n"
        f"Issue: #{issue_num}\n"
        f"Last agent: `{last_agent}`\n"
    ]
    if pr_url:
        parts.append(f"PR: {pr_url}\n")
    parts.append(f"\n🔗 https://github.com/{repo}/issues/{issue_num}")
    send_telegram_alert("".join(parts))


def _post_completion_comments_from_logs() -> None:
    """Post GitHub comments when agents write completion_summary JSON files.
    
    Uses nexus-core's scan_for_completions() to detect completion files,
    then handles GitHub posting and auto-chaining (app-specific logic).
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
            check_running = subprocess.run(
                ["pgrep", "-af", f"copilot.*issues/{issue_num}[^0-9]|copilot.*issues/{issue_num}$"],
                text=True, capture_output=True, timeout=5,
            )
            if check_running.stdout:
                continue

            # Dedup using framework's dedup_key
            comment_key = detection.dedup_key
            if comment_key in completion_comments:
                continue

            logger.info(f"📋 Found completion summary for issue #{issue_num} ({summary.agent_type})")

            project_name = _resolve_project_from_path(detection.file_path)
            if project_name:
                repo = get_github_repo(project_name)
            else:
                repo = _resolve_repo_for_issue(issue_num)

            comment_body = build_completion_comment(summary)
            try:
                platform = get_git_platform(repo)
                asyncio.run(platform.add_comment(str(issue_num), comment_body))
            except Exception as e:
                logger.warning(
                    f"Could not post comment for issue #{issue_num} on {repo}: {e}"
                )
                continue

            completion_comments[comment_key] = time.time()
            save_completion_comments(completion_comments)
            completed_agent = summary.agent_type
            logger.info(f"📝 Posted completion comment for issue #{issue_num} ({completed_agent})")

            # --- Auto-chain to next agent (uses framework's is_workflow_done) ---
            if summary.is_workflow_done:
                logger.info(f"✅ Workflow complete for issue #{issue_num} (last agent: {completed_agent})")
                _finalize_workflow(issue_num, repo, completed_agent, project_name)
                continue

            next_agent = summary.next_agent.strip()

            if is_recent_launch(issue_num):
                logger.info(f"⏭️ Skipping auto-chain for issue #{issue_num} — agent recently launched")
                continue

            if not project_name:
                logger.warning(f"Cannot auto-chain issue #{issue_num}: could not resolve project")
                continue

            proj_cfg = PROJECT_CONFIG.get(project_name, {})
            agents_dir = proj_cfg.get("agents_dir", "")
            workspace = proj_cfg.get("workspace", "")
            if not agents_dir or not workspace:
                logger.warning(f"Cannot auto-chain issue #{issue_num}: missing agents_dir/workspace for {project_name}")
                continue

            agents_abs = os.path.join(BASE_DIR, agents_dir)
            workspace_abs = os.path.join(BASE_DIR, workspace)
            issue_url = f"https://github.com/{repo}/issues/{issue_num}"

            # Send transition notification
            send_telegram_alert(
                f"🔗 **Agent Transition**\n\n"
                f"Issue: #{issue_num}\n"
                f"Completed: `{completed_agent}`\n"
                f"Launching: `{next_agent}`\n\n"
                f"🔗 https://github.com/{repo}/issues/{issue_num}"
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
                tier_name="fast-track",
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
                    f"❌ **Auto-chain Failed**\n\n"
                    f"Issue: #{issue_num}\n"
                    f"Completed: `{completed_agent}`\n"
                    f"Failed to launch: `{next_agent}`\n\n"
                    f"🔗 https://github.com/{repo}/issues/{issue_num}"
                )
        except json.JSONDecodeError as e:
            logger.warning(f"Invalid completion_summary.json for issue #{detection.issue_number}: {e}")
        except Exception as e:
            logger.warning(f"Error processing completion summary for issue #{detection.issue_number}: {e}")


def _get_initial_agent_from_workflow(project_name: str) -> str:
    """Get the first agent/agent_type from a workflow YAML definition.

    Delegates to nexus-core's WorkflowDefinition.from_yaml() to parse the
    workflow, then reads the first step's agent name.

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
        workflow = WorkflowDefinition.from_yaml(path)
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


def check_stuck_agents():
    """Monitor agent processes and handle timeouts with auto-kill and retry."""
    try:
        # Find all copilot log files using configured nexus_dir
        nexus_dir_name = get_nexus_dir_name()
        log_files = glob.glob(
            os.path.join(BASE_DIR, "**", nexus_dir_name, "tasks", "logs", "**", "copilot_*.log"),
            recursive=True
        )
        
        for log_file in log_files:
            # Extract issue number from filename: copilot_4_20260215_112450.log
            match = re.search(r"copilot_(\d+)_", os.path.basename(log_file))
            if not match:
                continue
            
            issue_num = match.group(1)
            
            # Get latest log file for this issue only (ignore old ones)
            all_logs_for_issue = sorted(
                [f for f in log_files if f"copilot_{issue_num}_" in f],
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
                    # Get agent type from launched agents tracker
                    launched_agents = load_launched_agents()
                    agent_data = launched_agents.get(str(issue_num), {})
                    agent_type = agent_data.get('agent_type', 'unknown')
                    
                    # Check if we should retry
                    will_retry = AgentMonitor.should_retry(issue_num, agent_type)
                    notify_agent_timeout(issue_num, agent_type, will_retry, project="nexus")
            
    except Exception as e:
        logger.error(f"Error in check_stuck_agents: {e}")


def check_agent_comments():
    """Monitor GitHub issues for agent comments requesting input across all projects."""
    try:
        # Query issues from all project repos
        all_issue_nums = []
        for project_name, _ in _iter_project_configs():
            repo = get_github_repo(project_name)
            try:
                result = subprocess.run(
                    ["gh", "issue", "list", "--repo", repo,
                     "--label", "workflow:full,workflow:shortened,workflow:fast-track",
                     "--state", "open", "--json", "number", "--jq", ".[].number"],
                    text=True, capture_output=True, timeout=10
                )
                
                if result.stdout:
                    issue_numbers = result.stdout.strip().split("\n")
                    all_issue_nums.extend([(num, project_name, repo) for num in issue_numbers if num])
            except subprocess.TimeoutExpired:
                logger.warning(f"GitHub issue list timed out for project {project_name}")
                continue
        
        if not all_issue_nums:
            return
        
        for issue_num, project_name, repo in all_issue_nums:
            if not issue_num:
                continue
                
            # Get issue comments via framework
            try:
                platform = get_git_platform(repo)
                comments = asyncio.run(platform.get_comments(str(issue_num)))
                # Filter to only the bot owner's comments
                bot_comments = [c for c in comments if c.author == "Ghabs95"]
            except Exception as e:
                logger.warning(f"Failed to fetch comments for issue #{issue_num}: {e}")
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
                    
    except Exception as e:
        logger.error(f"Error in check_agent_comments: {e}")


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
        platform = get_git_platform(repo)
        prs = asyncio.run(platform.search_linked_prs(str(issue_num)))

        open_prs = [pr for pr in prs if pr.state == "open"]
        if open_prs:
            pr = open_prs[0]
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

    With USE_NEXUS_CORE enabled, delegates to _post_completion_comments_from_logs()
    which uses the nexus-core framework for completion scanning and auto-chaining.
    """
    if not USE_NEXUS_CORE:
        logger.warning("Legacy check_completed_agents path removed — set USE_NEXUS_CORE=true")
        return
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
    - hotfix, chore, feature-simple, improvement-simple → fast-track (4 steps): 
        Triage, Implementation, Verify, Deploy
    - bug → shortened (6 steps): 
        Triage, RCA, Fix, Verify, Deploy, Doc
    - feature, improvement, release → full (9 steps): 
        Vision, Feasibility, Architecture, UX, Implementation, QA, Compliance, Deploy, Doc
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

    cmd = [
        "gh", "issue", "create",
        "--repo", github_repo,
        "--title", title,
        "--body", body,
        "--label", f"{project_label},{type_label},{workflow_label}"
    ]

    try:
        result = run_command_with_retry(cmd, max_attempts=3, timeout=30)
        issue_url = result.stdout.strip()
        logger.info(f"📋 Issue created: {issue_url}")
        return issue_url
    except RetryExhaustedError as e:
        logger.error(f"Failed to create issue after retries: {e}")
        return None
    except Exception as e:
        logger.error(f"Unexpected error creating issue: {e}")
        return None


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
        # filepath is .../workspace/.nexus/inbox/file.md
        # Find which project config has a workspace that matches this path
        project_root = os.path.dirname(os.path.dirname(os.path.dirname(filepath)))
        
        # Look up project by matching workspace path
        project_name = None
        config = None
        for key, cfg in _iter_project_configs():
            workspace = cfg.get("workspace")
            if workspace:
                workspace_abs = os.path.join(BASE_DIR, workspace)
                if project_root == workspace_abs or project_root.startswith(workspace_abs + os.sep):
                    project_name = key
                    config = cfg
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
            
            # Move file to project workspace active folder
            active_dir = get_tasks_active_dir(project_root)
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
                
                pid, tool_used = invoke_copilot_agent(
                    agents_dir=agents_abs,
                    workspace_dir=workspace_abs,
                    issue_url=issue_url,
                    tier_name="fast-track",  # Default tier for webhook tasks
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
        active_dir = get_tasks_active_dir(project_root)
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
            # Append issue URL to the task file
            try:
                with open(new_filepath, 'a') as f:
                    f.write(f"\n\n**Issue:** {issue_url}\n")
            except Exception as e:
                logger.error(f"Failed to append issue URL: {e}")
            
            # Create nexus-core workflow if enabled
            if USE_NEXUS_CORE:
                # Extract issue number from URL
                issue_num = issue_url.split('/')[-1]
                workflow_id = create_workflow_for_issue_sync(
                    issue_number=issue_num,
                    issue_title=slug,
                    project_name=project_name,
                    tier_name=tier_name,
                    task_type=task_type,
                    description=content
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
        # Scan for md files in project/{nexus_dir}/inbox/*.md
        nexus_dir_name = get_nexus_dir_name()
        pattern = os.path.join(BASE_DIR, "**", nexus_dir_name, "inbox", "*.md")
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

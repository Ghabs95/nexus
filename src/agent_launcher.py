"""
Shared agent launching logic for inbox processor and webhook server.

This module provides a unified interface for launching GitHub Copilot agents
in response to workflow events, whether triggered by polling (inbox processor)
or webhooks (webhook server).
"""

import logging
import os
import re
import subprocess
import time
import glob
import yaml

# Nexus Core framework imports
from nexus.core.agents import find_agent_yaml
from nexus.core.guards import LaunchGuard

from config import (
    BASE_DIR,
    get_github_repo,
    PROJECT_CONFIG,
    ORCHESTRATOR_CONFIG,
    get_nexus_dir_name
)
from state_manager import StateManager
from notifications import notify_agent_completed, send_telegram_alert
from ai_orchestrator import get_orchestrator, ToolUnavailableError
from plugin_runtime import get_profiled_plugin

logger = logging.getLogger(__name__)
_issue_plugin_cache = {}
_launch_policy_plugin = None


def _get_issue_plugin(repo: str):
    """Return GitHub issue plugin instance for repository."""
    if repo in _issue_plugin_cache:
        return _issue_plugin_cache[repo]

    plugin = get_profiled_plugin(
        "github_agent_launcher",
        overrides={
            "repo": repo,
        },
        cache_key=f"github:agent-launcher:{repo}",
    )
    if plugin:
        _issue_plugin_cache[repo] = plugin
    return plugin


def _get_launch_policy_plugin():
    """Return shared agent launch policy plugin instance."""
    global _launch_policy_plugin
    if _launch_policy_plugin:
        return _launch_policy_plugin

    plugin = get_profiled_plugin(
        "agent_launch_policy",
        cache_key="agent-launch:policy",
    )
    if plugin:
        _launch_policy_plugin = plugin
    return plugin


def _pgrep_and_logfile_guard(issue_id: str, agent_type: str) -> bool:
    """Custom guard: returns True (allow) if no running process AND no recent log.

    Check 1: pgrep for running Copilot process on this issue
    Check 2: recent log files (within last 2 minutes)
    """
    # Check 1: Running processes
    try:
        check_result = subprocess.run(
            ["pgrep", "-af",
             f"copilot.*issues/{issue_id}[^0-9]|copilot.*issues/{issue_id}$"],
            text=True, capture_output=True, timeout=5,
        )
        if check_result.stdout:
            logger.info(f"⏭️ Agent already running for issue #{issue_id} (PID found)")
            return False
    except Exception:
        pass

    # Check 2: Recent log files (within last 2 minutes)
    nexus_dir_name = get_nexus_dir_name()
    recent_logs = glob.glob(
        os.path.join(
            BASE_DIR, "**", nexus_dir_name, "tasks", "logs", "**",
            f"copilot_{issue_id}_*.log",
        ),
        recursive=True,
    )
    if recent_logs:
        recent_logs.sort(key=lambda p: os.path.getmtime(p), reverse=True)
        latest_log_age = time.time() - os.path.getmtime(recent_logs[0])
        if latest_log_age < 120:
            logger.info(
                f"⏭️ Recent log file for issue #{issue_id} ({latest_log_age:.0f}s old)"
            )
            return False

    return True  # allow launch


# Module-level singleton — LaunchGuard with 120s cooldown + pgrep/logfile custom guard.
_launch_guard = LaunchGuard(
    cooldown_seconds=120,
    custom_guard=_pgrep_and_logfile_guard,
)


def is_recent_launch(issue_number: str) -> bool:
    """Check if an agent was recently launched for this issue.

    Delegates to nexus-core's LaunchGuard (cooldown + pgrep + logfile checks).
    Returns True if launched within cooldown window.
    """
    # Use wildcard agent_type since callers don't differentiate
    return not _launch_guard.can_launch(str(issue_number), agent_type="*")


def record_agent_launch(issue_number: str, pid: int = None) -> None:
    """Record a successful agent launch in the LaunchGuard."""
    _launch_guard.record_launch(str(issue_number), agent_type="*", pid=pid)


def _resolve_workflow_path(project_name: str = None) -> str:
    """Resolve workflow definition path for project or global config."""
    workflow_path = ""
    if project_name:
        project_cfg = PROJECT_CONFIG.get(project_name, {})
        if isinstance(project_cfg, dict):
            workflow_path = project_cfg.get("workflow_definition_path", "")

    if not workflow_path:
        workflow_path = PROJECT_CONFIG.get("workflow_definition_path", "")

    if workflow_path and not os.path.isabs(workflow_path):
        workflow_path = os.path.join(BASE_DIR, workflow_path)

    return workflow_path


def _build_agent_search_dirs(agents_dir: str) -> list:
    """Build the ordered list of directories to search for agent YAML files.

    Starts with the project-specific *agents_dir*, then appends the shared
    org-level agents directory configured via ``shared_agents_dir`` in config.
    """
    dirs = [agents_dir]
    shared = PROJECT_CONFIG.get("shared_agents_dir", "")
    if shared:
        shared_abs = os.path.join(BASE_DIR, shared) if not os.path.isabs(shared) else shared
        if shared_abs != agents_dir:
            dirs.append(shared_abs)
    return dirs


def _get_copilot_translator_path() -> str:
    """Resolve the to_copilot.py translator path."""
    env_path = os.getenv("COPILOT_TRANSLATOR_PATH")
    if env_path:
        return env_path
    return os.path.join(
        BASE_DIR,
        "ghabs",
        "nexus-core",
        "examples",
        "translator",
        "to_copilot.py",
    )


def _ensure_agent_definition(agents_dir: str, agent_type: str) -> bool:
    """Ensure an agent definition exists by generating it from YAML if needed."""
    search_dirs = _build_agent_search_dirs(agents_dir)
    yaml_path = find_agent_yaml(agent_type, search_dirs)
    if not yaml_path:
        logger.error(f"Missing agent YAML for agent_type '{agent_type}' in {search_dirs}")
        send_telegram_alert(
            f"Missing agent YAML for agent_type '{agent_type}' in {search_dirs}."
        )
        return False

    agent_md_path = os.path.splitext(yaml_path)[0] + ".agent.md"
    if os.path.exists(agent_md_path):
        if os.path.getmtime(agent_md_path) >= os.path.getmtime(yaml_path):
            return True

    translator_path = _get_copilot_translator_path()
    if not os.path.exists(translator_path):
        logger.error(f"Missing translator script: {translator_path}")
        send_telegram_alert(
            f"Missing translator script: {translator_path}."
        )
        return False

    try:
        result = subprocess.run(
            ["python3", translator_path, yaml_path],
            capture_output=True,
            text=True,
            check=False,
        )
        if result.returncode != 0:
            logger.error(
                "Translator failed: %s",
                result.stderr.strip() or result.stdout.strip(),
            )
            send_telegram_alert(
                f"Translator failed for agent_type '{agent_type}'."
            )
            return False
        if not result.stdout.strip():
            logger.error(f"Translator produced empty output for {yaml_path}")
            send_telegram_alert(
                f"Translator produced empty output for agent_type '{agent_type}'."
            )
            return False
        with open(agent_md_path, "w", encoding="utf-8") as handle:
            handle.write(result.stdout)
        logger.info(f"✅ Generated agent instructions: {agent_md_path}")
        return True
    except Exception as e:
        logger.error(f"Translator error for {yaml_path}: {e}")
        send_telegram_alert(
            f"Translator error for agent_type '{agent_type}': {str(e)}"
        )
        return False


def get_sop_tier_from_issue(issue_number, project="nexus"):
    """Get workflow tier from issue labels.

    Delegates to nexus-core's GitHubPlatform.get_workflow_type_from_issue().

    Args:
        issue_number: GitHub issue number
        project: Project name to determine repo

    Returns: tier_name (full/shortened/fast-track) or None
    """
    from nexus.adapters.git.github import GitHubPlatform

    try:
        repo = get_github_repo(project)
        platform = GitHubPlatform(repo)
        return platform.get_workflow_type_from_issue(int(issue_number))
    except Exception as e:
        logger.error(f"Failed to get tier from issue #{issue_number} in {project}: {e}")
        return None


def get_workflow_name(tier_name):
    """Returns the workflow slash-command name for the tier."""
    policy = _get_launch_policy_plugin()
    if policy and hasattr(policy, "get_workflow_name"):
        return policy.get_workflow_name(tier_name)
    if tier_name in {"fast-track", "shortened"}:
        return "bug_fix"
    return "new_feature"



def invoke_copilot_agent(
    agents_dir,
    workspace_dir,
    issue_url,
    tier_name,
    task_content,
    continuation=False,
    continuation_prompt=None,
    use_gemini=False,
    log_subdir=None,
    agent_type="triage",
    project_name=None
):
    """Invokes an AI agent on the agents directory to process the task.

    Uses orchestrator to determine best tool (Copilot or Gemini CLI) with fallback support.
    Runs asynchronously (Popen) since agent execution can take several minutes.
    
    Args:
        agents_dir: Path to agents directory
        workspace_dir: Path to workspace directory
        issue_url: GitHub issue URL
        tier_name: Workflow tier (full/shortened/fast-track)
        task_content: Task description
        continuation: If True, this is a continuation of previous work
        continuation_prompt: Custom prompt for continuation
        use_gemini: If True, prefer Gemini CLI; if False, prefer Copilot (default: False)
        agent_type: Agent type to route to (triage, design, analysis, etc.)
        project_name: Project name for resolving workflow definition
        
    Returns:
        Tuple of (PID of launched process or None if failed, tool_used: str)
    """
    workflow_name = get_workflow_name(tier_name)
    workflow_path = _resolve_workflow_path(project_name)
    policy = _get_launch_policy_plugin()
    if policy and hasattr(policy, "build_agent_prompt"):
        prompt = policy.build_agent_prompt(
            issue_url=issue_url,
            tier_name=tier_name,
            task_content=task_content,
            agent_type=agent_type,
            continuation=continuation,
            continuation_prompt=continuation_prompt,
            workflow_path=workflow_path,
            nexus_dir=get_nexus_dir_name(),
        )
    else:
        prompt = (
            f"You are a {agent_type} agent.\n\n"
            f"Issue: {issue_url}\n"
            f"Tier: {tier_name}\n"
            f"Workflow: /{workflow_name}\n\n"
            f"Task details:\n{task_content}"
        )

    mode = "continuation" if continuation else "initial"
    logger.info(f"🤖 Launching {agent_type} agent in {agents_dir} (mode: {mode})")
    logger.info(f"   Workspace: {workspace_dir}")
    logger.info(f"   Workflow: /{workflow_name} (tier: {tier_name})")

    if not _ensure_agent_definition(agents_dir, agent_type):
        return None, None

    # Use orchestrator to launch agent
    orchestrator = get_orchestrator(ORCHESTRATOR_CONFIG)
    
    try:
        pid, tool_used = orchestrator.invoke_agent(
            agent_prompt=prompt,
            workspace_dir=workspace_dir,
            agents_dir=agents_dir,
            base_dir=BASE_DIR,
            issue_url=issue_url,
            agent_name=agent_type,
            use_gemini=use_gemini,
            log_subdir=log_subdir
        )
        
        logger.info(f"🚀 Agent launched with {tool_used.value} (PID: {pid})")
        
        # Extract issue number for tracking
        issue_match = re.search(r"/issues/(\d+)", issue_url or "")
        issue_num = issue_match.group(1) if issue_match else "unknown"
        
        # Save to launched agents tracker
        if issue_num != "unknown":
            launched_agents = StateManager.load_launched_agents()
            launched_agents[str(issue_num)] = {
                'timestamp': time.time(),
                'pid': pid,
                'tier': tier_name,
                'mode': mode,
                'tool': tool_used.value,
                'agent_type': agent_type
            }
            StateManager.save_launched_agents(launched_agents)
            
            # Record in LaunchGuard for dedup
            record_agent_launch(issue_num, pid=pid)
            
            # Audit log
            StateManager.audit_log(
                int(issue_num),
                "AGENT_LAUNCHED",
                f"Launched {tool_used.value} agent in {os.path.basename(agents_dir)} "
                f"(workflow: {workflow_name}/{tier_name}, mode: {mode}, PID: {pid})"
            )
        
        return pid, tool_used.value
        
    except ToolUnavailableError as e:
        logger.error(f"❌ All AI tools unavailable: {e}")
        
        issue_match = re.search(r"/issues/(\d+)", issue_url or "")
        issue_num = issue_match.group(1) if issue_match else "unknown"
        if issue_num != "unknown":
            StateManager.audit_log(
                int(issue_num),
                "AGENT_LAUNCH_FAILED",
                f"All tools unavailable: {str(e)}"
            )
        
        return None, None
    except Exception as e:
        logger.error(f"❌ Failed to launch agent: {e}")
        
        issue_match = re.search(r"/issues/(\d+)", issue_url or "")
        issue_num = issue_match.group(1) if issue_match else "unknown"
        if issue_num != "unknown":
            StateManager.audit_log(
                int(issue_num),
                "AGENT_LAUNCH_FAILED",
                f"Exception: {str(e)}"
            )
        
        return None, None

def launch_next_agent(issue_number, next_agent, trigger_source="unknown"):
    """
    Launch the next agent in the workflow chain.
    
    This is the main entry point used by both inbox_processor and webhook_server.
    
    Args:
        issue_number: GitHub issue number (string or int)
        next_agent: Name of the agent to launch (e.g., "Atlas", "Architect")
        trigger_source: Where the trigger came from ("github_webhook", "log_file", "github_comment")
        
    Returns:
        True if agent was launched successfully, False otherwise
    """
    issue_number = str(issue_number)
    logger.info(f"🔗 Launching next agent @{next_agent} for issue #{issue_number} (trigger: {trigger_source})")
    
    # Check for duplicate launches
    if is_recent_launch(issue_number):
        logger.info(f"⏭️ Skipping duplicate launch for issue #{issue_number}")
        return False
    
    # Get issue details
    try:
        repo = get_github_repo("nexus")
        plugin = _get_issue_plugin(repo)
        if not plugin:
            logger.error(f"GitHub issue plugin unavailable for repo {repo}")
            return False

        data = plugin.get_issue(str(issue_number), ["body"])
        if not data:
            logger.error(f"Failed to get issue #{issue_number} body")
            return False

        body = data.get("body", "")
    except Exception as e:
        logger.error(f"Failed to get issue #{issue_number} body: {e}")
        return False
    
    # Find task file
    task_file_match = re.search(r"\*\*Task File:\*\*\s*`([^`]+)`", body)
    if not task_file_match:
        logger.warning(f"No task file in issue #{issue_number}")
        return False
    
    task_file = task_file_match.group(1)
    if not os.path.exists(task_file):
        logger.warning(f"Task file not found: {task_file}")
        return False
    
    # Get project config
    project_root = None
    for key, cfg in PROJECT_CONFIG.items():
        workspace = cfg.get("workspace")
        if workspace:
            workspace_abs = os.path.join(BASE_DIR, workspace)
            if task_file.startswith(workspace_abs):
                project_root = key
                config = cfg
                break
    
    if not project_root or not config.get("agents_dir"):
        logger.warning(f"No project config for task file: {task_file}")
        return False
    
    # Read task content
    try:
        with open(task_file, "r") as f:
            task_content = f.read()
    except Exception as e:
        logger.error(f"Failed to read task file {task_file}: {e}")
        return False
    
    # Get workflow tier: launched_agents tracker → issue labels → halt if unknown
    from state_manager import StateManager
    repo = get_github_repo(project_root)
    tracker_tier = StateManager.get_last_tier_for_issue(issue_number)
    label_tier = get_sop_tier_from_issue(issue_number, project_root)
    tier_name = label_tier or tracker_tier
    if not tier_name:
        logger.error(
            f"Cannot determine workflow tier for issue #{issue_number}: "
            "no tracker entry and no workflow: label."
        )
        return False
    
    issue_url = f"https://github.com/{repo}/issues/{issue_number}"
    agents_abs = os.path.join(BASE_DIR, config["agents_dir"])
    workspace_abs = os.path.join(BASE_DIR, config["workspace"])
    
    # Create continuation prompt
    continuation_prompt = (
        f"You are a {next_agent} agent. The previous workflow step is complete.\n\n"
        f"Your task: Begin your step in the workflow.\n"
        f"Read recent GitHub comments to understand what's been completed.\n"
        f"Then perform your assigned work and post a status update.\n"
        f"End with a completion marker like: 'Ready for `@NextAgent`'"
    )
    
    # Launch agent
    pid, tool_used = invoke_copilot_agent(
        agents_dir=agents_abs,
        workspace_dir=workspace_abs,
        issue_url=issue_url,
        tier_name=tier_name,
        task_content=task_content,
        continuation=True,
        continuation_prompt=continuation_prompt,
        log_subdir=project_root,
        agent_type=next_agent,
        project_name=project_root
    )
    
    if pid:
        logger.info(f"✅ Successfully launched @{next_agent} for issue #{issue_number} (PID: {pid}, tool: {tool_used})")
        
        # Send notification
        try:
            project_label = project_root.replace("_", " ").title()
            notify_agent_completed(
                issue_number=int(issue_number),
                agent_name=next_agent,
                project=project_label
            )
        except Exception as e:
            logger.warning(f"Failed to send notification: {e}")
        
        return True
    else:
        logger.error(f"❌ Failed to launch @{next_agent} for issue #{issue_number}")
        return False

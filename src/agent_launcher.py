"""
Shared agent launching logic for inbox processor and webhook server.

This module provides a unified interface for launching GitHub Copilot agents
in response to workflow events, whether triggered by polling (inbox processor)
or webhooks (webhook server).
"""

import glob
import json
import logging
import os
import re
import subprocess
import time
import yaml

# Nexus Core framework imports
from nexus.core.completion import generate_completion_instructions
from nexus.core.guards import LaunchGuard
from nexus.core.workflow import WorkflowDefinition

from config import (
    BASE_DIR,
    get_github_repo,
    PROJECT_CONFIG,
    ORCHESTRATOR_CONFIG,
    get_nexus_dir_name
)
from state_manager import StateManager
from error_handling import run_command_with_retry
from notifications import notify_agent_completed, send_telegram_alert
from ai_orchestrator import get_orchestrator, ToolUnavailableError

logger = logging.getLogger(__name__)


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


def _get_workflow_steps_for_prompt(project_name: str = None) -> str:
    """Read the configured workflow YAML and return a formatted checklist
    of steps with agent_type names for embedding in agent prompts.

    Delegates to nexus-core's WorkflowDefinition.to_prompt_context().
    """
    # Resolve workflow path from project config
    workflow_path = ""
    if project_name:
        project_cfg = PROJECT_CONFIG.get(project_name, {})
        if isinstance(project_cfg, dict):
            workflow_path = project_cfg.get("workflow_definition_path", "")

    if not workflow_path:
        workflow_path = PROJECT_CONFIG.get("workflow_definition_path", "")

    if workflow_path and not os.path.isabs(workflow_path):
        workflow_path = os.path.join(BASE_DIR, workflow_path)

    if not workflow_path or not os.path.exists(workflow_path):
        return ""

    return WorkflowDefinition.to_prompt_context(workflow_path)


def _get_comment_and_summary_instructions(issue_url: str, agent_type: str, project_name: str = None) -> str:
    """Return prompt instructions telling the agent to post a structured GitHub comment
    and write a completion_summary JSON file when done.

    Delegates to nexus-core's generate_completion_instructions().
    """
    issue_match = re.search(r"/issues/(\d+)", issue_url or "")
    issue_num = issue_match.group(1) if issue_match else "UNKNOWN"

    workflow_steps = _get_workflow_steps_for_prompt(project_name)
    nexus_dir = get_nexus_dir_name()

    return generate_completion_instructions(
        issue_number=issue_num,
        agent_type=agent_type,
        workflow_steps_text=workflow_steps,
        nexus_dir=nexus_dir,
    )


def _normalize_agent_key(agent_name: str) -> str:
    """Normalize agent name for matching YAML metadata.name values."""
    name = re.sub(r"([a-z0-9])([A-Z])", r"\1-\2", agent_name)
    name = name.replace("_", "-").replace(" ", "-")
    name = re.sub(r"-+", "-", name)
    return name.lower()


def _find_agent_yaml(agents_dir: str, agent_type: str) -> str:
    """Find an agent YAML definition by spec.agent_type.
    
    Searches for an agent where spec.agent_type matches the requested type.
    This enables routing by abstract agent types (triage, design, debug, etc.)
    rather than specific agent names.
    """
    normalized = _normalize_agent_key(agent_type)
    patterns = [
        os.path.join(agents_dir, "**", "*.yaml"),
        os.path.join(agents_dir, "**", "*.yml"),
    ]
    for pattern in patterns:
        for path in glob.glob(pattern, recursive=True):
            try:
                with open(path, "r", encoding="utf-8") as handle:
                    data = yaml.safe_load(handle)
            except Exception:
                continue
            if not isinstance(data, dict):
                continue
            if data.get("kind") != "Agent":
                continue
            
            # Look for spec.agent_type field
            spec = data.get("spec", {})
            spec_agent_type = spec.get("agent_type", "")
            if not spec_agent_type:
                continue
            
            normalized_type = _normalize_agent_key(spec_agent_type)
            if normalized == normalized_type:
                return path
    return ""


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
    yaml_path = _find_agent_yaml(agents_dir, agent_type)
    if not yaml_path:
        logger.error(f"Missing agent YAML for agent_type '{agent_type}' in {agents_dir}")
        send_telegram_alert(
            f"Missing agent YAML for agent_type '{agent_type}' in {agents_dir}."
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
    
    Args:
        issue_number: GitHub issue number
        project: Project name to determine repo
    
    Returns: tier_name (full/shortened/fast-track) or None
    """
    try:
        repo = get_github_repo(project)
        result = run_command_with_retry(
            ["gh", "issue", "view", str(issue_number), "--repo", repo,
             "--json", "labels"],
            max_attempts=2,
            timeout=10
        )
        data = json.loads(result.stdout)
        labels = [l.get("name", "") for l in data.get("labels", [])]
        
        for label in labels:
            if "workflow:full" in label:
                return "full"
            elif "workflow:shortened" in label:
                return "shortened"
            elif "workflow:fast-track" in label:
                return "fast-track"
        
        return None
    except Exception as e:
        logger.error(f"Failed to get tier from issue #{issue_number} in {project}: {e}")
        return None


def get_workflow_name(tier_name):
    """Returns the workflow slash-command name for the tier."""
    if tier_name == "fast-track":
        return "bug_fix"
    elif tier_name == "shortened":
        return "bug_fix"
    else:
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
        agent_type: Agent type to route to (triage, design, debug, etc.)
        project_name: Project name for resolving workflow definition
        
    Returns:
        Tuple of (PID of launched process or None if failed, tool_used: str)
    """
    workflow_name = get_workflow_name(tier_name)

    if continuation:
        # Auto-chained continuation: use custom continuation_prompt directly
        if continuation_prompt and continuation_prompt.startswith("You are @"):
            # This is an auto-chain to a different agent, use it as-is
            prompt = (
                f"{continuation_prompt}\n\n"
                f"Issue: {issue_url}\n"
                f"Tier: {tier_name}\n"
                f"Workflow: /{workflow_name}\n\n"
                f"Review the previous work in the GitHub comments and task file, then complete your step.\n\n"
                f"**GIT WORKFLOW (CRITICAL):**\n"
                f"1. Check the issue body for **Target Branch** field (e.g., `feat/surveyor-plan`)\n"
                f"2. Identify the correct sub-repo within the workspace (e.g., casit-be, casit-app, casit-omi)\n"
                f"3. In that sub-repo: \n"
                f"   - For feat/fix/chore: create branch from `develop`: `git checkout develop && git pull && git checkout -b <branch-name>`\n"
                f"   - For hotfix: create branch from `main`: `git checkout main && git pull && git checkout -b <branch-name>`\n"
                f"4. Make your changes and commit with descriptive messages\n"
                f"5. Push the branch: `git push -u origin <branch-name>`\n"
                f"6. Include branch name in your GitHub comment (e.g., 'Pushed to feat/surveyor-plan in casit-be')\n\n"
                f"⛔ **GIT SAFETY RULES (STRICT):**\n"
                f"❌ NEVER push to protected branches: `main`, `develop`, `master`, `test`, `staging`, `production`\n"
                f"❌ NEVER delete any branch: No `git branch -d` or `git push --delete`\n"
                f"✅ ONLY push to the dedicated feature branch specified in **Target Branch** field\n"
                f"✅ Valid branch prefixes: feat/*, fix/*, hotfix/*, chore/*, refactor/*, docs/*, build/*, ci/*\n"
                f"⚠️  Violating these rules can break production and cause team disruption\n\n"
                f"{_get_comment_and_summary_instructions(issue_url, agent_type, project_name)}\n\n"
                f"Task context:\n{task_content}"
            )
        else:
            # Manual continuation - should not be used by webhook, but kept for compatibility
            base_prompt = continuation_prompt or "Please continue with the next step."
            prompt = (
                f"You are a {agent_type} agent. You previously started working on this task:\n\n"
                f"Issue: {issue_url}\n"
                f"Tier: {tier_name}\n"
                f"Workflow: /{workflow_name}\n\n"
                f"{base_prompt}\n\n"
                f"{_get_comment_and_summary_instructions(issue_url, agent_type, project_name)}\n\n"
                f"Task content:\n{task_content}"
            )
    else:
        # Fresh start prompt for initial agent (typically triage)
        prompt = (
            f"You are a {agent_type} agent. A new task has arrived and a GitHub issue has been created.\n\n"
            f"Issue: {issue_url}\n"
            f"Tier: {tier_name}\n"
            f"Workflow: /{workflow_name}\n\n"
            f"**YOUR JOB:** Analyze, triage, and route. DO NOT try to implement or invoke other agents.\n\n"
            f"REQUIRED ACTIONS:\n"
            f"1. Read the GitHub issue body and understand the task\n"
            f"2. Analyze the codebase to assess scope and complexity\n"
            f"3. Identify which sub-repo(s) are affected\n"
            f"4. Determine severity (Critical/High/Medium/Low)\n"
            f"5. Determine which agent type should handle it next\n\n"
            f"**DO NOT:**\n"
            f"❌ Read other agent configuration files\n"
            f"❌ Use any 'invoke', 'task', or 'run tool' to start other agents\n"
            f"❌ Try to implement the feature yourself\n\n"
            f"{_get_comment_and_summary_instructions(issue_url, agent_type, project_name)}\n\n"
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
        result = run_command_with_retry(
            ["gh", "issue", "view", str(issue_number), "--repo", get_github_repo("nexus"),
             "--json", "body"],
            max_attempts=2,
            timeout=10
        )
        data = json.loads(result.stdout)
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
    
    # Get workflow tier
    tier_name = get_sop_tier_from_issue(issue_number, project_root)
    if not tier_name:
        logger.warning(f"Could not determine workflow tier for issue #{issue_number}")
        tier_name = "full"  # Default to full workflow
    
    issue_url = f"https://github.com/{get_github_repo(project_root)}/issues/{issue_number}"
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

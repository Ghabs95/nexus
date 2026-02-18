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

from config import (
    BASE_DIR,
    get_github_repo,
    PROJECT_CONFIG,
    WORKFLOW_CHAIN,
    ORCHESTRATOR_CONFIG
)
from state_manager import StateManager
from error_handling import run_command_with_retry
from notifications import notify_agent_completed, notify_workflow_started
from ai_orchestrator import get_orchestrator, ToolUnavailableError

logger = logging.getLogger(__name__)


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





def is_recent_launch(issue_number):
    """Check if an agent was recently launched for this issue.
    
    Returns: True if launched within last 2 minutes
    """
    # Check 1: Running processes
    check_result = subprocess.run(
        ["pgrep", "-af", f"copilot.*issues/{issue_number}"],
        text=True, capture_output=True
    )
    if check_result.stdout:
        logger.info(f"⏭️ Agent already running for issue #{issue_number} (PID found)")
        return True
    
    # Check 2: Recently launched (persistent tracker)
    launched_agents_tracker = StateManager.load_launched_agents()
    if str(issue_number) in launched_agents_tracker:
        last_launch = launched_agents_tracker[str(issue_number)]
        age = time.time() - last_launch.get('timestamp', 0)
        if age < 120:  # Within last 2 minutes
            logger.info(f"⏭️ Agent recently launched for issue #{issue_number} ({age:.0f}s ago)")
            return True
    
    # Check 3: Recent log files (within last 2 minutes)
    recent_logs = glob.glob(
        os.path.join(
            BASE_DIR,
            "**",
            ".github",
            "tasks",
            "logs",
            "**",
            f"copilot_{issue_number}_*.log"
        ),
        recursive=True
    )
    if recent_logs:
        recent_logs.sort(key=lambda p: os.path.getmtime(p), reverse=True)
        latest_log_age = time.time() - os.path.getmtime(recent_logs[0])
        if latest_log_age < 120:  # Within last 2 minutes
            logger.info(f"⏭️ Recent log file for issue #{issue_number} ({latest_log_age:.0f}s old)")
            return True
    
    return False


def invoke_copilot_agent(
    agents_dir,
    workspace_dir,
    issue_url,
    tier_name,
    task_content,
    continuation=False,
    continuation_prompt=None,
    use_gemini=False,
    log_subdir=None
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
                f"When you finish your work:\n"
                f"1. Commit and push all changes to the target branch\n"
                f"2. Update the task file with your results\n"
                f"3. Post a GitHub comment summarizing your work and confirming the push\n"
                f"4. END YOUR COMMENT with a clear completion marker:\n\n"
                f"   **Required format:** 'Ready for @NextAgentName'\n"
                f"   Example: 'Ready for @ProductDesigner to begin UX design'\n\n"
                f"5. Exit when done - DO NOT attempt to invoke the next agent\n\n"
                f"The system will automatically detect your completion and launch the next agent.\n\n"
                f"Task context:\n{task_content}"
            )
        else:
            # Manual continuation - should not be used by webhook, but kept for compatibility
            base_prompt = continuation_prompt or "Please continue with the next step."
            prompt = (
                f"You are @ProjectLead. You previously started working on this task:\n\n"
                f"Issue: {issue_url}\n"
                f"Tier: {tier_name}\n"
                f"Workflow: /{workflow_name}\n\n"
                f"{base_prompt}\n\n"
                f"Complete your step and end with: 'Ready for @NextAgent'\n\n"
                f"Task content:\n{task_content}"
            )
    else:
        # Fresh start prompt for @ProjectLead
        prompt = (
            f"You are @ProjectLead. A new task has arrived and a GitHub issue has been created.\n\n"
            f"Issue: {issue_url}\n"
            f"Tier: {tier_name}\n"
            f"Workflow: /{workflow_name}\n\n"
            f"**YOUR JOB:** Triage and route only. DO NOT try to implement or invoke other agents.\n\n"
            f"REQUIRED ACTIONS:\n"
            f"1. Analyze the task requirements\n"
            f"2. Identify which sub-repo(s) are affected\n"
            f"3. Update the task file with triage details\n"
            f"4. Post a GitHub comment showing:\n"
            f"   - Task severity\n"
            f"   - Target sub-repo(s)\n"
            f"   - Which agent should handle it next\n"
            f"   - Use format: 'Ready for @NextAgent' (e.g., 'Ready for @Atlas')\n"
            f"5. **EXIT** - The system will auto-route to the next agent\n\n"
            f"**DO NOT:**\n"
            f"❌ Read other agent configuration files\n"
            f"❌ Use any 'invoke', 'task', or 'run tool' to start other agents\n"
            f"❌ Try to implement the feature yourself\n\n"
            f"Task details:\n{task_content}"
        )

    mode = "continuation" if continuation else "initial"
    logger.info(f"🤖 Launching AI agent in {agents_dir} (mode: {mode})")
    logger.info(f"   Workspace: {workspace_dir}")
    logger.info(f"   Workflow: /{workflow_name} (tier: {tier_name})")

    # Use orchestrator to launch agent
    orchestrator = get_orchestrator(ORCHESTRATOR_CONFIG)
    
    try:
        pid, tool_used = orchestrator.invoke_agent(
            agent_prompt=prompt,
            workspace_dir=workspace_dir,
            agents_dir=agents_dir,
            base_dir=BASE_DIR,
            issue_url=issue_url,
            agent_name="ProjectLead",  # This is always ProjectLead for workflow routing
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
                'tool': tool_used.value
            }
            StateManager.save_launched_agents(launched_agents)
            
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
        f"You are @{next_agent}. The previous workflow step is complete.\n\n"
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
        log_subdir=project_root
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

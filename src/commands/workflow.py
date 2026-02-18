"""Workflow control commands: pause, resume, stop, continue, new."""
import logging
import subprocess
from telegram import Update
from telegram.ext import ContextTypes
from state_manager import StateManager
from models import WorkflowState
from config import ALLOWED_USER_ID, USE_NEXUS_CORE, PROJECT_CONFIG
from nexus_core_helpers import (
    pause_workflow_sync, resume_workflow_sync, get_workflow_status_sync
)

logger = logging.getLogger(__name__)

PROJECT_ALIASES = {
    "casit": "case_italia",
    "wlbl": "wallible",
    "bm": "biome",
    "nexus": "nexus",
}


def _normalize_project_key(project: str) -> str:
    return PROJECT_ALIASES.get(project.lower(), project.lower())


def _get_project_repo(project_key: str) -> str:
    cfg = PROJECT_CONFIG.get(project_key, {})
    if isinstance(cfg, dict) and cfg.get("github_repo"):
        return cfg["github_repo"]
    raise ValueError(f"Unknown project '{project_key}'")


async def pause_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Pause auto-chaining for a workflow."""
    if ALLOWED_USER_ID and update.effective_user.id != ALLOWED_USER_ID:
        return

    if not context.args or len(context.args) < 2:
        await update.effective_message.reply_text(
            "⚠️ Usage: /pause <project> <issue#>"
        )
        return

    project_key = _normalize_project_key(context.args[0])
    if project_key not in PROJECT_CONFIG:
        await update.effective_message.reply_text("❌ Invalid project.")
        return

    issue_num = context.args[1].lstrip("#")
    if not issue_num.isdigit():
        await update.effective_message.reply_text("❌ Invalid issue number.")
        return

    # Try nexus-core first if enabled
    if USE_NEXUS_CORE:
        success = pause_workflow_sync(issue_num, reason="User requested via Telegram")
        if success:
            # Also update legacy StateManager for compatibility
            StateManager.set_workflow_state(issue_num, WorkflowState.PAUSED)
            StateManager.audit_log(int(issue_num), "WORKFLOW_PAUSED", "via nexus-core")
            
            # Get workflow status for richer feedback
            status = get_workflow_status_sync(issue_num)
            status_text = ""
            if status:
                status_text = (f"\n\n**Workflow:** {status['name']}\n"
                             f"**Step:** {status['current_step']}/{status['total_steps']} - {status['current_step_name']}")
            
            await update.effective_message.reply_text(
                f"⏸️ **Workflow paused for issue #{issue_num}**{status_text}\n\n"
                f"Auto-chaining is disabled. Agents can still complete work, but the next agent won't be launched automatically.\n\n"
                f"Use /resume {project_key} {issue_num} to re-enable auto-chaining."
            )
            return
    
    # Fallback to legacy StateManager
    StateManager.set_workflow_state(issue_num, WorkflowState.PAUSED)
    StateManager.audit_log(int(issue_num), "WORKFLOW_PAUSED")

    await update.effective_message.reply_text(
        f"⏸️ **Workflow paused for issue #{issue_num}**\n\n"
        f"Auto-chaining is disabled. Agents can still complete work, but the next agent won't be launched automatically.\n\n"
        f"Use /resume {project_key} {issue_num} to re-enable auto-chaining."
    )


async def resume_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Resume auto-chaining for a paused workflow."""
    if ALLOWED_USER_ID and update.effective_user.id != ALLOWED_USER_ID:
        return

    if not context.args or len(context.args) < 2:
        await update.effective_message.reply_text(
            "⚠️ Usage: /resume <project> <issue#>"
        )
        return

    project_key = _normalize_project_key(context.args[0])
    if project_key not in PROJECT_CONFIG:
        await update.effective_message.reply_text("❌ Invalid project.")
        return

    issue_num = context.args[1].lstrip("#")
    if not issue_num.isdigit():
        await update.effective_message.reply_text("❌ Invalid issue number.")
        return

    # Try nexus-core first if enabled
    if USE_NEXUS_CORE:
        success = resume_workflow_sync(issue_num)
        if success:
            # Also update legacy StateManager for compatibility
            StateManager.set_workflow_state(issue_num, WorkflowState.ACTIVE)
            StateManager.audit_log(int(issue_num), "WORKFLOW_RESUMED", "via nexus-core")
            
            # Get workflow status for richer feedback
            status = get_workflow_status_sync(issue_num)
            status_text = ""
            if status:
                status_text = (f"\n\n**Workflow:** {status['name']}\n"
                             f"**Step:** {status['current_step']}/{status['total_steps']} - {status['current_step_name']}")
            
            await update.effective_message.reply_text(
                f"▶️ **Workflow resumed for issue #{issue_num}**{status_text}\n\n"
                f"Auto-chaining is re-enabled. The next agent will be launched when the current step completes.\n"
                f"Check /active to see current progress."
            )
            return
    
    # Fallback to legacy StateManager
    StateManager.set_workflow_state(issue_num, WorkflowState.ACTIVE)
    StateManager.audit_log(int(issue_num), "WORKFLOW_RESUMED")

    await update.effective_message.reply_text(
        f"▶️ **Workflow resumed for issue #{issue_num}**\n\n"
        f"Auto-chaining is re-enabled. The next agent will be launched when the current step completes.\n"
        f"Check /active to see current progress."
    )


async def stop_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Stop a workflow and close the issue."""
    if ALLOWED_USER_ID and update.effective_user.id != ALLOWED_USER_ID:
        return

    if not context.args or len(context.args) < 2:
        await update.effective_message.reply_text(
            "⚠️ Usage: /stop <project> <issue#>"
        )
        return

    project_key = _normalize_project_key(context.args[0])
    if project_key not in PROJECT_CONFIG:
        await update.effective_message.reply_text("❌ Invalid project.")
        return

    issue_num = context.args[1].lstrip("#")
    if not issue_num.isdigit():
        await update.effective_message.reply_text("❌ Invalid issue number.")
        return

    # Kill any running agent first
    from inbox_processor import find_agent_pid_for_issue  # Import here to avoid circular dependency
    pid = find_agent_pid_for_issue(issue_num)
    if pid:
        try:
            subprocess.run(["kill", "-9", str(pid)], check=True, timeout=5)
            logger.info(f"Killed agent PID {pid} for issue #{issue_num}")
        except Exception as e:
            logger.error(f"Failed to kill agent: {e}")

    # Mark workflow as stopped
    StateManager.set_workflow_state(issue_num, WorkflowState.STOPPED)
    StateManager.audit_log(int(issue_num), "WORKFLOW_STOPPED")

    # Close the GitHub issue
    try:
        repo = _get_project_repo(project_key)
        subprocess.run(
            ["gh", "issue", "close", issue_num, "--repo", repo],
            check=True, timeout=10
        )
        logger.info(f"Closed issue #{issue_num}")
    except Exception as e:
        logger.error(f"Failed to close issue: {e}")

    await update.effective_message.reply_text(
        f"🛑 **Workflow stopped for issue #{issue_num}**\n\n"
        f"Auto-chaining disabled and issue closed.\n\n"
        f"Status: {pid and '✅ Agent killed' or '✅ No running agent'} | Issue closed"
    )

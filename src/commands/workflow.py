"""Workflow control commands: pause, resume, stop, continue, new."""
import logging
import subprocess
from telegram import Update
from telegram.ext import ContextTypes
from state_manager import StateManager
from models import WorkflowState
from config import ALLOWED_USER_ID

logger = logging.getLogger(__name__)


async def pause_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Pause auto-chaining for a workflow."""
    if ALLOWED_USER_ID and update.effective_user.id != ALLOWED_USER_ID:
        return

    if not context.args:
        await update.effective_message.reply_text(
            "⚠️ Usage: /pause <issue#>\n\nExample: /pause 0"
        )
        return

    issue_num = context.args[0].lstrip("#")
    if not issue_num.isdigit():
        await update.effective_message.reply_text("❌ Invalid issue number.")
        return

    StateManager.set_workflow_state(issue_num, WorkflowState.PAUSED)
    StateManager.audit_log(int(issue_num), "WORKFLOW_PAUSED")

    await update.effective_message.reply_text(
        f"⏸️ **Workflow paused for issue #{issue_num}**\n\n"
        f"Auto-chaining is disabled. Agents can still complete work, but the next agent won't be launched automatically.\n\n"
        f"Use /resume {issue_num} to re-enable auto-chaining."
    )


async def resume_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Resume auto-chaining for a paused workflow."""
    if ALLOWED_USER_ID and update.effective_user.id != ALLOWED_USER_ID:
        return

    if not context.args:
        await update.effective_message.reply_text(
            "⚠️ Usage: /resume <issue#>\n\nExample: /resume 0"
        )
        return

    issue_num = context.args[0].lstrip("#")
    if not issue_num.isdigit():
        await update.effective_message.reply_text("❌ Invalid issue number.")
        return

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

    if not context.args:
        await update.effective_message.reply_text(
            "⚠️ Usage: /stop <issue#>\n\nExample: /stop 0"
        )
        return

    issue_num = context.args[0].lstrip("#")
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
        subprocess.run(
            ["gh", "issue", "close", issue_num, "--repo", "Ghabs95/agents"],
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

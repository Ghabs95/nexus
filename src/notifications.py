"""Enhanced notifications with inline keyboards for Nexus.

Provides rich Telegram notifications with interactive buttons for quick actions.
"""
import logging
import requests
from typing import Dict, List, Optional
from config import TELEGRAM_TOKEN, TELEGRAM_CHAT_ID, get_github_repo

logger = logging.getLogger(__name__)


class InlineKeyboard:
    """Builder for Telegram inline keyboards."""
    
    def __init__(self):
        """Initialize keyboard builder."""
        self.rows: List[List[Dict]] = []
    
    def add_button(self, text: str, callback_data: Optional[str] = None, url: Optional[str] = None):
        """
        Add a button to the current row.
        
        Args:
            text: Button label text
            callback_data: Callback data for button press (ignored if url provided)
            url: Optional URL to open (makes button a URL button)
        
        Returns:
            Self for chaining
        """
        if not self.rows:
            self.rows.append([])
        
        button = {"text": text}
        if url:
            button["url"] = url
        elif callback_data:
            button["callback_data"] = callback_data
        else:
            raise ValueError("Either callback_data or url must be provided")
        
        self.rows[-1].append(button)
        return self
    
    def new_row(self):
        """Start a new row of buttons."""
        self.rows.append([])
        return self
    
    def build(self) -> Dict:
        """
        Build the keyboard structure.
        
        Returns:
            Inline keyboard markup dict
        """
        return {"inline_keyboard": self.rows}


def send_notification(
    message: str,
    parse_mode: str = "Markdown",
    keyboard: Optional[InlineKeyboard] = None
) -> bool:
    """
    Send a notification to Telegram with optional inline keyboard.
    
    Args:
        message: Message text
        parse_mode: Parse mode (Markdown or HTML)
        keyboard: Optional inline keyboard
    
    Returns:
        True if sent successfully
    """
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        logger.warning("Telegram credentials not configured, skipping notification")
        return False
    
    try:
        url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
        data = {
            "chat_id": TELEGRAM_CHAT_ID,
            "text": message,
            "parse_mode": parse_mode
        }
        
        if keyboard:
            data["reply_markup"] = keyboard.build()
        
        response = requests.post(url, json=data, timeout=10)
        return response.status_code == 200
    
    except Exception as e:
        logger.error(f"Failed to send notification: {e}")
        return False


def notify_agent_needs_input(issue_number: str, agent: str, preview: str, project: str = "nexus") -> bool:
    """
    Send notification that an agent needs input.
    
    Args:
        issue_number: GitHub issue number
        agent: Agent name
        preview: Preview of the agent's question
        project: Project name (default: nexus)
    
    Returns:
        True if sent successfully
    """
    message = (
        f"📋 **Agent Needs Input**\n\n"
        f"Issue: #{issue_number}\n"
        f"Agent: @{agent}\n\n"
        f"Preview:\n{preview}"
    )
    
    keyboard = (
        InlineKeyboard()
        .add_button("📝 View Full", callback_data=f"logs_{issue_number}")
        .add_button("🔗 GitHub", url=f"https://github.com/{get_github_repo(project)}/issues/{issue_number}")
        .new_row()
        .add_button("✍️ Respond", callback_data=f"respond_{issue_number}")
    )
    
    return send_notification(message, keyboard=keyboard)


def notify_workflow_started(issue_number: str, project: str, tier: str, task_type: str) -> bool:
    """
    Send notification that a workflow has started.
    
    Args:
        issue_number: GitHub issue number
        project: Project name
        tier: Workflow tier (full, shortened, fast-track)
        task_type: Task type (feature, bug, hotfix, etc.)
    
    Returns:
        True if sent successfully
    """
    tier_emoji = {
        "full": "🟡",
        "shortened": "🟠",
        "fast-track": "🟢"
    }
    
    message = (
        f"🚀 **Workflow Started**\n\n"
        f"Issue: #{issue_number}\n"
        f"Project: {project}\n"
        f"Type: {task_type}\n"
        f"Tier: {tier_emoji.get(tier, '⚪')} {tier}"
    )
    
    keyboard = (
        InlineKeyboard()
        .add_button("👀 Logs", callback_data=f"logs_{issue_number}")
        .add_button("📊 Status", callback_data=f"status_{issue_number}")
        .new_row()
        .add_button("🔗 GitHub", url=f"https://github.com/{get_github_repo(project)}/issues/{issue_number}")
        .add_button("⏸️ Pause", callback_data=f"pause_{issue_number}")
    )
    
    return send_notification(message, keyboard=keyboard)


def notify_agent_completed(issue_number: str, completed_agent: str, next_agent: str, project: str = "nexus") -> bool:
    """
    Send notification that an agent completed and next one started.
    
    Args:
        issue_number: GitHub issue number
        completed_agent: Agent that just completed
        next_agent: Agent that's starting next
        project: Project name (default: nexus)
    
    Returns:
        True if sent successfully
    """
    message = (
        f"✅ **Agent Completed → Auto-Chain**\n\n"
        f"Issue: #{issue_number}\n"
        f"Completed: @{completed_agent}\n"
        f"Next: @{next_agent}"
    )
    
    keyboard = (
        InlineKeyboard()
        .add_button("📝 View Logs", callback_data=f"logs_{issue_number}")
        .add_button("🔗 GitHub", url=f"https://github.com/{get_github_repo(project)}/issues/{issue_number}")
        .new_row()
        .add_button("⏸️ Pause Chain", callback_data=f"pause_{issue_number}")
        .add_button("🛑 Stop", callback_data=f"stop_{issue_number}")
    )
    
    return send_notification(message, keyboard=keyboard)


def notify_agent_timeout(issue_number: str, agent: str, will_retry: bool, project: str = "nexus") -> bool:
    """
    Send notification about agent timeout.
    
    Args:
        issue_number: GitHub issue number
        agent: Agent name
        will_retry: Whether the agent will be retried
        project: Project name (default: nexus)
    
    Returns:
        True if sent successfully
    """
    if will_retry:
        message = (
            f"⚠️ **Agent Timeout → Retrying**\n\n"
            f"Issue: #{issue_number}\n"
            f"Agent: @{agent}\n"
            f"Status: Process killed, retry scheduled"
        )
        
        keyboard = (
            InlineKeyboard()
            .add_button("📝 View Logs", callback_data=f"logs_{issue_number}")
            .add_button("🔗 GitHub", url=f"https://github.com/{get_github_repo(project)}/issues/{issue_number}")
            .new_row()
            .add_button("🔄 Reprocess Now", callback_data=f"reprocess_{issue_number}")
            .add_button("🛑 Stop", callback_data=f"stop_{issue_number}")
        )
    else:
        message = (
            f"❌ **Agent Failed → Max Retries**\n\n"
            f"Issue: #{issue_number}\n"
            f"Agent: @{agent}\n"
            f"Status: Manual intervention required"
        )
        
        keyboard = (
            InlineKeyboard()
            .add_button("📝 View Logs", callback_data=f"logs_{issue_number}")
            .add_button("🔗 GitHub", url=f"https://github.com/{get_github_repo(project)}/issues/{issue_number}")
            .new_row()
            .add_button("🔄 Reprocess", callback_data=f"reprocess_{issue_number}")
            .add_button("🛑 Stop Workflow", callback_data=f"stop_{issue_number}")
        )
    
    return send_notification(message, keyboard=keyboard)


def notify_workflow_completed(issue_number: str, project: str, pr_number: str = None, pr_url: str = None) -> bool:
    """
    Send notification that a workflow completed successfully.
    
    Args:
        issue_number: GitHub issue number
        project: Project name
        pr_number: Optional PR number if found
        pr_url: Optional PR URL if found
    
    Returns:
        True if sent successfully
    """
    if pr_number and pr_url:
        message = (
            f"🎉 **Workflow Completed**\n\n"
            f"Issue: #{issue_number}\n"
            f"Project: {project}\n"
            f"PR: #{pr_number}\n\n"
            f"All workflow steps completed. **Ready for review!**\n\n"
            f"🔗 Issue: https://github.com/{get_github_repo(project)}/issues/{issue_number}\n"
            f"🔗 PR: {pr_url}"
        )
        
        keyboard = (
            InlineKeyboard()
            .add_button("🔗 View PR", url=pr_url)
            .add_button("🔗 View Issue", url=f"https://github.com/{get_github_repo(project)}/issues/{issue_number}")
            .new_row()
            .add_button("✅ Approve", callback_data=f"approve_{issue_number}")
            .add_button("📝 Request Changes", callback_data=f"reject_{issue_number}")
            .new_row()
            .add_button("📝 Full Logs", callback_data=f"logsfull_{issue_number}")
            .add_button("📊 Audit", callback_data=f"audit_{issue_number}")
        )
    else:
        message = (
            f"🎉 **Workflow Completed**\n\n"
            f"Issue: #{issue_number}\n"
            f"Project: {project}\n"
            f"Status: All agents finished\n\n"
            f"⚠️ No PR found - implementation may be in progress."
        )
        
        keyboard = (
            InlineKeyboard()
            .add_button("📝 View Full Logs", callback_data=f"logsfull_{issue_number}")
            .add_button("🔗 GitHub", url=f"https://github.com/{get_github_repo(project)}/issues/{issue_number}")
            .new_row()
            .add_button("📊 View Audit Trail", callback_data=f"audit_{issue_number}")
        )
    
    return send_notification(message, keyboard=keyboard)


def notify_implementation_requested(issue_number: str, requester: str, project: str = "nexus") -> bool:
    """
    Send notification that implementation was requested.
    
    Args:
        issue_number: GitHub issue number
        requester: Who requested the implementation
        project: Project name (default: nexus)
    
    Returns:
        True if sent successfully
    """
    message = (
        f"🛠️ **Implementation Requested**\n\n"
        f"Issue: #{issue_number}\n"
        f"Requester: {requester}\n"
        f"Status: Awaiting approval"
    )
    
    keyboard = (
        InlineKeyboard()
        .add_button("✅ Approve", callback_data=f"approve_{issue_number}")
        .add_button("❌ Reject", callback_data=f"reject_{issue_number}")
        .new_row()
        .add_button("📝 View Details", callback_data=f"logs_{issue_number}")
        .add_button("🔗 GitHub", url=f"https://github.com/{get_github_repo(project)}/issues/{issue_number}")
    )
    
    return send_notification(message, keyboard=keyboard)


# Legacy compatibility function
def send_telegram_alert(message: str) -> bool:
    """
    Legacy function for backward compatibility.
    
    Args:
        message: Message text (Markdown)
    
    Returns:
        True if sent successfully
    """
    return send_notification(message, parse_mode="Markdown")

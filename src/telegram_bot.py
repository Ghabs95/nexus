import glob
import json
import logging
import os
import re
import subprocess
import sys
import time
from typing import List, Optional, Tuple
from datetime import datetime, timedelta
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, BotCommand, ReplyKeyboardMarkup, ReplyKeyboardRemove
from telegram.ext import (
    ApplicationBuilder, ContextTypes, CommandHandler,
    MessageHandler, CallbackQueryHandler, ConversationHandler, filters
)

# Import configuration from centralized config module
from config import (
    TELEGRAM_TOKEN, ALLOWED_USER_ID, BASE_DIR,
    DATA_DIR, TRACKED_ISSUES_FILE, get_github_repo, get_default_github_repo, PROJECT_CONFIG, ensure_data_dir,
    TELEGRAM_BOT_LOG_FILE, TELEGRAM_CHAT_ID, ORCHESTRATOR_CONFIG, LOGS_DIR,
    get_inbox_dir, get_tasks_active_dir, get_tasks_closed_dir, get_tasks_logs_dir, get_nexus_dir_name,
    NEXUS_CORE_STORAGE_DIR,
)
from state_manager import StateManager
from audit_store import AuditStore
from models import WorkflowState
from commands.workflow import (
    pause_handler as workflow_pause_handler,
    resume_handler as workflow_resume_handler,
    stop_handler as workflow_stop_handler,
)
from agent_launcher import invoke_copilot_agent, get_sop_tier_from_issue
from inbox_processor import get_sop_tier, _normalize_agent_reference
from nexus.core.completion import scan_for_completions
from ai_orchestrator import get_orchestrator
from nexus.plugins.builtin.ai_runtime_plugin import AIProvider
from plugin_runtime import get_profiled_plugin, get_runtime_ops_plugin, get_workflow_state_plugin
from error_handling import format_error_for_user, run_command_with_retry
from analytics import get_stats_report
from rate_limiter import get_rate_limiter, RateLimit
from report_scheduler import ReportScheduler
from user_manager import get_user_manager
from alerting import init_alerting_system

# --- LOGGING ---
logger = logging.getLogger(__name__)
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO,
    force=True,
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler(TELEGRAM_BOT_LOG_FILE)
    ]
)

# Initialize AI Orchestrator (CLI-only: gemini-cli + copilot-cli)
orchestrator = get_orchestrator(ORCHESTRATOR_CONFIG)

# Initialize rate limiter
rate_limiter = get_rate_limiter()

# Initialize user manager
user_manager = get_user_manager()

# Legacy alias for compatibility
GITHUB_REPO = get_default_github_repo()
_WORKFLOW_STATE_PLUGIN_KWARGS = {
    "storage_dir": NEXUS_CORE_STORAGE_DIR,
    "issue_to_workflow_id": StateManager.get_workflow_id_for_issue,
    "clear_pending_approval": StateManager.clear_pending_approval,
    "audit_log": AuditStore.audit_log,
}


def _get_direct_issue_plugin(repo: str):
    """Return GitHub issue plugin for direct Telegram operations."""
    return get_profiled_plugin(
        "github_telegram",
        overrides={
            "repo": repo,
        },
        cache_key=f"github:telegram:{repo}",
    )


# --- RATE LIMITING DECORATOR ---
def rate_limited(action: str, limit: RateLimit = None):
    """
    Decorator to add rate limiting to Telegram command handlers.
    
    Args:
        action: Rate limit action name (e.g., "logs", "stats", "implement")
        limit: Optional custom rate limit (uses default if not provided)
    
    Usage:
        @rate_limited("logs")
        async def logs_handler(update, context):
            ...
    """
    def decorator(func):
        async def wrapper(update: Update, context: ContextTypes.DEFAULT_TYPE):
            user_id = update.effective_user.id
            
            # Check rate limit
            allowed, error_msg = rate_limiter.check_limit(user_id, action, limit)
            
            if not allowed:
                # Rate limit exceeded
                await update.message.reply_text(error_msg)
                logger.warning(f"Rate limit blocked: user={user_id}, action={action}")
                return
            
            # Record the request
            rate_limiter.record_request(user_id, action)
            
            # Call the actual handler
            return await func(update, context)
        
        return wrapper
    return decorator


def load_tracked_issues():
    """Load tracked issues from file."""
    return StateManager.load_tracked_issues()


def save_tracked_issues(data):
    """Save tracked issues to file."""
    StateManager.save_tracked_issues(data)


def get_issue_details(issue_num, repo: str = None):
    """Query GitHub API for issue details."""
    try:
        repo = repo or GITHUB_REPO
        plugin = _get_direct_issue_plugin(repo)
        if not plugin:
            return None
        return plugin.get_issue(
            str(issue_num),
            ["number", "title", "state", "labels", "body", "updatedAt"],
        )
    except Exception as e:
        logger.error(f"Failed to fetch issue {issue_num}: {e}")
        return None


def find_task_logs(task_file):
    """Find task log files for the task file's project."""
    if not task_file:
        return []

    try:
        if "/.nexus/" in task_file:
            project_root = task_file.split("/.nexus/")[0]
        else:
            project_root = os.path.dirname(os.path.dirname(os.path.dirname(task_file)))

        project_key = _extract_project_from_nexus_path(task_file)
        logs_dir = get_tasks_logs_dir(project_root, project_key)
        if not os.path.isdir(logs_dir):
            return []

        pattern = os.path.join(logs_dir, "**", "*.log")
        return glob.glob(pattern, recursive=True)
    except Exception as e:
        logger.warning(f"Failed to list task logs: {e}")
        return []


def read_log_matches(log_path, issue_num, issue_url=None, max_lines=20):
    """Return lines from a log file that reference an issue."""
    if not log_path or not os.path.exists(log_path):
        return []

    matches = []
    needle = f"#{issue_num}"
    try:
        with open(log_path, "r") as f:
            for line in f:
                if needle in line or (issue_url and issue_url in line):
                    matches.append(line.rstrip())
    except Exception as e:
        logger.warning(f"Failed to read log file {log_path}: {e}")
        return []

    return matches[-max_lines:] if max_lines else matches


def search_logs_for_issue(issue_num):
    """Search bot/processor logs for an issue number."""
    log_paths = []
    if TELEGRAM_BOT_LOG_FILE:
        log_paths.append(TELEGRAM_BOT_LOG_FILE)
    if LOGS_DIR and os.path.isdir(LOGS_DIR):
        log_paths.extend(
            os.path.join(LOGS_DIR, f)
            for f in os.listdir(LOGS_DIR)
            if f.endswith(".log")
        )

    seen = set()
    results = []
    for path in log_paths:
        if path in seen:
            continue
        seen.add(path)
        results.extend(read_log_matches(path, issue_num, max_lines=10))
    return results


def read_latest_log_tail(task_file, max_lines=20):
    """Return tail of the newest task log file, if present."""
    log_files = find_task_logs(task_file)
    if not log_files:
        return []
    log_files.sort(key=lambda p: os.path.getmtime(p), reverse=True)
    latest = log_files[0]
    try:
        with open(latest, "r") as f:
            lines = f.readlines()
        return [f"[{os.path.basename(latest)}] {line.rstrip()}" for line in lines[-max_lines:]]
    except Exception as e:
        logger.warning(f"Failed to read latest log file {latest}: {e}")
        return []


def find_issue_log_files(issue_num, task_file=None):
    """Find task log files that match the issue number."""
    matches = []

    # If task file is known, search its project logs dir first
    if task_file:
        if "/.nexus/" in task_file:
            project_root = task_file.split("/.nexus/")[0]
        else:
            project_root = os.path.dirname(os.path.dirname(os.path.dirname(task_file)))
        project_key = _extract_project_from_nexus_path(task_file)
        logs_dir = get_tasks_logs_dir(project_root, project_key)
        if os.path.isdir(logs_dir):
            pattern = os.path.join(logs_dir, "**", f"*_{issue_num}_*.log")
            matches.extend(glob.glob(pattern, recursive=True))

    if matches:
        return matches

    # Fallback: scan all logs dirs
    nexus_dir_name = get_nexus_dir_name()
    pattern = os.path.join(
        BASE_DIR,
        "**",
        nexus_dir_name,
        "tasks",
        "*",
        "logs",
        "**",
        f"*_{issue_num}_*.log"
    )
    return glob.glob(pattern, recursive=True)


def read_latest_log_full(task_file):
    """Return full contents of the newest task log file, if present."""
    log_files = find_task_logs(task_file)
    if not log_files:
        return []
    log_files.sort(key=lambda p: os.path.getmtime(p), reverse=True)
    latest = log_files[0]
    try:
        with open(latest, "r") as f:
            lines = f.readlines()
        return [f"[{os.path.basename(latest)}] {line.rstrip()}" for line in lines]
    except Exception as e:
        logger.warning(f"Failed to read latest log file {latest}: {e}")
        return []


def resolve_project_config_from_task(task_file):
    """Resolve project config based on task file path."""
    if not task_file:
        return None, None

    task_path = os.path.abspath(task_file)

    # If task is inside a workspace repo (.nexus/...), derive project root
    if "/.nexus/" in task_path:
        project_root = task_path.split("/.nexus/")[0]
        # Match by configured workspace path instead of basename
        for key, cfg in PROJECT_CONFIG.items():
            if not isinstance(cfg, dict):
                continue
            workspace = cfg.get("workspace")
            if not workspace:
                continue
            workspace_abs = os.path.abspath(os.path.join(BASE_DIR, workspace))
            if project_root == workspace_abs or project_root.startswith(workspace_abs + os.sep):
                return key, cfg

    # If task is inside an agents repo, map by agents_dir
    for key, cfg in PROJECT_CONFIG.items():
        # Skip non-project config entries (global settings)
        if not isinstance(cfg, dict):
            continue
        
        agents_dir = cfg.get("agents_dir")
        if not agents_dir:
            continue
        agents_abs = os.path.abspath(os.path.join(BASE_DIR, agents_dir))
        if task_path.startswith(agents_abs + os.sep):
            return key, cfg

    return None, None


def find_task_file_by_issue(issue_num):
    """Search for a task file that references the issue number."""
    nexus_dir_name = get_nexus_dir_name()
    patterns = [
        os.path.join(BASE_DIR, "**", nexus_dir_name, "tasks", "*", "active", "*.md"),
        os.path.join(BASE_DIR, "**", nexus_dir_name, "inbox", "*", "*.md"),
    ]
    for pattern in patterns:
        for path in glob.glob(pattern, recursive=True):
            try:
                with open(path, "r") as f:
                    content = f.read()
                if re.search(
                    r"\*\*Issue:\*\*\s*https?://github.com/.+/issues/" + re.escape(issue_num),
                    content,
                ):
                    return path
            except Exception:
                continue
    return None


# --- DATA ---
PROJECTS = {
    "case_italia": "Case Italia",
    "wallible": "Wallible",
    "biome": "Biome",
    "nexus": "Nexus Core"
}
TYPES = {
    "feature": "✨ Feature (full)",
    "feature-simple": "✨ Simple Feature (fast-track)",
    "bug": "🩹 Bug Fix (shortened)",
    "hotfix": "🔥 Hotfix (fast-track)",
    "release": "📦 Release (full)",
    "chore": "🧹 Chore (fast-track)",
    "improvement": "🚀 Improvement (full)",
    "improvement-simple": "🚀 Simple Improvement (fast-track)"
}

PROJECT_ALIASES = {
    "casit": "case_italia",
    "wlbl": "wallible",
    "bm": "biome",
    "nexus": "nexus",
}


def _normalize_project_key(project: str) -> Optional[str]:
    if not project:
        return None
    project_key = project.lower()
    return PROJECT_ALIASES.get(project_key, project_key)


def _iter_project_keys() -> List[str]:
    keys = []
    for key, cfg in PROJECT_CONFIG.items():
        if isinstance(cfg, dict) and cfg.get("github_repo"):
            keys.append(key)
    return keys


def _get_project_label(project_key: str) -> str:
    return PROJECTS.get(project_key, project_key)


def _get_project_root(project_key: str) -> Optional[str]:
    cfg = PROJECT_CONFIG.get(project_key)
    if not isinstance(cfg, dict):
        return None
    workspace = cfg.get("workspace")
    if not workspace:
        return None
    return os.path.join(BASE_DIR, workspace)


def _get_project_logs_dir(project_key: str) -> Optional[str]:
    project_root = _get_project_root(project_key)
    if not project_root:
        return None
    logs_dir = get_tasks_logs_dir(project_root, project_key)
    return logs_dir if os.path.isdir(logs_dir) else None


def _extract_project_from_nexus_path(path: str) -> Optional[str]:
    if not path or "/.nexus/" not in path:
        return None

    normalized = path.replace("\\", "/")
    match = re.search(r"/\.nexus/(?:tasks|inbox)/([^/]+)/", normalized)
    if not match:
        return None

    project_key = _normalize_project_key(match.group(1))
    if project_key and project_key in _iter_project_keys():
        return project_key
    return None


async def _prompt_monitor_project_selection(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    command: str,
) -> None:
    keyboard = [[InlineKeyboardButton("All Projects", callback_data=f"pickmonitor:{command}:all")]]
    keyboard.extend(
        [InlineKeyboardButton(_get_project_label(key), callback_data=f"pickmonitor:{command}:{key}")]
        for key in _iter_project_keys()
    )
    keyboard.append([InlineKeyboardButton("❌ Close", callback_data="flow:close")])

    text = f"Select a project for /{command}:"
    if update.callback_query:
        await update.callback_query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(keyboard))
    else:
        await update.effective_message.reply_text(text, reply_markup=InlineKeyboardMarkup(keyboard))


def _list_project_issues(project_key: str, state: str = "open", limit: int = 10) -> List[dict]:
    """Fetch recent issues from a project's GitHub repo.

    Returns a list of dicts with 'number', 'title', and 'state' keys.
    """
    config = PROJECT_CONFIG.get(project_key, {})
    if not isinstance(config, dict):
        return []
    repo = config.get("github_repo")
    if not repo:
        return []
    try:
        plugin = _get_direct_issue_plugin(repo)
        if not plugin:
            return []
        return plugin.list_issues(state=state, limit=limit, fields=["number", "title", "state"])
    except Exception as e:
        logger.error(f"Failed to list issues for {project_key}: {e}")
        return []


async def _prompt_issue_selection(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    command: str,
    project_key: str,
    *,
    edit_message: bool = False,
    issue_state: str = "open",
) -> None:
    """Show a list of issues for the user to pick from."""
    issues = _list_project_issues(project_key, state=issue_state)
    state_label = "open" if issue_state == "open" else "closed"

    if not issues:
        # No issues in current state — still offer toggle + manual entry
        keyboard = []
        if issue_state == "open":
            keyboard.append([InlineKeyboardButton(
                "📦 Closed issues",
                callback_data=f"pickissue_state:closed:{command}:{project_key}",
            )])
        else:
            keyboard.append([InlineKeyboardButton(
                "🔓 Open issues",
                callback_data=f"pickissue_state:open:{command}:{project_key}",
            )])
        keyboard.append([InlineKeyboardButton("✏️ Enter manually", callback_data=f"pickissue_manual:{command}:{project_key}")])
        keyboard.append([InlineKeyboardButton("❌ Close", callback_data="flow:close")])

        text = (
            f"No {state_label} issues found for {_get_project_label(project_key)}."
        )
        if edit_message and update.callback_query:
            await update.callback_query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(keyboard))
        else:
            await update.effective_message.reply_text(text, reply_markup=InlineKeyboardMarkup(keyboard))
        return

    keyboard = []
    for issue in issues:
        num = issue["number"]
        title = issue["title"]
        label = f"#{num} — {title}"
        if len(label) > 60:
            label = label[:57] + "..."
        keyboard.append(
            [InlineKeyboardButton(label, callback_data=f"pickissue:{command}:{project_key}:{num}")]
        )

    # Toggle button: show closed when viewing open, and vice versa
    if issue_state == "open":
        keyboard.append([InlineKeyboardButton(
            "📦 Closed issues",
            callback_data=f"pickissue_state:closed:{command}:{project_key}",
        )])
    else:
        keyboard.append([InlineKeyboardButton(
            "🔓 Open issues",
            callback_data=f"pickissue_state:open:{command}:{project_key}",
        )])

    keyboard.append([InlineKeyboardButton("✏️ Enter manually", callback_data=f"pickissue_manual:{command}:{project_key}")])
    keyboard.append([InlineKeyboardButton("❌ Close", callback_data="flow:close")])

    emoji = "📋" if issue_state == "open" else "📦"
    text = f"{emoji} {state_label.capitalize()} issues for /{command} ({_get_project_label(project_key)}):"
    if edit_message and update.callback_query:
        await update.callback_query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(keyboard))
    else:
        await update.effective_message.reply_text(text, reply_markup=InlineKeyboardMarkup(keyboard))


async def _prompt_project_selection(update: Update, context: ContextTypes.DEFAULT_TYPE, command: str) -> None:
    keyboard = [
        [InlineKeyboardButton(_get_project_label(key), callback_data=f"pickcmd:{command}:{key}")]
        for key in _iter_project_keys()
    ]
    keyboard.append([InlineKeyboardButton("❌ Close", callback_data="flow:close")])
    await update.effective_message.reply_text(
        f"Select a project for /{command}:",
        reply_markup=InlineKeyboardMarkup(keyboard)
    )
    context.user_data["pending_command"] = command


def _parse_project_issue_args(args: List[str]) -> Tuple[Optional[str], Optional[str], List[str]]:
    if len(args) < 2:
        return None, None, []
    project_key = _normalize_project_key(args[0])
    issue_num = args[1].lstrip("#")
    rest = args[2:]
    return project_key, issue_num, rest


async def _ensure_project_issue(update: Update, context: ContextTypes.DEFAULT_TYPE, command: str) -> Tuple[Optional[str], Optional[str], List[str]]:
    project_key, issue_num, rest = _parse_project_issue_args(context.args)
    if not project_key or not issue_num:
        if len(context.args) == 1:
            arg = context.args[0]
            maybe_issue = arg.lstrip("#")
            if maybe_issue.isdigit():
                # Just an issue number — still need project selection
                context.user_data["pending_issue"] = maybe_issue
                await _prompt_project_selection(update, context, command)
            else:
                # Might be a project key — show issue list for that project
                normalized = _normalize_project_key(arg)
                if normalized and normalized in _iter_project_keys():
                    context.user_data["pending_command"] = command
                    context.user_data["pending_project"] = normalized
                    await _prompt_issue_selection(update, context, command, normalized)
                else:
                    await _prompt_project_selection(update, context, command)
        else:
            await _prompt_project_selection(update, context, command)
        return None, None, []
    if project_key not in _iter_project_keys():
        await update.effective_message.reply_text(
            f"❌ Unknown project '{project_key}'."
        )
        return None, None, []
    if not issue_num.isdigit():
        await update.effective_message.reply_text("❌ Invalid issue number.")
        return None, None, []
    return project_key, issue_num, rest


async def _handle_pending_issue_input(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    pending_command = context.user_data.get("pending_command")
    pending_project = context.user_data.get("pending_project")
    pending_issue = context.user_data.get("pending_issue")
    if not pending_command or not pending_project:
        return False

    text = (update.message.text or "").strip()
    if pending_issue is None:
        issue_num = text.lstrip("#")
        if not issue_num.isdigit():
            await update.effective_message.reply_text("Please enter a valid issue number (e.g., 1).")
            return True
        context.user_data["pending_issue"] = issue_num
        if pending_command == "respond":
            await update.effective_message.reply_text("Now send the response message for this issue.")
            return True
    else:
        issue_num = pending_issue

    project_key = pending_project
    rest = []
    if pending_command == "respond":
        rest = [text]

    context.user_data.pop("pending_command", None)
    context.user_data.pop("pending_project", None)
    context.user_data.pop("pending_issue", None)

    await _dispatch_command(update, context, pending_command, project_key, issue_num, rest)
    return True


def _command_handler_map():
    return {
        "logs": logs_handler,
        "logsfull": logsfull_handler,
        "tail": tail_handler,
        "audit": audit_handler,
        "comments": comments_handler,
        "reprocess": reprocess_handler,
        "continue": continue_handler,
        "respond": respond_handler,
        "kill": kill_handler,
        "assign": assign_handler,
        "implement": implement_handler,
        "prepare": prepare_handler,
        "pause": pause_handler,
        "resume": resume_handler,
        "stop": stop_handler,
        "track": track_handler,
        "untrack": untrack_handler,
    }


async def _dispatch_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    command: str,
    project_key: str,
    issue_num: str,
    rest: Optional[List[str]] = None,
) -> None:
    context.args = [project_key, issue_num] + (rest or [])
    handler = _command_handler_map().get(command)
    if handler:
        await handler(update, context)
    else:
        await update.effective_message.reply_text("Unsupported command.")


async def project_picker_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    if not query.data or not query.data.startswith("pickcmd:"):
        return

    _, command, project_key = query.data.split(":", 2)
    context.user_data["pending_command"] = command
    context.user_data["pending_project"] = project_key

    pending_issue = context.user_data.get("pending_issue")
    if pending_issue and command != "respond":
        context.user_data.pop("pending_issue", None)
        await _dispatch_command(update, context, command, project_key, pending_issue)
        return

    if pending_issue and command == "respond":
        await query.edit_message_text(
            f"Selected {_get_project_label(project_key)}. Now send the response message."
        )
        return

    # Show issue list instead of asking for raw text input
    await _prompt_issue_selection(update, context, command, project_key, edit_message=True)


async def issue_picker_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle issue selection from the inline keyboard."""
    query = update.callback_query
    await query.answer()

    if not query.data:
        return

    if query.data.startswith("pickissue_manual:"):
        # User wants to enter issue number manually
        _, command, project_key = query.data.split(":", 2)
        context.user_data["pending_command"] = command
        context.user_data["pending_project"] = project_key
        await query.edit_message_text(
            f"Selected {_get_project_label(project_key)}. Send the issue number."
        )
        return

    if query.data.startswith("pickissue_state:"):
        # Toggle between open/closed issue lists
        _, issue_state, command, project_key = query.data.split(":", 3)
        await _prompt_issue_selection(
            update, context, command, project_key,
            edit_message=True, issue_state=issue_state,
        )
        return

    if not query.data.startswith("pickissue:"):
        return

    _, command, project_key, issue_num = query.data.split(":", 3)
    await query.edit_message_reply_markup(reply_markup=None)
    await _dispatch_command(update, context, command, project_key, issue_num)


async def monitor_project_picker_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle project selection for non-issue monitoring commands."""
    query = update.callback_query
    await query.answer()

    if not query.data or not query.data.startswith("pickmonitor:"):
        return

    _, command, project_key = query.data.split(":", 2)
    context.args = [project_key]

    if command == "status":
        await status_handler(update, context)
        return
    if command == "active":
        await active_handler(update, context)
        return

    await query.edit_message_text("Unsupported monitoring command.")


async def close_flow_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    await query.edit_message_reply_markup(reply_markup=None)

# --- STATES ---
SELECT_PROJECT, SELECT_TYPE, INPUT_TASK = range(3)

tracked_issues = load_tracked_issues()  # Load on startup


# --- 0. HELP & INFO ---
async def help_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Lists available commands and usage info."""
    logger.info(f"Help triggered by user: {update.effective_user.id}")
    if ALLOWED_USER_ID and update.effective_user.id != ALLOWED_USER_ID:
        logger.warning(f"Unauthorized access attempt by ID: {update.effective_user.id}")
        return

    help_text = (
        "🤖 **Nexus Commands**\n\n"
        "Use /menu for a categorized, button-driven view.\n\n"
        "✨ **Task Creation:**\n"
        "/new - Start a menu-driven task creation\n"
        "/cancel - Abort the current guided process\n\n"
        "⚡ **Hands-Free Mode:**\n"
        "Send a **Voice Note** or **Text Message** directly. "
        "The bot will transcribe, route, and save the task.\n\n"
        "📋 **Workflow Tiers:**\n"
        "• 🔥 Hotfix/Chore → fast-track (triage → implement → verify → deploy)\n"
        "• 🩹 Bug → shortened (triage → debug → fix → verify → deploy → close)\n"
        "• ✨ Feature → full (triage → design → develop → review → compliance → deploy → close)\n"
        "• ✨ Simple Feature → fast-track (skip design)\n\n"
        "📊 **Monitoring & Tracking:**\n"
        "/status [project|all] - View pending tasks in inbox\n"
        "/active [project|all] [cleanup] - View tasks currently being worked on\n"
        "/track <project> <issue#> - Track issue per-project\n"
        "/untrack <project> <issue#> - Stop tracking per-project\n"
        "/myissues - View all your tracked issues\n"
        "/logs <project> <issue#> - View task logs\n"
        "/logsfull <project> <issue#> - Full log lines (no truncation)\n"
        "/tail <project> <issue#> [lines] - Tail recent log lines\n"
        "/audit <project> <issue#> - View workflow audit trail\n"
        "/stats [days] - View system analytics (default: 30 days)\n"
        "/comments <project> <issue#> - View issue comments\n\n"
        "🔁 **Recovery & Control:**\n"
        "/reprocess <project> <issue#> - Re-run agent processing\n"
        "/continue <project> <issue#> - Check stuck agent status\n"
        "/kill <project> <issue#> - Stop running agent process\n"
        "/pause <project> <issue#> - Pause auto-chaining (agents work but no auto-launch)\n"
        "/resume <project> <issue#> - Resume auto-chaining\n"
        "/stop <project> <issue#> - Stop workflow completely (closes issue, kills agent)\n"
        "/respond <project> <issue#> <text> - Respond to agent questions\n\n"
        "🤝 **Agent Management:**\n"
        "/agents <project> - List all agents for a project\n"
        "/direct <project> <@agent> <message> - Send direct request to an agent\n\n"
        "🔧 **GitHub Management:**\n"
        "/assign <project> <issue#> - Assign GitHub issue to yourself\n"
        "/implement <project> <issue#> - Request Copilot agent implementation\n"
        "/prepare <project> <issue#> - Add Copilot-friendly instructions\n\n"
        "ℹ️ /help - Show this list"
    )
    await update.message.reply_text(help_text, parse_mode='Markdown')


def build_menu_keyboard(button_rows, include_back=True):
    """Build a menu keyboard with optional back button."""
    keyboard = button_rows[:]
    if include_back:
        keyboard.append([InlineKeyboardButton("⬅️ Back", callback_data="menu:root")])
    keyboard.append([InlineKeyboardButton("❌ Close", callback_data="menu:close")])
    return InlineKeyboardMarkup(keyboard)


async def menu_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Show the main menu with submenus."""
    if ALLOWED_USER_ID and update.effective_user.id != ALLOWED_USER_ID:
        logger.warning(f"Unauthorized access attempt by ID: {update.effective_user.id}")
        return

    keyboard = [
        [InlineKeyboardButton("✨ Task Creation", callback_data="menu:tasks")],
        [InlineKeyboardButton("📊 Monitoring", callback_data="menu:monitor")],
        [InlineKeyboardButton("🔁 Workflow Control", callback_data="menu:workflow")],
        [InlineKeyboardButton("🤝 Agents", callback_data="menu:agents")],
        [InlineKeyboardButton("🔧 GitHub", callback_data="menu:github")],
        [InlineKeyboardButton("ℹ️ Help", callback_data="menu:help")],
        [InlineKeyboardButton("❌ Close", callback_data="menu:close")]
    ]
    await update.effective_message.reply_text(
        "📍 **Nexus Menu**\nChoose a category:",
        reply_markup=InlineKeyboardMarkup(keyboard),
        parse_mode='Markdown'
    )


async def menu_callback_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle menu navigation callbacks."""
    query = update.callback_query
    await query.answer()

    if not query.data:
        return

    menu_key = query.data.split(":", 1)[1]

    if menu_key == "close":
        await query.edit_message_reply_markup(reply_markup=None)
        return

    if menu_key == "root":
        keyboard = [
            [InlineKeyboardButton("✨ Task Creation", callback_data="menu:tasks")],
            [InlineKeyboardButton("📊 Monitoring", callback_data="menu:monitor")],
            [InlineKeyboardButton("🔁 Workflow Control", callback_data="menu:workflow")],
            [InlineKeyboardButton("🤝 Agents", callback_data="menu:agents")],
            [InlineKeyboardButton("🔧 GitHub", callback_data="menu:github")],
            [InlineKeyboardButton("ℹ️ Help", callback_data="menu:help")],
            [InlineKeyboardButton("❌ Close", callback_data="menu:close")]
        ]
        await query.edit_message_text(
            "📍 **Nexus Menu**\nChoose a category:",
            reply_markup=InlineKeyboardMarkup(keyboard),
            parse_mode='Markdown'
        )
        return

    menu_texts = {
        "tasks": (
            "✨ **Task Creation**\n"
            "- /new — Start task creation\n"
            "- /cancel — Abort the current guided process\n\n"
            "Tip: send a voice note or text to auto-create a task."
        ),
        "monitor": (
            "📊 **Monitoring**\n"
            "- /status — View pending tasks in inbox\n"
            "- /active — View tasks currently being worked on\n"
            "- /myissues — View your tracked issues\n"
            "- /logs <project> <issue#> — View task logs\n"
            "- /logsfull <project> <issue#> — Full log lines (no truncation)\n"
            "- /tail <project> <issue#> [lines] — Tail recent logs\n"
            "- /audit <project> <issue#> — View workflow audit trail\n"
            "- /stats [days] — View system analytics (default: 30 days)\n"
            "- /comments <project> <issue#> — View issue comments\n"
            "- /track <project> <issue#> — Subscribe to updates\n"
            "- /untrack <project> <issue#> — Stop tracking"
        ),
        "workflow": (
            "🔁 **Workflow Control**\n"
            "- /reprocess <project> <issue#> — Re-run agent processing\n"
            "- /continue <project> <issue#> — Resume a stuck agent\n"
            "- /kill <project> <issue#> — Stop a running agent\n"
            "- /pause <project> <issue#> — Pause auto-chaining\n"
            "- /resume <project> <issue#> — Resume auto-chaining\n"
            "- /stop <project> <issue#> — Stop workflow completely\n"
            "- /respond <project> <issue#> <text> — Respond to agent questions"
        ),
        "agents": (
            "🤝 **Agents**\n"
            "- /agents <project> — List agents for a project\n"
            "- /direct <project> <@agent> <message> — Send direct request"
        ),
        "github": (
            "🔧 **GitHub**\n"
            "- /assign <project> <issue#> — Assign issue to yourself\n"
            "- /implement <project> <issue#> — Request Copilot implementation\n"
            "- /prepare <project> <issue#> — Add Copilot-friendly instructions"
        ),
        "help": "ℹ️ Use /help for the full command list."
    }

    text = menu_texts.get(menu_key, "Unknown menu option.")
    await query.edit_message_text(
        text,
        reply_markup=build_menu_keyboard([]),
        parse_mode='Markdown'
    )


async def start_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Welcome message and persistent reply keyboard."""
    logger.info(f"Start triggered by user: {update.effective_user.id}")
    if ALLOWED_USER_ID and update.effective_user.id != ALLOWED_USER_ID:
        logger.warning(f"Unauthorized access attempt by ID: {update.effective_user.id}")
        return

    welcome = (
        "👋 Welcome to Nexus!\n\n"
        "Use the menu buttons to create tasks or monitor queues.\n"
        "Send voice or text to create a task automatically.\n\n"
        "💡 **Workflow Tiers:**\n"
        "• 🔥 Hotfix/Chore/Simple Feature → 4 steps (fast)\n"
        "• 🩹 Bug → 6 steps (moderate)\n"
        "• ✨ Feature/Improvement → 9 steps (full)\n\n"
        "Type /help for all commands."
    )

    keyboard = [
        ["/new"],
        ["/menu"],
        ["/status"],
        ["/active"],
        ["/help"]
    ]
    reply_markup = ReplyKeyboardMarkup(keyboard, resize_keyboard=True, one_time_keyboard=False)

    await update.message.reply_text(welcome, reply_markup=reply_markup)


async def on_startup(application):
    """Register bot commands so they appear in the Telegram client menu."""
    cmds = [
        BotCommand("new", "Start task creation"),
        BotCommand("menu", "Open command menu"),
        # BotCommand("cancel", "Cancel current process"),
        BotCommand("status", "Show pending tasks"),
        BotCommand("active", "Show active tasks"),
        BotCommand("progress", "Show agent progress details"),
        # BotCommand("track", "Subscribe to issue updates"),
        # BotCommand("untrack", "Stop tracking an issue"),
        # BotCommand("myissues", "View your tracked issues"),
        # BotCommand("logs", "View task execution logs"),
        # BotCommand("logsfull", "Full issue logs"),
        # BotCommand("audit", "View workflow audit trail"),
        # BotCommand("stats", "View system analytics"),
        # BotCommand("comments", "View issue comments"),
        # BotCommand("reprocess", "Re-run agent processing"),
        # BotCommand("continue", "Check stuck agent status"),
        # BotCommand("kill", "Stop running agent"),
        # BotCommand("pause", "Pause auto-chaining"),
        # BotCommand("resume", "Resume auto-chaining"),
        # BotCommand("stop", "Stop workflow completely"),
        # BotCommand("agents", "List project agents"),
        # BotCommand("direct", "Send direct agent request"),
        # BotCommand("respond", "Respond to agent questions"),
        # BotCommand("assign", "Assign an issue"),
        # BotCommand("implement", "Request implementation"),
        # BotCommand("prepare", "Prepare for Copilot"),
        BotCommand("help", "Show help")
    ]
    try:
        await application.bot.set_my_commands(cmds)
        logger.info("Registered bot commands for Telegram client menu")
    except Exception:
        logger.exception("Failed to set bot commands on startup")

    # Tool availability health check
    await _check_tool_health(application)


async def _check_tool_health(application):
    """Probe Copilot and Gemini availability and broadcast alerts on failure."""
    tools_to_check = [AIProvider.COPILOT, AIProvider.GEMINI]
    unavailable = []
    for tool in tools_to_check:
        try:
            available = orchestrator.check_tool_available(tool)
            if not available:
                unavailable.append(tool.value)
        except Exception as exc:
            logger.warning(f"Health check error for {tool.value}: {exc}")
            unavailable.append(tool.value)

    if unavailable:
        alert = (
            f"⚠️ *Nexus Startup Alert*\n"
            f"The following AI tools are unavailable: `{', '.join(unavailable)}`\n"
            f"Agents using these tools will fail until they recover."
        )
        logger.warning(f"Tool health check failed: {unavailable}")
        if TELEGRAM_CHAT_ID:
            try:
                await application.bot.send_message(
                    chat_id=TELEGRAM_CHAT_ID,
                    text=alert,
                    parse_mode="Markdown",
                )
            except Exception as exc:
                logger.warning(f"Failed to send health alert to Telegram: {exc}")
    else:
        logger.info("✅ Tool health check passed: Copilot and Gemini are available")


# --- HELPER: AUDIO TRANSCRIPTION (via Orchestrator CLI) ---
async def process_audio_with_gemini(voice_file_id, context):
    """Downloads Telegram audio and transcribes using orchestrator (CLI only)."""
    # 1. Download audio to temp file
    new_file = await context.bot.get_file(voice_file_id)
    await new_file.download_to_drive("temp_voice.ogg")

    # 2. Transcribe using orchestrator (gemini-cli + copilot-cli fallback)
    logger.info("🎧 Transcribing audio with orchestrator...")
    text = orchestrator.transcribe_audio_cli("temp_voice.ogg")

    # 3. Cleanup
    if os.path.exists("temp_voice.ogg"):
        os.remove("temp_voice.ogg")

    if text:
        logger.info(f"✅ Transcription successful ({len(text)} chars)")
        return text.strip()
    else:
        logger.error("❌ Transcription failed")
        return None


# --- 1. HANDS-FREE MODE (Auto-Router) ---
async def hands_free_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        logger.info(
            "Hands-free task received: user=%s message_id=%s has_voice=%s has_text=%s",
            update.effective_user.id,
            update.message.message_id if update.message else None,
            bool(update.message and update.message.voice),
            bool(update.message and update.message.text),
        )
        if ALLOWED_USER_ID and update.effective_user.id != ALLOWED_USER_ID:
            logger.warning(f"Unauthorized access attempt by ID: {update.effective_user.id}")
            return

        if await _handle_pending_issue_input(update, context):
            return

        # Guard: Don't process commands as tasks
        if update.message.text and update.message.text.startswith('/'):
            logger.info(f"Ignoring command in hands_free_handler: {update.message.text}")
            return

        text = ""
        status_msg = await update.message.reply_text("⚡ AI Listening...")

        # A. Handle Audio
        if update.message.voice:
            logger.info("Processing voice message...")
            text = await process_audio_with_gemini(update.message.voice.file_id, context)
            if not text:
                logger.warning("Voice transcription returned empty text")
                await context.bot.edit_message_text(
                    chat_id=update.effective_chat.id,
                    message_id=status_msg.message_id,
                    text="⚠️ Transcription failed"
                )
                return

            logger.info("Running analysis on transcribed text...")
            result = orchestrator.run_text_to_speech_analysis(
                text=text,
                task="classify",
                projects=list(PROJECTS.keys()),
                types=list(TYPES.keys())
            )

        # B. Handle Text
        else:
            logger.info(f"Processing text for auto-routing... text={update.message.text[:50]}")
            text = update.message.text
            result = orchestrator.run_text_to_speech_analysis(
                text=text,
                task="classify",
                projects=list(PROJECTS.keys()),
                types=list(TYPES.keys())
            )

        logger.info(f"Analysis result: {result}")

        # Parse Result
        try:
            if isinstance(result, dict) and result.get("parse_error") and result.get("text"):
                result = json.loads(result["text"].replace("```json", "").replace("```", ""))

            project = result.get("project")
            if not project or project not in PROJECTS:
                error_msg = f"❌ Could not classify project. Received: '{project}'\n\nPlease use /new command to manually select project."
                logger.error(f"Project classification failed: project={project}, valid={list(PROJECTS.keys())}")
                await context.bot.edit_message_text(
                    chat_id=update.effective_chat.id,
                    message_id=status_msg.message_id,
                    text=error_msg
                )
                return
            
            task_type = result.get("type", "feature")
            if task_type not in TYPES:
                logger.warning(f"Type '{task_type}' not in TYPES, defaulting to 'feature'")
                task_type = "feature"
            
            content = result.get("text", text or "")
            issue_name = result.get("issue_name", "")
            logger.info(f"Parsed: project={project}, type={task_type}, issue_name={issue_name}")
        except Exception as e:
            logger.error(f"JSON parsing error: {e}", exc_info=True)
            await context.bot.edit_message_text(
                chat_id=update.effective_chat.id,
                message_id=status_msg.message_id,
                text="⚠️ JSON Error"
            )
            return

        # Save to File
        logger.info(f"Getting inbox dir for project: {project}")
        
        # Map project name to workspace (e.g., "nexus" → "ghabs")
        workspace = project
        if project in PROJECT_CONFIG:
            workspace = PROJECT_CONFIG[project].get("workspace", project)
            logger.info(f"Mapped project '{project}' → workspace '{workspace}'")
        else:
            logger.warning(f"Project '{project}' not in PROJECT_CONFIG, using as-is for workspace")
        
        target_dir = get_inbox_dir(os.path.join(BASE_DIR, workspace), project)
        logger.info(f"Target inbox dir: {target_dir}")
        os.makedirs(target_dir, exist_ok=True)
        filename = f"voice_task_{update.message.message_id}.md"
        filepath = os.path.join(target_dir, filename)

        logger.info(f"Writing to file: {filepath}")
        with open(filepath, "w") as f:
            f.write(
                f"# {TYPES.get(task_type, 'Task')}\n**Project:** {PROJECTS.get(project, project)}\n**Type:** {task_type}\n**Issue Name:** {issue_name}\n**Status:** Pending\n\n{content}")

        logger.info(f"✅ File saved: {filepath}")
        await context.bot.edit_message_text(
            chat_id=update.effective_chat.id,
            message_id=status_msg.message_id,
            text=f"✅ Routed to `{project}`\n📝 *{content}*"
        )
    except Exception as e:
        logger.error(f"Unexpected error in hands_free_handler: {e}", exc_info=True)
        try:
            await update.message.reply_text(f"❌ Error: {str(e)[:100]}")
        except Exception:
            pass


# --- 2. SELECTION MODE (Menu) ---
# (Steps 1 & 2 are purely Telegram UI, no AI needed)

async def start_selection(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if ALLOWED_USER_ID and update.effective_user.id != ALLOWED_USER_ID: return
    keyboard = [[InlineKeyboardButton(name, callback_data=code)] for code, name in PROJECTS.items()]
    keyboard.append([InlineKeyboardButton("❌ Close", callback_data="flow:close")])
    await update.message.reply_text("📂 **Select Project:**", reply_markup=InlineKeyboardMarkup(keyboard),
                                    parse_mode='Markdown')
    return SELECT_PROJECT


async def project_selected(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    context.user_data['project'] = query.data
    keyboard = [[InlineKeyboardButton(name, callback_data=code)] for code, name in TYPES.items()]
    keyboard.append([InlineKeyboardButton("❌ Close", callback_data="flow:close")])
    await query.edit_message_text(f"📂 Project: **{PROJECTS[query.data]}**\n\n🛠 **Select Type:**",
                                  reply_markup=InlineKeyboardMarkup(keyboard), parse_mode='Markdown')
    return SELECT_TYPE


async def type_selected(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    context.user_data['type'] = query.data
    await query.edit_message_text(f"📝 **Speak or Type the task:**", parse_mode='Markdown')
    return INPUT_TASK


# --- 3. SAVING THE TASK (Uses Gemini only if Voice) ---
async def save_task(update: Update, context: ContextTypes.DEFAULT_TYPE):
    project = context.user_data['project']
    task_type = context.user_data['type']

    logger.info(
        "Selection task received: user=%s message_id=%s project=%s type=%s has_voice=%s",
        update.effective_user.id,
        update.message.message_id if update.message else None,
        project,
        task_type,
        bool(update.message and update.message.voice),
    )

    text = ""
    if update.message.voice:
        msg = await update.message.reply_text("🎧 Transcribing (CLI)...")
        # Re-use the helper function to just get text
        text = await process_audio_with_gemini(update.message.voice.file_id, context)
        await context.bot.delete_message(chat_id=update.effective_chat.id, message_id=msg.message_id)
    else:
        text = update.message.text

    if not text:
        await update.message.reply_text("⚠️ Transcription failed. Please try again.")
        return ConversationHandler.END

    # Refine description using orchestrator (Gemini CLI preferred)
    refined_text = text
    try:
        logger.info("Refining description with orchestrator (len=%s)", len(text))
        refine_result = orchestrator.run_text_to_speech_analysis(
            text=text,
            task="refine_description",
            project_name=PROJECTS.get(project)
        )
        candidate = refine_result.get("text", "").strip()
        if candidate:
            refined_text = candidate
    except Exception as e:
        logger.warning(f"Failed to refine description: {e}")

    # Generate issue name using orchestrator (CLI only)
    issue_name = ""
    try:
        logger.info("Generating issue name with orchestrator (len=%s)", len(refined_text))
        name_result = orchestrator.run_text_to_speech_analysis(
            text=refined_text[:300],
            task="generate_name",
            project_name=PROJECTS.get(project)
        )
        issue_name = name_result.get("text", "").strip().strip('"`\'')
    except Exception as e:
        logger.warning(f"Failed to generate issue name: {e}")
        issue_name = ""

    # Write File
    # Map project name to workspace (e.g., "nexus" → "ghabs")
    workspace = project
    if project in PROJECT_CONFIG:
        workspace = PROJECT_CONFIG[project].get("workspace", project)
    
    target_dir = get_inbox_dir(os.path.join(BASE_DIR, workspace), project)
    os.makedirs(target_dir, exist_ok=True)
    filename = f"{task_type}_{update.message.message_id}.md"

    with open(os.path.join(target_dir, filename), "w") as f:
        issue_name_line = f"**Issue Name:** {issue_name}\n" if issue_name else ""
        f.write(
            f"# {TYPES[task_type]}\n**Project:** {PROJECTS[project]}\n**Type:** {task_type}\n"
            f"{issue_name_line}**Status:** Pending\n\n"
            f"{refined_text}\n\n"
            f"---\n"
            f"**Raw Input:**\n{text}"
        )

    await update.message.reply_text(f"✅ Saved to `{project}`.")
    return ConversationHandler.END


async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("❌ Cancelled.")
    return ConversationHandler.END


async def flow_close_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle Close button for the /new flow."""
    query = update.callback_query
    await query.answer()
    await query.edit_message_text("❌ Cancelled.")
    return ConversationHandler.END


# --- MONITORING COMMANDS ---
def extract_issue_number_from_file(file_path):
    """Extract issue number from task file content if present."""
    try:
        with open(file_path, "r") as f:
            content = f.read()
        match = re.search(r"\*\*Issue:\*\*\s*https?://github.com/[^/]+/[^/]+/issues/(\d+)", content)
        if match:
            return match.group(1)
    except Exception as e:
        logger.warning(f"Failed to read issue number from {file_path}: {e}")
    return None


async def status_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Shows pending tasks in inbox folders."""
    logger.info(f"Status triggered by user: {update.effective_user.id}")
    if ALLOWED_USER_ID and update.effective_user.id != ALLOWED_USER_ID:
        logger.warning(f"Unauthorized access attempt by ID: {update.effective_user.id}")
        return

    project_filter = None
    if context.args:
        raw = context.args[0].strip().lower()
        if raw != "all":
            project_filter = _normalize_project_key(raw)
            if project_filter not in _iter_project_keys():
                await update.effective_message.reply_text(f"❌ Unknown project '{raw}'.")
                return
    else:
        await _prompt_monitor_project_selection(update, context, "status")
        return

    selected_projects = [project_filter] if project_filter else _iter_project_keys()

    status_text = "📥 Inbox Status (Pending Tasks)\n\n"
    total_tasks = 0

    for project_key in selected_projects:
        project_name = _get_project_label(project_key)
        project_root = _get_project_root(project_key)
        if not project_root:
            continue
        inbox_dir = get_inbox_dir(project_root, project_key)
        if os.path.exists(inbox_dir):
            files = [f for f in os.listdir(inbox_dir) if f.endswith(".md")]
            if files:
                repo = PROJECT_CONFIG[project_key].get("github_repo", GITHUB_REPO)
                status_text += f"{project_name}: {len(files)} task(s)\n"
                total_tasks += len(files)
                # Show first 3 files as preview
                for f in files[:3]:
                    task_type = f.split('_')[0]
                    emoji = TYPES.get(task_type, "📝")
                    file_path = os.path.join(inbox_dir, f)
                    issue_number = extract_issue_number_from_file(file_path)
                    if issue_number:
                        issue_link = f"https://github.com/{repo}/issues/{issue_number}"
                        issue_suffix = f" [#{issue_number}]({issue_link})"
                    else:
                        issue_suffix = " (issue ?)"
                    status_text += f"  • {emoji} `{f}`{issue_suffix}\n"
                if len(files) > 3:
                    status_text += f"  ... +{len(files) - 3} more\n"
                status_text += "\n"

    if total_tasks == 0:
        status_text += "✨ No pending tasks in inbox!\n"
    else:
        status_text += f"Total: {total_tasks} pending task(s)"

    await update.effective_message.reply_text(
        status_text,
        parse_mode='Markdown',
        disable_web_page_preview=True,
    )


async def progress_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Show active issues with current workflow step, agent type, tool, and duration."""
    logger.info(f"Progress requested by user: {update.effective_user.id}")
    if ALLOWED_USER_ID and update.effective_user.id != ALLOWED_USER_ID:
        logger.warning(f"Unauthorized access attempt by ID: {update.effective_user.id}")
        return

    launched_agents = StateManager.load_launched_agents()
    if not launched_agents:
        await update.effective_message.reply_text("ℹ️ No active agents tracked.")
        return

    now = time.time()
    lines = ["📊 *Agent Progress*\n"]
    for issue_num, info in sorted(launched_agents.items(), key=lambda x: x[0]):
        if not isinstance(info, dict):
            continue
        agent_type = info.get("agent_type", "unknown")
        tool = info.get("tool", "unknown")
        tier = info.get("tier", "unknown")
        ts = info.get("timestamp", 0)
        exclude = info.get("exclude_tools", [])
        elapsed = int(now - ts) if ts else 0
        hours, remainder = divmod(elapsed, 3600)
        minutes, seconds = divmod(remainder, 60)
        duration_str = (
            f"{hours}h {minutes}m" if hours else f"{minutes}m {seconds}s"
        )
        line = (
            f"• Issue *#{issue_num}* — `{agent_type}` via `{tool}`\n"
            f"  Tier: `{tier}` | Running: `{duration_str}`"
        )
        if exclude:
            line += f"\n  Excluded tools: `{', '.join(exclude)}`"
        lines.append(line)

    await update.effective_message.reply_text(
        "\n".join(lines),
        parse_mode="Markdown",
    )


async def active_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Shows active tasks being worked on."""
    logger.info(f"Active triggered by user: {update.effective_user.id}")
    if ALLOWED_USER_ID and update.effective_user.id != ALLOWED_USER_ID:
        logger.warning(f"Unauthorized access attempt by ID: {update.effective_user.id}")
        return

    cleanup_mode = any(arg.lower() in {"cleanup", "--cleanup"} for arg in (context.args or []))
    project_tokens = [arg for arg in (context.args or []) if arg.lower() not in {"cleanup", "--cleanup"}]
    project_filter = None
    if project_tokens:
        raw = project_tokens[0].strip().lower()
        if raw != "all":
            project_filter = _normalize_project_key(raw)
            if project_filter not in _iter_project_keys():
                await update.effective_message.reply_text(f"❌ Unknown project '{raw}'.")
                return
    elif not cleanup_mode:
        await _prompt_monitor_project_selection(update, context, "active")
        return

    selected_projects = [project_filter] if project_filter else _iter_project_keys()

    active_text = "🚀 Active Tasks (In Progress)\n\n"
    if cleanup_mode:
        active_text += "🧹 Cleanup mode: archiving closed tasks to `tasks/closed`\n\n"
    total_active = 0
    total_skipped_closed = 0
    total_archived = 0

    # Cache GitHub issue states to avoid duplicate API calls in one command run.
    issue_state_cache = {}

    # Check project workspace active folders
    for project_key in selected_projects:
        display_name = _get_project_label(project_key)
        project_root = _get_project_root(project_key)
        if not project_root:
            continue
        active_dir = get_tasks_active_dir(project_root, project_key)
        if os.path.exists(active_dir):
            files = [f for f in os.listdir(active_dir) if f.endswith(".md")]
            if files:
                repo = PROJECT_CONFIG[project_key].get("github_repo", GITHUB_REPO)
                open_files = []
                stale_count = 0

                for f in files:
                    file_path = os.path.join(active_dir, f)
                    issue_number = extract_issue_number_from_file(file_path)
                    if not issue_number:
                        # Try both issue_N.md and type_N.md naming conventions
                        filename_match = re.search(r"_(\d+)\.md$", f)
                        issue_number = filename_match.group(1) if filename_match else None

                    if not issue_number:
                        # No number at all — orphan task
                        open_files.append((f, None))
                        continue

                    cache_key = f"{repo}:{issue_number}"
                    if cache_key not in issue_state_cache:
                        details = get_issue_details(issue_number, repo=repo)
                        if not details:
                            # Number from filename but no matching GitHub issue → orphan
                            issue_state_cache[cache_key] = "orphan"
                        else:
                            # Normalize to lowercase — gh CLI returns "OPEN"/"CLOSED"
                            issue_state_cache[cache_key] = details.get("state", "unknown").lower()

                    issue_state = issue_state_cache[cache_key]

                    if issue_state == "orphan":
                        # Show as active but with orphan label — may need manual cleanup
                        open_files.append((f, None))
                        continue

                    if issue_state == "closed":
                        stale_count += 1
                        if cleanup_mode:
                            try:
                                closed_dir = get_tasks_closed_dir(project_root, project_key)
                                os.makedirs(closed_dir, exist_ok=True)
                                target_path = os.path.join(closed_dir, f)
                                if os.path.exists(target_path):
                                    base, ext = os.path.splitext(f)
                                    target_path = os.path.join(
                                        closed_dir,
                                        f"{base}_{int(time.time())}{ext}",
                                    )
                                os.replace(file_path, target_path)
                                total_archived += 1
                                logger.info(f"Archived closed task file: {file_path} -> {target_path}")
                            except Exception as exc:
                                logger.warning(f"Failed to archive {file_path}: {exc}")
                        continue

                    if issue_state in {"open", "unknown"}:
                        open_files.append((f, issue_number))
                    else:
                        stale_count += 1

                if not open_files:
                    total_skipped_closed += stale_count
                    continue

                active_text += f"{display_name}: {len(open_files)} task(s)\n"
                total_active += len(open_files)
                total_skipped_closed += stale_count

                for f, issue_number in open_files[:3]:
                    task_type = f.split('_')[0]
                    emoji = TYPES.get(task_type, "📝")
                    if issue_number:
                        issue_link = f"https://github.com/{repo}/issues/{issue_number}"
                        issue_suffix = f" [#{issue_number}]({issue_link})"
                    else:
                        issue_suffix = " _(orphan — no GitHub issue)_"
                    active_text += f"  • {emoji} `{f}`{issue_suffix}\n"
                if len(open_files) > 3:
                    active_text += f"  ... +{len(open_files) - 3} more\n"
                active_text += "\n"

    if total_active == 0:
        active_text += "💤 No active tasks at the moment.\n"
    else:
        active_text += f"Total: {total_active} active task(s)"

    if total_skipped_closed:
        active_text += f"\n\nℹ️ Skipped {total_skipped_closed} closed task file(s)."
    if cleanup_mode:
        active_text += f"\n📦 Archived {total_archived} closed task file(s) to `tasks/closed`."

    await update.effective_message.reply_text(
        active_text,
        parse_mode='Markdown',
        disable_web_page_preview=True,
    )


async def assign_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Assigns a GitHub issue to the user."""
    logger.info(f"Assign triggered by user: {update.effective_user.id}")
    if ALLOWED_USER_ID and update.effective_user.id != ALLOWED_USER_ID:
        logger.warning(f"Unauthorized access attempt by ID: {update.effective_user.id}")
        return

    # Parse issue number from command
    # Format: /assign 0 or /assign #0 or /assign https://github.com/owner/repo/issues/0
    if not context.args:
        await _prompt_project_selection(update, context, "assign")
        return

    project_key, issue_number, rest = await _ensure_project_issue(update, context, "assign")
    if not project_key:
        return

    repo = PROJECT_CONFIG[project_key].get("github_repo", GITHUB_REPO)
    # Optional assignee argument: `/assign 5 copilot` or `/assign 5 alice`
    assignee = "@me"
    if rest:
        raw_assignee = rest[0]
        if raw_assignee.lower() == "copilot":
            assignee = os.getenv("GITHUB_COPILOT_USER", "copilot")
        else:
            assignee = raw_assignee
    
    # Assign using plugin
    msg = await update.effective_message.reply_text(f"🔄 Assigning issue #{issue_number}...")
    
    try:
        plugin = _get_direct_issue_plugin(repo)
        if not plugin or not plugin.add_assignee(issue_number, assignee):
            await context.bot.edit_message_text(
                chat_id=update.effective_chat.id,
                message_id=msg.message_id,
                text=f"❌ Failed to assign issue #{issue_number}"
            )
            return

        display_assignee = assignee
        if display_assignee == "@me":
            display_assignee = "you (@me)"
        await context.bot.edit_message_text(
            chat_id=update.effective_chat.id,
            message_id=msg.message_id,
            text=f"✅ Issue #{issue_number} assigned to {display_assignee}!\n\nhttps://github.com/{repo}/issues/{issue_number}",
            parse_mode='Markdown'
        )
    except Exception as e:
        await context.bot.edit_message_text(
            chat_id=update.effective_chat.id,
            message_id=msg.message_id,
            text=f"❌ Failed to assign issue #{issue_number}\n\nError: {e}"
        )


@rate_limited("implement")
async def implement_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Requests Copilot agent implementation for an issue (approval workflow).

    Adds an `agent:requested` label and notifies `@ProjectLead` with a comment
    so they can approve (add `agent:approved`) or click "Code with agent mode".
    """
    logger.info(f"Implement requested by user: {update.effective_user.id}")
    if ALLOWED_USER_ID and update.effective_user.id != ALLOWED_USER_ID:
        logger.warning(f"Unauthorized access attempt by ID: {update.effective_user.id}")
        return

    if not context.args:
        await _prompt_project_selection(update, context, "implement")
        return

    project_key, issue_number, _ = await _ensure_project_issue(update, context, "implement")
    if not project_key:
        return

    repo = PROJECT_CONFIG[project_key].get("github_repo", GITHUB_REPO)

    msg = await update.message.reply_text(f"🔔 Requesting Copilot implementation for issue #{issue_number}...")

    try:
        plugin = _get_direct_issue_plugin(repo)
        if not plugin:
            await context.bot.edit_message_text(
                chat_id=update.effective_chat.id,
                message_id=msg.message_id,
                text="❌ Failed to initialize GitHub issue plugin"
            )
            return

        plugin.ensure_label(
            "agent:requested",
            "E6E6FA",
            "Requested Copilot implementation",
        )
        if not plugin.add_label(issue_number, "agent:requested"):
            await context.bot.edit_message_text(
                chat_id=update.effective_chat.id,
                message_id=msg.message_id,
                text=f"❌ Failed to request implementation for issue #{issue_number}."
            )
            return

        comment = (
            f"@ProjectLead — Copilot implementation has been requested via Telegram.\n\n"
            f"Please review the issue and either click 'Code with agent mode' in the GitHub UI or add the label `agent:approved` to start implementation.\n\n"
            f"Issue: https://github.com/{repo}/issues/{issue_number}"
        )

        if not plugin.add_comment(issue_number, comment):
            await context.bot.edit_message_text(
                chat_id=update.effective_chat.id,
                message_id=msg.message_id,
                text=f"❌ Failed to request implementation for issue #{issue_number}."
            )
            return

        await context.bot.edit_message_text(
            chat_id=update.effective_chat.id,
            message_id=msg.message_id,
            text=f"✅ Requested implementation for issue #{issue_number}. ProjectLead has been notified.\n\nhttps://github.com/{repo}/issues/{issue_number}",
            parse_mode='Markdown'
        )
    except Exception as e:
        await context.bot.edit_message_text(
            chat_id=update.effective_chat.id,
            message_id=msg.message_id,
            text=f"❌ Failed to request implementation for issue #{issue_number}.\n\nError: {e}"
        )


async def prepare_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Augments an issue with Copilot-friendly instructions and acceptance criteria."""
    logger.info(f"Prepare requested by user: {update.effective_user.id}")
    if ALLOWED_USER_ID and update.effective_user.id != ALLOWED_USER_ID:
        logger.warning(f"Unauthorized access attempt by ID: {update.effective_user.id}")
        return

    if not context.args:
        await _prompt_project_selection(update, context, "prepare")
        return

    project_key, issue_number, _ = await _ensure_project_issue(update, context, "prepare")
    if not project_key:
        return

    repo = PROJECT_CONFIG[project_key].get("github_repo", GITHUB_REPO)

    msg = await update.message.reply_text(f"🔧 Preparing issue #{issue_number} for Copilot...")

    try:
        plugin = _get_direct_issue_plugin(repo)
        if not plugin:
            await context.bot.edit_message_text(
                chat_id=update.effective_chat.id,
                message_id=msg.message_id,
                text="❌ Failed to initialize GitHub issue plugin"
            )
            return

        data = plugin.get_issue(issue_number, ["body", "title"])
        if not data:
            await context.bot.edit_message_text(
                chat_id=update.effective_chat.id,
                message_id=msg.message_id,
                text=f"❌ Failed to prepare issue #{issue_number}."
            )
            return
        body = data.get("body", "")
        title = data.get("title", "")

        # Extract helpful metadata if present
        branch_match = re.search(r'Target Branch:\s*`([^`]+)`', body)
        taskfile_match = re.search(r'Task File:\s*`([^`]+)`', body)
        branch_name = branch_match.group(1) if branch_match else "<create-branch>"
        task_file = taskfile_match.group(1) if taskfile_match else None

        copilot_block = """
## Copilot Instructions

- Follow existing repository style and tests.
- Create a branch: `{branch}` and open a PR against the appropriate base branch.
- Include unit tests or update existing tests when applicable.
- Keep changes minimal and focused; reference the task file if present.
""".format(branch=branch_name)

        if task_file:
            copilot_block += f"\n**Suggested files to modify:** `{task_file}`\n"

        copilot_block += "\n**Acceptance Criteria**\n- Add concise acceptance criteria here (one per line).\n"

        new_body = body + "\n\n---\n\n" + copilot_block

        if not plugin.update_issue_body(issue_number, new_body):
            await context.bot.edit_message_text(
                chat_id=update.effective_chat.id,
                message_id=msg.message_id,
                text=f"❌ Failed to prepare issue #{issue_number}."
            )
            return

        await context.bot.edit_message_text(
            chat_id=update.effective_chat.id,
            message_id=msg.message_id,
            text=f"✅ Prepared issue #{issue_number} for Copilot. You can now click 'Code with agent mode' in GitHub or ask ProjectLead to approve.\n\nhttps://github.com/{repo}/issues/{issue_number}",
            parse_mode='Markdown'
        )
    except Exception as e:
        await context.bot.edit_message_text(
            chat_id=update.effective_chat.id,
            message_id=msg.message_id,
            text=f"❌ Failed to prepare issue #{issue_number}.\n\nError: {e}"
        )


async def track_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Subscribe to issue updates and track status changes.
    
    Usage:
      /track <issue#>              - Track issue in default repo (legacy)
      /track <project> <issue#>    - Track issue in specific project
    
    Examples:
      /track 123
      /track casit 456
      /track wlbl 789
    """
    global tracked_issues
    logger.info(f"Track requested by user: {update.effective_user.id}")
    if ALLOWED_USER_ID and update.effective_user.id != ALLOWED_USER_ID:
        logger.warning(f"Unauthorized access attempt by ID: {update.effective_user.id}")
        return

    user = update.effective_user
    
    if not context.args:
        await update.effective_message.reply_text(
            "⚠️ Usage:\n"
            "/track <issue#> - Track issue globally\n"
            "/track <project> <issue#> - Track issue per-project\n\n"
            "Projects: casit, wlbl, bm\n\n"
            "Examples:\n"
            "  /track 123\n"
            "  /track casit 456"
        )
        return

    # Parse arguments - support both /track <issue> and /track <project> <issue>
    if len(context.args) >= 2:
        # Per-project tracking: /track <project> <issue>
        project = context.args[0].lower()
        issue_num = context.args[1].lstrip("#")
        
        # Validate project
        valid_projects = ['casit', 'wlbl', 'bm']
        if project not in valid_projects:
            await update.effective_message.reply_text(
                f"❌ Invalid project '{project}'.\n"
                f"Valid projects: {', '.join(valid_projects)}"
            )
            return
        
        if not issue_num.isdigit():
            await update.effective_message.reply_text("❌ Invalid issue number.")
            return
        
        # Track for user in specific project
        user_manager.track_issue(
            telegram_id=user.id,
            project=project,
            issue_number=issue_num,
            username=user.username,
            first_name=user.first_name
        )
        
        await update.effective_message.reply_text(
            f"👁️ Now tracking {project.upper()} issue #{issue_num} for you\n\n"
            f"Use /myissues to see all your tracked issues\n"
            f"Use /untrack {project} {issue_num} to stop tracking"
        )
    else:
        # Legacy global tracking: /track <issue>
        issue_num = context.args[0].lstrip("#")
        if not issue_num.isdigit():
            await update.effective_message.reply_text("❌ Invalid issue number.")
            return

        # Add to global system tracking
        tracked_issues[issue_num] = {
            "added_at": datetime.now().isoformat(),
            "last_seen_state": None,
            "last_seen_labels": []
        }
        save_tracked_issues(tracked_issues)

        details = get_issue_details(issue_num)
        if details:
            await update.effective_message.reply_text(
                f"👁️ Now tracking issue #{issue_num} (global)\n\n"
                f"Title: {details.get('title', 'N/A')}\n"
                f"Status: {details.get('state', 'N/A')}\n"
                f"Labels: {', '.join([l['name'] for l in details.get('labels', [])])}\n\n"
                f"🔗 https://github.com/{GITHUB_REPO}/issues/{issue_num}\n\n"
                f"💡 Tip: Use /track <project> <issue#> for per-project tracking"
            )
        else:
            await update.effective_message.reply_text(
                f"⚠️ Could not fetch issue details, but tracking started.\n\n"
                f"🔗 https://github.com/{GITHUB_REPO}/issues/{issue_num}"
            )


async def untrack_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Stop tracking an issue.
    
    Usage:
      /untrack <issue#>              - Stop global tracking
      /untrack <project> <issue#>    - Stop per-project tracking
    """
    global tracked_issues
    logger.info(f"Untrack requested by user: {update.effective_user.id}")
    if ALLOWED_USER_ID and update.effective_user.id != ALLOWED_USER_ID:
        logger.warning(f"Unauthorized access attempt by ID: {update.effective_user.id}")
        return

    user = update.effective_user

    if not context.args:
        await _prompt_project_selection(update, context, "untrack")
        return

    project_key, issue_num, _ = await _ensure_project_issue(update, context, "untrack")
    if not project_key:
        return

    success = user_manager.untrack_issue(
        telegram_id=user.id,
        project=project_key,
        issue_number=issue_num
    )
    
    if success:
        await update.effective_message.reply_text(
            f"✅ Stopped tracking {_get_project_label(project_key)} issue #{issue_num}"
        )
    else:
        await update.effective_message.reply_text(
            f"❌ You weren't tracking {_get_project_label(project_key)} issue #{issue_num}"
        )


async def myissues_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Show all issues tracked by the user across projects."""
    logger.info(f"My issues requested by user: {update.effective_user.id}")
    if ALLOWED_USER_ID and update.effective_user.id != ALLOWED_USER_ID:
        logger.warning(f"Unauthorized access attempt by ID: {update.effective_user.id}")
        return

    user = update.effective_user
    
    # Get user's tracked issues
    tracked = user_manager.get_user_tracked_issues(user.id)
    
    if not tracked:
        await update.effective_message.reply_text(
            "📋 You're not tracking any issues yet.\n\n"
            "Use /track <project> <issue#> to start tracking.\n\n"
            "Examples:\n"
            "  /track casit 123\n"
            "  /track wlbl 456"
        )
        return
    
    # Build message
    message = "📋 <b>Your Tracked Issues</b>\n\n"
    
    total_issues = 0
    for project, issues in sorted(tracked.items()):
        if issues:
            message += f"<b>{project.upper()}</b>\n"
            for issue_num in issues:
                total_issues += 1
                message += f"  • #{issue_num}\n"
            message += "\n"
    
    message += f"<b>Total:</b> {total_issues} issue(s)\n\n"
    message += "<i>Use /untrack &lt;project&gt; &lt;issue#&gt; to stop tracking</i>"
    
    await update.effective_message.reply_text(message, parse_mode='HTML')


@rate_limited("logs")
async def logs_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Show combined timeline of GitHub activity and bot/processor logs for an issue."""
    logger.info(f"Logs requested by user: {update.effective_user.id}")
    if ALLOWED_USER_ID and update.effective_user.id != ALLOWED_USER_ID:
        logger.warning(f"Unauthorized access attempt by ID: {update.effective_user.id}")
        return

    if not context.args:
        await _prompt_project_selection(update, context, "logs")
        return

    project_key, issue_num, _ = await _ensure_project_issue(update, context, "logs")
    if not project_key:
        return

    msg = await update.effective_message.reply_text(f"📋 Fetching logs for issue #{issue_num}...")

    # Task log files only (.nexus/tasks/<project>/logs/*.log)
    config = PROJECT_CONFIG[project_key]
    repo = config.get("github_repo", GITHUB_REPO)
    details = get_issue_details(issue_num, repo=repo)
    timeline = "Task Logs:\n"

    task_file = None
    if details and details.get("body"):
        match = re.search(r"Task File:\s*`([^`]+)`", details.get("body", ""))
        if match:
            task_file = match.group(1)
    if not task_file:
        task_file = find_task_file_by_issue(issue_num)

    issue_logs = find_issue_log_files(issue_num, task_file=task_file)
    if issue_logs:
        issue_logs.sort(key=lambda p: os.path.getmtime(p))
        # List all log files (oldest → newest) with size
        timeline += "\n"
        for lf in issue_logs:
            size = os.path.getsize(lf)
            mtime = time.strftime("%H:%M:%S", time.localtime(os.path.getmtime(lf)))
            timeline += f"• `{os.path.basename(lf)}` ({size}B, {mtime})\n"
        # Read last 50 lines from the most recent non-empty file
        non_empty = [lf for lf in reversed(issue_logs) if os.path.getsize(lf) > 0]
        latest = non_empty[0] if non_empty else issue_logs[-1]
        logger.info(f"Reading log file: {latest}")
        try:
            with open(latest, "r") as f:
                lines = f.readlines()[-50:]
            logger.info(f"Read {len(lines)} lines from log file")
            timeline += f"\n**{os.path.basename(latest)}** (last 50 lines):\n"
            for line in lines:
                timeline += f"{line.rstrip()}\n"
        except Exception as e:
            logger.error(f"Error reading log file: {e}", exc_info=True)
            timeline += f"\n❌ Failed to read {os.path.basename(latest)}: {e}\n"
    else:
        latest_tail = read_latest_log_tail(task_file, max_lines=50)
        if not latest_tail:
            # Do NOT fall back to the latest unrelated log — search for actual
            # references to this issue number in bot/processor logs instead.
            issue_refs = search_logs_for_issue(issue_num)
            if issue_refs:
                timeline += "\nReferences in service logs:\n"
                for line in issue_refs[-30:]:
                    timeline += f"{line}\n"
            else:
                timeline += "\n- No task logs found for this issue.\n"
            latest_tail = []  # skip the block below

        if latest_tail:
            timeline += "\nLatest Task Logs:\n"
            for log in latest_tail:
                timeline += f"{log}\n"
        else:
            timeline += "\n- No task logs found.\n"

    # Telegram message limit safety: send in chunks
    max_len = 3500
    if len(timeline) <= max_len:
        await context.bot.edit_message_text(
            chat_id=update.effective_chat.id,
            message_id=msg.message_id,
            text=timeline
        )
    else:
        # Split into chunks
        chunks = [timeline[i:i+max_len] for i in range(0, len(timeline), max_len)]
        for idx, chunk in enumerate(chunks):
            if idx == 0:
                await context.bot.edit_message_text(
                    chat_id=update.effective_chat.id,
                    message_id=msg.message_id,
                    text=chunk
                )
            else:
                await context.bot.send_message(
                    chat_id=update.effective_chat.id,
                    text=chunk
                )


@rate_limited("logs")
async def logsfull_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Show combined timeline of GitHub activity and full log lines for an issue."""
    logger.info(f"Logsfull requested by user: {update.effective_user.id}")
    if ALLOWED_USER_ID and update.effective_user.id != ALLOWED_USER_ID:
        logger.warning(f"Unauthorized access attempt by ID: {update.effective_user.id}")
        return

    if not context.args:
        await _prompt_project_selection(update, context, "logsfull")
        return

    project_key, issue_num, _ = await _ensure_project_issue(update, context, "logsfull")
    if not project_key:
        return

    msg = await update.effective_message.reply_text(f"📋 Fetching full logs for issue #{issue_num}...")
    config = PROJECT_CONFIG[project_key]
    repo = config.get("github_repo", GITHUB_REPO)
    issue_url = f"https://github.com/{repo}/issues/{issue_num}"

    details = get_issue_details(issue_num, repo=repo)
    timeline = "GitHub Activity:\n"
    if details:
        timeline += f"- Title: {details.get('title', 'N/A')}\n"
        timeline += f"- State: {details.get('state', 'open')}\n"
        timeline += f"- Last Updated: {details.get('updatedAt', 'N/A')}\n"
        if details.get('labels'):
            timeline += f"- Labels: {', '.join([l['name'] for l in details.get('labels', [])])}\n"
    else:
        timeline += "- Could not fetch issue details\n"

    system_logs = search_logs_for_issue(issue_num)
    if system_logs:
        timeline += "\nBot/Processor Logs:\n"
        for log in system_logs:
            timeline += f"- {log}\n"

    task_file = None
    if details and details.get("body"):
        match = re.search(r"Task File:\s*`([^`]+)`", details.get("body", ""))
        if match:
            task_file = match.group(1)

    latest_full = read_latest_log_full(task_file)
    if latest_full:
        timeline += "\nLatest Task Log (full):\n"
        for log in latest_full:
            timeline += f"- {log}\n"

    processor_log = os.path.join(BASE_DIR, "ghabs", "nexus", "inbox_processor.log")
    processor_matches = read_log_matches(processor_log, issue_num, issue_url, max_lines=20)
    if processor_matches:
        timeline += "\nProcessor Log:\n"
        for log in processor_matches:
            timeline += f"- {log}\n"

    # Telegram message limit safety: send in chunks
    max_len = 3500
    if len(timeline) <= max_len:
        await context.bot.edit_message_text(
            chat_id=update.effective_chat.id,
            message_id=msg.message_id,
            text=timeline
        )
        return

    chunks = [timeline[i:i + max_len] for i in range(0, len(timeline), max_len)]
    await context.bot.edit_message_text(
        chat_id=update.effective_chat.id,
        message_id=msg.message_id,
        text=chunks[0]
    )
    for part in chunks[1:]:
        await context.bot.send_message(chat_id=update.effective_chat.id, text=part)


@rate_limited("logs")
async def tail_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Show a short tail of the latest task log for an issue."""
    logger.info(f"Tail requested by user: {update.effective_user.id}")
    if ALLOWED_USER_ID and update.effective_user.id != ALLOWED_USER_ID:
        logger.warning(f"Unauthorized access attempt by ID: {update.effective_user.id}")
        return

    if not context.args:
        await update.effective_message.reply_text("⚠️ Usage: /tail <project> <issue#> [lines]")
        return

    project_key, issue_num, rest = await _ensure_project_issue(update, context, "tail")
    if not project_key:
        return

    max_lines = 50
    if rest:
        try:
            max_lines = max(5, min(200, int(rest[0])))
        except ValueError:
            await update.effective_message.reply_text("⚠️ Line count must be a number.")
            return

    config = PROJECT_CONFIG[project_key]
    repo = config.get("github_repo", GITHUB_REPO)
    details = get_issue_details(issue_num, repo=repo)

    task_file = None
    if details and details.get("body"):
        match = re.search(r"Task File:\s*`([^`]+)`", details.get("body", ""))
        if match:
            task_file = match.group(1)
    if not task_file:
        task_file = find_task_file_by_issue(issue_num)

    lines = read_latest_log_tail(task_file, max_lines=max_lines)
    if not lines:
        logs_dir = _get_project_logs_dir(project_key)
        if logs_dir:
            log_files = [
                os.path.join(logs_dir, f)
                for f in os.listdir(logs_dir)
                if f.endswith(".log")
            ]
            if log_files:
                log_files.sort(key=os.path.getmtime, reverse=True)
                latest = log_files[0]
                try:
                    with open(latest, "r") as f:
                        tail_lines = f.readlines()[-max_lines:]
                    lines = [
                        f"[{os.path.basename(latest)}] {line.rstrip()}"
                        for line in tail_lines
                    ]
                except Exception as e:
                    logger.error(f"Error reading log file: {e}", exc_info=True)

    if not lines:
        await update.effective_message.reply_text("⚠️ No task logs found yet.")
        return

    header = f"📋 Latest log tail (#{issue_num}, {max_lines} lines):\n"
    text = header + "\n".join(lines)

    max_len = 3500
    if len(text) <= max_len:
        await update.effective_message.reply_text(text)
    else:
        chunks = [text[i:i + max_len] for i in range(0, len(text), max_len)]
        await update.effective_message.reply_text(chunks[0])
        for part in chunks[1:]:
            await context.bot.send_message(chat_id=update.effective_chat.id, text=part)


async def audit_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Display workflow audit trail for an issue (timeline of state changes, agent launches, etc)."""
    logger.info(f"Audit trail requested by user: {update.effective_user.id}")
    if ALLOWED_USER_ID and update.effective_user.id != ALLOWED_USER_ID:
        logger.warning(f"Unauthorized access attempt by ID: {update.effective_user.id}")
        return

    if not context.args:
        await _prompt_project_selection(update, context, "audit")
        return

    project_key, issue_num, _ = await _ensure_project_issue(update, context, "audit")
    if not project_key:
        return

    try:
        # Import here to avoid circular imports
        from audit_store import AuditStore
        
        msg = await update.effective_message.reply_text(f"📊 Fetching audit trail for issue #{issue_num}...", parse_mode="Markdown")
        
        # Get audit history from StateManager (now returns structured dicts)
        audit_history = AuditStore.get_audit_history(issue_num, limit=100)
        
        if not audit_history:
            await context.bot.edit_message_text(
                chat_id=update.effective_chat.id,
                message_id=msg.message_id,
                text=f"📊 **Audit Trail for Issue #{issue_num}**\n\nNo audit events recorded yet."
            )
            return
        
        # Format audit trail from structured JSONL events
        timeline = f"📊 **Audit Trail for Issue #{issue_num}**\n"
        timeline += "=" * 40 + "\n\n"

        _EVENT_EMOJI = {
            "AGENT_LAUNCHED": "🚀",
            "AGENT_TIMEOUT_KILL": "⏱️",
            "AGENT_RETRY": "🔄",
            "AGENT_FAILED": "❌",
            "WORKFLOW_PAUSED": "⏸️",
            "WORKFLOW_RESUMED": "▶️",
            "WORKFLOW_STOPPED": "🛑",
            "AGENT_COMPLETION": "✅",
            "WORKFLOW_STARTED": "🎬",
            "WORKFLOW_CREATED": "📋",
            "STEP_STARTED": "▶️",
            "STEP_COMPLETED": "✅",
        }

        for evt in audit_history:
            try:
                event_type = evt.get("event_type", "?")
                timestamp = evt.get("timestamp", "?")
                data = evt.get("data", {})
                details = data.get("details", "") if isinstance(data, dict) else ""
                emoji = _EVENT_EMOJI.get(event_type, "•")

                timeline += f"{emoji} **{event_type}** ({timestamp})\n"
                if details:
                    timeline += f"   {details}\n"
                timeline += "\n"
            except Exception as e:
                logger.warning(f"Error formatting audit event: {e}")
                timeline += f"• {evt}\n\n"
        
        # Telegram message limit safety
        max_len = 3500
        if len(timeline) <= max_len:
            await context.bot.edit_message_text(
                chat_id=update.effective_chat.id,
                message_id=msg.message_id,
                text=timeline
            )
        else:
            # Split into chunks
            chunks = [timeline[i:i+max_len] for i in range(0, len(timeline), max_len)]
            await context.bot.edit_message_text(
                chat_id=update.effective_chat.id,
                message_id=msg.message_id,
                text=chunks[0]
            )
            for chunk in chunks[1:]:
                await context.bot.send_message(
                    chat_id=update.effective_chat.id,
                    text=chunk
                )
    except Exception as e:
        logger.error(f"Error in audit_handler: {e}", exc_info=True)
        error_msg = format_error_for_user(e, "while fetching audit trail")
        await update.effective_message.reply_text(error_msg)


@rate_limited("stats")
async def stats_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Display system analytics and performance statistics."""
    logger.info(f"Stats requested by user: {update.effective_user.id}")
    if ALLOWED_USER_ID and update.effective_user.id != ALLOWED_USER_ID:
        logger.warning(f"Unauthorized access attempt by ID: {update.effective_user.id}")
        return

    msg = await update.effective_message.reply_text("📊 Generating analytics report...", parse_mode="Markdown")
    
    try:
        # Parse optional lookback days argument
        lookback_days = 30  # default
        if context.args and len(context.args) > 0:
            try:
                lookback_days = int(context.args[0])
                if lookback_days < 1 or lookback_days > 365:
                    await update.effective_message.reply_text("⚠️ Lookback days must be between 1 and 365. Using default 30 days.")
                    lookback_days = 30
            except ValueError:
                await update.effective_message.reply_text("⚠️ Invalid lookback days. Using default 30 days.")
                lookback_days = 30
        
        # Generate report
        report = get_stats_report(lookback_days=lookback_days)
        
        # Send report (handle Telegram message length limits)
        max_len = 3500
        if len(report) <= max_len:
            await context.bot.edit_message_text(
                chat_id=update.effective_chat.id,
                message_id=msg.message_id,
                text=report,
                parse_mode="Markdown"
            )
        else:
            # Split into chunks
            chunks = [report[i:i+max_len] for i in range(0, len(report), max_len)]
            await context.bot.edit_message_text(
                chat_id=update.effective_chat.id,
                message_id=msg.message_id,
                text=chunks[0],
                parse_mode="Markdown"
            )
            for chunk in chunks[1:]:
                await context.bot.send_message(
                    chat_id=update.effective_chat.id,
                    text=chunk,
                    parse_mode="Markdown"
                )
    
    except FileNotFoundError:
        await context.bot.edit_message_text(
            chat_id=update.effective_chat.id,
            message_id=msg.message_id,
            text="📊 No audit log found. System has not logged any workflow events yet."
        )
    except Exception as e:
        logger.error(f"Error in stats_handler: {e}", exc_info=True)
        error_msg = format_error_for_user(e, "while generating analytics report")
        await context.bot.edit_message_text(
            chat_id=update.effective_chat.id,
            message_id=msg.message_id,
            text=error_msg
        )


@rate_limited("reprocess")
async def reprocess_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Re-run agent processing for an open issue."""
    logger.info(f"Reprocess requested by user: {update.effective_user.id}")
    if ALLOWED_USER_ID and update.effective_user.id != ALLOWED_USER_ID:
        logger.warning(f"Unauthorized access attempt by ID: {update.effective_user.id}")
        return

    if not context.args:
        await _prompt_project_selection(update, context, "reprocess")
        return

    project_key, issue_num, _ = await _ensure_project_issue(update, context, "reprocess")
    if not project_key:
        return

    task_file = find_task_file_by_issue(issue_num)
    details = None
    repo = None
    if not task_file:
        repo = PROJECT_CONFIG[project_key].get("github_repo", GITHUB_REPO)
        details = get_issue_details(issue_num, repo=repo)
        if not details:
            await update.effective_message.reply_text(f"❌ Could not load issue #{issue_num}.")
            return
        body = details.get("body", "")
        match = re.search(r"Task File:\s*`([^`]+)`", body)
        task_file = match.group(1) if match else None

    if not task_file:
        await update.effective_message.reply_text(f"❌ Task file not found for issue #{issue_num}.")
        return
    if not os.path.exists(task_file):
        await update.effective_message.reply_text(f"❌ Task file missing: {task_file}")
        return

    project_name, config = resolve_project_config_from_task(task_file)
    if not config or not config.get("agents_dir"):
        name = project_name or "unknown"
        await update.effective_message.reply_text(f"❌ No agents config for project '{name}'.")
        return

    repo = config.get("github_repo", GITHUB_REPO)
    if not details:
        details = get_issue_details(issue_num, repo=repo)
        if not details:
            await update.effective_message.reply_text(f"❌ Could not load issue #{issue_num}.")
            return

    if details.get("state") == "closed":
        await update.effective_message.reply_text(
            f"⚠️ Issue #{issue_num} is closed. Reprocess only applies to open issues."
        )
        return

    with open(task_file, "r") as f:
        content = f.read()

    type_match = re.search(r"\*\*Type:\*\*\s*(.+)", content)
    task_type = type_match.group(1).strip().lower() if type_match else "feature"

    # Resolve tier: launched_agents tracker → issue labels → halt if unknown
    from state_manager import StateManager
    tracker_tier = StateManager.get_last_tier_for_issue(issue_num)
    label_tier = get_sop_tier_from_issue(issue_num, project_name or project_key)
    tier_name = label_tier or tracker_tier
    if not tier_name:
        await update.effective_message.reply_text(
            f"⚠️ Cannot determine workflow tier for issue #{issue_num}.\n"
            f"Add a `workflow:` label (e.g. `workflow:full`) to the issue and retry."
        )
        return
    issue_url = f"https://github.com/{repo}/issues/{issue_num}"

    msg = await update.effective_message.reply_text(f"🔁 Reprocessing issue #{issue_num}...")

    agents_abs = os.path.join(BASE_DIR, config["agents_dir"])
    workspace_abs = os.path.join(BASE_DIR, config["workspace"])

    log_subdir = project_name or project_key
    pid, tool_used = invoke_copilot_agent(
        agents_dir=agents_abs,
        workspace_dir=workspace_abs,
        issue_url=issue_url,
        tier_name=tier_name,
        task_content=content,
        log_subdir=log_subdir,
        project_name=log_subdir
    )

    if pid:
        await context.bot.edit_message_text(
            chat_id=update.effective_chat.id,
            message_id=msg.message_id,
            text=(
                f"✅ Reprocess started for issue #{issue_num}. Agent PID: {pid} (Tool: {tool_used})\n\n"
                f"🔗 https://github.com/{repo}/issues/{issue_num}"
            )
        )
    else:
        await context.bot.edit_message_text(
            chat_id=update.effective_chat.id,
            message_id=msg.message_id,
            text=f"❌ Failed to launch reprocess for issue #{issue_num}."
        )


async def continue_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Continue/resume agent processing for an issue with a continuation prompt."""
    logger.info(f"Continue requested by user: {update.effective_user.id}")
    if ALLOWED_USER_ID and update.effective_user.id != ALLOWED_USER_ID:
        logger.warning(f"Unauthorized access attempt by ID: {update.effective_user.id}")
        return

    if not context.args:
        await _prompt_project_selection(update, context, "continue")
        return

    project_key, issue_num, rest = await _ensure_project_issue(update, context, "continue")
    if not project_key:
        return

    # Parse optional from:<step> argument to override the next agent
    forced_agent = None
    filtered_rest = []
    for token in (rest or []):
        if token.lower().startswith("from:"):
            forced_agent = token[5:].strip()
        else:
            filtered_rest.append(token)

    continuation_prompt = " ".join(filtered_rest) if filtered_rest else "Please continue with the next step."

    # Check if agent is already running
    runtime_ops = get_runtime_ops_plugin(cache_key="runtime-ops:telegram")
    pid = runtime_ops.find_agent_pid_for_issue(issue_num) if runtime_ops else None
    if pid:
        await update.effective_message.reply_text(
            f"⚠️ Agent is already running for issue #{issue_num} (PID: {pid}).\n\n"
            f"Use /kill {issue_num} first if you want to restart it."
        )
        return

    # Get task file and repo
    task_file = find_task_file_by_issue(issue_num)
    details = None
    repo = None
    if not task_file:
        repo = PROJECT_CONFIG[project_key].get("github_repo", GITHUB_REPO)
        details = get_issue_details(issue_num, repo=repo)
        if not details:
            await update.effective_message.reply_text(f"❌ Could not load issue #{issue_num}.")
            return
        body = details.get("body", "")
        match = re.search(r"Task File:\s*`([^`]+)`", body)
        task_file = match.group(1) if match else None

    if not task_file or not os.path.exists(task_file):
        await update.effective_message.reply_text(f"❌ Task file not found for issue #{issue_num}.")
        return

    project_name, config = resolve_project_config_from_task(task_file)
    if not config or not config.get("agents_dir"):
        name = project_name or "unknown"
        await update.effective_message.reply_text(f"❌ No agents config for project '{name}'.")
        return

    repo = config.get("github_repo", GITHUB_REPO)
    if not details:
        details = get_issue_details(issue_num, repo=repo)
        if not details:
            await update.effective_message.reply_text(f"❌ Could not load issue #{issue_num}.")
            return

    if details.get("state", "").lower() == "closed":
        await update.effective_message.reply_text(f"⚠️ Issue #{issue_num} is closed.")
        return

    with open(task_file, "r") as f:
        content = f.read()

    type_match = re.search(r"\*\*Type:\*\*\s*(.+)", content)
    task_type = type_match.group(1).strip().lower() if type_match else "feature"

    # Resolve which agent to launch: check last completion's next_agent first
    agent_type = None
    resumed_from = None
    workflow_already_done = False
    try:
        completions = scan_for_completions(BASE_DIR)
        issue_completions = [
            c for c in completions if c.issue_number == str(issue_num)
        ]
        if issue_completions:
            # Use the most recent completion (by file mtime)
            latest = max(issue_completions, key=lambda c: os.path.getmtime(c.file_path))
            if getattr(latest.summary, "is_workflow_done", False):
                workflow_already_done = True
                resumed_from = latest.summary.agent_type
            else:
                raw_next = latest.summary.next_agent
                normalized = _normalize_agent_reference(raw_next)
                if normalized and normalized.lower() not in {
                    "none", "n/a", "null", "done", "end", "finish", "complete", ""
                }:
                    agent_type = normalized
                    resumed_from = latest.summary.agent_type
                    logger.info(
                        f"Continue issue #{issue_num}: last step was {resumed_from}, "
                        f"resuming with next_agent={agent_type}"
                    )
    except Exception as e:
        logger.warning(f"Could not scan completions for issue #{issue_num}: {e}")

    # Allow caller to force a specific step via from:<step> (overrides done-check too)
    if forced_agent:
        agent_type = _normalize_agent_reference(forced_agent) or forced_agent
        workflow_already_done = False
        logger.info(f"Continue issue #{issue_num}: overriding agent to {agent_type} (from: arg)")

    # When workflow is already done: finalize if issue still open, else report done
    if workflow_already_done and not forced_agent:
        if details.get("state", "").lower() == "open":
            msg = await update.effective_message.reply_text(
                f"✅ Workflow complete for issue #{issue_num} (last agent: `{resumed_from}`)\n"
                f"Issue is still open — running finalization now..."
            )
            try:
                from inbox_processor import _finalize_workflow
                _finalize_workflow(issue_num, repo, resumed_from, project_name or project_key)
                await context.bot.edit_message_text(
                    chat_id=update.effective_chat.id,
                    message_id=msg.message_id,
                    text=(
                        f"✅ Workflow complete for issue #{issue_num}\n"
                        f"Last agent: `{resumed_from}`\n"
                        f"Issue finalized (closed + PR if applicable)."
                    ),
                )
            except Exception as exc:
                logger.error(f"Finalization failed for issue #{issue_num}: {exc}", exc_info=True)
                await context.bot.edit_message_text(
                    chat_id=update.effective_chat.id,
                    message_id=msg.message_id,
                    text=f"⚠️ Finalization error for issue #{issue_num}: {exc}",
                )
        else:
            await update.effective_message.reply_text(
                f"✅ Workflow for issue #{issue_num} is already complete and closed.\n"
                f"Last agent: `{resumed_from}`\n\n"
                f"Use `/continue {project_key} {issue_num} from:<agent>` to re-run a specific step."
            )
        return

    # Fallback: extract from task file (defaults to triage)
    if not agent_type:
        agent_type_match = re.search(r"\*\*Agent Type:\*\*\s*(.+)", content)
        agent_type = agent_type_match.group(1).strip() if agent_type_match else "triage"
        logger.info(f"Continue issue #{issue_num}: no prior completion found, starting with {agent_type}")

    # Prefer the workflow: label on the issue — task_type heuristic can be wrong
    # (e.g. feature-simple maps to fast-track but issue may have workflow:shortened)
    label_tier = get_sop_tier_from_issue(issue_num, project_name or project_key)
    if label_tier:
        tier_name = label_tier
    else:
        tier_name, _, _ = get_sop_tier(task_type)
    issue_url = f"https://github.com/{repo}/issues/{issue_num}"

    resume_info = f" (after {resumed_from})" if resumed_from else ""
    msg = await update.effective_message.reply_text(
        f"⏩ Continuing issue #{issue_num} with `{agent_type}`{resume_info}..."
    )

    agents_abs = os.path.join(BASE_DIR, config["agents_dir"])
    workspace_abs = os.path.join(BASE_DIR, config["workspace"])

    # Launch with continuation context
    log_subdir = project_name or project_key
    pid, tool_used = invoke_copilot_agent(
        agents_dir=agents_abs,
        workspace_dir=workspace_abs,
        issue_url=issue_url,
        tier_name=tier_name,
        task_content=content,
        continuation=True,
        continuation_prompt=continuation_prompt,
        log_subdir=log_subdir,
        agent_type=agent_type,
        project_name=log_subdir
    )

    if pid:
        await context.bot.edit_message_text(
            chat_id=update.effective_chat.id,
            message_id=msg.message_id,
            text=(
                f"✅ Agent continued for issue #{issue_num}. PID: {pid} (Tool: {tool_used})\n\n"
                f"Prompt: {continuation_prompt}\n\n"
                f"ℹ️ **Note:** The agent will first check if the workflow has already progressed.\n"
                f"If another agent is already handling the next step, this agent will exit gracefully.\n"
                f"Use `/continue` only when an agent is truly stuck mid-step.\n\n"
                f"🔗 https://github.com/{repo}/issues/{issue_num}"
            )
        )
    else:
        await context.bot.edit_message_text(
            chat_id=update.effective_chat.id,
            message_id=msg.message_id,
            text=f"❌ Failed to continue agent for issue #{issue_num}."
        )


async def kill_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Kill a running Copilot agent process."""
    logger.info(f"Kill requested by user: {update.effective_user.id}")
    if ALLOWED_USER_ID and update.effective_user.id != ALLOWED_USER_ID:
        logger.warning(f"Unauthorized access attempt by ID: {update.effective_user.id}")
        return

    if not context.args:
        await _prompt_project_selection(update, context, "kill")
        return

    project_key, issue_num, _ = await _ensure_project_issue(update, context, "kill")
    if not project_key:
        return

    runtime_ops = get_runtime_ops_plugin(cache_key="runtime-ops:telegram")
    pid = runtime_ops.find_agent_pid_for_issue(issue_num) if runtime_ops else None
    
    if not pid:
        await update.effective_message.reply_text(f"⚠️ No running agent found for issue #{issue_num}.")
        return

    msg = await update.effective_message.reply_text(f"🔪 Killing agent for issue #{issue_num} (PID: {pid})...")

    try:
        if not runtime_ops or not runtime_ops.kill_process(pid, force=False):
            raise RuntimeError(f"Failed to stop process {pid}")
        # Wait a moment and verify it's gone
        time.sleep(1)
        new_pid = runtime_ops.find_agent_pid_for_issue(issue_num) if runtime_ops else None
        if new_pid:
            # Try harder
            if not runtime_ops or not runtime_ops.kill_process(pid, force=True):
                raise RuntimeError(f"Failed to force kill process {pid}")
            await context.bot.edit_message_text(
                chat_id=update.effective_chat.id,
                message_id=msg.message_id,
                text=f"✅ Agent killed (PID: {pid}).\n\nUse /reprocess {issue_num} to restart."
            )
        else:
            await context.bot.edit_message_text(
                chat_id=update.effective_chat.id,
                message_id=msg.message_id,
                text=f"✅ Agent stopped (PID: {pid}).\n\nUse /reprocess {issue_num} to restart."
            )
    except subprocess.CalledProcessError as e:
        await context.bot.edit_message_text(
            chat_id=update.effective_chat.id,
            message_id=msg.message_id,
            text=f"❌ Failed to kill agent: {e}"
        )
    except Exception as e:
        await context.bot.edit_message_text(
            chat_id=update.effective_chat.id,
            message_id=msg.message_id,
            text=f"❌ Error: {e}"
        )


async def pause_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Pause auto-chaining with project picker support."""
    if not context.args:
        await _prompt_project_selection(update, context, "pause")
        return

    project_key, issue_num, _ = await _ensure_project_issue(update, context, "pause")
    if not project_key:
        return

    context.args = [project_key, issue_num]
    await workflow_pause_handler(update, context)


async def resume_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Resume auto-chaining with project picker support."""
    if not context.args:
        await _prompt_project_selection(update, context, "resume")
        return

    project_key, issue_num, _ = await _ensure_project_issue(update, context, "resume")
    if not project_key:
        return

    context.args = [project_key, issue_num]
    await workflow_resume_handler(update, context)


async def stop_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Stop workflow with project picker support."""
    if not context.args:
        await _prompt_project_selection(update, context, "stop")
        return

    project_key, issue_num, _ = await _ensure_project_issue(update, context, "stop")
    if not project_key:
        return

    context.args = [project_key, issue_num]
    await workflow_stop_handler(update, context)


# pause_handler, resume_handler, and stop_handler now wrap commands.workflow handlers


def get_agents_for_project(project_dir):
    """Parse agents from .nexus/agents/*.agent.md files.
    
    Returns a dictionary: {agent_display_name: agent_filename}
    Example: {'Architect': 'architect.agent.md', 'BackendLead': 'backend.agent.md'}
    """
    nexus_dir_name = get_nexus_dir_name()
    agents_nexus_dir = os.path.join(project_dir, nexus_dir_name, "agents")
    agents_map = {}
    
    if not os.path.exists(agents_nexus_dir):
        return agents_map
    
    try:
        for filename in sorted(os.listdir(agents_nexus_dir)):
            if filename.endswith(".agent.md"):
                filepath = os.path.join(agents_nexus_dir, filename)
                # Parse the name from the YAML frontmatter
                try:
                    with open(filepath, 'r') as f:
                        lines = f.readlines()
                        in_frontmatter = False
                        for line in lines:
                            if line.strip() == "---":
                                in_frontmatter = not in_frontmatter
                            elif in_frontmatter and line.startswith("name:"):
                                agent_name = line.split("name:", 1)[1].strip()
                                agents_map[agent_name] = filename
                                break
                except Exception as e:
                    logger.warning(f"Failed to parse agent file {filename}: {e}")
    except Exception as e:
        logger.warning(f"Error listing agent files: {e}")
    
    return agents_map


async def agents_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """List all agents for a specific project."""
    logger.info(f"Agents requested by user: {update.effective_user.id}")
    if ALLOWED_USER_ID and update.effective_user.id != ALLOWED_USER_ID:
        logger.warning(f"Unauthorized access attempt by ID: {update.effective_user.id}")
        return

    if not context.args:
        await update.effective_message.reply_text("⚠️ Usage: /agents <project>\n\nExample: /agents case_italia")
        return

    project = context.args[0].lower()
    from inbox_processor import PROJECT_CONFIG
    
    if project not in PROJECT_CONFIG:
        await update.effective_message.reply_text(
            f"❌ Unknown project '{project}'\n\n"
            f"Available: " + ", ".join(PROJECT_CONFIG.keys())
        )
        return
    
    agents_dir = os.path.join(BASE_DIR, PROJECT_CONFIG[project]["agents_dir"])
    if not os.path.exists(agents_dir):
        await update.effective_message.reply_text(f"⚠️ Agents directory not found for '{project}'")
        return
    
    try:
        agents_map = get_agents_for_project(agents_dir)
        
        if not agents_map:
            await update.effective_message.reply_text(f"No agents configured for '{project}'")
            return
        
        agents_list = "\n".join([f"• @{agent}" for agent in sorted(agents_map.keys())])
        await update.effective_message.reply_text(
            f"🤖 **Agents for {project}:**\n\n{agents_list}\n\n"
            f"Use `/direct <project> <@agent> <message>` to send a direct request."
        )
    except Exception as e:
        logger.error(f"Error listing agents: {e}")
        await update.effective_message.reply_text(f"❌ Error: {e}")


@rate_limited("direct")
async def direct_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Send a direct request to a specific agent for a project."""
    logger.info(f"Direct request by user: {update.effective_user.id}")
    if ALLOWED_USER_ID and update.effective_user.id != ALLOWED_USER_ID:
        logger.warning(f"Unauthorized access attempt by ID: {update.effective_user.id}")
        return

    if len(context.args) < 3:
        await update.effective_message.reply_text(
            "⚠️ Usage: /direct <project> <@agent> <message>\n\n"
            "Example: /direct case_italia @BackendLead Add caching to API endpoints"
        )
        return

    project = context.args[0].lower()
    agent = context.args[1].lstrip("@")
    message = " ".join(context.args[2:])
    
    from inbox_processor import PROJECT_CONFIG
    
    if project not in PROJECT_CONFIG:
        await update.effective_message.reply_text(f"❌ Unknown project '{project}'")
        return
    
    # Verify agent exists
    agents_dir = os.path.join(BASE_DIR, PROJECT_CONFIG[project]["agents_dir"])
    agents_map = get_agents_for_project(agents_dir)
    
    if agent not in agents_map:
        available = ", ".join([f"@{a}" for a in sorted(agents_map.keys())])
        await update.effective_message.reply_text(
            f"❌ Unknown agent '@{agent}' for {project}\n\n"
            f"Available: {available}"
        )
        return
    
    msg = await update.effective_message.reply_text(f"🚀 Creating direct request for @{agent}...")
    
    try:
        # Create an issue with a direct request to the specific agent
        title = f"Direct Request: {message[:50]}"
        body = f"""**Direct Request** to @{agent}

{message}

**Project:** {project}
**Assigned to:** @{agent}

---
*Created via /direct command - invoke {agent} immediately*"""
        
        repo = get_github_repo(project)
        plugin = _get_direct_issue_plugin(repo)
        if not plugin:
            await context.bot.edit_message_text(
                chat_id=update.effective_chat.id,
                message_id=msg.message_id,
                text="❌ Failed to initialize GitHub issue plugin"
            )
            return

        issue_url = plugin.create_issue(
            title=title,
            body=body,
            labels=["workflow:fast-track"],
        )
        if not issue_url:
            await context.bot.edit_message_text(
                chat_id=update.effective_chat.id,
                message_id=msg.message_id,
                text="❌ Failed to create issue"
            )
            return
        
        # Extract issue number
        import re as re_module
        match = re_module.search(r"/issues/(\d+)$", issue_url)
        if not match:
            await context.bot.edit_message_text(
                chat_id=update.effective_chat.id,
                message_id=msg.message_id,
                text=f"❌ Failed to get issue number"
            )
            return
        
        issue_num = match.group(1)
        # Post a completion marker comment to trigger immediate auto-chain to this agent
        comment_body = f"🎯 Direct request from @Ghabs\n\nReady for `@{agent}`"
        plugin.add_comment(issue_num, comment_body)
        
        await context.bot.edit_message_text(
            chat_id=update.effective_chat.id,
            message_id=msg.message_id,
            text=f"✅ Direct request created for @{agent} (Issue #{issue_num})\n\n"
                 f"Message: {message}\n\n"
                 f"The auto-chaining system will invoke @{agent} on the next cycle (~60s)\n\n"
                 f"🔗 {issue_url}"
        )
    except Exception as e:
        logger.error(f"Error in direct request: {e}")
        await context.bot.edit_message_text(
            chat_id=update.effective_chat.id,
            message_id=msg.message_id,
            text=f"❌ Error: {e}"
        )


async def comments_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """View recent comments on a GitHub issue."""
    logger.info(f"Comments requested by user: {update.effective_user.id}")
    if ALLOWED_USER_ID and update.effective_user.id != ALLOWED_USER_ID:
        logger.warning(f"Unauthorized access attempt by ID: {update.effective_user.id}")
        return

    if not context.args:
        await _prompt_project_selection(update, context, "comments")
        return

    project_key, issue_num, _ = await _ensure_project_issue(update, context, "comments")
    if not project_key:
        return

    config = PROJECT_CONFIG[project_key]
    repo = config.get("github_repo", GITHUB_REPO)

    msg = await update.effective_message.reply_text(f"💬 Fetching comments for issue #{issue_num}...")

    try:
        plugin = _get_direct_issue_plugin(repo)
        if not plugin:
            await context.bot.edit_message_text(
                chat_id=update.effective_chat.id,
                message_id=msg.message_id,
                text=f"❌ Failed to fetch comments for issue #{issue_num}"
            )
            return

        data = plugin.get_issue(issue_num, ["comments", "title"])
        if not data:
            await context.bot.edit_message_text(
                chat_id=update.effective_chat.id,
                message_id=msg.message_id,
                text=f"❌ Failed to fetch comments for issue #{issue_num}"
            )
            return
        title = data.get("title", "Unknown")
        comments = data.get("comments", [])

        if not comments:
            await context.bot.edit_message_text(
                chat_id=update.effective_chat.id,
                message_id=msg.message_id,
                text=(
                    f"💬 **Issue #{issue_num}: {title}**\n\n"
                    f"No comments yet.\n\n"
                    f"🔗 https://github.com/{repo}/issues/{issue_num}"
                ),
                parse_mode='Markdown'
            )
            return

        # Format comments
        comments_text = f"💬 **Issue #{issue_num}: {title}**\n\n"
        comments_text += f"Total comments: {len(comments)}\n\n"

        # Show last 5 comments
        recent_comments = comments[-5:]
        for i, comment in enumerate(recent_comments, 1):
            author_data = comment.get("author")
            if isinstance(author_data, dict):
                author = author_data.get("login", "unknown")
            else:
                author = author_data or "unknown"
            created = comment.get("created") or comment.get("createdAt", "")
            body = comment.get("body", "")
            
            # Format timestamp
            try:
                dt = datetime.fromisoformat(created.replace('Z', '+00:00'))
                time_str = dt.strftime("%Y-%m-%d %H:%M")
            except:
                time_str = created

            # Truncate long comments
            preview = body[:200] + "..." if len(body) > 200 else body
            
            comments_text += f"**{author}** ({time_str}):\n{preview}\n\n"

        if len(comments) > 5:
            comments_text += f"_...and {len(comments) - 5} more comments_\n\n"

        comments_text += f"🔗 https://github.com/{repo}/issues/{issue_num}"

        # Handle long messages
        max_len = 3500
        if len(comments_text) <= max_len:
            await context.bot.edit_message_text(
                chat_id=update.effective_chat.id,
                message_id=msg.message_id,
                text=comments_text,
                parse_mode='Markdown',
                disable_web_page_preview=True
            )
        else:
            chunks = [comments_text[i:i + max_len] for i in range(0, len(comments_text), max_len)]
            await context.bot.edit_message_text(
                chat_id=update.effective_chat.id,
                message_id=msg.message_id,
                text=chunks[0],
                parse_mode='Markdown',
                disable_web_page_preview=True
            )
            for part in chunks[1:]:
                await context.bot.send_message(
                    chat_id=update.effective_chat.id,
                    text=part,
                    parse_mode='Markdown',
                    disable_web_page_preview=True
                )

    except Exception as e:
        await context.bot.edit_message_text(
            chat_id=update.effective_chat.id,
            message_id=msg.message_id,
            text=f"❌ Error: {e}"
        )


async def respond_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Post a response to an issue and automatically continue the agent."""
    logger.info(f"Respond requested by user: {update.effective_user.id}")
    if ALLOWED_USER_ID and update.effective_user.id != ALLOWED_USER_ID:
        logger.warning(f"Unauthorized access attempt by ID: {update.effective_user.id}")
        return

    if not context.args:
        await _prompt_project_selection(update, context, "respond")
        return

    project_key, issue_num, rest = await _ensure_project_issue(update, context, "respond")
    if not project_key:
        return
    if not rest:
        await update.effective_message.reply_text("⚠️ Please include a response message.")
        return

    response_text = " ".join(rest)

    msg = await update.effective_message.reply_text(f"📝 Posting response to issue #{issue_num}...")

    try:
        # Post comment to GitHub issue
        task_file = find_task_file_by_issue(issue_num)
        details = None
        repo = None
        if not task_file:
            repo = PROJECT_CONFIG[project_key].get("github_repo", GITHUB_REPO)
            details = get_issue_details(issue_num, repo=repo)
            if details:
                body = details.get("body", "")
                match = re.search(r"Task File:\s*`([^`]+)`", body)
                task_file = match.group(1) if match else None

        if not task_file or not os.path.exists(task_file):
            await update.effective_message.reply_text(
                f"⚠️ Posted comment but couldn't find task file to continue agent."
            )
            return

        project_name, config = resolve_project_config_from_task(task_file)
        if not config or not config.get("agents_dir"):
            await update.effective_message.reply_text(
                f"⚠️ Posted comment but no agents config for project."
            )
            return

        repo = config.get("github_repo", GITHUB_REPO)

        plugin = _get_direct_issue_plugin(repo)
        if not plugin or not plugin.add_comment(issue_num, response_text):
            await context.bot.edit_message_text(
                chat_id=update.effective_chat.id,
                message_id=msg.message_id,
                text=f"❌ Failed to post response to issue #{issue_num}."
            )
            return

        await context.bot.edit_message_text(
            chat_id=update.effective_chat.id,
            message_id=msg.message_id,
            text=f"✅ Response posted to issue #{issue_num}.\n\n🤖 Continuing agent..."
        )

        # Now automatically continue the agent with the user's input
        if not details:
            details = get_issue_details(issue_num, repo=repo)
            if not details:
                await update.effective_message.reply_text(
                    f"⚠️ Posted comment but couldn't fetch issue details to continue agent."
                )
                return

        with open(task_file, "r") as f:
            content = f.read()

        type_match = re.search(r"\*\*Type:\*\*\s*(.+)", content)
        task_type = type_match.group(1).strip().lower() if type_match else "feature"

        tier_name, _, _ = get_sop_tier(task_type)
        issue_url = f"https://github.com/{repo}/issues/{issue_num}"

        agents_abs = os.path.join(BASE_DIR, config["agents_dir"])
        workspace_abs = os.path.join(BASE_DIR, config["workspace"])

        # Launch agent with continuation that includes the user's response
        continuation_prompt = (
            f"@Ghabs has provided input:\n\n{response_text}\n\n"
            f"Please proceed with the next step of the workflow."
        )

        log_subdir = project_name
        pid, tool_used = invoke_copilot_agent(
            agents_dir=agents_abs,
            workspace_dir=workspace_abs,
            issue_url=issue_url,
            tier_name=tier_name,
            task_content=content,
            continuation=True,
            continuation_prompt=continuation_prompt,
            log_subdir=log_subdir,
            project_name=log_subdir
        )

        if pid:
            await update.effective_message.reply_text(
                f"✅ Agent resumed for issue #{issue_num} (PID: {pid}, Tool: {tool_used})\n\n"
                f"Check /logs {issue_num} to monitor progress.\n\n"
                f"🔗 https://github.com/{repo}/issues/{issue_num}"
            )
        else:
            await update.effective_message.reply_text(
                f"⚠️ Response posted but failed to continue agent.\n"
                f"Use /continue {issue_num} to resume manually.\n\n"
                f"🔗 https://github.com/{repo}/issues/{issue_num}"
            )

    except subprocess.TimeoutExpired:
        await context.bot.edit_message_text(
            chat_id=update.effective_chat.id,
            message_id=msg.message_id,
            text=f"❌ Timeout posting comment to issue #{issue_num}"
        )
    except subprocess.CalledProcessError as e:
        error = e.stderr if e.stderr else str(e)
        await context.bot.edit_message_text(
            chat_id=update.effective_chat.id,
            message_id=msg.message_id,
            text=f"❌ Failed to post comment: {error}"
        )
    except Exception as e:
        await context.bot.edit_message_text(
            chat_id=update.effective_chat.id,
            message_id=msg.message_id,
            text=f"❌ Error: {e}"
        )


async def inline_keyboard_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle inline keyboard button presses from notifications."""
    query = update.callback_query
    await query.answer()
    
    if not query.data:
        return
    
    # Parse callback data: action_issuenum
    parts = query.data.split('_', 1)
    if len(parts) < 2:
        return
    
    action = parts[0]
    issue_num = parts[1]
    
    logger.info(f"Inline keyboard action: {action} for issue #{issue_num}")
    
    # Map actions to handler functions
    action_handlers = {
        'logs': logs_handler,
        'logsfull': logsfull_handler,
        'status': status_handler,
        'pause': pause_handler,
        'resume': resume_handler,
        'stop': stop_handler,
        'audit': audit_handler,
        'reprocess': reprocess_handler,
    }
    
    # For actions that need issue number in context.args
    if action in action_handlers:
        context.user_data["pending_command"] = action
        context.user_data["pending_issue"] = issue_num
        await _prompt_project_selection(update, context, action)
    elif action == 'respond':
        # For respond, just show instructions
        await query.edit_message_text(
            f"✍️ To respond to issue #{issue_num}, use:\\n\\n"
            f"`/respond {issue_num} <your message>`\\n\\n"
            f"Example:\\n"
            f"`/respond {issue_num} Approved, proceed with implementation`",
            parse_mode='Markdown'
        )
    elif action == 'approve':
        # Auto-approve implementation
        context.args = [issue_num]
        # Simulate approval by posting comment
        await query.edit_message_text(f"✅ Approving implementation for issue #{issue_num}...")
        
        try:
            plugin = _get_direct_issue_plugin(GITHUB_REPO)
            if not plugin or not plugin.add_comment(
                issue_num,
                "✅ Implementation approved by @Ghabs. Please proceed.",
            ):
                await query.edit_message_text(f"❌ Error approving issue #{issue_num}")
                return
            await query.edit_message_text(
                f"✅ Implementation approved for issue #{issue_num}\\n\\n"
                f"Agent will continue automatically.",
                parse_mode='Markdown'
            )
        except Exception as e:
            await query.edit_message_text(f"❌ Error approving: {e}")
    elif action == 'reject':
        # Reject implementation
        context.args = [issue_num]
        await query.edit_message_text(f"❌ Rejecting implementation for issue #{issue_num}...")
        
        try:
            plugin = _get_direct_issue_plugin(GITHUB_REPO)
            if not plugin or not plugin.add_comment(
                issue_num,
                "❌ Implementation rejected by @Ghabs. Please revise.",
            ):
                await query.edit_message_text(f"❌ Error rejecting issue #{issue_num}")
                return
            await query.edit_message_text(
                f"❌ Implementation rejected for issue #{issue_num}\\n\\n"
                f"Agent has been notified.",
                parse_mode='Markdown'
            )
        except Exception as e:
            await query.edit_message_text(f"❌ Error rejecting: {e}")
    elif action == 'wfapprove':
        # Approve a workflow approval gate: callback_data = wfapprove_{issue}_{step}
        parts2 = issue_num.split('_', 1)
        real_issue = parts2[0]
        step_num = parts2[1] if len(parts2) > 1 else "?"
        await query.edit_message_text(
            f"✅ Approving workflow step {step_num} for issue #{real_issue}..."
        )
        try:
            workflow_plugin = get_workflow_state_plugin(
                **_WORKFLOW_STATE_PLUGIN_KWARGS,
                cache_key="workflow:state-engine",
            )
            approved_by = update.effective_user.username or str(update.effective_user.id)
            if not workflow_plugin or not await workflow_plugin.approve_step(real_issue, approved_by):
                await query.edit_message_text(
                    f"❌ No workflow found for issue #{real_issue}"
                )
                return
            await query.edit_message_text(
                f"✅ Step {step_num} approved for issue #{real_issue}\\n\\n"
                f"Workflow will continue automatically.",
                parse_mode='Markdown'
            )
        except Exception as e:
            await query.edit_message_text(f"❌ Error approving workflow step: {e}")
    elif action == 'wfdeny':
        # Deny a workflow approval gate: callback_data = wfdeny_{issue}_{step}
        parts2 = issue_num.split('_', 1)
        real_issue = parts2[0]
        step_num = parts2[1] if len(parts2) > 1 else "?"
        await query.edit_message_text(
            f"❌ Denying workflow step {step_num} for issue #{real_issue}..."
        )
        try:
            workflow_plugin = get_workflow_state_plugin(
                **_WORKFLOW_STATE_PLUGIN_KWARGS,
                cache_key="workflow:state-engine",
            )
            denied_by = update.effective_user.username or str(update.effective_user.id)
            if not workflow_plugin or not await workflow_plugin.deny_step(
                real_issue,
                denied_by,
                reason="Denied via Telegram",
            ):
                await query.edit_message_text(
                    f"❌ No workflow found for issue #{real_issue}"
                )
                return
            await query.edit_message_text(
                f"❌ Step {step_num} denied for issue #{real_issue}\\n\\n"
                f"Workflow has been stopped.",
                parse_mode='Markdown'
            )
        except Exception as e:
            await query.edit_message_text(f"❌ Error denying workflow step: {e}")


# --- MAIN ---
if __name__ == '__main__':
    app = ApplicationBuilder().token(TELEGRAM_TOKEN).build()
    
    # Initialize report scheduler (start it after app runs)
    report_scheduler = None
    if os.getenv('ENABLE_SCHEDULED_REPORTS', 'true').lower() == 'true':
        report_scheduler = ReportScheduler(bot=app.bot, chat_id=TELEGRAM_CHAT_ID)
        logger.info("📊 Scheduled reports will be enabled after startup")
    
    # Initialize alerting system (start it after app runs)
    alerting_system = None
    if os.getenv('ENABLE_ALERTING', 'true').lower() == 'true':
        alerting_system = init_alerting_system(bot=app.bot, chat_id=TELEGRAM_CHAT_ID)
        logger.info("🚨 Alerting system will be enabled after startup")
    
    # Register commands on startup (Telegram client menu)
    original_post_init = on_startup
    
    async def post_init_with_scheduler(application):
        """Post init that also starts the report scheduler and alerting system."""
        await original_post_init(application)
        if report_scheduler:
            report_scheduler.start()
            logger.info("📊 Scheduled reports started")
        if alerting_system:
            alerting_system.start()
            logger.info("🚨 Alerting system started")
    
    app.post_init = post_init_with_scheduler

    conv_handler = ConversationHandler(
        entry_points=[CommandHandler("new", start_selection)],
        states={
            SELECT_PROJECT: [
                CallbackQueryHandler(
                    project_selected,
                    pattern=r'^(case_italia|wallible|biome|nexus)$'
                ),
                CallbackQueryHandler(flow_close_handler, pattern=r'^flow:close$')
            ],
            SELECT_TYPE: [
                CallbackQueryHandler(
                    type_selected,
                    pattern=r'^(feature|feature-simple|bug|hotfix|release|chore|improvement|improvement-simple)$'
                ),
                CallbackQueryHandler(flow_close_handler, pattern=r'^flow:close$')
            ],
            INPUT_TASK: [MessageHandler(filters.TEXT | filters.VOICE, save_task)]
        },
        fallbacks=[CommandHandler("cancel", cancel)],
        per_message=False
    )

    app.add_handler(conv_handler)
    app.add_handler(CommandHandler("start", start_handler))
    app.add_handler(CommandHandler("help", help_handler))
    app.add_handler(CommandHandler("menu", menu_handler))
    app.add_handler(CommandHandler("cancel", cancel))
    app.add_handler(CommandHandler("status", status_handler))
    app.add_handler(CommandHandler("active", active_handler))
    app.add_handler(CommandHandler("progress", progress_handler))
    app.add_handler(CommandHandler("track", track_handler))
    app.add_handler(CommandHandler("untrack", untrack_handler))
    app.add_handler(CommandHandler("myissues", myissues_handler))
    app.add_handler(CommandHandler("logs", logs_handler))
    app.add_handler(CommandHandler("logsfull", logsfull_handler))
    app.add_handler(CommandHandler("tail", tail_handler))
    app.add_handler(CommandHandler("audit", audit_handler))
    app.add_handler(CommandHandler("stats", stats_handler))
    app.add_handler(CommandHandler("comments", comments_handler))
    app.add_handler(CommandHandler("reprocess", reprocess_handler))
    app.add_handler(CommandHandler("continue", continue_handler))
    app.add_handler(CommandHandler("kill", kill_handler))
    app.add_handler(CommandHandler("pause", pause_handler))
    app.add_handler(CommandHandler("resume", resume_handler))
    app.add_handler(CommandHandler("stop", stop_handler))
    app.add_handler(CommandHandler("agents", agents_handler))
    app.add_handler(CommandHandler("direct", direct_handler))
    app.add_handler(CommandHandler("respond", respond_handler))
    app.add_handler(CommandHandler("assign", assign_handler))
    app.add_handler(CommandHandler("implement", implement_handler))
    app.add_handler(CommandHandler("prepare", prepare_handler))
    # Menu navigation callbacks
    app.add_handler(CallbackQueryHandler(menu_callback_handler, pattern=r'^menu:'))
    app.add_handler(CallbackQueryHandler(project_picker_handler, pattern=r'^pickcmd:'))
    app.add_handler(CallbackQueryHandler(issue_picker_handler, pattern=r'^pickissue'))
    app.add_handler(CallbackQueryHandler(monitor_project_picker_handler, pattern=r'^pickmonitor:'))
    app.add_handler(CallbackQueryHandler(close_flow_handler, pattern=r'^flow:close$'))
    # Inline keyboard callback handler (must be before ConversationHandler callbacks)
    app.add_handler(CallbackQueryHandler(inline_keyboard_handler, pattern=r'^(logs|logsfull|status|pause|resume|stop|audit|reprocess|respond|approve|reject)_'))
    # Exclude commands from the auto-router catch-all
    app.add_handler(MessageHandler((filters.TEXT | filters.VOICE) & (~filters.COMMAND), hands_free_handler))

    print("Nexus Online...")
    app.run_polling(drop_pending_updates=True, allowed_updates=Update.ALL_TYPES)

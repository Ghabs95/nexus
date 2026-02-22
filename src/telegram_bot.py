import glob
import asyncio
import json
import logging
import os
import re
import subprocess
import sys
import time
from typing import Any, Dict, List, Optional, Tuple
from datetime import datetime, timedelta
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, BotCommand, ReplyKeyboardMarkup, ReplyKeyboardRemove
from telegram.ext import (
    ApplicationBuilder, ContextTypes, CommandHandler,
    MessageHandler, CallbackQueryHandler, ConversationHandler, filters
)

# Import configuration from centralized config module
from config import (
    TELEGRAM_TOKEN, TELEGRAM_ALLOWED_USER_IDS, BASE_DIR,
    DATA_DIR, TRACKED_ISSUES_FILE, get_github_repo, get_github_repos, get_default_github_repo,
    get_default_project,
    PROJECT_CONFIG, ensure_data_dir,
    TELEGRAM_BOT_LOG_FILE, TELEGRAM_CHAT_ID, ORCHESTRATOR_CONFIG, LOGS_DIR,
    get_inbox_dir, get_tasks_active_dir, get_tasks_closed_dir, get_tasks_logs_dir, get_nexus_dir_name,
    AI_PERSONA,
    NEXUS_CORE_STORAGE_DIR,
)
from services.memory_service import get_chat_history, append_message
from state_manager import StateManager
from audit_store import AuditStore
from models import WorkflowState
from commands.workflow import (
    pause_handler as workflow_pause_handler,
    resume_handler as workflow_resume_handler,
    stop_handler as workflow_stop_handler,
)
from runtime.agent_launcher import invoke_copilot_agent, get_sop_tier_from_issue
from inbox_processor import get_sop_tier, _normalize_agent_reference
from nexus.core.completion import scan_for_completions
from orchestration.ai_orchestrator import get_orchestrator
from nexus.plugins.builtin.ai_runtime_plugin import AIProvider
from orchestration.plugin_runtime import get_profiled_plugin, get_runtime_ops_plugin, get_workflow_state_plugin
from services.workflow_signal_sync import (
    extract_structured_completion_signals,
    read_latest_local_completion,
    write_local_completion_from_signal,
)
from services.workflow_ops_service import (
    build_workflow_snapshot,
    reconcile_issue_from_signals,
)
from services.workflow_control_service import (
    kill_issue_agent,
    prepare_continue_context,
)
from integrations.git_platform_utils import build_issue_url, resolve_repo
from handlers.workflow_command_handlers import (
    WorkflowHandlerDeps,
    continue_handler as workflow_continue_handler,
    forget_handler as workflow_forget_handler,
    kill_handler as workflow_kill_handler,
    pause_handler as workflow_pause_picker_handler,
    reconcile_handler as workflow_reconcile_handler,
    reprocess_handler as workflow_reprocess_handler,
    resume_handler as workflow_resume_picker_handler,
    stop_handler as workflow_stop_picker_handler,
    wfstate_handler as workflow_wfstate_handler,
)
from handlers.monitoring_command_handlers import (
    MonitoringHandlerDeps,
    active_handler as monitoring_active_handler,
    fuse_handler as monitoring_fuse_handler,
    logs_handler as monitoring_logs_handler,
    logsfull_handler as monitoring_logsfull_handler,
    status_handler as monitoring_status_handler,
    tail_handler as monitoring_tail_handler,
    tailstop_handler as monitoring_tailstop_handler,
)
from handlers.issue_command_handlers import (
    IssueHandlerDeps,
    assign_handler as issue_assign_handler,
    comments_handler as issue_comments_handler,
    implement_handler as issue_implement_handler,
    myissues_handler as issue_myissues_handler,
    prepare_handler as issue_prepare_handler,
    respond_handler as issue_respond_handler,
    track_handler as issue_track_handler,
    untrack_handler as issue_untrack_handler,
)
from handlers.ops_command_handlers import (
    OpsHandlerDeps,
    agents_handler as ops_agents_handler,
    audit_handler as ops_audit_handler,
    direct_handler as ops_direct_handler,
    stats_handler as ops_stats_handler,
)
from handlers.callback_command_handlers import (
    CallbackHandlerDeps,
    close_flow_handler as callback_close_flow_handler,
    flow_close_handler as callback_flow_close_handler,
    inline_keyboard_handler as callback_inline_keyboard_handler,
    issue_picker_handler as callback_issue_picker_handler,
    menu_callback_handler as callback_menu_callback_handler,
    monitor_project_picker_handler as callback_monitor_project_picker_handler,
    project_picker_handler as callback_project_picker_handler,
)
from handlers.chat_command_handlers import chat_menu_handler, chat_callback_handler
from error_handling import format_error_for_user, run_command_with_retry
from analytics import get_stats_report
from rate_limiter import get_rate_limiter, RateLimit
from report_scheduler import ReportScheduler
from user_manager import get_user_manager
from alerting import init_alerting_system
from handlers.inbox_routing_handler import process_inbox_task, save_resolved_task, PROJECTS, TYPES


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


def _extract_json_dict(text: str) -> Optional[Dict[str, Any]]:
    if not text:
        return None

    candidates: List[str] = [text.strip()]

    fenced_blocks = re.findall(r"```(?:json)?\s*([\s\S]*?)\s*```", text, flags=re.IGNORECASE)
    candidates.extend(block.strip() for block in fenced_blocks if block.strip())

    first_brace = text.find("{")
    last_brace = text.rfind("}")
    if first_brace != -1 and last_brace > first_brace:
        candidates.append(text[first_brace:last_brace + 1].strip())

    seen = set()
    for candidate in candidates:
        if not candidate or candidate in seen:
            continue
        seen.add(candidate)
        try:
            parsed = json.loads(candidate)
            if isinstance(parsed, dict):
                return parsed
        except json.JSONDecodeError:
            continue
    return None


class _SecretRedactingFilter(logging.Filter):
    """Redact sensitive values from log messages."""

    def __init__(self, secrets: List[str]):
        super().__init__()
        self._secrets = [s for s in secrets if s]

    def _redact(self, value):
        if isinstance(value, str):
            redacted = value
            for secret in self._secrets:
                redacted = redacted.replace(secret, "[REDACTED_BOT_TOKEN]")
            return redacted
        if isinstance(value, tuple):
            return tuple(self._redact(v) for v in value)
        if isinstance(value, list):
            return [self._redact(v) for v in value]
        if isinstance(value, dict):
            return {k: self._redact(v) for k, v in value.items()}
        return value

    def filter(self, record: logging.LogRecord) -> bool:
        record.msg = self._redact(record.msg)
        record.args = self._redact(record.args)
        return True


_redaction_filter = _SecretRedactingFilter([TELEGRAM_TOKEN or ""])
for _handler in logging.getLogger().handlers:
    _handler.addFilter(_redaction_filter)

# Long-polling calls Telegram getUpdates repeatedly by design.
# Keep these transport logs at WARNING to avoid noisy INFO output.
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)

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


def _workflow_handler_deps() -> WorkflowHandlerDeps:
    return WorkflowHandlerDeps(
        allowed_user_ids=TELEGRAM_ALLOWED_USER_IDS,
        default_repo=GITHUB_REPO,
        project_config=PROJECT_CONFIG,
        workflow_state_plugin_kwargs=_WORKFLOW_STATE_PLUGIN_KWARGS,
        prompt_project_selection=_prompt_project_selection,
        ensure_project_issue=_ensure_project_issue,
        find_task_file_by_issue=find_task_file_by_issue,
        project_repo=_project_repo,
        get_issue_details=get_issue_details,
        resolve_project_config_from_task=resolve_project_config_from_task,
        invoke_copilot_agent=invoke_copilot_agent,
        get_sop_tier_from_issue=get_sop_tier_from_issue,
        get_sop_tier=get_sop_tier,
        get_last_tier_for_issue=StateManager.get_last_tier_for_issue,
        prepare_continue_context=prepare_continue_context,
        kill_issue_agent=kill_issue_agent,
        get_runtime_ops_plugin=get_runtime_ops_plugin,
        get_workflow_state_plugin=get_workflow_state_plugin,
        scan_for_completions=scan_for_completions,
        normalize_agent_reference=_normalize_agent_reference,
        get_expected_running_agent_from_workflow=_get_expected_running_agent_from_workflow,
        reconcile_issue_from_signals=reconcile_issue_from_signals,
        get_direct_issue_plugin=_get_direct_issue_plugin,
        extract_structured_completion_signals=_extract_structured_completion_signals,
        write_local_completion_from_signal=_write_local_completion_from_signal,
        build_workflow_snapshot=build_workflow_snapshot,
        read_latest_local_completion=_read_latest_local_completion,
        workflow_pause_handler=workflow_pause_handler,
        workflow_resume_handler=workflow_resume_handler,
        workflow_stop_handler=workflow_stop_handler,
    )


def _monitoring_handler_deps() -> MonitoringHandlerDeps:
    from runtime.nexus_agent_runtime import get_retry_fuse_status

    return MonitoringHandlerDeps(
        logger=logger,
        allowed_user_ids=TELEGRAM_ALLOWED_USER_IDS,
        base_dir=BASE_DIR,
        project_config=PROJECT_CONFIG,
        types_map=TYPES,
        prompt_monitor_project_selection=_prompt_monitor_project_selection,
        prompt_project_selection=_prompt_project_selection,
        ensure_project_issue=_ensure_project_issue,
        normalize_project_key=_normalize_project_key,
        iter_project_keys=_iter_project_keys,
        get_project_label=_get_project_label,
        get_project_root=_get_project_root,
        get_project_logs_dir=_get_project_logs_dir,
        project_repo=_project_repo,
        get_issue_details=get_issue_details,
        get_inbox_dir=get_inbox_dir,
        get_tasks_active_dir=get_tasks_active_dir,
        get_tasks_closed_dir=get_tasks_closed_dir,
        extract_issue_number_from_file=extract_issue_number_from_file,
        build_issue_url=build_issue_url,
        find_task_file_by_issue=find_task_file_by_issue,
        find_issue_log_files=find_issue_log_files,
        read_latest_log_tail=read_latest_log_tail,
        search_logs_for_issue=search_logs_for_issue,
        read_latest_log_full=read_latest_log_full,
        read_log_matches=read_log_matches,
        active_tail_sessions=active_tail_sessions,
        active_tail_tasks=active_tail_tasks,
        get_retry_fuse_status=get_retry_fuse_status,
        normalize_agent_reference=_normalize_agent_reference,
        get_expected_running_agent_from_workflow=_get_expected_running_agent_from_workflow,
        get_direct_issue_plugin=_get_direct_issue_plugin,
        extract_structured_completion_signals=_extract_structured_completion_signals,
        read_latest_local_completion=_read_latest_local_completion,
        build_workflow_snapshot=build_workflow_snapshot,
    )


def _issue_handler_deps() -> IssueHandlerDeps:
    return IssueHandlerDeps(
        logger=logger,
        allowed_user_ids=TELEGRAM_ALLOWED_USER_IDS,
        base_dir=BASE_DIR,
        default_repo=GITHUB_REPO,
        prompt_project_selection=_prompt_project_selection,
        ensure_project_issue=_ensure_project_issue,
        project_repo=_project_repo,
        project_issue_url=_project_issue_url,
        get_issue_details=get_issue_details,
        get_direct_issue_plugin=_get_direct_issue_plugin,
        resolve_project_config_from_task=resolve_project_config_from_task,
        invoke_copilot_agent=invoke_copilot_agent,
        get_sop_tier=get_sop_tier,
        find_task_file_by_issue=find_task_file_by_issue,
        resolve_repo=resolve_repo,
        build_issue_url=build_issue_url,
        user_manager=user_manager,
        save_tracked_issues=save_tracked_issues,
        tracked_issues_ref=tracked_issues,
        default_issue_url=_default_issue_url,
        get_project_label=_get_project_label,
        track_short_projects=["casit", "wlbl", "bm"],
    )


def _ops_handler_deps() -> OpsHandlerDeps:
    return OpsHandlerDeps(
        logger=logger,
        allowed_user_ids=TELEGRAM_ALLOWED_USER_IDS,
        base_dir=BASE_DIR,
        project_config=PROJECT_CONFIG,
        prompt_project_selection=_prompt_project_selection,
        ensure_project_issue=_ensure_project_issue,
        get_project_label=_get_project_label,
        get_stats_report=get_stats_report,
        format_error_for_user=format_error_for_user,
        get_audit_history=AuditStore.get_audit_history,
        get_agents_for_project=get_agents_for_project,
        get_github_repo=get_github_repo,
        get_direct_issue_plugin=_get_direct_issue_plugin,
    )


def _callback_handler_deps() -> CallbackHandlerDeps:
    return CallbackHandlerDeps(
        logger=logger,
        github_repo=GITHUB_REPO,
        prompt_issue_selection=_prompt_issue_selection,
        prompt_project_selection=_prompt_project_selection,
        dispatch_command=_dispatch_command,
        get_project_label=_get_project_label,
        status_handler=status_handler,
        active_handler=active_handler,
        get_direct_issue_plugin=_get_direct_issue_plugin,
        get_workflow_state_plugin=get_workflow_state_plugin,
        workflow_state_plugin_kwargs=_WORKFLOW_STATE_PLUGIN_KWARGS,
        action_handlers={
            "logs": logs_handler,
            "logsfull": logsfull_handler,
            "status": status_handler,
            "pause": pause_handler,
            "resume": resume_handler,
            "stop": stop_handler,
            "audit": audit_handler,
            "reprocess": reprocess_handler,
        },
    )


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


# Moved `_extract_json_dict` and `_refine_task_description` to inbox_routing_handler.py

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


def _get_expected_running_agent_from_workflow(issue_num: str) -> Optional[str]:
    """Return the current RUNNING workflow agent for an issue, if available."""
    workflow_id = StateManager.get_workflow_id_for_issue(str(issue_num))
    if not workflow_id:
        return None

    workflow_path = os.path.join(
        NEXUS_CORE_STORAGE_DIR,
        "workflows",
        f"{workflow_id}.json",
    )
    try:
        with open(workflow_path, "r", encoding="utf-8") as handle:
            payload = json.load(handle)
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return None

    state = str(payload.get("state", "")).strip().lower()
    if state in {"completed", "failed", "cancelled"}:
        return None

    for step in payload.get("steps", []):
        if not isinstance(step, dict):
            continue
        if str(step.get("status", "")).strip().lower() != "running":
            continue

        agent = step.get("agent")
        if not isinstance(agent, dict):
            continue

        name = str(agent.get("name", "")).strip()
        display_name = str(agent.get("display_name", "")).strip()
        if name:
            return name
        if display_name:
            return display_name
    return None


def _extract_structured_completion_signals(comments: List[dict]) -> List[Dict[str, str]]:
    return extract_structured_completion_signals(comments)


def _write_local_completion_from_signal(project_key: str, issue_num: str, signal: Dict[str, str]) -> str:
    return write_local_completion_from_signal(
        BASE_DIR,
        get_nexus_dir_name(),
        project_key,
        issue_num,
        signal,
        key_findings=[
            "Workflow/comment/local completion drift reconciled via /reconcile",
            f"Source comment id: {signal.get('comment_id', 'n/a')}",
        ],
    )


def _read_latest_local_completion(issue_num: str) -> Optional[Dict[str, Any]]:
    return read_latest_local_completion(BASE_DIR, get_nexus_dir_name(), issue_num)


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
        if not isinstance(cfg, dict):
            continue
        repo = cfg.get("git_repo")
        repo_list = cfg.get("git_repos")
        has_primary = isinstance(repo, str) and bool(repo.strip())
        has_multi = isinstance(repo_list, list) and any(
            isinstance(item, str) and item.strip() for item in repo_list
        )
        if has_primary or has_multi:
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


def _project_repo(project_key: str) -> str:
    config = PROJECT_CONFIG.get(project_key)
    return resolve_repo(config if isinstance(config, dict) else None, GITHUB_REPO)


def _project_issue_url(project_key: str, issue_num: str) -> str:
    config = PROJECT_CONFIG.get(project_key)
    cfg = config if isinstance(config, dict) else None
    return build_issue_url(_project_repo(project_key), issue_num, cfg)


def _default_issue_url(issue_num: str) -> str:
    try:
        project_key = get_default_project()
        return _project_issue_url(project_key, issue_num)
    except Exception:
        return f"https://github.com/{GITHUB_REPO}/issues/{issue_num}"


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
    repo = config.get("git_repo")
    if (not isinstance(repo, str) or not repo.strip()) and isinstance(config.get("git_repos"), list):
        repos = [r for r in config.get("git_repos", []) if isinstance(r, str) and r.strip()]
        repo = repos[0] if repos else None
    if not repo:
        repos = get_github_repos(project_key)
        repo = repos[0] if repos else None
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
        "fuse": fuse_handler,
        "audit": audit_handler,
        "comments": comments_handler,
        "wfstate": wfstate_handler,
        "reprocess": reprocess_handler,
        "reconcile": reconcile_handler,
        "continue": continue_handler,
        "forget": forget_handler,
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
        "agents": agents_handler,
    }


async def _dispatch_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    command: str,
    project_key: str,
    issue_num: str,
    rest: Optional[List[str]] = None,
) -> None:
    project_only_commands = {"agents"}
    if command in project_only_commands:
        context.args = [project_key] + (rest or [])
    else:
        context.args = [project_key, issue_num] + (rest or [])
    handler = _command_handler_map().get(command)
    if handler:
        await handler(update, context)
    else:
        await update.effective_message.reply_text("Unsupported command.")


async def project_picker_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await callback_project_picker_handler(update, context, _callback_handler_deps())


async def issue_picker_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await callback_issue_picker_handler(update, context, _callback_handler_deps())


async def monitor_project_picker_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await callback_monitor_project_picker_handler(update, context, _callback_handler_deps())


async def close_flow_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await callback_close_flow_handler(update, context, _callback_handler_deps())

# --- STATES ---
SELECT_PROJECT, SELECT_TYPE, INPUT_TASK = range(3)

tracked_issues = load_tracked_issues()  # Load on startup
active_tail_sessions: dict[tuple[int, int], str] = {}
active_tail_tasks: dict[tuple[int, int], asyncio.Task] = {}


# --- 0. HELP & INFO ---
async def help_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Lists available commands and usage info."""
    logger.info(f"Help triggered by user: {update.effective_user.id}")
    if TELEGRAM_ALLOWED_USER_IDS and update.effective_user.id not in TELEGRAM_ALLOWED_USER_IDS:
        logger.warning(f"Unauthorized access attempt by ID: {update.effective_user.id}")
        return

    help_text = (
        "🤖 **Nexus Commands**\n\n"
        "Use /menu for a categorized, button-driven view.\n\n"
        "✨ **Task Creation:**\n"
        "/menu - Open command menu\n"
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
        "/tail <project> <issue#> [lines] [seconds] - Follow live log tail\n"
        "/tailstop - Stop current live tail session\n"
        "/fuse <project> <issue#> - View retry fuse state\n"
        "/audit <project> <issue#> - View workflow audit trail\n"
        "/stats [days] - View system analytics (default: 30 days)\n"
        "/comments <project> <issue#> - View issue comments\n\n"
        "🔁 **Recovery & Control:**\n"
        "/reprocess <project> <issue#> - Re-run agent processing\n"
        "/wfstate <project> <issue#> - Show workflow state and drift snapshot\n"
        "/reconcile <project> <issue#> - Reconcile workflow/comment/local state\n"
        "/continue <project> <issue#> - Check stuck agent status\n"
        "/forget <project> <issue#> - Permanently clear local state for an issue\n"
        "/kill <project> <issue#> - Stop running agent process\n"
        "/pause <project> <issue#> - Pause auto-chaining (agents work but no auto-launch)\n"
        "/resume <project> <issue#> - Resume auto-chaining\n"
        "/stop <project> <issue#> - Stop workflow completely (closes issue, kills agent)\n"
        "/respond <project> <issue#> <text> - Respond to agent questions\n\n"
        "🤝 **Agent Management:**\n"
        "/agents <project> - List all agents for a project\n"
        "/direct <project> <@agent> <message> - Send direct request to an agent\n\n"
        "🔧 **Git Platform Management:**\n"
        "/assign <project> <issue#> - Assign issue to yourself\n"
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
    if TELEGRAM_ALLOWED_USER_IDS and update.effective_user.id not in TELEGRAM_ALLOWED_USER_IDS:
        logger.warning(f"Unauthorized access attempt by ID: {update.effective_user.id}")
        return

    keyboard = [
        [InlineKeyboardButton("✨ Task Creation", callback_data="menu:tasks")],
        [InlineKeyboardButton("📊 Monitoring", callback_data="menu:monitor")],
        [InlineKeyboardButton("🔁 Workflow Control", callback_data="menu:workflow")],
        [InlineKeyboardButton("🤝 Agents", callback_data="menu:agents")],
        [InlineKeyboardButton("🔧 Git Platform", callback_data="menu:github")],
        [InlineKeyboardButton("ℹ️ Help", callback_data="menu:help")],
        [InlineKeyboardButton("❌ Close", callback_data="menu:close")]
    ]
    await update.effective_message.reply_text(
        "📍 **Nexus Menu**\nChoose a category:",
        reply_markup=InlineKeyboardMarkup(keyboard),
        parse_mode='Markdown'
    )


async def menu_callback_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await callback_menu_callback_handler(update, context, _callback_handler_deps())


async def start_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Welcome message and persistent reply keyboard."""
    logger.info(f"Start triggered by user: {update.effective_user.id}")
    if TELEGRAM_ALLOWED_USER_IDS and update.effective_user.id not in TELEGRAM_ALLOWED_USER_IDS:
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
        ["/menu"],
        ["/new"],
        ["/status"],
        ["/active"],
        ["/help"]
    ]
    reply_markup = ReplyKeyboardMarkup(keyboard, resize_keyboard=True, one_time_keyboard=False)

    await update.message.reply_text(welcome, reply_markup=reply_markup)


async def on_startup(application):
    """Register bot commands so they appear in the Telegram client menu."""
    cmds = [
        BotCommand("menu", "Open command menu"),
        BotCommand("new", "Start task creation"),
        # BotCommand("cancel", "Cancel current process"),
        BotCommand("status", "Show pending tasks"),
        BotCommand("active", "Show active tasks"),
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


def _refine_task_description(text: str, project_key: Optional[str] = None) -> str:
    """Refine task description using orchestrator with graceful fallback."""
    candidate_text = (text or "").strip()
    if not candidate_text:
        return ""

    try:
        logger.info("Refining description with orchestrator (len=%s)", len(candidate_text))
        refine_result = orchestrator.run_text_to_speech_analysis(
            text=candidate_text,
            task="refine_description",
            project_name=PROJECTS.get(project_key) if project_key else None,
        )
        refined = str(refine_result.get("text", "")).strip()
        if refined:
            return refined
    except Exception as exc:
        logger.warning("Failed to refine description: %s", exc)

    return candidate_text


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
        if TELEGRAM_ALLOWED_USER_IDS and update.effective_user.id not in TELEGRAM_ALLOWED_USER_IDS:
            logger.warning(f"Unauthorized access attempt by ID: {update.effective_user.id}")
            return

        if (not update.message.voice) and await _handle_pending_issue_input(update, context):
            return

        # Guard: Don't process commands as tasks
        if update.message.text and update.message.text.startswith('/'):
            logger.info(f"Ignoring command in hands_free_handler: {update.message.text}")
            return

        pending_project = context.user_data.get("pending_task_project_resolution")
        if pending_project:
            selected = _normalize_project_key((update.message.text or "").strip())
            if not selected or selected not in PROJECTS:
                options = ", ".join(sorted(PROJECTS.keys()))
                await update.message.reply_text(
                    f"Please reply with a valid project key: {options}"
                )
                return

            context.user_data.pop("pending_task_project_resolution", None)
            result = await save_resolved_task(pending_project, selected, str(update.message.message_id))
            
            await context.bot.edit_message_text(
                chat_id=update.effective_chat.id,
                message_id=status_msg.message_id,
                text=result["message"]
            )
            return

        text = ""
        status_msg = await update.message.reply_text("⚡ AI Listening...")

        # Get text from Audio or Text
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
        else:
            logger.info(f"Processing text input... text={update.message.text[:50]}")
            text = update.message.text

        logger.info(f"Detecting intent for: {text[:50]}...")
        intent_result = orchestrator.run_text_to_speech_analysis(text=text, task="detect_intent")
        
        if isinstance(intent_result, dict) and intent_result.get("text"):
            needs_reparse = intent_result.get("parse_error") or "intent" not in intent_result
            if needs_reparse:
                reparsed = _extract_json_dict(intent_result["text"])
                if reparsed:
                    intent_result = reparsed

        intent = intent_result.get("intent", "task")
        
        if intent == "conversation":
            user_id = update.effective_user.id
            history = get_chat_history(user_id)
            append_message(user_id, "user", text)
            
            await context.bot.edit_message_text(
                chat_id=update.effective_chat.id,
                message_id=status_msg.message_id,
                text="🤖 *Nexus:* Thinking...",
                parse_mode="Markdown"
            )
            
            chat_result = orchestrator.run_text_to_speech_analysis(
                text=text, 
                task="business_chat",
                history=history,
                persona=AI_PERSONA
            )
            
            reply_text = chat_result.get("text", "I'm offline right now, how can I help later?")
            append_message(user_id, "assistant", reply_text)
            
            await context.bot.edit_message_text(
                chat_id=update.effective_chat.id,
                message_id=status_msg.message_id,
                text=f"🤖 *Nexus*: \n\n{reply_text}",
                parse_mode="Markdown"
            )
            return

        result = await process_inbox_task(text, orchestrator, str(update.message.message_id))
        
        if not result["success"]:
            if "pending_resolution" in result:
                context.user_data["pending_task_project_resolution"] = result["pending_resolution"]
            
        await context.bot.edit_message_text(
            chat_id=update.effective_chat.id,
            message_id=status_msg.message_id,
            text=result["message"]
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
    if TELEGRAM_ALLOWED_USER_IDS and update.effective_user.id not in TELEGRAM_ALLOWED_USER_IDS: return
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

    # Generate task name using orchestrator (CLI only)
    task_name = ""
    try:
        logger.info("Generating task name with orchestrator (len=%s)", len(refined_text))
        name_result = orchestrator.run_text_to_speech_analysis(
            text=refined_text[:300],
            task="generate_name",
            project_name=PROJECTS.get(project)
        )
        task_name = name_result.get("text", "").strip().strip('"`\'')
    except Exception as e:
        logger.warning(f"Failed to generate task name: {e}")
        task_name = ""

    # Write File
    # Map project name to workspace (e.g., "nexus" → "ghabs")
    workspace = project
    if project in PROJECT_CONFIG:
        workspace = PROJECT_CONFIG[project].get("workspace", project)
    
    target_dir = get_inbox_dir(os.path.join(BASE_DIR, workspace), project)
    os.makedirs(target_dir, exist_ok=True)
    filename = f"{task_type}_{update.message.message_id}.md"

    with open(os.path.join(target_dir, filename), "w") as f:
        task_name_line = f"**Task Name:** {task_name}\n" if task_name else ""
        f.write(
            f"# {TYPES[task_type]}\n**Project:** {PROJECTS[project]}\n**Type:** {task_type}\n"
            f"{task_name_line}**Status:** Pending\n\n"
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
    return await callback_flow_close_handler(update, context, _callback_handler_deps())


# --- MONITORING COMMANDS ---
def extract_issue_number_from_file(file_path):
    """Extract issue number from task file content if present."""
    try:
        with open(file_path, "r") as f:
            content = f.read()
        match = re.search(r"\*\*Issue:\*\*\s*https?://[^\s`]+/(?:-/)?issues/(\d+)", content)
        if match:
            return match.group(1)
    except Exception as e:
        logger.warning(f"Failed to read issue number from {file_path}: {e}")
    return None


async def status_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Shows pending tasks in inbox folders."""
    await monitoring_status_handler(update, context, _monitoring_handler_deps())


async def progress_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Show active issues with current workflow step, agent type, tool, and duration."""
    logger.info(f"Progress requested by user: {update.effective_user.id}")
    if TELEGRAM_ALLOWED_USER_IDS and update.effective_user.id not in TELEGRAM_ALLOWED_USER_IDS:
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
    await monitoring_active_handler(update, context, _monitoring_handler_deps())


async def assign_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Assigns a GitHub issue to the user."""
    await issue_assign_handler(update, context, _issue_handler_deps())


@rate_limited("implement")
async def implement_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Requests Copilot agent implementation for an issue (approval workflow).

    Adds an `agent:requested` label and notifies `@ProjectLead` with a comment
    so they can approve (add `agent:approved`) or click "Code with agent mode".
    """
    await issue_implement_handler(update, context, _issue_handler_deps())


async def prepare_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Augments an issue with Copilot-friendly instructions and acceptance criteria."""
    await issue_prepare_handler(update, context, _issue_handler_deps())


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
    await issue_track_handler(update, context, _issue_handler_deps())


async def untrack_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Stop tracking an issue.
    
    Usage:
      /untrack <issue#>              - Stop global tracking
      /untrack <project> <issue#>    - Stop per-project tracking
    """
    await issue_untrack_handler(update, context, _issue_handler_deps())


async def myissues_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Show all issues tracked by the user across projects."""
    await issue_myissues_handler(update, context, _issue_handler_deps())


@rate_limited("logs")
async def logs_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Show combined timeline of GitHub activity and bot/processor logs for an issue."""
    await monitoring_logs_handler(update, context, _monitoring_handler_deps())


@rate_limited("logs")
async def logsfull_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Show combined timeline of GitHub activity and full log lines for an issue."""
    await monitoring_logsfull_handler(update, context, _monitoring_handler_deps())


@rate_limited("logs")
async def tail_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Show a short tail of the latest task log for an issue."""
    await monitoring_tail_handler(update, context, _monitoring_handler_deps())


@rate_limited("logs")
async def tailstop_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Stop an active live tail session for the current user/chat."""
    await monitoring_tailstop_handler(update, context, _monitoring_handler_deps())


@rate_limited("logs")
async def fuse_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Show retry-fuse status for an issue."""
    await monitoring_fuse_handler(update, context, _monitoring_handler_deps())


async def audit_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Display workflow audit trail for an issue (timeline of state changes, agent launches, etc)."""
    await ops_audit_handler(update, context, _ops_handler_deps())


@rate_limited("stats")
async def stats_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Display system analytics and performance statistics."""
    await ops_stats_handler(update, context, _ops_handler_deps())


@rate_limited("reprocess")
async def reprocess_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Re-run agent processing for an open issue."""
    await workflow_reprocess_handler(update, context, _workflow_handler_deps())


async def continue_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Continue/resume agent processing for an issue with a continuation prompt."""
    await workflow_continue_handler(update, context, _workflow_handler_deps())


async def forget_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Permanently forget local workflow/tracker state for an issue."""
    await workflow_forget_handler(update, context, _workflow_handler_deps())


async def kill_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Kill a running Copilot agent process."""
    await workflow_kill_handler(update, context, _workflow_handler_deps())


async def reconcile_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Reconcile workflow and local completion from structured GitHub comments."""
    await workflow_reconcile_handler(update, context, _workflow_handler_deps())


async def wfstate_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Show workflow state + signal drift snapshot for an issue."""
    await workflow_wfstate_handler(update, context, _workflow_handler_deps())


async def pause_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Pause auto-chaining with project picker support."""
    await workflow_pause_picker_handler(update, context, _workflow_handler_deps())


async def resume_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Resume auto-chaining with project picker support."""
    await workflow_resume_picker_handler(update, context, _workflow_handler_deps())


async def stop_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Stop workflow with project picker support."""
    await workflow_stop_picker_handler(update, context, _workflow_handler_deps())


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
    await ops_agents_handler(update, context, _ops_handler_deps())


@rate_limited("direct")
async def direct_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Send a direct request to a specific agent for a project."""
    await ops_direct_handler(update, context, _ops_handler_deps())


async def comments_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """View recent comments on an issue."""
    await issue_comments_handler(update, context, _issue_handler_deps())


async def respond_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Post a response to an issue and automatically continue the agent."""
    await issue_respond_handler(update, context, _issue_handler_deps())


async def inline_keyboard_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle inline keyboard button presses from notifications."""
    await callback_inline_keyboard_handler(update, context, _callback_handler_deps())


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
    app.add_handler(CommandHandler("tailstop", tailstop_handler))
    app.add_handler(CommandHandler("fuse", fuse_handler))
    app.add_handler(CommandHandler("audit", audit_handler))
    app.add_handler(CommandHandler("wfstate", wfstate_handler))
    app.add_handler(CommandHandler("stats", stats_handler))
    app.add_handler(CommandHandler("comments", comments_handler))
    app.add_handler(CommandHandler("reprocess", reprocess_handler))
    app.add_handler(CommandHandler("reconcile", reconcile_handler))
    app.add_handler(CommandHandler("continue", continue_handler))
    app.add_handler(CommandHandler("forget", forget_handler))
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
    app.add_handler(CommandHandler("chat", chat_menu_handler))
    # Menu navigation callbacks
    app.add_handler(CallbackQueryHandler(chat_callback_handler, pattern=r'^chat:'))
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

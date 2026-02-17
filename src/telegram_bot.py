import glob
import json
import logging
import os
import re
import subprocess
import sys
import time
from datetime import datetime, timedelta
from google import genai
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, BotCommand, ReplyKeyboardMarkup, ReplyKeyboardRemove
from telegram.ext import (
    ApplicationBuilder, ContextTypes, CommandHandler,
    MessageHandler, CallbackQueryHandler, ConversationHandler, filters
)

# Import configuration from centralized config module
from config import (
    TELEGRAM_TOKEN, GOOGLE_API_KEY, GOOGLE_AI_MODEL, ALLOWED_USER_ID, BASE_DIR,
    DATA_DIR, TRACKED_ISSUES_FILE, GITHUB_AGENTS_REPO, PROJECT_CONFIG, ensure_data_dir,
    TELEGRAM_BOT_LOG_FILE, TELEGRAM_CHAT_ID
)
from state_manager import StateManager
from models import WorkflowState
from commands.workflow import pause_handler, resume_handler, stop_handler
from inbox_processor import get_sop_tier, invoke_copilot_agent
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
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler(TELEGRAM_BOT_LOG_FILE)
    ]
)

# Configure Gemini
client = genai.Client(api_key=GOOGLE_API_KEY)

# Initialize rate limiter
rate_limiter = get_rate_limiter()

# Initialize user manager
user_manager = get_user_manager()

# Legacy alias for compatibility
GITHUB_REPO = GITHUB_AGENTS_REPO


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


def get_issue_details(issue_num):
    """Query GitHub API for issue details."""
    try:
        result = subprocess.run(
            ["gh", "issue", "view", str(issue_num), "--repo", GITHUB_REPO, "--json",
             "number,title,state,labels,body,updatedAt"],
            check=True, text=True, capture_output=True
        )
        return json.loads(result.stdout)
    except Exception as e:
        logger.error(f"Failed to fetch issue {issue_num}: {e}")
        return None


def search_logs_for_issue(issue_num):
    """Search bot and processor logs for mentions of issue."""
    logs = []
    try:
        # Search in systemd journal
        result = subprocess.run(
            ["sudo", "journalctl", "-u", "nexus-bot", "-u", "nexus-processor",
             "--grep", f"issue|#{issue_num}", "-n", "50", "--no-pager"],
            text=True, capture_output=True, timeout=5
        )
        if result.stdout:
            logs.extend(result.stdout.strip().split("\n")[-10:])  # Last 10 entries
    except Exception as e:
        logger.warning(f"Failed to search logs for issue {issue_num}: {e}")
    return logs


def read_log_matches(file_path, issue_num, issue_url, max_lines=5):
    """Return last matching lines from a log file."""
    if not os.path.exists(file_path):
        return []

    matches = []
    try:
        with open(file_path, "r") as f:
            for line in f:
                if issue_num in line or issue_url in line:
                    matches.append(line.rstrip())
        if len(matches) > max_lines:
            matches = matches[-max_lines:]
    except Exception as e:
        logger.warning(f"Failed to read log file {file_path}: {e}")
    return matches


def find_task_logs(task_file):
    """Locate task logs folder based on task file path."""
    if not task_file:
        return []
    project_root = os.path.dirname(os.path.dirname(os.path.dirname(task_file)))
    logs_dir = os.path.join(project_root, ".github", "tasks", "logs")
    if not os.path.isdir(logs_dir):
        return []
    return [os.path.join(logs_dir, f) for f in os.listdir(logs_dir) if f.endswith(".log")]


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
        project_root = os.path.dirname(os.path.dirname(os.path.dirname(task_file)))
        logs_dir = os.path.join(project_root, ".github", "tasks", "logs")
        if os.path.isdir(logs_dir):
            pattern = os.path.join(logs_dir, f"copilot_{issue_num}_*.log")
            matches.extend(glob.glob(pattern))

    if matches:
        return matches

    # Fallback: scan all logs dirs
    pattern = os.path.join(BASE_DIR, "**", ".github", "tasks", "logs", f"copilot_{issue_num}_*.log")
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

    # If task is inside a workspace repo (.github/...), derive project root
    if "/.github/" in task_path:
        project_root = task_path.split("/.github/")[0]
        project_name = os.path.basename(project_root)
        config = PROJECT_CONFIG.get(project_name)
        if config:
            return project_name, config

    # If task is inside an agents repo, map by agents_dir
    for key, cfg in PROJECT_CONFIG.items():
        agents_dir = cfg.get("agents_dir")
        if not agents_dir:
            continue
        agents_abs = os.path.abspath(os.path.join(BASE_DIR, agents_dir))
        if task_path.startswith(agents_abs + os.sep):
            return key, cfg

    return None, None


def find_task_file_by_issue(issue_num):
    """Search for a task file that references the issue number."""
    issue_url = f"https://github.com/{GITHUB_REPO}/issues/{issue_num}"
    patterns = [
        os.path.join(BASE_DIR, "**", ".github", "tasks", "active", "*.md"),
        os.path.join(BASE_DIR, "**", ".github", "inbox", "*.md"),
    ]
    for pattern in patterns:
        for path in glob.glob(pattern, recursive=True):
            try:
                with open(path, "r") as f:
                    content = f.read()
                if issue_url in content or re.search(r"\*\*Issue:\*\*\s*https?://github.com/.+/issues/" + re.escape(issue_num), content):
                    return path
            except Exception:
                continue
    return None


# --- DATA ---
PROJECTS = {
    "case_italia": "Case Italia",
    "wallible": "Wallible",
    "biome": "Biome",
    "nexus": "General Inbox (Nexus)"
}
TYPES = {
    "feature": "✨ Feature (9-step workflow)",
    "feature-simple": "✨ Simple Feature (4-step fast-track)",
    "bug": "🩹 Bug Fix (6-step workflow)",
    "hotfix": "🔥 Hotfix (4-step fast-track)",
    "release": "📦 Release (9-step workflow)",
    "chore": "🧹 Chore (4-step fast-track)",
    "improvement": "🚀 Improvement (9-step workflow)",
    "improvement-simple": "🚀 Simple Improvement (4-step fast-track)"
}

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
        "🤖 **Nexus (Google Edition) Commands**\n\n"
        "✨ **Task Creation:**\n"
        "/new - Start a menu-driven task creation\n"
        "/cancel - Abort the current guided process\n\n"
        "⚡ **Hands-Free Mode:**\n"
        "Just send a **Voice Note** or **Text Message** directly. "
        "Gemini will automatically transcribe, route, and save the task "
        "based on its content!\n\n"
        "📋 **Workflow Tiers:**\n"
        "• 🔥 Hotfix/Chore → 4-step fast-track (quick fixes)\n"
        "• 🩹 Bug → 6-step shortened (triage → fix → deploy)\n"
        "• ✨ Feature → 9-step full (design → implement → deploy)\n"
        "• ✨ Simple Feature → 4-step fast-track (skip design for easy features)\n\n"
        "📊 **Monitoring & Tracking:**\n"
        "/status - View pending tasks in inbox\n"
        "/active - View tasks currently being worked on\n"
        "/track <issue#> - Subscribe to issue updates (global)\n"
        "/track <project> <issue#> - Track issue per-project (casit/wlbl/bm)\n"
        "/untrack <issue#> - Stop tracking globally\n"
        "/untrack <project> <issue#> - Stop tracking per-project\n"
        "/myissues - View all your tracked issues\n"
        "/logs <issue#> - View task execution logs\n"
        "/logsfull <issue#> - Full log lines (no truncation)\n"
        "/audit <issue#> - View workflow audit trail (state changes, agent launches, timeouts)\n"
        "/stats [days] - View system analytics and performance metrics (default: 30 days)\n"
        "/comments <issue#> - View issue comments\n\n"
        "🔁 **Recovery & Control:**\n"
        "/reprocess <issue#> - Re-run agent processing\n"
        "/continue <issue#> - Check stuck agent status\n"
        "/kill <issue#> - Stop running agent process\n"
        "/pause <issue#> - Pause auto-chaining (agents work but no auto-launch)\n"
        "/resume <issue#> - Resume auto-chaining\n"
        "/stop <issue#> - Stop workflow completely (closes issue, kills agent)\n"
        "/respond <issue#> <text> - Respond to agent questions\n\n"
        "🤝 **Agent Management:**\n"
        "/agents <project> - List all agents for a project\n"
        "/direct <project> <@agent> <message> - Send direct request to an agent\n\n"
        "🔧 **GitHub Management:**\n"
        "/assign <issue#> - Assign GitHub issue to yourself\n"
        "/implement <issue#> - Request Copilot agent implementation\n"
        "/prepare <issue#> - Add Copilot-friendly instructions\n\n"
        "ℹ️ /help - Show this list"
    )
    await update.message.reply_text(help_text, parse_mode='Markdown')


async def start_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Welcome message and persistent reply keyboard."""
    logger.info(f"Start triggered by user: {update.effective_user.id}")
    if ALLOWED_USER_ID and update.effective_user.id != ALLOWED_USER_ID:
        logger.warning(f"Unauthorized access attempt by ID: {update.effective_user.id}")
        return

    welcome = (
        "👋 Welcome to Nexus (Google Edition)!\n\n"
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
        BotCommand("cancel", "Cancel current process"),
        BotCommand("status", "Show pending tasks"),
        BotCommand("active", "Show active tasks"),
        BotCommand("track", "Subscribe to issue updates"),
        BotCommand("untrack", "Stop tracking an issue"),
        BotCommand("myissues", "View your tracked issues"),
        BotCommand("logs", "View task execution logs"),
        BotCommand("logsfull", "Full issue logs"),
        BotCommand("audit", "View workflow audit trail"),
        BotCommand("stats", "View system analytics"),
        BotCommand("comments", "View issue comments"),
        BotCommand("reprocess", "Re-run agent processing"),
        BotCommand("continue", "Check stuck agent status"),
        BotCommand("kill", "Stop running agent"),
        BotCommand("pause", "Pause auto-chaining"),
        BotCommand("resume", "Resume auto-chaining"),
        BotCommand("stop", "Stop workflow completely"),
        BotCommand("agents", "List project agents"),
        BotCommand("direct", "Send direct agent request"),
        BotCommand("respond", "Respond to agent questions"),
        BotCommand("assign", "Assign an issue"),
        BotCommand("implement", "Request implementation"),
        BotCommand("prepare", "Prepare for Copilot"),
        BotCommand("help", "Show help")
    ]
    try:
        await application.bot.set_my_commands(cmds)
        logger.info("Registered bot commands for Telegram client menu")
    except Exception:
        logger.exception("Failed to set bot commands on startup")


# --- HELPER: GEMINI AUDIO PROCESSOR ---
async def process_audio_with_gemini(voice_file_id, context):
    """Downloads Telegram audio and sends to Gemini for text."""
    # 1. Download (.ogg)
    new_file = await context.bot.get_file(voice_file_id)
    await new_file.download_to_drive("temp_voice.ogg")

    # 2. Upload & Transcribe (Gemini supports .ogg)
    logger.info("Uploading audio to Gemini...")
    audio_file = await client.aio.files.upload(file="temp_voice.ogg")

    # Prompt just for transcription
    logger.info("Starting transcription...")
    response = await client.aio.models.generate_content(
        model=GOOGLE_AI_MODEL,
        contents=[
            "Transcribe this audio exactly. Return ONLY the text.",
            audio_file
        ]
    )

    # Cleanup
    if os.path.exists("temp_voice.ogg"): os.remove("temp_voice.ogg")

    return response.text.strip()


# --- 1. HANDS-FREE MODE (Auto-Router) ---
async def hands_free_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    logger.info(f"Hands-free triggered by user: {update.effective_user.id}")
    if ALLOWED_USER_ID and update.effective_user.id != ALLOWED_USER_ID:
        logger.warning(f"Unauthorized access attempt by ID: {update.effective_user.id}")
        return

    # Guard: Don't process commands as tasks
    if update.message.text and update.message.text.startswith('/'):
        logger.info(f"Ignoring command in hands_free_handler: {update.message.text}")
        return

    text = ""
    status_msg = await update.message.reply_text("⚡ Gemini Listening...")

    # A. Handle Audio
    if update.message.voice:
        # Download
        new_file = await context.bot.get_file(update.message.voice.file_id)
        await new_file.download_to_drive("temp_voice.ogg")

        # Multimodal Prompt: "Listen and Route"
        logger.info("Uploading audio for auto-routing...")
        with open("temp_voice.ogg", "rb") as f:
            audio_file = await client.aio.files.upload(file=f)

        response = await client.aio.models.generate_content(
            model=GOOGLE_AI_MODEL,
            contents=[
                f"""
                You are a project router. Listen to the audio.
                1. Transcribe the text.
                2. Map it to one of these keys: {list(PROJECTS.keys())}.
                3. Classify type as one of: {list(TYPES.keys())}.
                   - Use 'feature-simple' for straightforward features without UX/architecture needs
                   - Use 'feature' for complex features needing design review
                   - Use 'improvement-simple' for minor enhancements
                   - Use 'improvement' for significant enhancements
                4. Generate a concise issue name (3-6 words, kebab-case, no project name).
                5. Return JSON: {{"project": "key", "type": "type_key", "text": "transcription", "issue_name": "issue-name-here"}}
                """,
                audio_file
            ]
        )

        # Cleanup
        os.remove("temp_voice.ogg")

    # B. Handle Text
    else:
        logger.info("Processing text for auto-routing...")
        text_input = update.message.text
        response = await client.aio.models.generate_content(
            model=GOOGLE_AI_MODEL,
            contents=f"""
                1. Map this text to one of these keys: {list(PROJECTS.keys())}.
                2. Classify type as one of: {list(TYPES.keys())}.
                   - Use 'feature-simple' for straightforward features without UX/architecture needs
                   - Use 'feature' for complex features needing design review
                   - Use 'improvement-simple' for minor enhancements
                   - Use 'improvement' for significant enhancements
                3. Generate a concise issue name (3-6 words, kebab-case, no project name).
                4. Return JSON: {{"project": "key", "type": "type_key", "text": "{text_input}", "issue_name": "issue-name-here"}}
                Input: {text_input}
            """
        )

    # Parse Result
    try:
        result = json.loads(response.text.replace("```json", "").replace("```", ""))
        project = result.get("project", "inbox")
        task_type = result.get("type", "feature")
        content = result.get("text", "")
        issue_name = result.get("issue_name", "")
    except:
        await context.bot.edit_message_text(chat_id=update.effective_chat.id, message_id=status_msg.message_id,
                                            text="⚠️ JSON Error")
        return

    # Save to File
    target_dir = os.path.join(BASE_DIR, project, ".github", "inbox")
    os.makedirs(target_dir, exist_ok=True)
    filename = f"voice_task_{update.message.message_id}.md"

    with open(os.path.join(target_dir, filename), "w") as f:
        f.write(
            f"# {TYPES.get(task_type, 'Task')}\n**Project:** {PROJECTS.get(project, project)}\n**Type:** {task_type}\n**Issue Name:** {issue_name}\n**Status:** Pending\n\n{content}")

    await context.bot.edit_message_text(
        chat_id=update.effective_chat.id,
        message_id=status_msg.message_id,
        text=f"✅ Routed to `{project}`\n📝 *{content}*"
    )


# --- 2. SELECTION MODE (Menu) ---
# (Steps 1 & 2 are purely Telegram UI, no AI needed)

async def start_selection(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if ALLOWED_USER_ID and update.effective_user.id != ALLOWED_USER_ID: return
    keyboard = [[InlineKeyboardButton(name, callback_data=code)] for code, name in PROJECTS.items()]
    await update.message.reply_text("📂 **Select Project:**", reply_markup=InlineKeyboardMarkup(keyboard),
                                    parse_mode='Markdown')
    return SELECT_PROJECT


async def project_selected(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    context.user_data['project'] = query.data
    keyboard = [[InlineKeyboardButton(name, callback_data=code)] for code, name in TYPES.items()]
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

    text = ""
    if update.message.voice:
        msg = await update.message.reply_text("🎧 Transcribing with Gemini...")
        # Re-use the helper function to just get text
        text = await process_audio_with_gemini(update.message.voice.file_id, context)
        await context.bot.delete_message(chat_id=update.effective_chat.id, message_id=msg.message_id)
    else:
        text = update.message.text

    # Generate issue name with Gemini
    issue_name = ""
    try:
        response = await client.aio.models.generate_content(
            model=GOOGLE_AI_MODEL,
            contents=f"""Generate a concise issue name (3-6 words, kebab-case) for this task.
Task: {text[:300]}
Project: {PROJECTS.get(project)}
Type: {TYPES.get(task_type)}

Return only the issue name, nothing else."""
        )
        issue_name = response.text.strip().strip('"`\'')
    except Exception as e:
        logger.warning(f"Failed to generate issue name: {e}")
        issue_name = ""

    # Write File
    target_dir = os.path.join(BASE_DIR, project, ".github", "inbox")
    os.makedirs(target_dir, exist_ok=True)
    filename = f"{task_type}_{update.message.message_id}.md"

    with open(os.path.join(target_dir, filename), "w") as f:
        issue_name_line = f"**Issue Name:** {issue_name}\n" if issue_name else ""
        f.write(
            f"# {TYPES[task_type]}\n**Project:** {PROJECTS[project]}\n**Type:** {task_type}\n{issue_name_line}**Status:** Pending\n\n{text}")

    await update.message.reply_text(f"✅ Saved to `{project}`.")
    return ConversationHandler.END


async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("❌ Cancelled.")
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

    status_text = "📥 Inbox Status (Pending Tasks)\n\n"
    total_tasks = 0

    for project_key, project_name in PROJECTS.items():
        inbox_dir = os.path.join(BASE_DIR, project_key, ".github", "inbox")
        if os.path.exists(inbox_dir):
            files = [f for f in os.listdir(inbox_dir) if f.endswith(".md")]
            if files:
                status_text += f"{project_name}: {len(files)} task(s)\n"
                total_tasks += len(files)
                # Show first 3 files as preview
                for f in files[:3]:
                    task_type = f.split('_')[0]
                    emoji = TYPES.get(task_type, "📝")
                    file_path = os.path.join(inbox_dir, f)
                    issue_number = extract_issue_number_from_file(file_path)
                    if issue_number:
                        issue_link = f"https://github.com/{GITHUB_REPO}/issues/{issue_number}"
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

    await update.message.reply_text(status_text, parse_mode='Markdown', disable_web_page_preview=True)


async def active_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Shows active tasks being worked on."""
    logger.info(f"Active triggered by user: {update.effective_user.id}")
    if ALLOWED_USER_ID and update.effective_user.id != ALLOWED_USER_ID:
        logger.warning(f"Unauthorized access attempt by ID: {update.effective_user.id}")
        return

    active_text = "🚀 Active Tasks (In Progress)\n\n"
    total_active = 0

    # Check project workspace active folders
    project_dirs = {
        "case_italia": "Case Italia",
        "wallible": "Wallible",
        "biome": "Biome",
    }

    for project_key, display_name in project_dirs.items():
        active_dir = os.path.join(BASE_DIR, project_key, ".github", "tasks", "active")
        if os.path.exists(active_dir):
            files = [f for f in os.listdir(active_dir) if f.endswith(".md")]
            if files:
                active_text += f"{display_name}: {len(files)} task(s)\n"
                total_active += len(files)
                for f in files[:3]:
                    task_type = f.split('_')[0]
                    emoji = TYPES.get(task_type, "📝")
                    file_path = os.path.join(active_dir, f)
                    issue_number = extract_issue_number_from_file(file_path)
                    if issue_number:
                        issue_link = f"https://github.com/{GITHUB_REPO}/issues/{issue_number}"
                        issue_suffix = f" [#{issue_number}]({issue_link})"
                    else:
                        issue_suffix = " (issue ?)"
                    active_text += f"  • {emoji} `{f}`{issue_suffix}\n"
                if len(files) > 3:
                    active_text += f"  ... +{len(files) - 3} more\n"
                active_text += "\n"

    if total_active == 0:
        active_text += "💤 No active tasks at the moment.\n"
    else:
        active_text += f"Total: {total_active} active task(s)"

    await update.message.reply_text(active_text, parse_mode='Markdown', disable_web_page_preview=True)


async def assign_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Assigns a GitHub issue to the user."""
    logger.info(f"Assign triggered by user: {update.effective_user.id}")
    if ALLOWED_USER_ID and update.effective_user.id != ALLOWED_USER_ID:
        logger.warning(f"Unauthorized access attempt by ID: {update.effective_user.id}")
        return

    # Parse issue number from command
    # Format: /assign 0 or /assign #0 or /assign https://github.com/owner/repo/issues/0
    if not context.args:
        await update.effective_message.reply_text(
            "⚠️ Usage: `/assign <issue#> [assignee]`\n\n"
            "Examples:\n"
            "  `/assign 0` (assigns to you / @me)\n"
            "  `/assign 0 copilot` (assigns to configured Copilot user)\n"
            "  `/assign https://github.com/Ghabs95/agents/issues/0 alice`",
            parse_mode='Markdown'
        )
        return

    issue_input = context.args[0]
    
    # Extract issue number
    issue_number = None
    if issue_input.startswith("#"):
        issue_number = issue_input[1:]
    elif issue_input.startswith("http"):
        # Extract from URL
        match = re.search(r'/issues/(\d+)', issue_input)
        if match:
            issue_number = match.group(1)
    else:
        issue_number = issue_input

    if not issue_number or not issue_number.isdigit():
        await update.effective_message.reply_text("❌ Invalid issue number. Please use a number like `5` or `#5`.", parse_mode='Markdown')
        return

    # Get repo from env or use default
    repo = os.getenv("GITHUB_AGENTS_REPO", "Ghabs95/agents")
    # Optional assignee argument: `/assign 5 copilot` or `/assign 5 alice`
    assignee = "@me"
    if len(context.args) > 1:
        raw_assignee = context.args[1]
        if raw_assignee.lower() == "copilot":
            assignee = os.getenv("GITHUB_COPILOT_USER", "copilot")
        else:
            assignee = raw_assignee
    
    # Assign using gh CLI
    msg = await update.effective_message.reply_text(f"🔄 Assigning issue #{issue_number}...")
    
    try:
        result = subprocess.run(
            ["gh", "issue", "edit", issue_number, "--repo", repo, "--add-assignee", assignee],
            check=True,
            text=True,
            capture_output=True
        )
        display_assignee = assignee
        if display_assignee == "@me":
            display_assignee = "you (@me)"
        await context.bot.edit_message_text(
            chat_id=update.effective_chat.id,
            message_id=msg.message_id,
            text=f"✅ Issue #{issue_number} assigned to {display_assignee}!\n\nhttps://github.com/{repo}/issues/{issue_number}",
            parse_mode='Markdown'
        )
    except subprocess.CalledProcessError as e:
        error_msg = e.stderr if e.stderr else "Unknown error"
        await context.bot.edit_message_text(
            chat_id=update.effective_chat.id,
            message_id=msg.message_id,
            text=f"❌ Failed to assign issue #{issue_number}\n\nError: {error_msg}"
        )
    except FileNotFoundError:
        await context.bot.edit_message_text(
            chat_id=update.effective_chat.id,
            message_id=msg.message_id,
            text="❌ Error: `gh` CLI not found on server."
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
        await update.message.reply_text(
            "⚠️ Usage: `/implement <issue#>`\n\nExamples:\n  `/implement 0`\n  `/implement #0`\n  `/implement https://github.com/Ghabs95/agents/issues/0`",
            parse_mode='Markdown'
        )
        return

    issue_input = context.args[0]
    issue_number = None
    if issue_input.startswith("#"):
        issue_number = issue_input[1:]
    elif issue_input.startswith("http"):
        match = re.search(r'/issues/(\d+)', issue_input)
        if match:
            issue_number = match.group(1)
    else:
        issue_number = issue_input

    if not issue_number or not issue_number.isdigit():
        await update.message.reply_text("❌ Invalid issue number. Please use a number like `5` or `#5`.", parse_mode='Markdown')
        return

    repo = os.getenv("GITHUB_AGENTS_REPO", "Ghabs95/agents")

    msg = await update.message.reply_text(f"🔔 Requesting Copilot implementation for issue #{issue_number}...")

    try:
        # Create the label if missing (ignore errors)
        subprocess.run(["gh", "label", "create", "agent:requested", "--repo", repo, "--color", "E6E6FA", "--description", "Requested Copilot implementation"], check=False, text=True, capture_output=True)

        # Add the request label
        subprocess.run(["gh", "issue", "edit", issue_number, "--repo", repo, "--add-label", "agent:requested"], check=True, text=True, capture_output=True)

        comment = (
            f"@ProjectLead — Copilot implementation has been requested via Telegram.\n\n"
            f"Please review the issue and either click 'Code with agent mode' in the GitHub UI or add the label `agent:approved` to start implementation.\n\n"
            f"Issue: https://github.com/{repo}/issues/{issue_number}"
        )

        subprocess.run(["gh", "issue", "comment", issue_number, "--repo", repo, "--body", comment], check=True, text=True, capture_output=True)

        await context.bot.edit_message_text(
            chat_id=update.effective_chat.id,
            message_id=msg.message_id,
            text=f"✅ Requested implementation for issue #{issue_number}. ProjectLead has been notified.\n\nhttps://github.com/{repo}/issues/{issue_number}",
            parse_mode='Markdown'
        )
    except subprocess.CalledProcessError as e:
        err = e.stderr if e.stderr else str(e)
        await context.bot.edit_message_text(
            chat_id=update.effective_chat.id,
            message_id=msg.message_id,
            text=f"❌ Failed to request implementation for issue #{issue_number}.\n\nError: {err}"
        )
    except FileNotFoundError:
        await context.bot.edit_message_text(
            chat_id=update.effective_chat.id,
            message_id=msg.message_id,
            text="❌ Error: `gh` CLI not found on server."
        )


async def prepare_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Augments an issue with Copilot-friendly instructions and acceptance criteria."""
    logger.info(f"Prepare requested by user: {update.effective_user.id}")
    if ALLOWED_USER_ID and update.effective_user.id != ALLOWED_USER_ID:
        logger.warning(f"Unauthorized access attempt by ID: {update.effective_user.id}")
        return

    if not context.args:
        await update.message.reply_text(
            "⚠️ Usage: `/prepare <issue#>`\n\nExamples:\n  `/prepare 0`\n  `/prepare #0`\n  `/prepare https://github.com/Ghabs95/agents/issues/0`",
            parse_mode='Markdown'
        )
        return

    issue_input = context.args[0]
    issue_number = None
    if issue_input.startswith("#"):
        issue_number = issue_input[1:]
    elif issue_input.startswith("http"):
        match = re.search(r'/issues/(\d+)', issue_input)
        if match:
            issue_number = match.group(1)
    else:
        issue_number = issue_input

    if not issue_number or not issue_number.isdigit():
        await update.message.reply_text("❌ Invalid issue number. Please use a number like `5` or `#5`.", parse_mode='Markdown')
        return

    repo = os.getenv("GITHUB_AGENTS_REPO", "Ghabs95/agents")

    msg = await update.message.reply_text(f"🔧 Preparing issue #{issue_number} for Copilot...")

    try:
        # Fetch current issue body and title
        result = subprocess.run(["gh", "issue", "view", issue_number, "--repo", repo, "--json", "body,title"], check=True, text=True, capture_output=True)
        data = json.loads(result.stdout)
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

        # Update the issue body
        subprocess.run(["gh", "issue", "edit", issue_number, "--repo", repo, "--body", new_body], check=True, text=True, capture_output=True)

        await context.bot.edit_message_text(
            chat_id=update.effective_chat.id,
            message_id=msg.message_id,
            text=f"✅ Prepared issue #{issue_number} for Copilot. You can now click 'Code with agent mode' in GitHub or ask ProjectLead to approve.\n\nhttps://github.com/{repo}/issues/{issue_number}",
            parse_mode='Markdown'
        )
    except subprocess.CalledProcessError as e:
        err = e.stderr if e.stderr else str(e)
        await context.bot.edit_message_text(
            chat_id=update.effective_chat.id,
            message_id=msg.message_id,
            text=f"❌ Failed to prepare issue #{issue_number}.\n\nError: {err}"
        )
    except FileNotFoundError:
        await context.bot.edit_message_text(
            chat_id=update.effective_chat.id,
            message_id=msg.message_id,
            text="❌ Error: `gh` CLI not found on server."
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
        await update.effective_message.reply_text(
            "⚠️ Usage:\n"
            "/untrack <issue#> - Stop global tracking\n"
            "/untrack <project> <issue#> - Stop per-project tracking\n\n"
            "Examples:\n"
            "  /untrack 123\n"
            "  /untrack casit 456"
        )
        return

    # Parse arguments - support both /untrack <issue> and /untrack <project> <issue>
    if len(context.args) >= 2:
        # Per-project untracking
        project = context.args[0].lower()
        issue_num = context.args[1].lstrip("#")
        
        success = user_manager.untrack_issue(
            telegram_id=user.id,
            project=project,
            issue_number=issue_num
        )
        
        if success:
            await update.effective_message.reply_text(
                f"✅ Stopped tracking {project.upper()} issue #{issue_num}"
            )
        else:
            await update.effective_message.reply_text(
                f"❌ You weren't tracking {project.upper()} issue #{issue_num}"
            )
    else:
        # Legacy global untracking
        issue_num = context.args[0].lstrip("#")
        if issue_num in tracked_issues:
            del tracked_issues[issue_num]
            save_tracked_issues(tracked_issues)
            await update.effective_message.reply_text(
                f"✅ Stopped tracking issue #{issue_num} (global)\n\n"
                f"🔗 https://github.com/{GITHUB_REPO}/issues/{issue_num}"
            )
        else:
            await update.effective_message.reply_text(f"❌ Issue #{issue_num} is not being tracked globally.")


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
        await update.effective_message.reply_text("⚠️ Usage: /logs <issue#>")
        return

    issue_num = context.args[0].lstrip("#")
    if not issue_num.isdigit():
        await update.effective_message.reply_text("❌ Invalid issue number.")
        return

    msg = await update.effective_message.reply_text(f"📋 Fetching logs for issue #{issue_num}...")

    issue_url = f"https://github.com/{GITHUB_REPO}/issues/{issue_num}"

    # Task log files only (.github/tasks/logs/*.log)
    details = get_issue_details(issue_num)
    timeline = "Task Logs:\n"

    task_file = None
    if details and details.get("body"):
        match = re.search(r"Task File:\s*`([^`]+)`", details.get("body", ""))
        if match:
            task_file = match.group(1)

    issue_logs = find_issue_log_files(issue_num, task_file=task_file)
    if issue_logs:
        issue_logs.sort(key=lambda p: os.path.getmtime(p), reverse=True)
        latest = issue_logs[0]
        logger.info(f"Reading log file: {latest}")
        try:
            with open(latest, "r") as f:
                lines = f.readlines()[-50:]
            logger.info(f"Read {len(lines)} lines from log file")
            timeline += f"\n**{os.path.basename(latest)}**\n"
            for line in lines:
                timeline += f"{line.rstrip()}\n"
        except Exception as e:
            logger.error(f"Error reading log file: {e}", exc_info=True)
            timeline += f"\n❌ Failed to read {os.path.basename(latest)}: {e}\n"
    else:
        latest_tail = read_latest_log_tail(task_file, max_lines=50)
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
        await update.effective_message.reply_text("⚠️ Usage: /logsfull <issue#>")
        return

    issue_num = context.args[0].lstrip("#")
    if not issue_num.isdigit():
        await update.effective_message.reply_text("❌ Invalid issue number.")
        return

    msg = await update.effective_message.reply_text(f"📋 Fetching full logs for issue #{issue_num}...")
    issue_url = f"https://github.com/{GITHUB_REPO}/issues/{issue_num}"

    details = get_issue_details(issue_num)
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


async def audit_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Display workflow audit trail for an issue (timeline of state changes, agent launches, etc)."""
    logger.info(f"Audit trail requested by user: {update.effective_user.id}")
    if ALLOWED_USER_ID and update.effective_user.id != ALLOWED_USER_ID:
        logger.warning(f"Unauthorized access attempt by ID: {update.effective_user.id}")
        return

    if not context.args:
        await update.effective_message.reply_text("⚠️ Usage: /audit <issue#>")
        return

    issue_num = context.args[0].lstrip("#")
    if not issue_num.isdigit():
        await update.effective_message.reply_text("❌ Invalid issue number.")
        return

    try:
        # Import here to avoid circular imports
        from state_manager import StateManager
        
        msg = await update.effective_message.reply_text(f"📊 Fetching audit trail for issue #{issue_num}...", parse_mode="Markdown")
        
        # Get audit history from StateManager
        audit_history = StateManager.get_audit_history(issue_num, limit=100)
        
        if not audit_history:
            await context.bot.edit_message_text(
                chat_id=update.effective_chat.id,
                message_id=msg.message_id,
                text=f"📊 **Audit Trail for Issue #{issue_num}**\n\nNo audit events recorded yet."
            )
            return
        
        # Format audit trail
        timeline = f"📊 **Audit Trail for Issue #{issue_num}**\n"
        timeline += "=" * 40 + "\n\n"
        
        for event in audit_history:
            # Format: timestamp | Issue #N | EVENT_TYPE | details
            try:
                parts = event.split(" | ", 3)
                timestamp = parts[0] if len(parts) > 0 else "?"
                issue_ref = parts[1] if len(parts) > 1 else "?"
                event_type = parts[2] if len(parts) > 2 else "?"
                details = parts[3] if len(parts) > 3 else ""
                
                # Format event with emoji based on type
                event_emoji = {
                    "AGENT_LAUNCHED": "🚀",
                    "AGENT_TIMEOUT_KILL": "⏱️",
                    "AGENT_RETRY": "🔄",
                    "AGENT_FAILED": "❌",
                    "WORKFLOW_PAUSED": "⏸️",
                    "WORKFLOW_RESUMED": "▶️",
                    "WORKFLOW_STOPPED": "🛑",
                    "AGENT_COMPLETION": "✅",
                    "WORKFLOW_STARTED": "🎬"
                }.get(event_type, "•")
                
                timeline += f"{event_emoji} **{event_type}** ({timestamp})\n"
                if details:
                    timeline += f"   {details}\n"
                timeline += "\n"
            except Exception as e:
                logger.warning(f"Error parsing audit event: {e}")
                timeline += f"• {event}\n\n"
        
        # Telegram message limit safety
        max_len = 3500
        if len(timeline) <= max_len:
            await context.bot.edit_message_text(
                chat_id=update.effective_chat.id,
                message_id=msg.message_id,
                text=timeline,
                parse_mode="Markdown"
            )
        else:
            # Split into chunks
            chunks = [timeline[i:i+max_len] for i in range(0, len(timeline), max_len)]
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
        # Get audit log path from config
        audit_log_path = os.path.join(DATA_DIR, "audit.log")
        
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
        report = get_stats_report(audit_log_path, lookback_days=lookback_days)
        
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
        await update.effective_message.reply_text("⚠️ Usage: /reprocess <issue#>")
        return

    issue_num = context.args[0].lstrip("#")
    if not issue_num.isdigit():
        await update.effective_message.reply_text("❌ Invalid issue number.")
        return

    details = get_issue_details(issue_num)
    if not details:
        await update.effective_message.reply_text(f"❌ Could not load issue #{issue_num}.")
        return

    if details.get("state") == "closed":
        await update.effective_message.reply_text(f"⚠️ Issue #{issue_num} is closed. Reprocess only applies to open issues.")
        return

    body = details.get("body", "")
    match = re.search(r"Task File:\s*`([^`]+)`", body)
    task_file = match.group(1) if match else None
    if not task_file:
        task_file = find_task_file_by_issue(issue_num)

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

    with open(task_file, "r") as f:
        content = f.read()

    type_match = re.search(r"\*\*Type:\*\*\s*(.+)", content)
    task_type = type_match.group(1).strip().lower() if type_match else "feature"

    tier_name, _, _ = get_sop_tier(task_type)
    issue_url = f"https://github.com/{GITHUB_REPO}/issues/{issue_num}"

    msg = await update.effective_message.reply_text(f"🔁 Reprocessing issue #{issue_num}...")

    agents_abs = os.path.join(BASE_DIR, config["agents_dir"])
    workspace_abs = os.path.join(BASE_DIR, config["workspace"])

    pid = invoke_copilot_agent(
        agents_dir=agents_abs,
        workspace_dir=workspace_abs,
        issue_url=issue_url,
        tier_name=tier_name,
        task_content=content
    )

    if pid:
        await context.bot.edit_message_text(
            chat_id=update.effective_chat.id,
            message_id=msg.message_id,
            text=(
                f"✅ Reprocess started for issue #{issue_num}. Agent PID: {pid}\n\n"
                f"🔗 https://github.com/{GITHUB_REPO}/issues/{issue_num}"
            )
        )
    else:
        await context.bot.edit_message_text(
            chat_id=update.effective_chat.id,
            message_id=msg.message_id,
            text=f"❌ Failed to launch reprocess for issue #{issue_num}."
        )


def find_agent_pid_for_issue(issue_num):
    """Find the PID of the running Copilot agent for an issue."""
    try:
        # Search for copilot processes with the issue reference
        result = subprocess.run(
            ["pgrep", "-af", f"copilot.*issues/{issue_num}"],
            text=True, capture_output=True
        )
        if result.stdout:
            lines = result.stdout.strip().split("\n")
            for line in lines:
                parts = line.split(None, 1)
                if parts:
                    return int(parts[0])
        return None
    except Exception as e:
        logger.error(f"Failed to find agent PID: {e}")
        return None


async def continue_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Continue/resume agent processing for an issue with a continuation prompt."""
    logger.info(f"Continue requested by user: {update.effective_user.id}")
    if ALLOWED_USER_ID and update.effective_user.id != ALLOWED_USER_ID:
        logger.warning(f"Unauthorized access attempt by ID: {update.effective_user.id}")
        return

    if not context.args:
        await update.effective_message.reply_text("⚠️ Usage: /continue <issue#> [prompt]\n\nExample: /continue 0 Please proceed with implementation")
        return

    issue_num = context.args[0].lstrip("#")
    if not issue_num.isdigit():
        await update.effective_message.reply_text("❌ Invalid issue number.")
        return

    continuation_prompt = " ".join(context.args[1:]) if len(context.args) > 1 else "Please continue with the next step."

    # Check if agent is already running
    pid = find_agent_pid_for_issue(issue_num)
    if pid:
        await update.effective_message.reply_text(
            f"⚠️ Agent is already running for issue #{issue_num} (PID: {pid}).\n\n"
            f"Use /kill {issue_num} first if you want to restart it."
        )
        return

    # Get issue details and task file
    details = get_issue_details(issue_num)
    if not details:
        await update.effective_message.reply_text(f"❌ Could not load issue #{issue_num}.")
        return

    if details.get("state") == "closed":
        await update.effective_message.reply_text(f"⚠️ Issue #{issue_num} is closed.")
        return

    body = details.get("body", "")
    match = re.search(r"Task File:\s*`([^`]+)`", body)
    task_file = match.group(1) if match else None
    if not task_file:
        task_file = find_task_file_by_issue(issue_num)

    if not task_file or not os.path.exists(task_file):
        await update.effective_message.reply_text(f"❌ Task file not found for issue #{issue_num}.")
        return

    project_name, config = resolve_project_config_from_task(task_file)
    if not config or not config.get("agents_dir"):
        name = project_name or "unknown"
        await update.effective_message.reply_text(f"❌ No agents config for project '{name}'.")
        return

    with open(task_file, "r") as f:
        content = f.read()

    type_match = re.search(r"\*\*Type:\*\*\s*(.+)", content)
    task_type = type_match.group(1).strip().lower() if type_match else "feature"

    tier_name, _, _ = get_sop_tier(task_type)
    issue_url = f"https://github.com/{GITHUB_REPO}/issues/{issue_num}"

    msg = await update.effective_message.reply_text(f"⏩ Continuing agent for issue #{issue_num}...")

    agents_abs = os.path.join(BASE_DIR, config["agents_dir"])
    workspace_abs = os.path.join(BASE_DIR, config["workspace"])

    # Launch with continuation context
    pid = invoke_copilot_agent(
        agents_dir=agents_abs,
        workspace_dir=workspace_abs,
        issue_url=issue_url,
        tier_name=tier_name,
        task_content=content,
        continuation=True,
        continuation_prompt=continuation_prompt
    )

    if pid:
        await context.bot.edit_message_text(
            chat_id=update.effective_chat.id,
            message_id=msg.message_id,
            text=(
                f"✅ Agent continued for issue #{issue_num}. PID: {pid}\n\n"
                f"Prompt: {continuation_prompt}\n\n"
                f"ℹ️ **Note:** The agent will first check if the workflow has already progressed.\n"
                f"If another agent is already handling the next step, this agent will exit gracefully.\n"
                f"Use `/continue` only when an agent is truly stuck mid-step.\n\n"
                f"🔗 https://github.com/{GITHUB_REPO}/issues/{issue_num}"
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
        await update.effective_message.reply_text("⚠️ Usage: /kill <issue#>")
        return

    issue_num = context.args[0].lstrip("#")
    if not issue_num.isdigit():
        await update.effective_message.reply_text("❌ Invalid issue number.")
        return

    pid = find_agent_pid_for_issue(issue_num)
    
    if not pid:
        await update.effective_message.reply_text(f"⚠️ No running agent found for issue #{issue_num}.")
        return

    msg = await update.effective_message.reply_text(f"🔪 Killing agent for issue #{issue_num} (PID: {pid})...")

    try:
        subprocess.run(["kill", str(pid)], check=True, timeout=5)
        # Wait a moment and verify it's gone
        time.sleep(1)
        new_pid = find_agent_pid_for_issue(issue_num)
        if new_pid:
            # Try harder
            subprocess.run(["kill", "-9", str(pid)], check=True, timeout=5)
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


async def pause_handler_REMOVED():
    """This function has been moved to commands/workflow.py"""
    pass


# pause_handler, resume_handler, and stop_handler are now imported from commands.workflow


def get_agents_for_project(project_dir):
    """Parse agents from .github/agents/*.agent.md files.
    
    Returns a dictionary: {agent_display_name: agent_filename}
    Example: {'Architect': 'architect.agent.md', 'BackendLead': 'backend.agent.md'}
    """
    agents_github_dir = os.path.join(project_dir, ".github", "agents")
    agents_map = {}
    
    if not os.path.exists(agents_github_dir):
        return agents_map
    
    try:
        for filename in sorted(os.listdir(agents_github_dir)):
            if filename.endswith(".agent.md"):
                filepath = os.path.join(agents_github_dir, filename)
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
        
        result = subprocess.run(
            ["gh", "issue", "create", "--repo", GITHUB_REPO,
             "--title", title,
             "--body", body,
             "--no-editor", "--label", "workflow:fast-track"],
            text=True, capture_output=True, timeout=15
        )
        
        if result.returncode != 0:
            await context.bot.edit_message_text(
                chat_id=update.effective_chat.id,
                message_id=msg.message_id,
                text=f"❌ Failed to create issue"
            )
            return
        
        # Extract issue number
        import re as re_module
        match = re_module.search(r'#(\d+)', result.stderr + result.stdout)
        if not match:
            await context.bot.edit_message_text(
                chat_id=update.effective_chat.id,
                message_id=msg.message_id,
                text=f"❌ Failed to get issue number"
            )
            return
        
        issue_num = match.group(1)
        issue_url = f"https://github.com/{GITHUB_REPO}/issues/{issue_num}"
        
        # Post a completion marker comment to trigger immediate auto-chain to this agent
        comment_body = f"🎯 Direct request from @Ghabs\n\nReady for `@{agent}`"
        subprocess.run(
            ["gh", "issue", "comment", issue_num, "--repo", GITHUB_REPO, "--body", comment_body],
            check=False, text=True, capture_output=True, timeout=10
        )
        
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
        await update.effective_message.reply_text("⚠️ Usage: /comments <issue#>\n\nExample: /comments 0")
        return

    issue_num = context.args[0].lstrip("#")
    if not issue_num.isdigit():
        await update.effective_message.reply_text("❌ Invalid issue number.")
        return

    msg = await update.effective_message.reply_text(f"💬 Fetching comments for issue #{issue_num}...")

    try:
        # Fetch issue comments
        result = subprocess.run(
            ["gh", "issue", "view", issue_num, "--repo", GITHUB_REPO, 
             "--json", "comments,title", "--jq", 
             '{title: .title, comments: [.comments[] | {author: .author.login, created: .createdAt, body: .body}]}'],
            check=True, text=True, capture_output=True, timeout=15
        )

        data = json.loads(result.stdout)
        title = data.get("title", "Unknown")
        comments = data.get("comments", [])

        if not comments:
            await context.bot.edit_message_text(
                chat_id=update.effective_chat.id,
                message_id=msg.message_id,
                text=(
                    f"💬 **Issue #{issue_num}: {title}**\n\n"
                    f"No comments yet.\n\n"
                    f"🔗 https://github.com/{GITHUB_REPO}/issues/{issue_num}"
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
            author = comment.get("author", "unknown")
            created = comment.get("created", "")
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

        comments_text += f"🔗 https://github.com/{GITHUB_REPO}/issues/{issue_num}"

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

    except subprocess.TimeoutExpired:
        await context.bot.edit_message_text(
            chat_id=update.effective_chat.id,
            message_id=msg.message_id,
            text=f"❌ Timeout fetching comments for issue #{issue_num}"
        )
    except subprocess.CalledProcessError as e:
        error = e.stderr if e.stderr else str(e)
        await context.bot.edit_message_text(
            chat_id=update.effective_chat.id,
            message_id=msg.message_id,
            text=f"❌ Failed to fetch comments: {error}"
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

    if not context.args or len(context.args) < 2:
        await update.effective_message.reply_text(
            "⚠️ Usage: /respond <issue#> <your response>\n\n"
            "Example: /respond 0 The surveyor feature should allow users to view property boundaries and measurements"
        )
        return

    issue_num = context.args[0].lstrip("#")
    if not issue_num.isdigit():
        await update.effective_message.reply_text("❌ Invalid issue number.")
        return

    response_text = " ".join(context.args[1:])

    msg = await update.effective_message.reply_text(f"📝 Posting response to issue #{issue_num}...")

    try:
        # Post comment to GitHub issue
        result = subprocess.run(
            ["gh", "issue", "comment", issue_num, "--repo", GITHUB_REPO, 
             "--body", response_text],
            check=True, text=True, capture_output=True, timeout=15
        )

        await context.bot.edit_message_text(
            chat_id=update.effective_chat.id,
            message_id=msg.message_id,
            text=f"✅ Response posted to issue #{issue_num}.\n\n🤖 Continuing agent..."
        )

        # Now automatically continue the agent with the user's input
        details = get_issue_details(issue_num)
        if not details:
            await update.effective_message.reply_text(f"⚠️ Posted comment but couldn't fetch issue details to continue agent.")
            return

        body = details.get("body", "")
        match = re.search(r"Task File:\s*`([^`]+)`", body)
        task_file = match.group(1) if match else None
        if not task_file:
            task_file = find_task_file_by_issue(issue_num)

        if not task_file or not os.path.exists(task_file):
            await update.effective_message.reply_text(f"⚠️ Posted comment but couldn't find task file to continue agent.")
            return

        project_name, config = resolve_project_config_from_task(task_file)
        if not config or not config.get("agents_dir"):
            await update.effective_message.reply_text(f"⚠️ Posted comment but no agents config for project.")
            return

        with open(task_file, "r") as f:
            content = f.read()

        type_match = re.search(r"\*\*Type:\*\*\s*(.+)", content)
        task_type = type_match.group(1).strip().lower() if type_match else "feature"

        tier_name, _, _ = get_sop_tier(task_type)
        issue_url = f"https://github.com/{GITHUB_REPO}/issues/{issue_num}"

        agents_abs = os.path.join(BASE_DIR, config["agents_dir"])
        workspace_abs = os.path.join(BASE_DIR, config["workspace"])

        # Launch agent with continuation that includes the user's response
        continuation_prompt = (
            f"@Ghabs has provided input:\n\n{response_text}\n\n"
            f"Please proceed with the next step of the workflow."
        )

        pid = invoke_copilot_agent(
            agents_dir=agents_abs,
            workspace_dir=workspace_abs,
            issue_url=issue_url,
            tier_name=tier_name,
            task_content=content,
            continuation=True,
            continuation_prompt=continuation_prompt
        )

        if pid:
            await update.effective_message.reply_text(
                f"✅ Agent resumed for issue #{issue_num} (PID: {pid})\n\n"
                f"Check /logs {issue_num} to monitor progress.\n\n"
                f"🔗 https://github.com/{GITHUB_REPO}/issues/{issue_num}"
            )
        else:
            await update.effective_message.reply_text(
                f"⚠️ Response posted but failed to continue agent.\n"
                f"Use /continue {issue_num} to resume manually.\n\n"
                f"🔗 https://github.com/{GITHUB_REPO}/issues/{issue_num}"
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
        # Create a fake update with the command
        context.args = [issue_num]
        
        # Call the appropriate handler
        handler = action_handlers[action]
        await handler(update, context)
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
            subprocess.run(
                ["gh", "issue", "comment", issue_num, "--repo", GITHUB_REPO,
                 "--body", "✅ Implementation approved by @Ghabs. Please proceed."],
                check=True, timeout=10
            )
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
            subprocess.run(
                ["gh", "issue", "comment", issue_num, "--repo", GITHUB_REPO,
                 "--body", "❌ Implementation rejected by @Ghabs. Please revise."],
                check=True, timeout=10
            )
            await query.edit_message_text(
                f"❌ Implementation rejected for issue #{issue_num}\\n\\n"
                f"Agent has been notified.",
                parse_mode='Markdown'
            )
        except Exception as e:
            await query.edit_message_text(f"❌ Error rejecting: {e}")


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
            SELECT_PROJECT: [CallbackQueryHandler(project_selected)],
            SELECT_TYPE: [CallbackQueryHandler(type_selected)],
            INPUT_TASK: [MessageHandler(filters.TEXT | filters.VOICE, save_task)]
        },
        fallbacks=[CommandHandler("cancel", cancel)],
        per_message=True
    )

    app.add_handler(conv_handler)
    app.add_handler(CommandHandler("start", start_handler))
    app.add_handler(CommandHandler("help", help_handler))
    app.add_handler(CommandHandler("status", status_handler))
    app.add_handler(CommandHandler("active", active_handler))
    app.add_handler(CommandHandler("track", track_handler))
    app.add_handler(CommandHandler("untrack", untrack_handler))
    app.add_handler(CommandHandler("myissues", myissues_handler))
    app.add_handler(CommandHandler("logs", logs_handler))
    app.add_handler(CommandHandler("logsfull", logsfull_handler))
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
    # Inline keyboard callback handler (must be before ConversationHandler callbacks)
    app.add_handler(CallbackQueryHandler(inline_keyboard_handler, pattern=r'^(logs|logsfull|status|pause|resume|stop|audit|reprocess|respond|approve|reject)_'))
    # Exclude commands from the auto-router catch-all
    app.add_handler(MessageHandler((filters.TEXT | filters.VOICE) & (~filters.COMMAND), hands_free_handler))

    print("Nexus (Google Edition) Online...")
    app.run_polling(drop_pending_updates=True, allowed_updates=Update.ALL_TYPES)

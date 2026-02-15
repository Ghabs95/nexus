import glob
import json
import logging
import os
import re
import subprocess
import sys
import time
from datetime import datetime, timedelta
from dotenv import load_dotenv
from google import genai
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, BotCommand, ReplyKeyboardMarkup, ReplyKeyboardRemove
from telegram.ext import (
    ApplicationBuilder, ContextTypes, CommandHandler,
    MessageHandler, CallbackQueryHandler, ConversationHandler, filters
)
from inbox_processor import PROJECT_CONFIG, get_sop_tier, invoke_copilot_agent

# Load secrets from a local file if it exists
SECRET_FILE = "vars.secret"
if os.path.exists(SECRET_FILE):
    logging.info(f"Loading environment from {SECRET_FILE}")
    load_dotenv(SECRET_FILE)
else:
    logging.info(f"No {SECRET_FILE} found, relying on shell environment")

# --- CONFIGURATION ---
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
GOOGLE_API_KEY = os.getenv("AI_API_KEY")
GOOGLE_AI_MODEL = os.getenv("AI_MODEL") or "gemini-1.5-flash"
ALLOWED_USER_ID = int(os.getenv("ALLOWED_USER")) if os.getenv("ALLOWED_USER") else None
BASE_DIR = os.getenv("BASE_DIR", "/home/ubuntu/git")

# --- LOGGING ---
logger = logging.getLogger(__name__)
logging.basicConfig(format='%(asctime)s - %(name)s - %(levelname)s - %(message)s', level=logging.INFO)

# --- VALIDATION ---
logger.info(f"Using BASE_DIR: {BASE_DIR}")
if not TELEGRAM_TOKEN:
    logger.error("TELEGRAM_TOKEN is missing! Please set it in your environment or vars.secret.")
    sys.exit(1)
if not GOOGLE_API_KEY:
    logger.error("AI_API_KEY (Google API Key) is missing! Please set it in your environment or vars.secret.")
    sys.exit(1)
if not ALLOWED_USER_ID:
    logger.warning("ALLOWED_USER is missing! Handlers will not respond to anyone.")

# Configure Gemini
client = genai.Client(api_key=GOOGLE_API_KEY)

# --- TRACKING ---
TRACKED_ISSUES_FILE = "tracked_issues.json"
GITHUB_REPO = os.getenv("GITHUB_AGENTS_REPO", "Ghabs95/agents")


def load_tracked_issues():
    """Load tracked issues from file."""
    if os.path.exists(TRACKED_ISSUES_FILE):
        try:
            with open(TRACKED_ISSUES_FILE) as f:
                return json.load(f)
        except Exception as e:
            logger.error(f"Failed to load tracked issues: {e}")
    return {}


def save_tracked_issues(data):
    """Save tracked issues to file."""
    try:
        with open(TRACKED_ISSUES_FILE, "w") as f:
            json.dump(data, f, indent=2)
    except Exception as e:
        logger.error(f"Failed to save tracked issues: {e}")


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
    "feature": "✨ Feature",
    "bug": "🩹 Bug Fix",
    "hotfix": "🔥 Hotfix",
    "release": "📦 Release",
    "chore": "🧹 Chore",
    "improvement": "🚀 Improvement"
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
        "📊 **Monitoring & Tracking:**\n"
        "/status - View pending tasks in inbox\n"
        "/active - View tasks currently being worked on\n"
        "/track <issue#> - Subscribe to issue updates\n"
        "/untrack <issue#> - Stop tracking an issue\n"
        "/logs <issue#> - View task execution logs\n"
        "/logsfull <issue#> - Full log lines (no truncation)\n"
        "/comments <issue#> - View issue comments\n\n"
        "🔁 **Recovery & Control:**\n"
        "/reprocess <issue#> - Re-run agent processing\n"
        "/continue <issue#> - Check stuck agent status\n"
        "/kill <issue#> - Stop running agent process\n"
        "/respond <issue#> <text> - Respond to agent questions\n\n"
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
        "For commands that need parameters, type / and select the command, then add the parameters before sending.\n"
        "Send voice or text to create a task automatically."
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
        BotCommand("status", "Show pending tasks"),
        BotCommand("active", "Show active tasks"),
        BotCommand("track", "Subscribe to issue updates"),
        BotCommand("untrack", "Stop tracking an issue"),
        BotCommand("logs", "View task execution logs"),
        BotCommand("logsfull", "Full issue logs"),
        BotCommand("comments", "View issue comments"),
        BotCommand("reprocess", "Re-run agent processing"),
        BotCommand("continue", "Check stuck agent status"),
        BotCommand("kill", "Stop running agent"),
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
                4. Return JSON: {{"project": "key", "type": "type_key", "text": "transcription"}}
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
                3. Return JSON: {{"project": "key", "type": "type_key", "text": "{text_input}"}}
                Input: {text_input}
            """
        )

    # Parse Result
    try:
        result = json.loads(response.text.replace("```json", "").replace("```", ""))
        project = result.get("project", "inbox")
        task_type = result.get("type", "feature")
        content = result.get("text", "")
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
            f"# {TYPES.get(task_type, 'Task')}\n**Project:** {PROJECTS.get(project, project)}\n**Type:** {task_type}\n**Status:** Pending\n\n{content}")

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

    # Write File
    target_dir = os.path.join(BASE_DIR, project, ".github", "inbox")
    os.makedirs(target_dir, exist_ok=True)
    filename = f"{task_type}_{update.message.message_id}.md"

    with open(os.path.join(target_dir, filename), "w") as f:
        f.write(
            f"# {TYPES[task_type]}\n**Project:** {PROJECTS[project]}\n**Type:** {task_type}\n**Status:** Pending\n\n{text}")

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
    # Format: /assign 123 or /assign #123 or /assign https://github.com/owner/repo/issues/123
    if not context.args:
        await update.effective_message.reply_text(
            "⚠️ Usage: `/assign <issue#> [assignee]`\n\n"
            "Examples:\n"
            "  `/assign 5` (assigns to you / @me)\n"
            "  `/assign 5 copilot` (assigns to configured Copilot user)\n"
            "  `/assign https://github.com/Ghabs95/agents/issues/5 alice`",
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
            "⚠️ Usage: `/implement <issue#>`\n\nExamples:\n  `/implement 5`\n  `/implement #5`\n  `/implement https://github.com/Ghabs95/agents/issues/5`",
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
            "⚠️ Usage: `/prepare <issue#>`\n\nExamples:\n  `/prepare 5`\n  `/prepare #5`\n  `/prepare https://github.com/Ghabs95/agents/issues/5`",
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
    """Subscribe to issue updates and track status changes."""
    global tracked_issues
    logger.info(f"Track requested by user: {update.effective_user.id}")
    if ALLOWED_USER_ID and update.effective_user.id != ALLOWED_USER_ID:
        logger.warning(f"Unauthorized access attempt by ID: {update.effective_user.id}")
        return

    if not context.args:
        await update.effective_message.reply_text(
            "⚠️ Usage: /track <issue#>\n\nExample: /track 5"
        )
        return

    issue_num = context.args[0].lstrip("#")
    if not issue_num.isdigit():
        await update.effective_message.reply_text("❌ Invalid issue number.")
        return

    tracked_issues[issue_num] = {
        "added_at": datetime.now().isoformat(),
        "last_seen_state": None,
        "last_seen_labels": []
    }
    save_tracked_issues(tracked_issues)

    details = get_issue_details(issue_num)
    if details:
        await update.effective_message.reply_text(
            f"👁️ Now tracking issue #{issue_num}\n\n"
            f"Title: {details.get('title', 'N/A')}\n"
            f"Status: {details.get('state', 'N/A')}\n"
            f"Labels: {', '.join([l['name'] for l in details.get('labels', [])])}\n\n"
            f"🔗 https://github.com/{GITHUB_REPO}/issues/{issue_num}"
        )
    else:
        await update.effective_message.reply_text(
            f"⚠️ Could not fetch issue details, but tracking started.\n\n"
            f"🔗 https://github.com/{GITHUB_REPO}/issues/{issue_num}"
        )


async def untrack_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Stop tracking an issue."""
    global tracked_issues
    logger.info(f"Untrack requested by user: {update.effective_user.id}")
    if ALLOWED_USER_ID and update.effective_user.id != ALLOWED_USER_ID:
        logger.warning(f"Unauthorized access attempt by ID: {update.effective_user.id}")
        return

    if not context.args:
        await update.effective_message.reply_text("⚠️ Usage: /untrack <issue#>")
        return

    issue_num = context.args[0].lstrip("#")
    if issue_num in tracked_issues:
        del tracked_issues[issue_num]
        save_tracked_issues(tracked_issues)
        await update.effective_message.reply_text(
            f"✅ Stopped tracking issue #{issue_num}\n\n"
            f"🔗 https://github.com/{GITHUB_REPO}/issues/{issue_num}"
        )
    else:
        await update.effective_message.reply_text(f"❌ Issue #{issue_num} is not being tracked.")


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
        try:
            with open(latest, "r") as f:
                lines = f.readlines()[-50:]
            timeline += f"- [{os.path.basename(latest)}]\n"
            for line in lines:
                timeline += f"  {line.rstrip()}\n"
        except Exception as e:
            timeline += f"- Failed to read {os.path.basename(latest)}: {e}\n"
    else:
        latest_tail = read_latest_log_tail(task_file, max_lines=50)
        if latest_tail:
            for log in latest_tail:
                timeline += f"- {log}\n"
        else:
            timeline += "- No task logs found.\n"

    await context.bot.edit_message_text(
        chat_id=update.effective_chat.id,
        message_id=msg.message_id,
        text=timeline
    )


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
        await update.effective_message.reply_text("⚠️ Usage: /continue <issue#> [prompt]\n\nExample: /continue 4 Please proceed with implementation")
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


async def comments_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """View recent comments on a GitHub issue."""
    logger.info(f"Comments requested by user: {update.effective_user.id}")
    if ALLOWED_USER_ID and update.effective_user.id != ALLOWED_USER_ID:
        logger.warning(f"Unauthorized access attempt by ID: {update.effective_user.id}")
        return

    if not context.args:
        await update.effective_message.reply_text("⚠️ Usage: /comments <issue#>\n\nExample: /comments 4")
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
            "Example: /respond 4 The surveyor feature should allow users to view property boundaries and measurements"
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


# --- MAIN ---
if __name__ == '__main__':
    app = ApplicationBuilder().token(TELEGRAM_TOKEN).build()
    # Register commands on startup (Telegram client menu)
    app.post_init = on_startup

    conv_handler = ConversationHandler(
        entry_points=[CommandHandler("new", start_selection)],
        states={
            SELECT_PROJECT: [CallbackQueryHandler(project_selected)],
            SELECT_TYPE: [CallbackQueryHandler(type_selected)],
            INPUT_TASK: [MessageHandler(filters.TEXT | filters.VOICE, save_task)]
        },
        fallbacks=[CommandHandler("cancel", cancel)],
        per_message=False
    )

    app.add_handler(conv_handler)
    app.add_handler(CommandHandler("start", start_handler))
    app.add_handler(CommandHandler("help", help_handler))
    app.add_handler(CommandHandler("status", status_handler))
    app.add_handler(CommandHandler("active", active_handler))
    app.add_handler(CommandHandler("track", track_handler))
    app.add_handler(CommandHandler("untrack", untrack_handler))
    app.add_handler(CommandHandler("logs", logs_handler))
    app.add_handler(CommandHandler("logsfull", logsfull_handler))
    app.add_handler(CommandHandler("comments", comments_handler))
    app.add_handler(CommandHandler("reprocess", reprocess_handler))
    app.add_handler(CommandHandler("continue", continue_handler))
    app.add_handler(CommandHandler("kill", kill_handler))
    app.add_handler(CommandHandler("respond", respond_handler))
    app.add_handler(CommandHandler("assign", assign_handler))
    app.add_handler(CommandHandler("implement", implement_handler))
    app.add_handler(CommandHandler("prepare", prepare_handler))
    # Exclude commands from the auto-router catch-all
    app.add_handler(MessageHandler((filters.TEXT | filters.VOICE) & (~filters.COMMAND), hands_free_handler))

    print("Nexus (Google Edition) Online...")
    app.run_polling(drop_pending_updates=True, allowed_updates=Update.ALL_TYPES)

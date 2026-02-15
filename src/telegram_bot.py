import json
import logging
import os
import re
import subprocess
import sys
from dotenv import load_dotenv
from google import genai
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, BotCommand, ReplyKeyboardMarkup, ReplyKeyboardRemove
from telegram.ext import (
    ApplicationBuilder, ContextTypes, CommandHandler,
    MessageHandler, CallbackQueryHandler, ConversationHandler, filters
)

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


# --- 0. HELP & INFO ---
async def help_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Lists available commands and usage info."""
    logger.info(f"Help triggered by user: {update.effective_user.id}")
    if ALLOWED_USER_ID and update.effective_user.id != ALLOWED_USER_ID:
        logger.warning(f"Unauthorized access attempt by ID: {update.effective_user.id}")
        return

    help_text = (
        "🤖 **Nexus (Google Edition) Commands**\n\n"
        "✨ **Guided Mode:**\n"
        "/new - Start a menu-driven task creation\n"
        "/cancel - Abort the current guided process\n\n"
        "⚡ **Hands-Free Mode:**\n"
        "Just send a **Voice Note** or **Text Message** directly. "
        "Gemini will automatically transcribe, route, and save the task "
        "based on its content!\n\n"
        "📊 **Monitoring:**\n"
        "/status - View pending tasks in inbox\n"
        "/active - View tasks currently being worked on\n"
        "/assign <issue#> - Assign GitHub issue to yourself\n"
        "/implement <issue#> - Request Copilot agent implementation (needs ProjectLead approval)\n"
        "/prepare <issue#> - Add Copilot-friendly instructions to issue\n\n"
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
        "Send voice or text to create a task automatically."
    )

    keyboard = [
        ["/new"],
        ["/status"],
        ["/active"],
        ["/assign"],
        ["/help"]
    ]
    reply_markup = ReplyKeyboardMarkup(keyboard, resize_keyboard=True, one_time_keyboard=False)

    await update.message.reply_text(welcome, reply_markup=reply_markup)


async def on_startup(application):
    """Register bot commands so they appear in the Telegram client menu."""
    cmds = [
        BotCommand("new", "Start selection mode"),
        BotCommand("status", "Show pending tasks"),
        BotCommand("active", "Show active tasks"),
        BotCommand("assign", "Assign an issue"),
        BotCommand("implement", "Request Copilot implementation"),
        BotCommand("prepare", "Prepare issue for Copilot"),
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
        audio_file = await client.aio.files.upload(file="temp_voice.ogg")

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
async def status_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Shows pending tasks in inbox folders."""
    logger.info(f"Status triggered by user: {update.effective_user.id}")
    if ALLOWED_USER_ID and update.effective_user.id != ALLOWED_USER_ID:
        logger.warning(f"Unauthorized access attempt by ID: {update.effective_user.id}")
        return

    status_text = "📥 **Inbox Status** (Pending Tasks)\n\n"
    total_tasks = 0

    for project_key, project_name in PROJECTS.items():
        inbox_dir = os.path.join(BASE_DIR, project_key, ".github", "inbox")
        if os.path.exists(inbox_dir):
            files = [f for f in os.listdir(inbox_dir) if f.endswith(".md")]
            if files:
                status_text += f"**{project_name}:** {len(files)} task(s)\n"
                total_tasks += len(files)
                # Show first 3 files as preview
                for f in files[:3]:
                    task_type = f.split('_')[0]
                    emoji = TYPES.get(task_type, "📝")
                    status_text += f"  • {emoji} `{f}`\n"
                if len(files) > 3:
                    status_text += f"  ... +{len(files) - 3} more\n"
                status_text += "\n"

    if total_tasks == 0:
        status_text += "✨ No pending tasks in inbox!\n"
    else:
        status_text += f"**Total:** {total_tasks} pending task(s)"

    await update.message.reply_text(status_text, parse_mode='Markdown')


async def active_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Shows active tasks being worked on."""
    logger.info(f"Active triggered by user: {update.effective_user.id}")
    if ALLOWED_USER_ID and update.effective_user.id != ALLOWED_USER_ID:
        logger.warning(f"Unauthorized access attempt by ID: {update.effective_user.id}")
        return

    active_text = "🚀 **Active Tasks** (In Progress)\n\n"
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
                active_text += f"**{display_name}:** {len(files)} task(s)\n"
                total_active += len(files)
                for f in files[:3]:
                    task_type = f.split('_')[0]
                    emoji = TYPES.get(task_type, "📝")
                    active_text += f"  • {emoji} `{f}`\n"
                if len(files) > 3:
                    active_text += f"  ... +{len(files) - 3} more\n"
                active_text += "\n"

    if total_active == 0:
        active_text += "💤 No active tasks at the moment.\n"
    else:
        active_text += f"**Total:** {total_active} active task(s)"

    await update.message.reply_text(active_text, parse_mode='Markdown')


async def assign_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Assigns a GitHub issue to the user."""
    logger.info(f"Assign triggered by user: {update.effective_user.id}")
    if ALLOWED_USER_ID and update.effective_user.id != ALLOWED_USER_ID:
        logger.warning(f"Unauthorized access attempt by ID: {update.effective_user.id}")
        return

    # Parse issue number from command
    # Format: /assign 123 or /assign #123 or /assign https://github.com/owner/repo/issues/123
    if not context.args:
        await update.message.reply_text(
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
        await update.message.reply_text("❌ Invalid issue number. Please use a number like `5` or `#5`.", parse_mode='Markdown')
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
    msg = await update.message.reply_text(f"🔄 Assigning issue #{issue_number}...")
    
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
    app.add_handler(CommandHandler("assign", assign_handler))
    app.add_handler(CommandHandler("implement", implement_handler))
    app.add_handler(CommandHandler("prepare", prepare_handler))
    # Exclude commands from the auto-router catch-all
    app.add_handler(MessageHandler((filters.TEXT | filters.VOICE) & (~filters.COMMAND), hands_free_handler))

    print("Nexus (Google Edition) Online...")
    app.run_polling(drop_pending_updates=True, allowed_updates=Update.ALL_TYPES)

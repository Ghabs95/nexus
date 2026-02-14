import os
import logging
import json
import google.generativeai as genai
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    ApplicationBuilder, ContextTypes, CommandHandler, 
    MessageHandler, CallbackQueryHandler, ConversationHandler, filters
)
from pydub import AudioSegment

# --- CONFIGURATION ---
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
GOOGLE_API_KEY = os.getenv("AI_API_KEY")
GOOGLE_AI_MODEL = os.getenv("AI_MODEL")
ALLOWED_USER_ID = os.getenv("ALLOWED_USER")
BASE_DIR = "/home/ubuntu/git"

# Configure Gemini
genai.configure(api_key=GOOGLE_API_KEY)
model = genai.GenerativeModel(GOOGLE_AI_MODEL)

# --- DATA ---
PROJECTS = {
    "casit": "Case Italia",
    "wlbl": "Wallible",
    "bm": "Biome",
    "inbox": "General Inbox"
}
TYPES = {
    "feature": "✨ Feature",
    "bug": "Hz Bug Fix",
    "improvement": "🚀 Improvement"
}

# --- STATES ---
SELECT_PROJECT, SELECT_TYPE, INPUT_TASK = range(3)

logging.basicConfig(format='%(asctime)s - %(name)s - %(levelname)s - %(message)s', level=logging.INFO)

# --- HELPER: GEMINI AUDIO PROCESSOR ---
async def process_audio_with_gemini(voice_file_id, context):
    """Downloads Telegram audio, converts to mp3, sends to Gemini for text."""
    # 1. Download
    new_file = await context.bot.get_file(voice_file_id)
    await new_file.download_to_drive("temp_voice.ogg")
    
    # 2. Convert (Gemini needs standard audio formats)
    AudioSegment.from_ogg("temp_voice.ogg").export("temp_voice.mp3", format="mp3")
    
    # 3. Upload & Transcribe
    audio_file = genai.upload_file("temp_voice.mp3")
    
    # Prompt just for transcription
    response = model.generate_content([
        "Transcribe this audio exactly. Return ONLY the text.",
        audio_file
    ])
    
    # Cleanup
    if os.path.exists("temp_voice.mp3"): os.remove("temp_voice.mp3")
    if os.path.exists("temp_voice.ogg"): os.remove("temp_voice.ogg")
    
    return response.text.strip()

# --- 1. HANDS-FREE MODE (Auto-Router) ---
async def hands_free_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ALLOWED_USER_ID: return

    text = ""
    status_msg = await update.message.reply_text("⚡ Gemini Listening...")

    # A. Handle Audio
    if update.message.voice:
        # Download and Convert
        new_file = await context.bot.get_file(update.message.voice.file_id)
        await new_file.download_to_drive("temp_voice.ogg")
        AudioSegment.from_ogg("temp_voice.ogg").export("temp_voice.mp3", format="mp3")
        
        # Multimodal Prompt: "Listen and Route"
        audio_file = genai.upload_file("temp_voice.mp3")
        
        response = model.generate_content([
            f"""
            You are a project router. Listen to the audio.
            1. Transcribe the text.
            2. Map it to one of these keys: {list(PROJECTS.keys())}.
            3. Return JSON: {{"project": "key", "text": "transcription"}}
            """,
            audio_file
        ])
        
        # Cleanup
        os.remove("temp_voice.mp3")
        os.remove("temp_voice.ogg")

    # B. Handle Text
    else:
        text_input = update.message.text
        response = model.generate_content(f"""
            Map this text to one of these keys: {list(PROJECTS.keys())}.
            Return JSON: {{"project": "key", "text": "{text_input}"}}
            Input: {text_input}
        """)

    # Parse Result
    try:
        result = json.loads(response.text.replace("```json", "").replace("```", ""))
        project = result.get("project", "inbox")
        content = result.get("text", "")
    except:
        await context.bot.edit_message_text(chat_id=update.effective_chat.id, message_id=status_msg.message_id, text="⚠️ JSON Error")
        return

    # Save to File
    target_dir = os.path.join(BASE_DIR, project, ".github", "inbox")
    os.makedirs(target_dir, exist_ok=True)
    filename = f"voice_task_{update.message.message_id}.md"
    
    with open(os.path.join(target_dir, filename), "w") as f:
        f.write(f"# Auto-Routed Task\n**Project:** {PROJECTS.get(project, project)}\n**Content:** {content}")

    await context.bot.edit_message_text(
        chat_id=update.effective_chat.id, 
        message_id=status_msg.message_id, 
        text=f"✅ Routed to `{project}`\n📝 *{content}*"
    )

# --- 2. SELECTION MODE (Menu) ---
# (Steps 1 & 2 are purely Telegram UI, no AI needed)

async def start_selection(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ALLOWED_USER_ID: return
    keyboard = [[InlineKeyboardButton(name, callback_data=code)] for code, name in PROJECTS.items()]
    await update.message.reply_text("📂 **Select Project:**", reply_markup=InlineKeyboardMarkup(keyboard), parse_mode='Markdown')
    return SELECT_PROJECT

async def project_selected(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    context.user_data['project'] = query.data
    keyboard = [[InlineKeyboardButton(name, callback_data=code)] for code, name in TYPES.items()]
    await query.edit_message_text(f"📂 Project: **{PROJECTS[query.data]}**\n\n🛠 **Select Type:**", reply_markup=InlineKeyboardMarkup(keyboard), parse_mode='Markdown')
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
        f.write(f"# {TYPES[task_type]}\n**Project:** {PROJECTS[project]}\n**Status:** Pending\n\n{text}")

    await update.message.reply_text(f"✅ Saved to `{project}`.")
    return ConversationHandler.END

async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("❌ Cancelled.")
    return ConversationHandler.END

# --- MAIN ---
if __name__ == '__main__':
    app = ApplicationBuilder().token(TELEGRAM_TOKEN).build()
    
    conv_handler = ConversationHandler(
        entry_points=[CommandHandler("new", start_selection)],
        states={
            SELECT_PROJECT: [CallbackQueryHandler(project_selected)],
            SELECT_TYPE: [CallbackQueryHandler(type_selected)],
            INPUT_TASK: [MessageHandler(filters.TEXT | filters.VOICE, save_task)]
        },
        fallbacks=[CommandHandler("cancel", cancel)]
    )

    app.add_handler(conv_handler)
    app.add_handler(MessageHandler(filters.TEXT | filters.VOICE, hands_free_handler))

    print("Nexus (Google Edition) Online...")
    app.run_polling()
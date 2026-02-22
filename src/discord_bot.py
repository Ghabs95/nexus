import os
import sys
import logging
import asyncio
import io
import discord
from discord.ext import commands

# Setup logging
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

# Add src directories to check for local imports
sys.path.append(os.path.dirname(os.path.abspath(__file__)))
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from config import (
    DISCORD_TOKEN,
    DISCORD_ALLOWED_USER_IDS,
    DISCORD_GUILD_ID,
    AI_PERSONA,
    ORCHESTRATOR_CONFIG
)

from services.memory_service import (
    get_chat_history,
    append_message,
    create_chat,
    list_chats,
    delete_chat,
    switch_chat,
    get_active_chat,
)
from orchestration.ai_orchestrator import get_orchestrator
from utils.voice_utils import transcribe_audio
from handlers.inbox_routing_handler import process_inbox_task


# --- SETUP BOT ---
intents = discord.Intents.default()
intents.message_content = True  # Required to read text messages
bot = commands.Bot(command_prefix="!", intents=intents)

# Initialize Orchestrator
orchestrator = get_orchestrator(ORCHESTRATOR_CONFIG)

def check_permission(user_id: int) -> bool:
    """Check if the user is allowed to interact with the bot."""
    if not DISCORD_ALLOWED_USER_IDS:
        return True
    return user_id in DISCORD_ALLOWED_USER_IDS

# --- DISCORD UI VIEWS (Similar to Telegram Inline Keyboards) ---

class ChatMenuView(discord.ui.View):
    def __init__(self, user_id: int):
        super().__init__(timeout=None)
        self.user_id = user_id

    @discord.ui.button(label="📝 New Chat", style=discord.ButtonStyle.primary, custom_id="chat:new")
    async def new_chat(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not check_permission(interaction.user.id):
            return
        
        create_chat(interaction.user.id)
        # Re-render the menu
        await send_chat_menu(interaction, interaction.user.id)

    @discord.ui.button(label="📋 Switch Chat", style=discord.ButtonStyle.secondary, custom_id="chat:list")
    async def switch_chat_list(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not check_permission(interaction.user.id):
            return
        
        view = ChatListView(interaction.user.id)
        await interaction.response.edit_message(content="**Select a chat:**", view=view)

    @discord.ui.button(label="🗑️ Delete Current", style=discord.ButtonStyle.danger, custom_id="chat:delete")
    async def delete_active_chat(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not check_permission(interaction.user.id):
            return
            
        active_chat_id = get_active_chat(interaction.user.id)
        if active_chat_id:
            delete_chat(interaction.user.id, active_chat_id)
        
        # After deleting, send the main menu which will pick the next active chat or create a default
        await send_chat_menu(interaction, interaction.user.id)

class ChatListView(discord.ui.View):
    def __init__(self, user_id: int):
        super().__init__(timeout=None)
        self.user_id = user_id
        
        chats = list_chats(user_id)
        active_chat_id = get_active_chat(user_id)
        
        for c in chats:
            chat_id = c.get("id")
            title = c.get("title", "Chat")
            
            # Truncate title for button label limits (Discord limit is 80)
            if len(title) > 70:
                title = title[:67] + "..."
                
            label = f"✅ {title}" if chat_id == active_chat_id else title
            style = discord.ButtonStyle.success if chat_id == active_chat_id else discord.ButtonStyle.secondary
            
            # Using dynamic callback creation
            button = discord.ui.Button(label=label, style=style, custom_id=f"switch_chat:{chat_id}")
            button.callback = self.create_switch_callback(chat_id)
            self.add_item(button)
            
        # Back button
        back_btn = discord.ui.Button(label="⬅️ Back", style=discord.ButtonStyle.danger, custom_id="chat:back")
        back_btn.callback = self.back_callback
        self.add_item(back_btn)

    def create_switch_callback(self, chat_id: str):
        async def callback(interaction: discord.Interaction):
            switch_chat(interaction.user.id, chat_id)
            await send_chat_menu(interaction, interaction.user.id)
        return callback
        
    async def back_callback(self, interaction: discord.Interaction):
        await send_chat_menu(interaction, interaction.user.id)


async def send_chat_menu(interaction: discord.Interaction, user_id: int):
    """Helper to send or edit the current message with the main chat menu."""
    active_chat_id = get_active_chat(user_id)
    chats = list_chats(user_id)
    
    active_chat_title = "Unknown"
    for c in chats:
        if c.get("id") == active_chat_id:
            active_chat_title = c.get("title")
            break

    text = f"🗣️ **Nexus Chat Menu**\n\n"
    text += f"**Active Chat:** {active_chat_title}\n"
    text += f"_(All conversational history is saved under this thread)_"
    
    view = ChatMenuView(user_id)
    
    # If this is responding to a button click, edit the message
    if interaction.response.is_done():
        await interaction.message.edit(content=text, view=view)
    else:
        await interaction.response.edit_message(content=text, view=view)


# --- SLASH COMMANDS ---

@bot.tree.command(name="chat", description="Manage conversational chat threads")
async def chat_command(interaction: discord.Interaction):
    if not check_permission(interaction.user.id):
        await interaction.response.send_message("🔒 Unauthorized.", ephemeral=True)
        return
        
    user_id = interaction.user.id
    active_chat_id = get_active_chat(user_id)
    chats = list_chats(user_id)
    
    active_chat_title = "Unknown"
    for c in chats:
        if c.get("id") == active_chat_id:
            active_chat_title = c.get("title")
            break

    text = f"🗣️ **Nexus Chat Menu**\n\n"
    text += f"**Active Chat:** {active_chat_title}\n"
    text += f"_(All conversational history is saved under this thread)_"
    
    view = ChatMenuView(user_id)
    await interaction.response.send_message(content=text, view=view)


# --- MESSAGE HANDLING ---

@bot.event
async def on_message(message: discord.Message):
    # Ignore bot's own messages
    if message.author == bot.user:
        return
        
    # Ignore messages not from allowed user
    if not check_permission(message.author.id):
        return
        
    # Ignore slash commands or other prefix commands
    if message.content.startswith("!") or message.content.startswith("/"):
        return

    text = ""
    status_msg = await message.reply("⚡ Processing...")

    # Check for voice attachments (Discord native voice messages are just .ogg attachments)
    if message.attachments:
        attachment = message.attachments[0]
        if attachment.content_type and "audio/ogg" in attachment.content_type:
            logger.info("Processing voice message...")
            
            # Download audio to a BytesIO object
            audio_data = io.BytesIO()
            await attachment.save(audio_data)
            audio_data.seek(0)
            
            # Since our transcribe_audio expects a path, write to a temp file
            import tempfile
            with tempfile.NamedTemporaryFile(suffix=".oga", delete=False) as tmp:
                tmp.write(audio_data.read())
                tmp_path = tmp.name
                
            try:
                # Transcribe
                text = transcribe_audio(tmp_path)
            finally:
                if os.path.exists(tmp_path):
                    os.remove(tmp_path)
                    
            if not text:
                logger.warning("Voice transcription returned empty text")
                await status_msg.edit(content="⚠️ Transcription failed")
                return
                
    # If no voice, or in addition to voice, use message text
    if not text:
        text = message.content

    if not text:
        await status_msg.edit(content="I didn't understand that.")
        return

    logger.info(f"Detecting intent for: {text[:50]}...")
    intent_result = orchestrator.run_text_to_speech_analysis(text=text, task="detect_intent")
    
    intent = "task"
    if isinstance(intent_result, dict) and "intent" in intent_result:
        intent = intent_result.get("intent", "task")
    elif isinstance(intent_result, dict) and intent_result.get("text"):
        # Handle case where LLM wrapped JSON in markdown
        import json
        try:
            clean = intent_result["text"].replace("```json", "").replace("```", "").strip()
            parsed = json.loads(clean)
            intent = parsed.get("intent", "task")
        except:
            pass
            
    if intent == "conversation":
        user_id = message.author.id
        history = get_chat_history(user_id)
        append_message(user_id, "user", text)
        
        await status_msg.edit(content="🤖 **Nexus:** Thinking...")
        
        chat_result = orchestrator.run_text_to_speech_analysis(
            text=text, 
            task="business_chat",
            history=history,
            persona=AI_PERSONA
        )
        
        reply_text = chat_result.get("text", "I'm offline right now, how can I help later?")
        append_message(user_id, "assistant", reply_text)
        
        await status_msg.edit(content=f"🤖 **Nexus**: \n\n{reply_text}")
        return

    # If it's a task, route through the shared inbox_routing_handler
    result = await process_inbox_task(text, orchestrator, str(message.id))
    
    # Store pending_resolution state if manual project selection is needed
    if not result["success"] and "pending_resolution" in result:
        # Note: We don't have a direct equivalent to Telegram's `context.user_data` here easily accessible across events
        # In a robust implementation, we would store this pending state in Redis or memory, mapping message.author.id to the state
        # For this PoC, we send the error message which asks them to reply with a project key, 
        # but the fallback logic to actually *catch* that project key reply is slightly different.
        # Given this is just a quick port of the happy path, we'll log it.
        logger.warning(f"Task needs manual project resolution: {result['pending_resolution']}")

    await status_msg.edit(content=result["message"])


@bot.event
async def on_ready():
    logger.info(f"Discord bot connected as {bot.user}")
    
    # Sync slash commands
    if DISCORD_GUILD_ID:
        guild = discord.Object(id=DISCORD_GUILD_ID)
        bot.tree.copy_global_to(guild=guild)
        await bot.tree.sync(guild=guild)
        logger.info(f"Synced slash commands to guild {DISCORD_GUILD_ID}")
    else:
        await bot.tree.sync()
        logger.info("Synced slash commands globally")


if __name__ == "__main__":
    if not DISCORD_TOKEN:
        logger.error("DISCORD_TOKEN environment variable not set.")
        sys.exit(1)
        
    bot.run(DISCORD_TOKEN)

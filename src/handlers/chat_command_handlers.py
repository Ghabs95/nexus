import logging
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ContextTypes
from services.memory_service import list_chats, get_active_chat, create_chat, delete_chat, set_active_chat

logger = logging.getLogger(__name__)

async def chat_menu_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handler for the /chat command to show the active chat and options."""
    user_id = update.effective_user.id
    
    active_chat_id = get_active_chat(user_id)
    chats = list_chats(user_id)
    
    active_chat_title = "Unknown"
    for c in chats:
        if c.get("id") == active_chat_id:
            active_chat_title = c.get("title")
            break

    text = f"🗣️ *Nexus Chat Menu*\n\n"
    text += f"*Active Chat:* {active_chat_title}\n"
    text += f"_(All conversational history is saved under this thread)_"
    
    keyboard = [
        [InlineKeyboardButton("📝 New Chat", callback_data="chat:new"),
         InlineKeyboardButton("📋 Switch Chat", callback_data="chat:list")],
        [InlineKeyboardButton("🗑️ Delete Current", callback_data=f"chat:delete:{active_chat_id}")]
    ]
    
    reply_markup = InlineKeyboardMarkup(keyboard)
    
    await update.message.reply_text(
        text=text,
        reply_markup=reply_markup,
        parse_mode="Markdown"
    )

async def chat_callback_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handles inline keyboard callbacks for the chat menu."""
    query = update.callback_query
    await query.answer()
    
    user_id = update.effective_user.id
    data = query.data
    
    if data == "chat:new":
        chat_id = create_chat(user_id)
        active_chat_id = get_active_chat(user_id)
        chats = list_chats(user_id)
        active_chat_title = next((c.get("title") for c in chats if c.get("id") == active_chat_id), "Unknown")
        
        text = f"🗣️ *Nexus Chat Menu*\n\n✅ *New Chat Created & Activated!*\n*Active Chat:* {active_chat_title}"
        keyboard = [
            [InlineKeyboardButton("📝 New Chat", callback_data="chat:new"),
             InlineKeyboardButton("📋 Switch Chat", callback_data="chat:list")],
            [InlineKeyboardButton("🗑️ Delete Current", callback_data=f"chat:delete:{active_chat_id}")]
        ]
        await query.edit_message_text(text=text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="Markdown")
        
    elif data == "chat:list":
        chats = list_chats(user_id)
        active_chat_id = get_active_chat(user_id)
        
        if not chats:
            await query.edit_message_text(text="You have no saved chats.")
            return
            
        text = "📋 *Select a Chat Thread:*"
        keyboard = []
        for c in chats:
            chat_id = c.get("id")
            title = c.get("title")
            prefix = "✅ " if chat_id == active_chat_id else ""
            keyboard.append([InlineKeyboardButton(f"{prefix}{title}", callback_data=f"chat:select:{chat_id}")])
            
        keyboard.append([InlineKeyboardButton("🔙 Back to Menu", callback_data="chat:menu")])
        await query.edit_message_text(text=text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="Markdown")
        
    elif data.startswith("chat:delete:"):
        chat_id = data.split(":")[2]
        delete_chat(user_id, chat_id)
        
        # Go back to the main menu view
        active_chat_id = get_active_chat(user_id)
        chats = list_chats(user_id)
        active_chat_title = next((c.get("title") for c in chats if c.get("id") == active_chat_id), "Unknown")
        
        text = f"🗣️ *Nexus Chat Menu*\n\n🗑️ *Chat Deleted!*\n*Active Chat:* {active_chat_title}"
        keyboard = [
            [InlineKeyboardButton("📝 New Chat", callback_data="chat:new"),
             InlineKeyboardButton("📋 Switch Chat", callback_data="chat:list")],
            [InlineKeyboardButton("🗑️ Delete Current", callback_data=f"chat:delete:{active_chat_id}")]
        ]
        await query.edit_message_text(text=text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="Markdown")
        
    elif data.startswith("chat:select:"):
        chat_id = data.split(":")[2]
        set_active_chat(user_id, chat_id)
        
        active_chat_id = get_active_chat(user_id)
        chats = list_chats(user_id)
        active_chat_title = next((c.get("title") for c in chats if c.get("id") == active_chat_id), "Unknown")
        
        text = f"🗣️ *Nexus Chat Menu*\n\n✅ *Switched Active Chat!*\n*Active Chat:* {active_chat_title}"
        keyboard = [
            [InlineKeyboardButton("📝 New Chat", callback_data="chat:new"),
             InlineKeyboardButton("📋 Switch Chat", callback_data="chat:list")],
            [InlineKeyboardButton("🗑️ Delete Current", callback_data=f"chat:delete:{active_chat_id}")]
        ]
        await query.edit_message_text(text=text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="Markdown")
        
    elif data == "chat:menu":
        active_chat_id = get_active_chat(user_id)
        chats = list_chats(user_id)
        active_chat_title = next((c.get("title") for c in chats if c.get("id") == active_chat_id), "Unknown")

        text = f"🗣️ *Nexus Chat Menu*\n\n*Active Chat:* {active_chat_title}\n_(All conversational history is saved under this thread)_"
        keyboard = [
            [InlineKeyboardButton("📝 New Chat", callback_data="chat:new"),
             InlineKeyboardButton("📋 Switch Chat", callback_data="chat:list")],
            [InlineKeyboardButton("🗑️ Delete Current", callback_data=f"chat:delete:{active_chat_id}")]
        ]
        await query.edit_message_text(text=text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="Markdown")

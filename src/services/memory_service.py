import json
import logging
import uuid
from datetime import datetime
import redis
from config import REDIS_URL

logger = logging.getLogger(__name__)

# Singleton redis client
_redis_client = None

def get_redis() -> redis.Redis:
    global _redis_client
    if _redis_client is None:
        try:
            _redis_client = redis.from_url(REDIS_URL, decode_responses=True)
            _redis_client.ping()
        except redis.ConnectionError as e:
            logger.error(f"Failed to connect to Redis at {REDIS_URL}: {e}")
            raise
    return _redis_client

def create_chat(user_id: int, title: str = None) -> str:
    """Creates a new chat and sets it as active."""
    r = get_redis()
    chat_id = uuid.uuid4().hex
    if not title:
        title = f"Chat {datetime.now().strftime('%Y-%m-%d %H:%M')}"
        
    chat_data = {
        "id": chat_id,
        "title": title,
        "created_at": datetime.now().isoformat()
    }
    
    r.hset(f"user_chats:{user_id}", chat_id, json.dumps(chat_data))
    set_active_chat(user_id, chat_id)
    return chat_id

def get_active_chat(user_id: int) -> str:
    """Gets the active chat_id for a user. Creates a default one if none exists."""
    r = get_redis()
    active_chat_id = r.get(f"active_chat:{user_id}")
    
    # Verify the chat still exists
    if active_chat_id and r.hexists(f"user_chats:{user_id}", active_chat_id):
        return active_chat_id
        
    # If no active chat, see if they have *any* chats
    chats = r.hgetall(f"user_chats:{user_id}")
    if chats:
        # Pick the first one (or newest)
        first_chat_id = list(chats.keys())[0]
        set_active_chat(user_id, first_chat_id)
        return first_chat_id
        
    # No chats exist at all, create a new one
    return create_chat(user_id, "Main Chat")

def set_active_chat(user_id: int, chat_id: str) -> bool:
    """Sets the active chat for a user. Returns True if successful."""
    r = get_redis()
    if r.hexists(f"user_chats:{user_id}", chat_id):
        r.set(f"active_chat:{user_id}", chat_id)
        return True
    return False

def list_chats(user_id: int) -> list:
    """Lists all chats for a user, sorted by newest first."""
    r = get_redis()
    chats_raw = r.hgetall(f"user_chats:{user_id}")
    chats = []
    for chat_str in chats_raw.values():
        try:
            chats.append(json.loads(chat_str))
        except json.JSONDecodeError:
            continue
            
    # Sort by created_at descending
    chats.sort(key=lambda x: x.get("created_at", ""), reverse=True)
    return chats

def delete_chat(user_id: int, chat_id: str) -> bool:
    """Deletes a chat and its history. Returns True if successful."""
    r = get_redis()
    if r.hexists(f"user_chats:{user_id}", chat_id):
        r.hdel(f"user_chats:{user_id}", chat_id)
        r.delete(f"chat_history:{chat_id}")
        
        # If the deleted chat was active, un-set it
        active = r.get(f"active_chat:{user_id}")
        if active == chat_id:
            r.delete(f"active_chat:{user_id}")
        return True
    return False

def get_chat_history(user_id: int, limit: int = 10, chat_id: str = None) -> str:
    """Retrieve the recent chat history for a given chat. Uses active chat if not provided."""
    try:
        r = get_redis()
        if not chat_id:
            chat_id = get_active_chat(user_id)
            
        key = f"chat_history:{chat_id}"
        messages = r.lrange(key, -limit, -1)
        if not messages:
            return ""
        
        history = []
        for msg in messages:
            try:
                data = json.loads(msg)
                role = data.get("role", "unknown")
                text = data.get("text", "")
                history.append(f"{role.capitalize()}: {text}")
            except json.JSONDecodeError:
                continue
        return "\n".join(history)
    except Exception as e:
        logger.error(f"Error retrieving chat history for {user_id}: {e}")
        return ""

def append_message(user_id: int, role: str, text: str, ttl_seconds: int = 604800, chat_id: str = None):
    """Append a message to the chat history with a TTL (default 7 days)."""
    try:
        r = get_redis()
        if not chat_id:
            chat_id = get_active_chat(user_id)
            
        key = f"chat_history:{chat_id}"
        message = json.dumps({"role": role, "text": text})
        
        pipe = r.pipeline()
        pipe.rpush(key, message)
        # Keep only the last 30 messages to avoid indefinitely growing lists
        pipe.ltrim(key, -30, -1)
        # Extend the TTL of the chat history every time a message is added
        pipe.expire(key, ttl_seconds)
        pipe.execute()
    except Exception as e:
        logger.error(f"Error appending chat message for {user_id}: {e}")

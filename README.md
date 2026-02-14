# Nexus - Telegram AI Task Router Bot

A Telegram bot powered by Google Gemini that intelligently routes voice messages and text input to project-specific GitHub inboxes. Perfect for quickly capturing ideas and tasks on the go.

## Features

- **🎧 Voice Recognition**: Transcribe voice messages using Google Gemini
- **🤖 Smart Auto-Routing**: Automatically detect project and route tasks
- **📂 Menu-Driven Mode**: Manual project and task type selection
- **💾 File-Based Storage**: Save tasks directly to project inbox directories
- **🔐 User Authentication**: Only authorized users can access the bot

## Two Operating Modes

### 1. Hands-Free Mode (Default)
Simply send voice or text to the bot - it automatically:
- Transcribes audio (if applicable)
- Maps content to the appropriate project
- Routes and saves the task

### 2. Selection Mode (`/new` command)
Step through an interactive menu:
1. Select a project
2. Choose a task type (Feature, Bug, Improvement)
3. Send voice or text input
4. Task is saved with full metadata

## Supported Projects

- **casit** - Case Italia
- **wlbl** - Wallible
- **bm** - Biome
- **inbox** - General Inbox (default)

## Task Types

- ✨ Feature
- 🐛 Bug Fix
- 🚀 Improvement

## Setup Instructions

### Prerequisites
- Python 3.8+
- Telegram account
- Google Gemini API key
- FFmpeg (for audio processing)

### Installation

1. **Install FFmpeg**:
   ```bash
   sudo apt-get install ffmpeg
   ```

2. **Install Python dependencies**:
   ```bash
   pip install -r requirements.txt
   ```

3. **Set environment variables**:
   ```bash
   export TELEGRAM_TOKEN="your_telegram_bot_token"
   export AI_API_KEY="your_google_gemini_api_key"
   export AI_MODEL="gemini-3.0-flash"  # or desired model
   export ALLOWED_USER="your_user_id"
   ```

### Running the Bot

```bash
python telegram_bot.py
```

The bot will start polling and display: `Nexus (Google Edition) Online...`

## Usage

### Auto-Router Mode
1. Start a chat with the bot
2. Send a voice message or text: *"Add pagination to user dashboard"*
3. Bot transcribes, analyzes, and automatically saves to the appropriate project inbox

### Menu Mode
1. Send `/new` command
2. Select a project from the inline keyboard
3. Select a task type
4. Send voice or text description
5. Task is saved with project and type metadata

## File Structure

Tasks are saved as markdown files in:
```
/home/ubuntu/git/{project}/.github/inbox/{task_type}_{message_id}.md
```

Example file content:
```markdown
# ✨ Feature
**Project:** Wallible
**Status:** Pending

Add dark mode support to mobile app
```

## Commands

- `/new` - Start selection mode
- `/cancel` - Cancel current conversation

## Configuration

Edit the following dictionaries in `telegram_bot.py` to customize:

- `PROJECTS` - Add or modify supported projects
- `TYPES` - Customize task type categories
- `BASE_DIR` - Change task storage location

## Requirements

See [requirements.txt](requirements.txt) for all dependencies.

## Troubleshooting

- **"JSON Error" message**: Bot couldn't parse Gemini's response. Try rephrasing your input.
- **Audio not transcribing**: Ensure FFmpeg is installed and audio format is supported.
- **"Unauthorized user"**: Check `ALLOWED_USER` environment variable matches your Telegram user ID.

## License

This project is part of the Nexus task management system.

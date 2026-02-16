import glob
import json
import logging
import os
import re
import shutil
import subprocess
import time
import requests
from dotenv import load_dotenv
from google import genai

# Config
load_dotenv("vars.secret")
BASE_DIR = os.getenv("BASE_DIR", "/home/ubuntu/git")
GITHUB_AGENTS_REPO = os.getenv("GITHUB_AGENTS_REPO", "ghabs/agents")
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("ALLOWED_USER")
GOOGLE_API_KEY = os.getenv("AI_API_KEY")
GOOGLE_AI_MODEL = os.getenv("AI_MODEL") or "gemini-1.5-flash"
SLEEP_INTERVAL = 10
STUCK_AGENT_THRESHOLD = 60  # 60 seconds - alert if no activity for 1 minute

# Track alerted agents to avoid spam
alerted_agents = set()
notified_comments = set()  # Track comment IDs we've already notified about
auto_chained_agents = {}  # Track issue -> log_file to avoid re-chaining same completion

# Initialize Gemini client if API key is available
gemini_client = genai.Client(api_key=GOOGLE_API_KEY) if GOOGLE_API_KEY else None

# Data directory for persistent state
DATA_DIR = os.path.join(os.path.dirname(os.path.dirname(__file__)), "data")
LAUNCHED_AGENTS_FILE = os.path.join(DATA_DIR, "launched_agents.json")
WORKFLOW_STATE_FILE = os.path.join(DATA_DIR, "workflow_state.json")

def load_launched_agents():
    """Load recently launched agents from persistent storage."""
    if os.path.exists(LAUNCHED_AGENTS_FILE):
        try:
            with open(LAUNCHED_AGENTS_FILE) as f:
                data = json.load(f)
                # Clean up entries older than 5 minutes
                cutoff = time.time() - 300
                return {k: v for k, v in data.items() if v.get('timestamp', 0) > cutoff}
        except Exception as e:
            logger.error(f"Failed to load launched agents: {e}")
    return {}

def save_launched_agents(data):
    """Save launched agents to persistent storage."""
    try:
        os.makedirs(DATA_DIR, exist_ok=True)
        with open(LAUNCHED_AGENTS_FILE, "w") as f:
            json.dump(data, f, indent=2)
    except Exception as e:
        logger.error(f"Failed to save launched agents: {e}")

def load_workflow_state():
    """Load workflow state (paused/stopped issues) from persistent storage."""
    if os.path.exists(WORKFLOW_STATE_FILE):
        try:
            with open(WORKFLOW_STATE_FILE) as f:
                return json.load(f)
        except Exception as e:
            logger.error(f"Failed to load workflow state: {e}")
    return {}

def save_workflow_state(data):
    """Save workflow state to persistent storage."""
    try:
        os.makedirs(DATA_DIR, exist_ok=True)
        with open(WORKFLOW_STATE_FILE, "w") as f:
            json.dump(data, f, indent=2)
    except Exception as e:
        logger.error(f"Failed to save workflow state: {e}")

def set_workflow_state(issue_num, state):
    """Set workflow state for an issue (paused, stopped, or active)."""
    data = load_workflow_state()
    if state == "active":
        data.pop(str(issue_num), None)  # Remove entry to mark as active
    else:
        data[str(issue_num)] = {"state": state, "timestamp": time.time()}
    save_workflow_state(data)

def get_workflow_state(issue_num):
    """Get workflow state for an issue. Returns 'active', 'paused', 'stopped', or None."""
    data = load_workflow_state()
    return data.get(str(issue_num), {}).get("state")

# Load persisted state
launched_agents_tracker = load_launched_agents()

# Project Configuration
# Each project maps to its agents directory (for Copilot CLI) and workspace (for file operations).
# The workspace is the parent folder containing the actual sub-repos.
PROJECT_CONFIG = {
    "case_italia": {
        "agents_dir": "ghabs/agents/casit-agents",
        "workspace": "case_italia",
        "github_repo": GITHUB_AGENTS_REPO,
    },
    "wallible": {
        "agents_dir": "ghabs/agents/wlbl-agents",
        "workspace": "wallible",
        "github_repo": GITHUB_AGENTS_REPO,
    },
    "biome": {
        "agents_dir": "ghabs/agents/bm-agents",
        "workspace": "biome",
        "github_repo": GITHUB_AGENTS_REPO,
    },
    "nexus": {
        "agents_dir": None,  # Nexus tasks are handled directly
        "workspace": "ghabs/nexus",
        "github_repo": "Ghabs95/nexus",
    }
}

# Logging
logging.basicConfig(
    format='%(asctime)s - %(levelname)s - %(message)s',
    level=logging.INFO,
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler("inbox_processor.log")
    ]
)
logger = logging.getLogger("InboxProcessor")


def slugify(text):
    """Converts text to a branch-friendly slug."""
    text = text.lower()
    text = re.sub(r'[^a-z0-9\s-]', '', text)
    text = re.sub(r'\s+', '-', text)
    return text[:50]


def send_telegram_alert(message):
    """Send alert via Telegram."""
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        logger.warning("Telegram credentials not configured, skipping alert")
        return False
    
    try:
        url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
        data = {
            "chat_id": TELEGRAM_CHAT_ID,
            "text": message,
            "parse_mode": "Markdown"
        }
        response = requests.post(url, json=data, timeout=10)
        return response.status_code == 200
    except Exception as e:
        logger.error(f"Failed to send Telegram alert: {e}")
        return False


def check_stuck_agents():
    """Monitor agent processes and alert if stuck."""
    try:
        # Find all copilot log files
        log_pattern = os.path.join(BASE_DIR, "**", ".github", "tasks", "logs", "copilot_*.log")
        log_files = glob.glob(log_pattern, recursive=True)
        
        current_time = time.time()
        
        for log_file in log_files:
            # Extract issue number from filename: copilot_4_20260215_112450.log
            match = re.search(r"copilot_(\d+)_", os.path.basename(log_file))
            if not match:
                continue
            
            issue_num = match.group(1)
            
            # Check last modification time
            try:
                last_modified = os.path.getmtime(log_file)
                time_since_update = current_time - last_modified
                
                # If log hasn't been updated in threshold seconds
                if time_since_update > STUCK_AGENT_THRESHOLD:
                    # Extract PID from log filename to check if THIS specific agent is still running
                    # Format: copilot_4_20260215_112450.log -> check for PID in ps output
                    result = subprocess.run(
                        ["pgrep", "-af", f"copilot.*issues/{issue_num}"],
                        text=True, capture_output=True
                    )
                    
                    if result.stdout:
                        # Agent(s) running for this issue - but check if it's for THIS log file
                        # Get the timestamp from the log filename
                        timestamp_match = re.search(r"copilot_\d+_(\d{8}_\d{6})\.log", os.path.basename(log_file))
                        
                        # For now, only alert if the log file is the LATEST one for this issue
                        all_logs_for_issue = sorted([f for f in log_files if f"copilot_{issue_num}_" in f], 
                                                   key=os.path.getmtime, reverse=True)
                        
                        if all_logs_for_issue and log_file != all_logs_for_issue[0]:
                            # This is an old log file, skip it
                            continue
                        
                        # Agent is running but log hasn't updated
                        pid_match = re.search(r"^(\d+)", result.stdout)
                        pid = pid_match.group(1) if pid_match else "unknown"
                        
                        # Track by issue + PID to avoid re-alerting for same stuck process
                        alert_key = f"{issue_num}_{pid}"
                        if alert_key in alerted_agents:
                            continue  # Already alerted for this issue+pid combo
                        
                        minutes = int(time_since_update / 60)
                        message = (
                            f"⚠️ **Agent Stuck Alert**\n\n"
                            f"Issue: #{issue_num}\n"
                            f"PID: {pid}\n"
                            f"No log activity for {minutes} minutes\n\n"
                            f"**This agent is trying to invoke other agents instead of completing.**\n"
                            f"**Instructions must be followed by the agent.**\n\n"
                            f"Actions:\n"
                            f"• Check /logs {issue_num}\n"
                            f"• Use /kill {issue_num} to stop and restart"
                        )
                        
                        if send_telegram_alert(message):
                            logger.info(f"🚨 Sent stuck agent alert for issue #{issue_num} (PID: {pid})")
                            alerted_agents.add(alert_key)
                        else:
                            logger.warning(f"Failed to send alert for issue #{issue_num}")
            
            except OSError as e:
                logger.error(f"Error checking log file {log_file}: {e}")
                
    except Exception as e:
        logger.error(f"Error in check_stuck_agents: {e}")


def check_agent_comments():
    """Monitor GitHub issues for agent comments requesting input."""
    try:
        # Get all open issues with workflow labels
        result = subprocess.run(
            ["gh", "issue", "list", "--repo", GITHUB_AGENTS_REPO,
             "--label", "workflow:full,workflow:shortened,workflow:fast-track",
             "--state", "open", "--json", "number", "--jq", ".[].number"],
            text=True, capture_output=True, timeout=10
        )
        
        if not result.stdout:
            return
        
        issue_numbers = result.stdout.strip().split("\n")
        
        for issue_num in issue_numbers:
            if not issue_num:
                continue
                
            # Get issue comments
            result = subprocess.run(
                ["gh", "issue", "view", issue_num, "--repo", GITHUB_AGENTS_REPO,
                 "--json", "comments", "--jq", ".comments[] | select(.author.login == \"Ghabs95\") | {id: .id, body: .body, createdAt: .createdAt}"],
                text=True, capture_output=True, timeout=10
            )
            
            if not result.stdout:
                continue
            
            # Parse comments (one JSON object per line)
            for line in result.stdout.strip().split("\n"):
                if not line:
                    continue
                
                try:
                    comment = json.loads(line)
                    comment_id = str(comment.get("id"))
                    body = comment.get("body", "")
                    
                    # Skip if we've already notified about this comment
                    if comment_id in notified_comments:
                        continue
                    
                    # Check if comment contains questions or blockers
                    needs_input = any(pattern in body.lower() for pattern in [
                        "questions for @ghabs",
                        "questions for `@ghabs",  # Escaped mention
                        "waiting for @ghabs",
                        "waiting for `@ghabs",  # Escaped mention
                        "need your input",
                        "please provide",
                        "owner:** @ghabs",
                        "owner:** `@ghabs",  # Escaped mention
                        "blocker:",
                        "your input to proceed"
                    ])
                    
                    if needs_input:
                        # Extract preview (first 200 chars)
                        preview = body[:200] + "..." if len(body) > 200 else body
                        
                        message = (
                            f"📋 **Agent Needs Input**\n\n"
                            f"Issue: #{issue_num}\n"
                            f"Agent: @ProjectLead\n\n"
                            f"Preview:\n{preview}\n\n"
                            f"**Actions:**\n"
                            f"• View full: /logs {issue_num}\n"
                            f"• Respond: /respond {issue_num} <your answer>\n"
                            f"• View on GitHub: https://github.com/{GITHUB_AGENTS_REPO}/issues/{issue_num}"
                        )
                        
                        if send_telegram_alert(message):
                            logger.info(f"📨 Sent input request alert for issue #{issue_num}")
                            notified_comments.add(comment_id)
                        else:
                            logger.warning(f"Failed to send input alert for issue #{issue_num}")
                
                except json.JSONDecodeError as e:
                    logger.error(f"Failed to parse comment JSON: {e}")
                    
    except subprocess.TimeoutExpired:
        logger.warning("GitHub comment check timed out")
    except Exception as e:
        logger.error(f"Error in check_agent_comments: {e}")


# Agent workflow mappings - keyed by tier name for reference
# Actual chaining extracts next agent from log output dynamically
WORKFLOW_CHAIN = {
    "full": [  # new_feature workflow
        ("ProjectLead", "Vision & Scope"),  # Step 1
        ("Atlas", "Technical Feasibility"),  # Step 2
        ("Architect", "Architecture Design"),  # Step 3
        ("ProductDesigner", "UX Design"),  # Step 4
        ("Tier2Lead", "Implementation"),  # Step 5
        ("QAGuard", "Quality Gate"),  # Step 6
        ("Privacy", "Compliance Gate"),  # Step 7
        ("OpsCommander", "Deployment"),  # Step 8
        ("Scribe", "Documentation")  # Step 9
    ],
    "shortened": [  # bug_fix workflow
        ("ProjectLead", "Triage"),  # Step 1
        ("Tier2Lead", "Root Cause Analysis"),  # Step 2
        ("Tier2Lead", "Fix"),  # Step 3
        ("QAGuard", "Verify"),  # Step 4
        ("OpsCommander", "Deploy"),  # Step 5
        ("Scribe", "Document")  # Step 6
    ],
    "fast-track": [  # fast-track workflow
        ("ProjectLead", "Triage"),  # Step 1
        ("Copilot", "Implementation"),  # Step 2
        ("QAGuard", "Verify"),  # Step 3
        ("OpsCommander", "Deploy")  # Step 4
    ]
}


def check_completed_agents():
    """Monitor for completed agent steps and auto-chain to next agent.
    
    Checks both:
    1. Log files in .github/tasks/logs/
    2. GitHub issue comments for completion markers
    """
    try:
        # FIRST: Check GitHub comments for recent completions
        try:
            # Get all open workflow issues (OR logic: any of the workflow labels)
            workflow_labels = ["workflow:full", "workflow:shortened", "workflow:fast-track"]
            issue_numbers = set()
            
            for label in workflow_labels:
                result = subprocess.run(
                    ["gh", "issue", "list", "--repo", GITHUB_AGENTS_REPO,
                     "--label", label,
                     "--state", "open", "--json", "number", "--jq", ".[].number"],
                    text=True, capture_output=True, timeout=10
                )
                
                if result.stdout and result.stdout.strip():
                    issue_numbers.update(result.stdout.strip().split("\n"))
            
            # Process each unique open workflow issue
            for issue_num in issue_numbers:
                    if not issue_num:
                        continue
                    
                    # Check workflow state (paused/stopped = skip auto-chain)
                    state = get_workflow_state(issue_num)
                    if state in ("paused", "stopped"):
                        logger.debug(f"Skipping auto-chain for issue #{issue_num} (state: {state})")
                        continue
                    
                    # Check if we've already processed this issue
                    comment_chain_key = f"comment_{issue_num}"
                    if comment_chain_key in auto_chained_agents:
                        continue
                    
                    # Get recent comments
                    try:
                        result = subprocess.run(
                            ["gh", "issue", "view", issue_num, "--repo", GITHUB_AGENTS_REPO,
                             "--json", "comments", "--jq", ".comments[-1].body"],
                            text=True, capture_output=True, timeout=10
                        )
                        
                        if result.stdout and result.stdout.strip():
                            comment_body = result.stdout.strip()
                            
                            # Look for "Ready for @Agent" patterns (with or without backticks)
                            # Patterns: "Ready for `@Agent`", "Ready for @Agent", "routing to @Agent"
                            ready_patterns = [
                                r"Ready for `@(\w+)`",
                                r"Ready for @(\w+)",
                                r"ready for `@(\w+)`",
                                r"ready for @(\w+)",
                                r"routing to `@(\w+)`",
                                r"routing to @(\w+)",
                                r"🔄 @(\w+) \(.*?\) ← NEXT"  # From workflow progress sections
                            ]
                            
                            next_agent = None
                            for pattern in ready_patterns:
                                match = re.search(pattern, comment_body, re.IGNORECASE)
                                if match:
                                    next_agent = match.group(1)
                                    logger.info(f"🔍 Detected completion in GitHub comment for issue #{issue_num}, next agent: @{next_agent}")
                                    break
                            
                            if not next_agent:
                                continue  # No agent mentioned, skip this issue
                            
                            # CRITICAL: Check for duplicate launches
                            # Check 1: Running processes
                            check_result = subprocess.run(
                                ["pgrep", "-af", f"copilot.*issues/{issue_num}"],
                                text=True, capture_output=True
                            )
                            if check_result.stdout:
                                logger.info(f"⏭️ Agent already running for issue #{issue_num} (PID found), skipping auto-chain")
                                auto_chained_agents[comment_chain_key] = True
                                continue
                            
                            # Check 2: Recently launched (persistent tracker)
                            launched_agents_tracker = load_launched_agents()  # Reload to catch other process launches
                            if issue_num in launched_agents_tracker:
                                last_launch = launched_agents_tracker[issue_num]
                                age = time.time() - last_launch.get('timestamp', 0)
                                if age < 120:  # Within last 2 minutes
                                    logger.info(f"⏭️ Agent recently launched for issue #{issue_num} ({age:.0f}s ago), skipping duplicate")
                                    auto_chained_agents[comment_chain_key] = True
                                    continue
                            
                            # Check 3: Recent log files (within last 2 minutes)
                            recent_logs = glob.glob(os.path.join(BASE_DIR, "**", ".github", "tasks", "logs", f"copilot_{issue_num}_*.log"), recursive=True)
                            if recent_logs:
                                recent_logs.sort(key=lambda p: os.path.getmtime(p), reverse=True)
                                latest_log_age = time.time() - os.path.getmtime(recent_logs[0])
                                if latest_log_age < 120:  # Within last 2 minutes
                                    logger.info(f"⏭️ Recent log file for issue #{issue_num} ({latest_log_age:.0f}s old), skipping duplicate")
                                    auto_chained_agents[comment_chain_key] = True
                                    continue
                            
                            # Launch the agent
                            try:
                                result = subprocess.run(
                                    ["gh", "issue", "view", issue_num, "--repo", GITHUB_AGENTS_REPO,
                                     "--json", "body"],
                                    text=True, capture_output=True, timeout=10
                                )
                                data = json.loads(result.stdout)
                                body = data.get("body", "")
                                
                                # Debug: log first 200 chars of body
                                logger.debug(f"Issue #{issue_num} body preview: {body[:200]}")
                                
                                # Find task file (format: **Task File:** `/path/to/file`)
                                task_file_match = re.search(r"\*\*Task File:\*\*\s*`([^`]+)`", body)
                                if not task_file_match:
                                    logger.warning(f"No task file in issue #{issue_num}")
                                    continue
                                
                                task_file = task_file_match.group(1)
                                if not os.path.exists(task_file):
                                    logger.warning(f"Task file not found: {task_file}")
                                    continue
                                        
                                
                                # Get project config
                                project_root = None
                                for key, cfg in PROJECT_CONFIG.items():
                                    workspace = cfg.get("workspace")
                                    if workspace:
                                        workspace_abs = os.path.join(BASE_DIR, workspace)
                                        if task_file.startswith(workspace_abs):
                                            project_root = key
                                            config = cfg
                                            break
                                
                                if not project_root or not config.get("agents_dir"):
                                    logger.warning(f"No project config for task file: {task_file}")
                                    continue
                                
                                # Read task content
                                with open(task_file, "r") as f:
                                    task_content = f.read()
                                
                                # Determine tier
                                type_match = re.search(r"\*\*Type:\*\*\s*(.+)", task_content)
                                task_type = type_match.group(1).strip().lower() if type_match else "feature"
                                tier_name, _, _ = get_sop_tier(task_type)
                                
                                issue_url = f"https://github.com/{GITHUB_AGENTS_REPO}/issues/{issue_num}"
                                agents_abs = os.path.join(BASE_DIR, config["agents_dir"])
                                workspace_abs = os.path.join(BASE_DIR, config["workspace"])
                                
                                # Create continuation prompt
                                continuation_prompt = (
                                    f"You are @{next_agent}. The previous workflow step is complete.\n\n"
                                    f"Your task: Begin your step in the workflow.\n"
                                    f"Read recent GitHub comments to understand what's been completed.\n"
                                    f"Then perform your assigned work and post a status update.\n"
                                    f"End with a completion marker like: 'Ready for `@NextAgent`'"
                                )
                                
                                pid = invoke_copilot_agent(
                                    agents_dir=agents_abs,
                                    workspace_dir=workspace_abs,
                                    issue_url=issue_url,
                                    tier_name=tier_name,
                                    task_content=task_content,
                                    continuation=True,
                                    continuation_prompt=continuation_prompt
                                )
                                
                                if pid:
                                    logger.info(f"🔗 Auto-chained from GitHub comment to @{next_agent} for issue #{issue_num} (PID: {pid})")
                                    auto_chained_agents[comment_chain_key] = True
                                    
                                    # Track launch in persistent storage
                                    launched_agents_tracker[issue_num] = {
                                        'pid': pid,
                                        'timestamp': time.time(),
                                        'agent': next_agent
                                    }
                                    save_launched_agents(launched_agents_tracker)
                                    
                                    # Send notification
                                    message = (
                                        f"🔗 **Auto-Chain from GitHub Comment**\n\n"
                                        f"Issue: #{issue_num}\n"
                                        f"Next agent: @{next_agent}\n"
                                        f"PID: {pid}\n\n"
                                        f"🔗 https://github.com/{GITHUB_AGENTS_REPO}/issues/{issue_num}"
                                    )
                                    send_telegram_alert(message)
                            except Exception as e:
                                logger.error(f"Error launching agent from GitHub comment: {e}")
                    
                    except subprocess.TimeoutExpired:
                        logger.warning(f"Timeout checking comments for issue #{issue_num}")
                    except Exception as e:
                        logger.error(f"Error checking GitHub comments for issue #{issue_num}: {e}")
        
        except Exception as e:
            logger.debug(f"GitHub comment detection not available: {e}")
        
        # SECOND: Check log files for completions (existing logic)
        log_pattern = os.path.join(BASE_DIR, "**", ".github", "tasks", "logs", "copilot_*.log")
        log_files = glob.glob(log_pattern, recursive=True)
        
        for log_file in log_files:
            # Extract issue number from filename
            match = re.search(r"copilot_(\d+)_", os.path.basename(log_file))
            if not match:
                continue
            
            issue_num = match.group(1)
            
            # Check workflow state (paused/stopped = skip auto-chain)
            state = get_workflow_state(issue_num)
            if state in ("paused", "stopped"):
                logger.debug(f"Skipping auto-chain for issue #{issue_num} (state: {state})")
                continue
            
            # Skip if we've already chained from this exact log file
            chain_key = f"{issue_num}_{os.path.basename(log_file)}"
            if chain_key in auto_chained_agents:
                continue
            
            # Check if process is still running
            result = subprocess.run(
                ["pgrep", "-af", f"copilot.*issues/{issue_num}"],
                text=True, capture_output=True
            )
            
            # Only process if agent has finished
            if not result.stdout:
                # Check log for completion indicators
                try:
                    with open(log_file, "r") as f:
                        log_content = f.read()
                    
                    # Look for completion markers
                    completion_patterns = [
                        r"Step \d+ Complete",
                        r"✅.*(?:ready|complete|done)",
                        r"(?:Technical|[A-Z].*?) (?:assessment|work|review) (?:complete|done)",
                        r"Testing complete",
                        r"Task Complete",
                        r"`@\w+`",  # Any backtick-escaped mention is a signal of context
                    ]
                    
                    completed = any(re.search(pattern, log_content, re.IGNORECASE) for pattern in completion_patterns)
                    
                    if not completed:
                        continue
                    
                    # Extract next agent mention - look for ANY backtick-escaped agent name
                    # Patterns: `@AgentName`, "Ball is in `@Agent`'s court", etc.
                    next_agent_matches = re.findall(r"`@(\w+)`", log_content)
                    
                    if not next_agent_matches:
                        logger.warning(f"Completion detected but no backtick-escaped agent mentioned in issue #{issue_num}")
                        auto_chained_agents[chain_key] = True  # Mark as processed to avoid re-processing
                        continue
                    
                    # Use the last mentioned agent (most likely the next one)
                    next_agent = next_agent_matches[-1]
                    
                    # Get issue details to find task file
                    try:
                        result = subprocess.run(
                            ["gh", "issue", "view", issue_num, "--repo", GITHUB_AGENTS_REPO,
                             "--json", "body"],
                            text=True, capture_output=True, timeout=10
                        )
                        data = json.loads(result.stdout)
                        body = data.get("body", "")
                        
                        # Find task file (format: **Task File:** `/path/to/file`)
                        task_file_match = re.search(r"\*\*Task File:\*\*\s*`([^`]+)`", body)
                        if not task_file_match:
                            logger.warning(f"No task file found for issue #{issue_num}")
                            continue
                        
                        task_file = task_file_match.group(1)
                        if not os.path.exists(task_file):
                            logger.warning(f"Task file not found: {task_file}")
                            continue
                        
                        # Get project config
                        project_root = None
                        for key, cfg in PROJECT_CONFIG.items():
                            workspace = cfg.get("workspace")
                            if workspace:
                                workspace_abs = os.path.join(BASE_DIR, workspace)
                                if task_file.startswith(workspace_abs):
                                    project_root = key
                                    config = cfg
                                    break
                        
                        if not project_root or not config.get("agents_dir"):
                            logger.warning(f"No project config for task file: {task_file}")
                            continue
                        
                        # Read task content
                        with open(task_file, "r") as f:
                            task_content = f.read()
                        
                        # Determine tier
                        type_match = re.search(r"\*\*Type:\*\*\s*(.+)", task_content)
                        task_type = type_match.group(1).strip().lower() if type_match else "feature"
                        tier_name, _, _ = get_sop_tier(task_type)
                        
                        issue_url = f"https://github.com/{GITHUB_AGENTS_REPO}/issues/{issue_num}"
                        agents_abs = os.path.join(BASE_DIR, config["agents_dir"])
                        workspace_abs = os.path.join(BASE_DIR, config["workspace"])
                        
                        # CRITICAL: Check for duplicate launches
                        # Check 1: Running processes
                        check_running = subprocess.run(
                            ["pgrep", "-af", f"copilot.*issues/{issue_num}"],
                            text=True, capture_output=True
                        )
                        if check_running.stdout:
                            logger.info(f"⏭️ Agent already running for issue #{issue_num} (PID found), skipping auto-chain from log")
                            auto_chained_agents[chain_key] = True
                            continue
                        
                        # Check 2: Recently launched (persistent tracker)
                        launched_agents_tracker = load_launched_agents()  # Reload
                        if issue_num in launched_agents_tracker:
                            last_launch = launched_agents_tracker[issue_num]
                            age = time.time() - last_launch.get('timestamp', 0)
                            if age < 120:  # Within last 2 minutes
                                logger.info(f"⏭️ Agent recently launched for issue #{issue_num} ({age:.0f}s ago), skipping duplicate from log")
                                auto_chained_agents[chain_key] = True
                                continue
                        
                        # Launch next agent with clear instructions
                        continuation_prompt = (
                            f"You are @{next_agent}. The previous step has been completed by another agent.\n\n"
                            f"Your task: Complete the next step in the workflow.\n"
                            f"1. Review previous agent's work in GitHub comments and the task file\n"
                            f"2. Perform your assigned work for this step\n"
                            f"3. Update the task file with your results\n"
                            f"4. Post a GitHub comment with your findings\n"
                            f"5. **END YOUR RESPONSE WITH AN EXACT COMPLETION MARKER** (copy one):\n\n"
                            f"   ✅ Step X Complete - Ready for `@NextAgent`\n"
                            f"   OR\n"
                            f"   ✅ ready for `@NextAgent`\n\n"
                            f"**Replace 'NextAgent' with the actual name** (e.g., `@Architect`, `@QAGuard`)\n"
                            f"**IMPORTANT:** Use backticks around @AgentName - this is required for detection\n\n"
                            f"DO NOT attempt to invoke the next agent yourself.\n"
                            f"The system will automatically detect your completion marker and chain to the next agent.\n"
                            f"Simply complete your work, add the marker, and exit."
                        )
                        
                        pid = invoke_copilot_agent(
                            agents_dir=agents_abs,
                            workspace_dir=workspace_abs,
                            issue_url=issue_url,
                            tier_name=tier_name,
                            task_content=task_content,
                            continuation=True,
                            continuation_prompt=continuation_prompt
                        )
                        
                        if pid:
                            logger.info(f"🔗 Auto-chained to @{next_agent} for issue #{issue_num} (PID: {pid})")
                            auto_chained_agents[chain_key] = True
                            
                            # Track launch in persistent storage
                            launched_agents_tracker[issue_num] = {
                                'pid': pid,
                                'timestamp': time.time(),
                                'agent': next_agent
                            }
                            save_launched_agents(launched_agents_tracker)
                            
                            # Send notification
                            message = (
                                f"🔗 **Auto-Chain**\n\n"
                                f"Issue: #{issue_num}\n"
                                f"Previous agent completed\n"
                                f"Next agent: @{next_agent}\n"
                                f"PID: {pid}\n\n"
                                f"🔗 https://github.com/{GITHUB_AGENTS_REPO}/issues/{issue_num}"
                            )
                            send_telegram_alert(message)
                        else:
                            logger.error(f"Failed to auto-chain to @{next_agent} for issue #{issue_num}")
                    
                    except subprocess.TimeoutExpired:
                        logger.warning(f"Timeout fetching issue #{issue_num} for auto-chain")
                    except json.JSONDecodeError as e:
                        logger.error(f"Failed to parse issue data: {e}")
                    except Exception as e:
                        logger.error(f"Error in auto-chain for issue #{issue_num}: {e}")
                
                except Exception as e:
                    logger.error(f"Error reading log file {log_file}: {e}")
    
    except Exception as e:
        logger.error(f"Error in check_completed_agents: {e}")


# SOP Checklist Templates
SOP_FULL = """## SOP Checklist — New Feature
- [ ] 1. **Vision & Scope** — `Ghabs`: Founder's Check
- [ ] 2. **Technical Feasibility** — `Atlas`: HOW and WHEN
- [ ] 3. **Architecture Design** — `Architect`: ADR + breakdown
- [ ] 4. **UX Design** — `ProductDesigner`: Wireframes
- [ ] 5. **Implementation** — Tier 2 Lead: Code + tests
- [ ] 6. **Quality Gate** — `QAGuard`: Coverage check
- [ ] 7. **Compliance Gate** — `Privacy`: PIA (if user data)
- [ ] 8. **Deployment** — `OpsCommander`: Production
- [ ] 9. **Documentation** — `Scribe`: Changelog + docs"""

SOP_SHORTENED = """## SOP Checklist — Bug Fix
- [ ] 1. **Triage** — `ProjectLead`: Severity + routing
- [ ] 2. **Root Cause Analysis** — Tier 2 Lead
- [ ] 3. **Fix** — Tier 2 Lead: Code + regression test
- [ ] 4. **Verify** — `QAGuard`: Regression suite
- [ ] 5. **Deploy** — `OpsCommander`
- [ ] 6. **Document** — `Scribe`: Changelog"""

SOP_FAST_TRACK = """## SOP Checklist — Fast-Track
- [ ] 1. **Triage** — `ProjectLead`: Route to repo
- [ ] 2. **Implementation** — Copilot: Code + tests
- [ ] 3. **Verify** — `QAGuard`: Quick check
- [ ] 4. **Deploy** — `OpsCommander`"""


def get_sop_tier(task_type):
    """Returns (tier_name, sop_template, workflow_label) based on task type.
    
    Workflow mapping:
    - hotfix, chore, feature-simple, improvement-simple → fast-track (4 steps): 
        Triage, Implementation, Verify, Deploy
    - bug → shortened (6 steps): 
        Triage, RCA, Fix, Verify, Deploy, Doc
    - feature, improvement, release → full (9 steps): 
        Vision, Feasibility, Architecture, UX, Implementation, QA, Compliance, Deploy, Doc
    """
    if any(t in task_type for t in ["hotfix", "chore", "simple"]):
        return "fast-track", SOP_FAST_TRACK, "workflow:fast-track"
    elif "bug" in task_type:
        return "shortened", SOP_SHORTENED, "workflow:shortened"
    else:
        return "full", SOP_FULL, "workflow:full"


def get_workflow_name(tier_name):
    """Returns the workflow slash-command name for the tier."""
    if tier_name == "fast-track":
        return "bug_fix"  # Fast-track follows simplified bug_fix flow
    elif tier_name == "shortened":
        return "bug_fix"
    else:
        return "new_feature"


def is_final_agent(tier_name, agent_name):
    """Check if this agent is the final step in the workflow."""
    workflow = WORKFLOW_CHAIN.get(tier_name, [])
    if workflow and workflow[-1][0] == agent_name:
        return True
    return False


def create_github_issue(title, body, project, workflow_label, task_type, tier_name, github_repo):
    """Creates a GitHub Issue in the specified repo with SOP checklist."""
    type_label = f"type:{task_type}"
    project_label = f"project:{project}"

    cmd = [
        "gh", "issue", "create",
        "--repo", github_repo,
        "--title", title,
        "--body", body,
        "--label", f"{project_label},{type_label},{workflow_label}"
    ]

    try:
        result = subprocess.run(cmd, check=True, text=True, capture_output=True)
        issue_url = result.stdout.strip()
        logger.info(f"📋 Issue created: {issue_url}")
        return issue_url
    except subprocess.CalledProcessError as e:
        logger.error(f"Failed to create issue: {e.stderr}")
        return None
    except FileNotFoundError:
        logger.error("'gh' CLI not found. Install: https://cli.github.com")
        return None


def invoke_copilot_agent(agents_dir, workspace_dir, issue_url, tier_name, task_content, 
                         continuation=False, continuation_prompt=None):
    """Invokes Copilot CLI on the agents directory to process the task.

    Runs asynchronously (Popen) since agent execution can take several minutes.
    The @ProjectLead agent will follow the SOP workflow to:
    1. Triage the task
    2. Determine the target sub-repo within the workspace
    3. Route to the correct Tier 2 Lead for implementation
    
    Args:
        agents_dir: Path to agents directory
        workspace_dir: Path to workspace directory
        issue_url: GitHub issue URL
        tier_name: Workflow tier (full/shortened/fast-track)
        task_content: Task description
        continuation: If True, this is a continuation of previous work
        continuation_prompt: Custom prompt for continuation
    """
    workflow_name = get_workflow_name(tier_name)

    if continuation:
        # Auto-chained continuation: use custom continuation_prompt directly
        if continuation_prompt and continuation_prompt.startswith("You are @"):
            # This is an auto-chain to a different agent, use it as-is
            prompt = (
                f"{continuation_prompt}\n\n"
                f"Issue: {issue_url}\n"
                f"Tier: {tier_name}\n"
                f"Workflow: /{workflow_name}\n\n"
                f"Review the previous work in the GitHub comments and task file, then complete your step.\n\n"
                f"**GIT WORKFLOW (CRITICAL):**\n"
                f"1. Check the issue body for **Target Branch** field (e.g., `feat/surveyor-plan`)\n"
                f"2. Identify the correct sub-repo within the workspace (e.g., casit-be, casit-app, casit-omi)\n"
                f"3. In that sub-repo: \n"
                f"   - For feat/fix/chore: create branch from `develop`: `git checkout develop && git pull && git checkout -b <branch-name>`\n"
                f"   - For hotfix: create branch from `main`: `git checkout main && git pull && git checkout -b <branch-name>`\n"
                f"4. Make your changes and commit with descriptive messages\n"
                f"5. Push the branch: `git push -u origin <branch-name>`\n"
                f"6. Include branch name in your GitHub comment (e.g., 'Pushed to feat/surveyor-plan in casit-be')\n\n"
                f"⛔ **GIT SAFETY RULES (STRICT):**\n"
                f"❌ NEVER push to protected branches: `main`, `develop`, `master`, `test`, `staging`, `production`\n"
                f"❌ NEVER delete any branch: No `git branch -d` or `git push --delete`\n"
                f"✅ ONLY push to the dedicated feature branch specified in **Target Branch** field\n"
                f"✅ Valid branch prefixes: feat/*, fix/*, hotfix/*, chore/*, refactor/*, docs/*, build/*, ci/*\n"
                f"⚠️  Violating these rules can break production and cause team disruption\n\n"
                f"When you finish your work:\n"
                f"1. Commit and push all changes to the target branch\n"
                f"2. Update the task file with your results\n"
                f"3. Post a GitHub comment summarizing your work and confirming the push\n"
                f"4. END YOUR COMMENT with a clear completion marker:\n\n"
                f"   **Required format:** 'Ready for @NextAgentName'\n"
                f"   Example: 'Ready for @ProductDesigner to begin UX design'\n\n"
                f"5. Exit when done - DO NOT attempt to invoke the next agent\n\n"
                f"The system will automatically detect your completion and launch the next agent.\n\n"
                f"Task context:\n{task_content}"
            )
        else:
            # Manual continuation (user ran /continue): wrap with @ProjectLead context
            base_prompt = continuation_prompt or "Please continue with the next step."
            prompt = (
                f"You are @ProjectLead. You previously started working on this task:\n\n"
                f"Issue: {issue_url}\n"
                f"Tier: {tier_name}\n"
                f"Workflow: /{workflow_name}\n\n"
                f"⚠️ **CRITICAL:** Before doing anything:\n"
                f"1. Check the most recent GitHub comments\n"
                f"2. If the next agent has already been invoked (e.g., '@Atlas has been assigned'), DO NOT invoke them again\n"
                f"3. If your step is already complete with a completion marker, simply EXIT - do NOT continue\n"
                f"4. ONLY proceed if you are actually stuck DURING your step (incomplete work)\n\n"
                f"{base_prompt}\n\n"
                f"**GIT WORKFLOW (CRITICAL):**\n"
                f"1. Check the issue body for **Target Branch** field (e.g., `feat/surveyor-plan`)\n"
                f"2. Identify the correct sub-repo within the workspace (e.g., casit-be, casit-app, casit-omi)\n"
                f"3. In that sub-repo: \n"
                f"   - For feat/fix/chore: create from `develop`: `git checkout develop && git pull && git checkout -b <branch-name>`\n"
                f"   - For hotfix: create from `main`: `git checkout main && git pull && git checkout -b <branch-name>`\n"
                f"   - If branch exists: `git checkout <branch-name> && git pull`\n"
                f"4. Make your changes and commit with descriptive messages\n"
                f"5. Push the branch: `git push -u origin <branch-name>`\n"
                f"6. Include branch name in your GitHub comment\n\n"
                f"⛔ **GIT SAFETY RULES (STRICT):**\n"
                f"❌ NEVER push to protected branches: `main`, `develop`, `master`, `test`, `staging`, `production`\n"
                f"❌ NEVER delete any branch: No `git branch -d` or `git push --delete`\n"
                f"✅ ONLY push to the dedicated feature branch specified in **Target Branch** field\n"
                f"✅ Valid branch prefixes: feat/*, fix/*, hotfix/*, chore/*, refactor/*, docs/*, build/*, ci/*\n"
                f"⚠️  Violating these rules can break production and cause team disruption\n\n"
                f"If you do proceed:\n"
                f"- Review your previous work (check logs, session state, and git branches)\n"
                f"- Complete ONLY your step\n"
                f"- Commit and push all changes\n"
                f"- Post completion marker: 'Ready for `@NextAgent`'\n"
                f"- EXIT immediately\n\n"
                f"When you complete your step, end your GitHub comment with:\n"
                f"'Ready for @NextAgent' (e.g., 'Ready for @Atlas')\n\n"
                f"Original task content:\n{task_content}"
            )
    else:
        # Fresh start prompt for @ProjectLead
        prompt = (
            f"You are @ProjectLead. A new task has arrived and a GitHub issue has been created.\n\n"
            f"Issue: {issue_url}\n"
            f"Tier: {tier_name}\n"
            f"Workflow: /{workflow_name}\n\n"
            f"**YOUR JOB:** Triage and route only. DO NOT try to implement or invoke other agents.\n\n"
            f"**GIT WORKFLOW INSTRUCTION (for downstream agents):**\n"
            f"The issue body contains **Target Branch** field (e.g., `feat/surveyor-plan`).\n"
            f"When you route to implementation agents (@BackendLead, @MobileLead, etc.), remind them:\n"
            f"1. Work in the appropriate sub-repo (casit-be, casit-app, casit-omi, etc.)\n"
            f"2. Create branch from correct base:\n"
            f"   - feat/fix/chore/refactor/docs branches: from `develop`\n"
            f"   - hotfix branches: from `main`\n"
            f"3. Commit and push all changes to that branch\n"
            f"4. Mention the branch and push status in GitHub comments\n"
            f"5. ⛔ SAFETY: NEVER push to protected branches (main/develop/master/test/staging/production)\n"
            f"6. ⛔ SAFETY: NEVER delete branches\n"
            f"7. ✅ Valid branch prefixes: feat/*, fix/*, hotfix/*, chore/*, refactor/*, docs/*, build/*, ci/*\n\n"
            f"REQUIRED ACTIONS:\n"
            f"1. Analyze the task requirements\n"
            f"2. Identify which sub-repo(s) are affected\n"
            f"3. Update the task file with triage details\n"
            f"4. Post a GitHub comment showing:\n"
            f"   - Task severity\n"
            f"   - Target sub-repo(s)\n"
            f"   - Target branch (from issue body)\n"
            f"   - Which agent should handle it next\n"
            f"   - Use format: 'Ready for @NextAgent' (e.g., 'Ready for @Atlas')\n"
            f"5. **EXIT** - The system will auto-route to the next agent\n\n"
            f"**DO NOT:**\n"
            f"❌ Read other agent configuration files\n"
            f"❌ Use any 'invoke', 'task', or 'run tool' to start other agents\n"
            f"❌ Try to implement the feature yourself\n"
            f"❌ Make unnecessary commits or branch changes (you're just triaging)\n\n"
            f"**REQUIRED COMPLETION MARKER:**\n"
            f"Your final comment MUST include: 'Ready for @NextAgentName'\n"
            f"Example: 'Ready for @Atlas to assess technical feasibility'\n\n"
            f"Task details:\n{task_content}"
        )

    cmd = [
        "copilot",
        "-p", prompt,
        "--add-dir", BASE_DIR,  # Parent directory for cross-project access
        "--add-dir", workspace_dir,
        "--add-dir", agents_dir,
        "--allow-all-tools"
    ]

    mode = "continuation" if continuation else "initial"
    logger.info(f"🤖 Launching Copilot CLI agent in {agents_dir} (mode: {mode})")
    logger.info(f"   Workspace: {workspace_dir}")
    logger.info(f"   Workflow: /{workflow_name} (tier: {tier_name})")

    # Log copilot output to a file for debugging
    log_dir = os.path.join(workspace_dir, ".github", "tasks", "logs")
    os.makedirs(log_dir, exist_ok=True)
    timestamp = time.strftime("%Y%m%d_%H%M%S")
    issue_match = re.search(r"/issues/(\d+)", issue_url or "")
    issue_num = issue_match.group(1) if issue_match else "unknown"
    log_path = os.path.join(log_dir, f"copilot_{issue_num}_{timestamp}.log")
    logger.info(f"   Log file: {log_path}")

    try:
        log_file = open(log_path, "w")
        process = subprocess.Popen(
            cmd,
            cwd=workspace_dir,  # Start in workspace, not agents dir
            stdout=log_file,
            stderr=subprocess.STDOUT
        )
        logger.info(f"🚀 Copilot CLI launched (PID: {process.pid})")
        return process.pid
    except FileNotFoundError:
        logger.error("'copilot' CLI not found. Install: brew install copilot-cli")
        return None
    except Exception as e:
        logger.error(f"Failed to launch Copilot CLI: {e}")
        return None


def generate_issue_name(content, project_name):
    """Generate a concise issue name using Gemini AI.
    
    Returns a slugified name in format: "this-is-the-issue-name"
    Falls back to slugified content if Gemini is unavailable.
    """
    if not gemini_client:
        logger.warning("Gemini unavailable, using fallback slug generation")
        body = re.sub(r'^#.*\n', '', content)
        body = re.sub(r'\*\*.*\*\*.*\n', '', body)
        return slugify(body.strip()) or "generic-task"
    
    try:
        logger.info("Generating concise issue name with Gemini...")
        response = gemini_client.models.generate_content(
            model=GOOGLE_AI_MODEL,
            contents=f"""Generate a concise, descriptive issue name (3-6 words max) for this task.

Task content:
{content[:500]}

Project: {project_name}

Return ONLY the issue name in kebab-case format (e.g., "implement-user-authentication").
Do NOT include the project name, type prefix, or brackets.
Be specific but brief."""
        )
        
        suggested_name = response.text.strip()
        # Clean up response (remove quotes, extra formatting)
        suggested_name = suggested_name.strip('"`\'').strip()
        # Ensure it's slugified
        slug = slugify(suggested_name)
        
        if slug and len(slug) > 0:
            logger.info(f"✨ Gemini suggested: {slug}")
            return slug
        else:
            logger.warning("Gemini returned invalid slug, using fallback")
            raise ValueError("Invalid slug from Gemini")
            
    except Exception as e:
        logger.warning(f"Gemini name generation failed: {e}, using fallback")
        body = re.sub(r'^#.*\n', '', content)
        body = re.sub(r'\*\*.*\*\*.*\n', '', body)
        return slugify(body.strip()) or "generic-task"



def process_file(filepath):
    """Processes a single task file."""
    logger.info(f"Processing: {filepath}")

    try:
        with open(filepath, "r") as f:
            content = f.read()

        # Parse Metadata
        type_match = re.search(r'\*\*Type:\*\*\s*(.+)', content)
        task_type = type_match.group(1).strip().lower() if type_match else "feature"

        # Determine project from filepath
        # filepath is .../project/.github/inbox/file.md
        # project_root is .../project
        project_root = os.path.dirname(os.path.dirname(os.path.dirname(filepath)))
        project_name = os.path.basename(project_root)

        config = PROJECT_CONFIG.get(project_name)
        if not config:
            logger.warning(f"⚠️ No project config for '{project_name}', skipping.")
            return

        logger.info(f"Project: {project_name}")

        # Check if issue name was already generated (in telegram_bot)
        issue_name_match = re.search(r'\*\*Issue Name:\*\*\s*(.+)', content)
        if issue_name_match:
            slug = slugify(issue_name_match.group(1).strip())
            logger.info(f"✅ Using pre-generated issue name: {slug}")
        else:
            # Fallback: Generate concise issue name using Gemini AI
            slug = generate_issue_name(content, project_name)

        # Determine SOP tier
        tier_name, sop_template, workflow_label = get_sop_tier(task_type)
        sop_checklist = sop_template

        # Move file to project workspace active folder
        active_dir = os.path.join(project_root, ".github", "tasks", "active")
        os.makedirs(active_dir, exist_ok=True)
        new_filepath = os.path.join(active_dir, os.path.basename(filepath))
        logger.info(f"Moving task to active: {new_filepath}")
        shutil.move(filepath, new_filepath)

        # Create GitHub Issue with SOP checklist
        # Build type prefix for issue title
        type_prefixes = {
            "feature": "feat",
            "feature-simple": "feat",
            "bug": "fix",
            "hotfix": "hotfix",
            "chore": "chore",
            "refactor": "refactor",
            "improvement": "feat",
            "improvement-simple": "feat",
        }
        prefix = type_prefixes.get(task_type, task_type.split("-")[0] if "-" in task_type else task_type)
        issue_title = f"[{project_name}] {prefix}/{slug}"
        
        # Determine target branch name
        branch_name = f"{prefix}/{slug}"
        
        issue_body = f"""## Task
{content}

---

{sop_checklist}

---

**Project:** {project_name}
**Tier:** {tier_name}
**Target Branch:** `{branch_name}`
**Task File:** `{new_filepath}`"""

        issue_url = create_github_issue(
            title=issue_title,
            body=issue_body,
            project=project_name,
            workflow_label=workflow_label,
            task_type=task_type,
            tier_name=tier_name,
            github_repo=config["github_repo"]
        )

        if issue_url:
            # Append issue URL to the task file
            try:
                with open(new_filepath, 'a') as f:
                    f.write(f"\n\n**Issue:** {issue_url}\n")
            except Exception as e:
                logger.error(f"Failed to append issue URL: {e}")

        # Invoke Copilot CLI agent (if agents_dir is configured)
        agents_dir_val = config["agents_dir"]
        if agents_dir_val is not None and issue_url:
            agents_abs = os.path.join(BASE_DIR, agents_dir_val)
            workspace_abs = os.path.join(BASE_DIR, config["workspace"])

            pid = invoke_copilot_agent(
                agents_dir=agents_abs,
                workspace_dir=workspace_abs,
                issue_url=issue_url,
                tier_name=tier_name,
                task_content=content
            )

            if pid:
                # Log PID for tracking
                try:
                    with open(new_filepath, 'a') as f:
                        f.write(f"**Agent PID:** {pid}\n")
                except Exception as e:
                    logger.error(f"Failed to append PID: {e}")
        else:
            logger.info(f"ℹ️ No agents directory for {project_name}, skipping Copilot CLI invocation.")

        logger.info(f"✅ Dispatch complete for [{project_name}] {slug} (Tier: {tier_name})")

    except Exception as e:
        logger.error(f"Failed to process {filepath}: {e}")


def main():
    logger.info(f"Inbox Processor started on {BASE_DIR}")
    logger.info(f"Stuck agent monitoring enabled (threshold: {STUCK_AGENT_THRESHOLD}s)")
    logger.info(f"Agent comment monitoring enabled")
    last_check = time.time()
    check_interval = 60  # Check for stuck agents and comments every 60 seconds
    
    while True:
        # Scan for md files in project/.github/inbox/*.md
        pattern = os.path.join(BASE_DIR, "**", ".github", "inbox", "*.md")
        files = glob.glob(pattern, recursive=True)

        for filepath in files:
            process_file(filepath)
        
        # Periodically check for stuck agents and agent comments
        current_time = time.time()
        if current_time - last_check >= check_interval:
            check_stuck_agents()
            check_agent_comments()
            check_completed_agents()
            last_check = current_time

        time.sleep(SLEEP_INTERVAL)


if __name__ == "__main__":
    main()

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

# Config
load_dotenv("vars.secret")
BASE_DIR = os.getenv("BASE_DIR", "/home/ubuntu/git")
GITHUB_AGENTS_REPO = os.getenv("GITHUB_AGENTS_REPO", "ghabs/agents")
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("ALLOWED_USER")
SLEEP_INTERVAL = 10
STUCK_AGENT_THRESHOLD = 60  # 60 seconds - alert if no activity for 1 minute

# Track alerted agents to avoid spam
alerted_agents = set()
notified_comments = set()  # Track comment IDs we've already notified about
auto_chained_agents = {}  # Track issue -> log_file to avoid re-chaining same completion

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
            
            # Check if we've already alerted for this agent run
            alert_key = f"{issue_num}_{os.path.basename(log_file)}"
            if alert_key in alerted_agents:
                continue
            
            # Check last modification time
            try:
                last_modified = os.path.getmtime(log_file)
                time_since_update = current_time - last_modified
                
                # If log hasn't been updated in threshold seconds
                if time_since_update > STUCK_AGENT_THRESHOLD:
                    # Check if process is still running
                    result = subprocess.run(
                        ["pgrep", "-af", f"copilot.*issues/{issue_num}"],
                        text=True, capture_output=True
                    )
                    
                    if result.stdout:
                        # Agent is running but log hasn't updated
                        pid_match = re.search(r"^(\d+)", result.stdout)
                        pid = pid_match.group(1) if pid_match else "unknown"
                        
                        minutes = int(time_since_update / 60)
                        message = (
                            f"⚠️ **Agent Stuck Alert**\n\n"
                            f"Issue: #{issue_num}\n"
                            f"PID: {pid}\n"
                            f"No log activity for {minutes} minutes\n\n"
                            f"Actions:\n"
                            f"• Check /logs {issue_num}\n"
                            f"• Use /continue {issue_num} to resume\n"
                            f"• Use /kill {issue_num} to stop"
                        )
                        
                        if send_telegram_alert(message):
                            logger.info(f"🚨 Sent stuck agent alert for issue #{issue_num}")
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
    """Monitor for completed agent steps and auto-chain to next agent."""
    try:
        # Find all copilot log files
        log_pattern = os.path.join(BASE_DIR, "**", ".github", "tasks", "logs", "copilot_*.log")
        log_files = glob.glob(log_pattern, recursive=True)
        
        for log_file in log_files:
            # Extract issue number from filename
            match = re.search(r"copilot_(\d+)_", os.path.basename(log_file))
            if not match:
                continue
            
            issue_num = match.group(1)
            
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
                        r"Task Complete",
                        r"✅.*Complete",
                        r"ready for `@(\w+)`",
                        r"routing to `@(\w+)`",
                        r"Next.*`@(\w+)`"
                    ]
                    
                    completed = any(re.search(pattern, log_content, re.IGNORECASE) for pattern in completion_patterns)
                    
                    if not completed:
                        continue
                    
                    # Extract next agent mention
                    next_agent_match = re.search(r"(?:ready for|routing to|Next.*?)`@(\w+)`", log_content, re.IGNORECASE)
                    
                    if next_agent_match:
                        next_agent = next_agent_match.group(1)
                        
                        # Get issue details to find task file
                        try:
                            result = subprocess.run(
                                ["gh", "issue", "view", issue_num, "--repo", GITHUB_AGENTS_REPO,
                                 "--json", "body"],
                                text=True, capture_output=True, timeout=10
                            )
                            data = json.loads(result.stdout)
                            body = data.get("body", "")
                            
                            # Find task file
                            task_file_match = re.search(r"Task File:\s*`([^`]+)`", body)
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
                            
                            # Launch next agent
                            continuation_prompt = f"You are @{next_agent}. The previous step has been completed. Please begin your work on this task following the workflow."
                            
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
    """Returns (tier_name, sop_template, workflow_label) based on task type."""
    if any(t in task_type for t in ["hotfix", "chore"]):
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
        # Continuation prompt references previous work
        base_prompt = continuation_prompt or "Please continue with the next step."
        prompt = (
            f"You are @ProjectLead. You previously started working on this task:\n\n"
            f"Issue: {issue_url}\n"
            f"Tier: {tier_name}\n"
            f"Workflow: /{workflow_name}\n\n"
            f"{base_prompt}\n\n"
            f"Review your previous work (check logs, session state, and git branches) "
            f"and continue from where you left off.\n\n"
            f"**IMPORTANT:** When mentioning usernames or agent roles in GitHub comments "
            f"(like @Ghabs, @Atlas, @Architect, @QAGuard), ALWAYS escape them with backticks: "
            f"`@Ghabs`, `@Atlas`, etc. This prevents unwanted GitHub notifications and tagging "
            f"non-existent users.\n\n"
            f"Original task content:\n{task_content}"
        )
    else:
        # Fresh start prompt
        prompt = (
            f"You are @ProjectLead. A new task has arrived and a GitHub issue has been created.\n\n"
            f"Issue: {issue_url}\n"
            f"Tier: {tier_name}\n\n"
            f"Follow the /{workflow_name} workflow.\n"
            f"1. Triage this task and determine severity.\n"
            f"2. Identify which sub-repo(s) in the workspace are affected.\n"
            f"3. Route to the correct Tier 2 Lead (check the routing table in your agent definition).\n"
            f"4. Create the appropriate branch in the target sub-repo.\n"
            f"5. Begin implementation following the SOP steps.\n\n"
            f"**IMPORTANT:** When mentioning usernames or agent roles in GitHub comments "
            f"(like @Ghabs, @Atlas, @Architect, @QAGuard), ALWAYS escape them with backticks: "
            f"`@Ghabs`, `@Atlas`, etc. This prevents unwanted GitHub notifications and tagging "
            f"non-existent users.\n\n"
            f"Task content:\n{task_content}"
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
            cwd=agents_dir,
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


def process_file(filepath):
    """Processes a single task file."""
    logger.info(f"Processing: {filepath}")

    try:
        with open(filepath, "r") as f:
            content = f.read()

        # Parse Metadata
        type_match = re.search(r'\*\*Type:\*\*\s*(.+)', content)
        task_type = type_match.group(1).strip().lower() if type_match else "feature"

        # Extract body for slug
        body = re.sub(r'^#.*\n', '', content)
        body = re.sub(r'\*\*.*\*\*.*\n', '', body)
        slug = slugify(body.strip())

        if not slug:
            slug = "generic-task"

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
            "bug": "fix",
            "hotfix": "hotfix",
            "chore": "chore",
            "refactor": "refactor",
        }
        prefix = type_prefixes.get(task_type, task_type)
        issue_title = f"[{project_name}] {prefix}/{slug}"
        issue_body = f"""## Task
{content}

---

{sop_checklist}

---

**Project:** {project_name}
**Tier:** {tier_name}
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

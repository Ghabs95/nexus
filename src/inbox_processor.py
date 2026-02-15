import glob
import json
import logging
import os
import re
import shutil
import subprocess
import time
from dotenv import load_dotenv

# Config
load_dotenv("vars.secret")
BASE_DIR = os.getenv("BASE_DIR", "/home/ubuntu/git")
GITHUB_AGENTS_REPO = os.getenv("GITHUB_AGENTS_REPO", "ghabs/agents")
SLEEP_INTERVAL = 10

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


def invoke_copilot_agent(agents_dir, workspace_dir, issue_url, tier_name, task_content):
    """Invokes Copilot CLI on the agents directory to process the task.

    Runs asynchronously (Popen) since agent execution can take several minutes.
    The @ProjectLead agent will follow the SOP workflow to:
    1. Triage the task
    2. Determine the target sub-repo within the workspace
    3. Route to the correct Tier 2 Lead for implementation
    """
    workflow_name = get_workflow_name(tier_name)

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
        f"Task content:\n{task_content}"
    )

    cmd = [
        "copilot",
        "-p", prompt,
        "--add-dir", workspace_dir,
        "--allow-all-tools"
    ]

    logger.info(f"🤖 Launching Copilot CLI agent in {agents_dir}")
    logger.info(f"   Workspace: {workspace_dir}")
    logger.info(f"   Workflow: /{workflow_name} (tier: {tier_name})")

    # Log copilot output to a file for debugging
    log_dir = os.path.join(workspace_dir, ".github", "tasks", "logs")
    os.makedirs(log_dir, exist_ok=True)
    timestamp = time.strftime("%Y%m%d_%H%M%S")
    log_path = os.path.join(log_dir, f"copilot_{timestamp}.log")
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
    while True:
        # Scan for md files in project/.github/inbox/*.md
        pattern = os.path.join(BASE_DIR, "**", ".github", "inbox", "*.md")
        files = glob.glob(pattern, recursive=True)

        for filepath in files:
            process_file(filepath)

        time.sleep(SLEEP_INTERVAL)


if __name__ == "__main__":
    main()

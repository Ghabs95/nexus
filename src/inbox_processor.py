import glob
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
SLEEP_INTERVAL = 10

# Agent Folder Mapping
AGENT_MAPPING = {
    "case_italia": "ghabs/agents/casit-agents",
    "wallible": "ghabs/agents/wlbl-agents",
    "biome": "ghabs/agents/bm-agents",
    "nexus": "ghabs/nexus"
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


def run_git(cwd, command):
    """Runs a git command in the specified directory."""
    try:
        result = subprocess.run(
            command, cwd=cwd, check=True, text=True, capture_output=True
        )
        return result.stdout.strip()
    except subprocess.CalledProcessError as e:
        logger.error(f"Git error in {cwd}: {e.stderr}")
        raise


def slugify(text):
    """Converts text to a branch-friendly slug."""
    text = text.lower()
    text = re.sub(r'[^a-z0-9\s-]', '', text)
    text = re.sub(r'\s+', '-', text)
    return text[:50]  # Limit length


def process_file(filepath):
    """Processes a single task file."""
    logger.info(f"Processing: {filepath}")

    try:
        with open(filepath, "r") as f:
            content = f.read()

        # Parse Metadata (Regex)
        # Looking for: **Type:** feature
        type_match = re.search(r'\*\*Type:\*\*\s*(.+)', content)
        task_type = type_match.group(1).strip().lower() if type_match else "feature"

        # Looking for content (everything after the headers)
        # We assume headers end with double newline usually, or just take the whole text for slug
        # Let's clean headers out for the slug
        body = re.sub(r'^#.*\n', '', content)  # Remove title
        body = re.sub(r'\*\*.*\*\*.*\n', '', body)  # Remove keys
        slug = slugify(body.strip())

        if not slug:
            slug = "generic-task"

        # Determine Branch Name and Base Branch
        # Based on Conventional Branch
        branch_prefix = "feat"  # or feature
        base_branch = "develop"  # Default for feat, fix, chore, release

        if "bug" in task_type:
            branch_prefix = "fix"  # or bugfix
        elif "hotfix" in task_type:
            branch_prefix = "hotfix"
            base_branch = "main"  # Hotfixes come from main/master
        elif "release" in task_type:
            branch_prefix = "release"
        elif "chore" in task_type:
            branch_prefix = "chore"
        elif "improvement" in task_type:
            branch_prefix = "refactor"

        branch_name = f"{branch_prefix}/{slug}"

        # Git Operations
        # filepath is .../project/.github/inbox/file.md
        # git_root is .../project
        git_root = os.path.dirname(os.path.dirname(os.path.dirname(filepath)))

        logger.info(f"Target Repo: {git_root}")
        logger.info(f"Creating Branch: {branch_name} from {base_branch}")

        # 1. Checkout base branch and pull
        # Try main or master if base is main
        if base_branch == "main":
            try:
                run_git(git_root, ["git", "checkout", "main"])
            except:
                logger.warning("main branch not found, trying master")
                run_git(git_root, ["git", "checkout", "master"])
        else:
            run_git(git_root, ["git", "checkout", base_branch])

        run_git(git_root, ["git", "pull"])

        # 2. Create new branch
        # Check if branch exists?
        try:
            run_git(git_root, ["git", "checkout", "-b", branch_name])
        except:
            logger.warning(f"Branch {branch_name} might exist, checking out...")
            run_git(git_root, ["git", "checkout", branch_name])

        project_name = os.path.basename(git_root)
        agent_rel_path = AGENT_MAPPING.get(project_name)

        if agent_rel_path:
            # Construct agent active dir: BASE_DIR/../agent_path/.github/tasks/active
            # assuming BASE_DIR is .../git
            # agent_path is ghabs/agents/casit-agents
            agent_active_dir = os.path.join(BASE_DIR, agent_rel_path, ".github", "tasks", "active")
            os.makedirs(agent_active_dir, exist_ok=True)
            new_filepath = os.path.join(agent_active_dir, os.path.basename(filepath))
            logger.info(f"Moving file to Agent Repo: {new_filepath}")
            shutil.move(filepath, new_filepath)
        else:
            logger.warning(f"No agent mapping found for {project_name}, keeping file in active of project")
            # Fallback to local active
            inbox_dir = os.path.dirname(filepath)
            active_dir = os.path.join(os.path.dirname(inbox_dir), "active")
            os.makedirs(active_dir, exist_ok=True)
            new_filepath = os.path.join(active_dir, os.path.basename(filepath))
            shutil.move(filepath, new_filepath)

        # 4. Commit and Push
        run_git(git_root, ["git", "add", "."])
        run_git(git_root, ["git", "commit", "-m", f"Initialize task: {slug}"])
        # run_git(git_root, ["git", "push", "-u", "origin", branch_name]) 
        # Commented push out for safety in local test run, user can uncomment.

        logger.info(f"✅ Auto-Workflow Complete for {branch_name}")
        logger.info("Triggering @ProjectLead... (Simulated)")

    except Exception as e:
        logger.error(f"Failed to process {filepath}: {e}")


def main():
    logger.info(f"Warning Inbox Processor started on {BASE_DIR}")
    while True:
        # Scan for md files in */.github/inbox/*.md
        # BASE_DIR/project/.github/inbox/*.md
        pattern = os.path.join(BASE_DIR, "**", ".github", "inbox", "*.md")
        files = glob.glob(pattern, recursive=True)

        for filepath in files:
            process_file(filepath)

        time.sleep(SLEEP_INTERVAL)


if __name__ == "__main__":
    main()

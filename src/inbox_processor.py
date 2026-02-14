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
GITHUB_AGENTS_REPO = os.getenv("GITHUB_AGENTS_REPO", "ghabs/agents")
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
    return text[:50]


# SOP Checklist Templates
SOP_FULL = """## SOP Checklist — New Feature
- [ ] 1. **Vision & Scope** — @Ghabs: Founder's Check
- [ ] 2. **Technical Feasibility** — @Atlas: HOW and WHEN
- [ ] 3. **Architecture Design** — @Architect: ADR + breakdown
- [ ] 4. **UX Design** — @ProductDesigner: Wireframes
- [ ] 5. **Branching** — Tier 2 Lead: `{branch_name}` from `{base_branch}`
- [ ] 6. **Implementation** — Tier 2 Lead: Code + tests
- [ ] 7. **Quality Gate** — @QAGuard: Coverage check
- [ ] 8. **Compliance Gate** — @Privacy: PIA (if user data)
- [ ] 9. **Deployment** — @OpsCommander: Production
- [ ] 10. **Documentation** — @Scribe: Changelog + docs"""

SOP_SHORTENED = """## SOP Checklist — Bug Fix
- [ ] 1. **Triage** — @ProjectLead: Severity + routing
- [ ] 2. **Root Cause Analysis** — Tier 2 Lead
- [ ] 3. **Branching** — Tier 2 Lead: `{branch_name}` from `{base_branch}`
- [ ] 4. **Fix** — Tier 2 Lead: Code + regression test
- [ ] 5. **Verify** — @QAGuard: Regression suite
- [ ] 6. **Deploy** — @OpsCommander
- [ ] 7. **Document** — @Scribe: Changelog"""

SOP_FAST_TRACK = """## SOP Checklist — Fast-Track
- [ ] 1. **Triage** — @ProjectLead: Route to repo
- [ ] 2. **Implementation** — @copilot: Code + tests
- [ ] 3. **Verify** — @QAGuard: Quick check
- [ ] 4. **Deploy** — @OpsCommander"""


def get_sop_tier(task_type):
    """Returns (tier_name, sop_template, workflow_label) based on task type."""
    if any(t in task_type for t in ["hotfix", "chore"]):
        return "fast-track", SOP_FAST_TRACK, "workflow:fast-track"
    elif "bug" in task_type:
        return "shortened", SOP_SHORTENED, "workflow:shortened"
    else:
        return "full", SOP_FULL, "workflow:full"


def create_github_issue(title, body, project, workflow_label, task_type):
    """Creates a GitHub Issue in the agents repo with SOP checklist."""
    type_label = f"type:{task_type}"
    project_label = f"project:{project}"

    cmd = [
        "gh", "issue", "create",
        "--repo", GITHUB_AGENTS_REPO,
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

        # Check if git_root is a git repo
        is_git_repo = os.path.isdir(os.path.join(git_root, ".git"))

        if is_git_repo:
            logger.info(f"Creating Branch: {branch_name} from {base_branch}")
            # 1. Checkout base branch and pull
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
        else:
            logger.warning(f"⚠️  {git_root} is NOT a git repository. Skipping branch creation.")

        # 3. Append Branch Name to File
        # This allows the Agent (User + Copilot) to know the target branch
        try:
            with open(filepath, 'a') as f:
                f.write(f"\n\n**Target Branch:** `{branch_name}`\n")
            logger.info(f"Appended branch name to {filepath}")
        except Exception as e:
            logger.error(f"Failed to append branch name: {e}")

        # 4. Move file to Agent Repo 'active' folder
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

        # 4. Commit and Push (only if git repo)
        if is_git_repo:
            run_git(git_root, ["git", "add", "."])
            run_git(git_root, ["git", "commit", "-m", f"Initialize task: {slug}"])
            # run_git(git_root, ["git", "push", "-u", "origin", branch_name])
        # Commented push out for safety in local test run, user can uncomment.

        # 5. Create GitHub Issue with SOP checklist
        tier_name, sop_template, workflow_label = get_sop_tier(task_type)
        sop_checklist = sop_template.format(
            branch_name=branch_name,
            base_branch=base_branch
        )

        issue_title = f"[{project_name}] {branch_name}"
        issue_body = f"""## Task
{content}

---

{sop_checklist}

---

**Target Branch:** `{branch_name}`
**Project:** {project_name}
**Tier:** {tier_name}
**Task File:** `{new_filepath}`"""

        issue_url = create_github_issue(
            title=issue_title,
            body=issue_body,
            project=project_name,
            workflow_label=workflow_label,
            task_type=task_type
        )

        if issue_url:
            # Append issue URL to the task file
            try:
                with open(new_filepath, 'a') as f:
                    f.write(f"\n**Issue:** {issue_url}\n")
            except Exception as e:
                logger.error(f"Failed to append issue URL: {e}")

        logger.info(f"✅ Workflow Complete for {branch_name} (Tier: {tier_name})")

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

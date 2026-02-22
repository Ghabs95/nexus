"""Centralized configuration for Nexus bot and processor."""
import os
import sys
import logging
import subprocess
import urllib.parse
from typing import List

import yaml
from dotenv import load_dotenv

# Load secrets from local file if exists
SECRET_FILE = "vars.secret"
if os.path.exists(SECRET_FILE):
    logging.info(f"Loading environment from {SECRET_FILE}")
    load_dotenv(SECRET_FILE)
else:
    logging.info(f"No {SECRET_FILE} found, relying on shell environment")


def _get_int_env(name: str, default: int) -> int:
    """Return integer environment variable value or fallback default."""
    raw = os.getenv(name)
    if raw is None:
        return default
    try:
        value = int(str(raw).strip())
    except (TypeError, ValueError):
        return default
    return value if value > 0 else default

def _parse_int_list(name: str) -> List[int]:
    raw = os.getenv(name, "")
    return [int(x.strip()) for x in raw.split(",") if x.strip().isdigit()]

# --- TELEGRAM CONFIGURATION ---
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")

TELEGRAM_ALLOWED_USER_IDS = _parse_int_list("TELEGRAM_ALLOWED_USER_IDS")
if not TELEGRAM_ALLOWED_USER_IDS and os.getenv("ALLOWED_USER"):
    TELEGRAM_ALLOWED_USER_IDS = [int(os.getenv("ALLOWED_USER").strip())]
TELEGRAM_CHAT_ID = TELEGRAM_ALLOWED_USER_IDS[0] if TELEGRAM_ALLOWED_USER_IDS else None

# --- DISCORD CONFIGURATION ---
DISCORD_TOKEN = os.getenv("DISCORD_TOKEN")
DISCORD_ALLOWED_USER_IDS = _parse_int_list("DISCORD_ALLOWED_USER_IDS")
DISCORD_GUILD_ID = int(os.getenv("DISCORD_GUILD_ID")) if os.getenv("DISCORD_GUILD_ID") else None

# --- PATHS & DIRECTORIES ---
BASE_DIR = os.getenv("BASE_DIR", "/home/ubuntu/git")
DATA_DIR = os.path.join(os.path.dirname(os.path.dirname(__file__)), "data")
LOGS_DIR = os.path.join(os.path.dirname(os.path.dirname(__file__)), "logs")
TRACKED_ISSUES_FILE = os.path.join(DATA_DIR, "tracked_issues.json")
LAUNCHED_AGENTS_FILE = os.path.join(DATA_DIR, "launched_agents.json")
WORKFLOW_STATE_FILE = os.path.join(DATA_DIR, "workflow_state.json")
AUDIT_LOG_FILE = os.path.join(LOGS_DIR, "audit.log")
INBOX_PROCESSOR_LOG_FILE = os.path.join(LOGS_DIR, "inbox_processor.log")
TELEGRAM_BOT_LOG_FILE = os.path.join(LOGS_DIR, "telegram_bot.log")

# --- AI CONFIGURATION ---
AI_PERSONA = os.getenv(
    "AI_PERSONA", 
    "You are Nexus, a brilliant business advisor and technical architect (like Jarvis from Iron Man). The user is Ghabs, an ambitious CEO and Founder of many projects.\n\nAnswer the following question or brainstorm ideas directly and concisely. Keep your tone professional, highly capable, and slightly witty but always helpful."
)

# --- REDIS CONFIGURATION ---
REDIS_URL = os.getenv("REDIS_URL", "redis://localhost:6379/0")

# --- GIT PLATFORM CONFIGURATION ---
# Note: PROJECT_CONFIG_PATH is read from environment each time it's needed (for testing with monkeypatch)

# Lazy-load PROJECT_CONFIG to support testing with monkeypatch
_project_config_cache = None
_cached_config_path = None  # Track which path was cached


def _load_project_config(path: str) -> dict:
    """Load PROJECT_CONFIG from YAML file (required).
    
    Args:
        path: Path to project config YAML file
        
    Returns:
        Loaded project configuration dict
        
    Raises:
        FileNotFoundError: If file doesn't exist
        ValueError: If YAML is invalid or not a mapping
    """
    with open(path, "r", encoding="utf-8") as f:
        data = yaml.safe_load(f) or {}
    if not isinstance(data, dict):
        raise ValueError("PROJECT_CONFIG must be a YAML mapping")
    return data


def _validate_config_with_project_config(config: dict) -> None:
    """Validate project configuration dict."""
    if not config or len(config) == 0:
        # Empty config is okay for tests
        return
    
    # Global config keys that aren't projects
    global_keys = {
        'nexus_dir',  # VCS-agnostic inbox/tasks directory name
        'workflow_definition_path',
        'ai_tool_preferences',
        'workflow_chains',
        'final_agents',
        'require_human_merge_approval',  # PR merge approval policy (deprecated - use nexus-core approval gates)
        'github_issue_triage',  # GitHub issue → triage agent routing configuration
        'shared_agents_dir',  # Shared org-level agent YAML definitions directory
    }
    
    for project, proj_config in config.items():
        # Skip global settings
        if project in global_keys:
            continue
        
        # All other keys should be project dicts
        if not isinstance(proj_config, dict):
            raise ValueError(f"PROJECT_CONFIG['{project}'] must be a dict")
        
        if 'workspace' not in proj_config:
            raise ValueError(f"PROJECT_CONFIG['{project}'] missing 'workspace' key")

        repos_list = proj_config.get("git_repos")

        if repos_list is not None:
            if not isinstance(repos_list, list):
                raise ValueError(
                    f"PROJECT_CONFIG['{project}']['git_repos'] must be a list"
                )
            for repo_name in repos_list:
                if not isinstance(repo_name, str) or not repo_name.strip():
                    raise ValueError(
                        f"PROJECT_CONFIG['{project}']['git_repos'] contains invalid repo entry"
                    )

        git_platform = str(proj_config.get("git_platform", "github")).lower().strip()
        if git_platform not in {"github", "gitlab"}:
            raise ValueError(
                f"PROJECT_CONFIG['{project}']['git_platform'] must be 'github' or 'gitlab'"
            )


def _load_and_validate_project_config() -> dict:
    """Load and validate PROJECT_CONFIG from file.
    
    Raises:
        ValueError: If PROJECT_CONFIG_PATH is not set
        FileNotFoundError: If config file not found
        ValueError: If config is invalid
    """
    global _project_config_cache, _cached_config_path
    
    # Read PROJECT_CONFIG_PATH from environment (not cached to support monkeypatch in tests)
    project_config_path = os.getenv("PROJECT_CONFIG_PATH")
    if not project_config_path:
        raise ValueError(
            "PROJECT_CONFIG_PATH environment variable is required. "
            "It must point to a YAML file with project configuration."
        )
    
    # Clear cache if PROJECT_CONFIG_PATH changed (e.g., in tests with monkeypatch)
    if _cached_config_path != project_config_path:
        _project_config_cache = None
        _cached_config_path = project_config_path
    
    if _project_config_cache is not None:
        return _project_config_cache
    
    resolved_config_path = (
        project_config_path
        if os.path.isabs(project_config_path)
        else os.path.join(BASE_DIR, project_config_path)
    )
    
    try:
        _project_config_cache = _load_project_config(resolved_config_path)
    except FileNotFoundError:
        raise FileNotFoundError(
            f"PROJECT_CONFIG file not found: {resolved_config_path}"
        )
    except Exception as e:
        raise ValueError(
            f"Failed to load PROJECT_CONFIG from {resolved_config_path}: {e}"
        )
    
    # Validate the loaded config
    _validate_config_with_project_config(_project_config_cache)
    return _project_config_cache


# Create a property-like accessor for PROJECT_CONFIG
def _get_project_config() -> dict:
    """Get PROJECT_CONFIG, loading it lazily on first access."""
    return _load_and_validate_project_config()


# Initialize PROJECT_CONFIG on module load 
# Note: This loads the config immediately when the module is imported
# If you need truly lazy loading, wrap in a property descriptor instead
PROJECT_CONFIG = _get_project_config()



# --- WEBHOOK CONFIGURATION ---
WEBHOOK_PORT = int(os.getenv("WEBHOOK_PORT", "8081"))
WEBHOOK_SECRET = os.getenv("WEBHOOK_SECRET")  # GitHub webhook secret for signature verification

# --- AI ORCHESTRATOR CONFIGURATION ---
# These are now loaded from project_config.yaml
# Get defaults from config, with per-project overrides supported
def get_ai_tool_preferences(project: str = "nexus") -> dict:
    """Get AI tool preferences for a project.
    
    Priority:
    1. Project-specific ai_tool_preferences in PROJECT_CONFIG
    2. Global ai_tool_preferences in PROJECT_CONFIG
    3. Empty dict (no preferences defined)
    
    Args:
        project: Project name (default: "nexus")
        
    Returns:
        Dictionary mapping agent names to AI tools (copilot, gemini)
    """
    config = _get_project_config()
    
    # Check project-specific override
    if project in config:
        proj_config = config[project]
        if isinstance(proj_config, dict) and "ai_tool_preferences" in proj_config:
            return proj_config["ai_tool_preferences"]
    
    # Fall back to global
    if "ai_tool_preferences" in config:
        return config["ai_tool_preferences"]
    
    return {}


# Caching wrappers for lazy-loading on first access (support monkeypatch in tests)
_ai_tool_preferences_cache = {}


class _LazyConfigWrapper:
    """Wrapper that lazily loads config values to support monkeypatch."""
    
    def __init__(self, get_func, cache_dict, project="nexus"):
        self.get_func = get_func
        self.cache_dict = cache_dict
        self.project = project
    
    def _ensure_loaded(self):
        """Load value from config if not cached."""
        if "value" not in self.cache_dict:
            self.cache_dict["value"] = self.get_func(self.project)
        return self.cache_dict["value"]
    
    def keys(self):
        return self._ensure_loaded().keys()
    
    def items(self):
        return self._ensure_loaded().items()
    
    def values(self):
        return self._ensure_loaded().values()
    
    def get(self, *args):
        return self._ensure_loaded().get(*args)
    
    def __getitem__(self, key):
        return self._ensure_loaded()[key]
    
    def __contains__(self, key):
        return key in self._ensure_loaded()
    
    def __iter__(self):
        return iter(self._ensure_loaded())
    
    def __len__(self):
        return len(self._ensure_loaded())
    
    def __repr__(self):
        return repr(self._ensure_loaded())


# Create lazy-loading wrappers (for backward compatibility with code that accesses these directly)
# Note: These will get the global defaults from project_config.yaml when first accessed
AI_TOOL_PREFERENCES = _LazyConfigWrapper(get_ai_tool_preferences, _ai_tool_preferences_cache, "nexus")

# Orchestrator configuration (lazy-loaded)
_orchestrator_config_cache = {}


def _get_orchestrator_config():
    """Get orchestrator config, loading AI_TOOL_PREFERENCES lazily."""
    if "value" not in _orchestrator_config_cache:
        _orchestrator_config_cache["value"] = {
            "gemini_cli_path": os.getenv("GEMINI_CLI_PATH", "gemini"),
            "gemini_model": os.getenv("GEMINI_MODEL", "").strip(),
            "copilot_cli_path": os.getenv("COPILOT_CLI_PATH", "copilot"),
            "tool_preferences": AI_TOOL_PREFERENCES._ensure_loaded(),
            "fallback_enabled": os.getenv("AI_FALLBACK_ENABLED", "true").lower() == "true",
            "rate_limit_ttl": int(os.getenv("AI_RATE_LIMIT_TTL", "3600")),
            "max_retries": int(os.getenv("AI_MAX_RETRIES", "2")),
            "transcription_primary": os.getenv("TRANSCRIPTION_PRIMARY", "gemini").strip().lower(),
            "gemini_transcription_timeout": _get_int_env("GEMINI_TRANSCRIPTION_TIMEOUT", 60),
            "copilot_transcription_timeout": _get_int_env("COPILOT_TRANSCRIPTION_TIMEOUT", 120),
            "whisper_model": os.getenv("WHISPER_MODEL", "whisper-1").strip(),
            "whisper_language": os.getenv("WHISPER_LANGUAGE", "").strip().lower(),
            "whisper_languages": os.getenv("WHISPER_LANGUAGES", "").strip().lower(),
        }
    return _orchestrator_config_cache["value"]


class _LazyOrchestrator:
    """Lazy-loading wrapper for ORCHESTRATOR_CONFIG."""
    
    def __getitem__(self, key):
        return _get_orchestrator_config()[key]
    
    def __contains__(self, key):
        return key in _get_orchestrator_config()
    
    def __repr__(self):
        return repr(_get_orchestrator_config())
    
    def get(self, *args):
        return _get_orchestrator_config().get(*args)


ORCHESTRATOR_CONFIG = _LazyOrchestrator()

# --- NEXUS-CORE FRAMEWORK CONFIGURATION ---
# nexus-core workflow engine is mandatory
NEXUS_CORE_STORAGE_DIR = os.path.join(DATA_DIR, "nexus-core-workflows")
WORKFLOW_ID_MAPPING_FILE = os.path.join(DATA_DIR, "workflow_id_mapping.json")
APPROVAL_STATE_FILE = os.path.join(DATA_DIR, "approval_state.json")

# Nexus-Core storage backend configuration
NEXUS_CORE_STORAGE_BACKEND = os.getenv("NEXUS_CORE_STORAGE", "file")  # Options: file, postgres, redis

# --- PROJECT CONFIGURATION ---
def get_default_project() -> str:
    """Return default project key for legacy call sites.

    Preference order:
    1. explicit "nexus" project when present
    2. first configured project dict containing workspace + repo metadata
    """
    config = _get_project_config()
    if isinstance(config.get("nexus"), dict):
        return "nexus"

    for key, value in config.items():
        if isinstance(value, dict) and value.get("workspace"):
            return key

    raise ValueError("No project with repository configuration found in PROJECT_CONFIG")


def get_github_repos(project: str) -> List[str]:
    """Get all GitHub repositories configured for a project.

    Uses provider-neutral ``git_repo`` / ``git_repos``.
    """
    config = _get_project_config()
    if project not in config:
        raise KeyError(
            f"Project '{project}' not found in PROJECT_CONFIG. "
            f"Available projects: {[k for k in config.keys() if isinstance(config.get(k), dict)]}"
        )

    project_cfg = config[project]
    if not isinstance(project_cfg, dict):
        raise ValueError(f"Project '{project}' configuration must be a mapping")

    repos: List[str] = []
    single_repo = project_cfg.get("git_repo")
    if isinstance(single_repo, str) and single_repo.strip():
        repos.append(single_repo.strip())

    repo_list = project_cfg.get("git_repos")
    if isinstance(repo_list, list):
        for repo_name in repo_list:
            if isinstance(repo_name, str):
                value = repo_name.strip()
                if value and value not in repos:
                    repos.append(value)

    if not repos:
        repos = _discover_workspace_repos(project_cfg)

    if not repos:
        raise ValueError(
            f"Project '{project}' is missing repository configuration and "
            "workspace auto-discovery found no git remotes"
        )

    return repos


def _discover_workspace_repos(project_cfg: dict) -> List[str]:
    """Discover repository slugs from local git remotes in workspace.

    Scans workspace root and first-level subdirectories that are git repos.
    """
    workspace = project_cfg.get("workspace") if isinstance(project_cfg, dict) else None
    if not workspace:
        return []

    workspace_abs = workspace if os.path.isabs(workspace) else os.path.join(BASE_DIR, workspace)
    if not os.path.isdir(workspace_abs):
        return []

    candidates = [workspace_abs]
    try:
        for entry in os.scandir(workspace_abs):
            if entry.is_dir(follow_symlinks=False):
                candidates.append(entry.path)
    except Exception:
        pass

    repos: List[str] = []
    for candidate in candidates:
        if not os.path.isdir(os.path.join(candidate, ".git")):
            continue
        try:
            result = subprocess.run(
                ["git", "-C", candidate, "remote", "get-url", "origin"],
                capture_output=True,
                text=True,
                timeout=5,
                check=False,
            )
        except Exception:
            continue

        if result.returncode != 0:
            continue

        slug = _repo_slug_from_remote_url(result.stdout.strip())
        if slug and slug not in repos:
            repos.append(slug)

    return repos


def _repo_slug_from_remote_url(remote_url: str) -> str:
    """Normalize git remote URL into ``namespace/repo`` slug."""
    if not remote_url:
        return ""

    value = remote_url.strip()

    # SCP-like URLs: git@host:group/subgroup/repo.git
    if value.startswith("git@") and ":" in value:
        value = value.split(":", 1)[1]
    elif "://" in value:
        try:
            parsed = urllib.parse.urlparse(value)
            value = parsed.path or ""
        except Exception:
            return ""

    value = value.strip().lstrip("/")
    if value.endswith(".git"):
        value = value[:-4]

    return value


def get_default_github_repo() -> str:
    """Return default GitHub repo for legacy single-repo call sites."""
    return get_github_repo(get_default_project())


def get_project_platform(project: str) -> str:
    """Return VCS platform type for a project (``github`` or ``gitlab``)."""
    config = _get_project_config()
    if project not in config or not isinstance(config[project], dict):
        raise KeyError(f"Project '{project}' not found in PROJECT_CONFIG")
    return str(config[project].get("git_platform", "github")).lower().strip()


def get_gitlab_base_url(project: str) -> str:
    """Return GitLab base URL for a project.

    Priority:
    1. project-level ``gitlab_base_url``
    2. env var ``GITLAB_BASE_URL``
    3. default ``https://gitlab.com``
    """
    config = _get_project_config()
    project_cfg = config.get(project, {}) if isinstance(config, dict) else {}
    if isinstance(project_cfg, dict):
        project_url = project_cfg.get("gitlab_base_url")
        if isinstance(project_url, str) and project_url.strip():
            return project_url.strip()
    return os.getenv("GITLAB_BASE_URL", "https://gitlab.com")


def get_github_repo(project: str) -> str:
    """Get GitHub repo for a project from PROJECT_CONFIG.
    
    Args:
        project: Project name (e.g., "case_italia", "nexus")
        
    Returns:
        GitHub repo string (e.g., "Ghabs95/agents")
        
    Raises:
        KeyError: If project not found in PROJECT_CONFIG
    """
    return get_github_repos(project)[0]


def get_nexus_dir_name() -> str:
    """Get the nexus directory name for globbing patterns.
    
    Returns:
        Directory name (e.g., ".nexus") from config
    """
    config = _get_project_config()
    return config.get("nexus_dir", ".nexus")


def get_nexus_dir(workspace: str = None) -> str:
    """Get Nexus directory path (VCS-agnostic inbox/tasks storage).
    
    Default: workspace_root/.nexus (can be configured via config)
    
    Args:
        workspace: Workspace directory (uses current if not specified)
        
    Returns:
        Path to nexus directory (e.g., /path/to/workspace/.nexus)
    """
    if workspace is None:
        workspace = os.getcwd()
    
    # Get nexus_dir from config (defaults to .nexus)
    config = _get_project_config()
    nexus_dir_name = config.get("nexus_dir", ".nexus")
    
    return os.path.join(workspace, nexus_dir_name)


def get_inbox_dir(workspace: str = None, project: str = None) -> str:
    """Get inbox directory path for workflow tasks.

    Args:
        workspace: Workspace directory
        project: Optional project key subdirectory under inbox
    
    Returns:
        Path to {nexus_dir}/inbox or {nexus_dir}/inbox/{project}
    """
    nexus_dir = get_nexus_dir(workspace)
    inbox_dir = os.path.join(nexus_dir, "inbox")
    if project:
        inbox_dir = os.path.join(inbox_dir, project)
    return inbox_dir


def get_tasks_active_dir(workspace: str, project: str) -> str:
    """Get active tasks directory path for in-progress work.

    Args:
        workspace: Workspace directory
        project: Project key subdirectory under tasks (required)

    Returns:
        Path to {nexus_dir}/tasks/{project}/active
    """
    nexus_dir = get_nexus_dir(workspace)
    return os.path.join(nexus_dir, "tasks", project, "active")


def get_tasks_closed_dir(workspace: str, project: str) -> str:
    """Get closed tasks directory path for archived work.

    Args:
        workspace: Workspace directory
        project: Project key subdirectory under tasks (required)

    Returns:
        Path to {nexus_dir}/tasks/{project}/closed
    """
    nexus_dir = get_nexus_dir(workspace)
    return os.path.join(nexus_dir, "tasks", project, "closed")


def get_tasks_logs_dir(workspace: str, project: str) -> str:
    """Get task logs directory path for agent execution logs.

    Args:
        workspace: Workspace directory
        project: Project key subdirectory under tasks (required)

    Returns:
        Path to {nexus_dir}/tasks/{project}/logs
    """
    nexus_dir = get_nexus_dir(workspace)
    return os.path.join(nexus_dir, "tasks", project, "logs")


# --- TIMING CONFIGURATION ---
INBOX_CHECK_INTERVAL = 10  # seconds - how often to check for new completions
SLEEP_INTERVAL = INBOX_CHECK_INTERVAL  # Alias for backward compatibility
AGENT_RECENT_WINDOW = 120   # seconds - consider agent "recently launched" within this window
AUTO_CHAIN_CYCLE = 60       # seconds - frequency of auto-chain polling

# --- LOGGING CONFIGURATION ---
LOG_FORMAT = '%(asctime)s - %(name)s - %(levelname)s - %(message)s'
LOG_LEVEL = logging.INFO

# --- VALIDATION ---
logger = logging.getLogger(__name__)
logging.basicConfig(format=LOG_FORMAT, level=LOG_LEVEL)

logger.info(f"Using BASE_DIR: {BASE_DIR}")


def validate_configuration():
    """Validate all configuration on startup with detailed error messages.
    
    Note: This must be called AFTER PROJECT_CONFIG is loaded (via _get_project_config()).
    """
    errors = []
    warnings = []
    
    # Check required environment variables
    if not TELEGRAM_TOKEN:
        errors.append("TELEGRAM_TOKEN is missing! Set it in vars.secret or environment.")
    
    if not TELEGRAM_ALLOWED_USER_IDS:
        warnings.append("TELEGRAM_ALLOWED_USER_IDS are missing! Bot will not respond to anyone.")
    
    # Validate PROJECT_CONFIG (when loaded)
    try:
        config = _get_project_config()
        if config:
            for project, proj_config in config.items():
                # Skip non-dict values (e.g., global settings like workflow_definition_path)
                if not isinstance(proj_config, dict):
                    if project in ('workflow_definition_path',):
                        continue
                    errors.append(f"PROJECT_CONFIG['{project}'] must be a dict")
                else:
                    if 'workspace' not in proj_config:
                        errors.append(f"PROJECT_CONFIG['{project}'] missing 'workspace' key")
                    # git_repo/git_repos are optional when workspace auto-discovery is used.
                    repo_list = proj_config.get("git_repos")
                    if repo_list is not None and not isinstance(repo_list, list):
                        errors.append(
                            f"PROJECT_CONFIG['{project}']['git_repos'] must be a list"
                        )
    except Exception as e:
        # If PROJECT_CONFIG can't be loaded, that's okay during import (tests handle this)
        pass
    
    # Check if BASE_DIR is writable
    try:
        test_file = os.path.join(BASE_DIR, '.config_test')
        with open(test_file, 'w') as f:
            f.write('test')
        os.remove(test_file)
    except Exception as e:
        errors.append(f"BASE_DIR ({BASE_DIR}) is not writable: {e}")
    
    # Log results
    if errors:
        logger.error("❌ CONFIGURATION VALIDATION FAILED:")
        for error in errors:
            logger.error(f"  - {error}")
        logger.error("Please fix configuration errors before running.")
        sys.exit(1)
    
    if warnings:
        logger.warning("⚠️  Configuration warnings:")
        for warning in warnings:
            logger.warning(f"  - {warning}")
    
    logger.info("✅ Configuration validation passed")


def ensure_data_dir():
    """Ensure data directory exists."""
    os.makedirs(DATA_DIR, exist_ok=True)
    logger.debug(f"✅ Data directory ready: {DATA_DIR}")


def ensure_logs_dir():
    """Ensure logs directory exists."""
    os.makedirs(LOGS_DIR, exist_ok=True)
    logger.debug(f"✅ Logs directory ready: {LOGS_DIR}")


# Initialize directories (non-blocking)
try:
    ensure_data_dir()
    ensure_logs_dir()
except Exception as e:
    logger.warning(f"Could not initialize directories: {e}")

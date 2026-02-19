"""Centralized configuration for Nexus bot and processor."""
import os
import sys
import logging

import yaml
from dotenv import load_dotenv

# Load secrets from local file if exists
SECRET_FILE = "vars.secret"
if os.path.exists(SECRET_FILE):
    logging.info(f"Loading environment from {SECRET_FILE}")
    load_dotenv(SECRET_FILE)
else:
    logging.info(f"No {SECRET_FILE} found, relying on shell environment")

# --- TELEGRAM CONFIGURATION ---
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
ALLOWED_USER_ID = int(os.getenv("ALLOWED_USER")) if os.getenv("ALLOWED_USER") else None
TELEGRAM_CHAT_ID = ALLOWED_USER_ID  # Same as allowed user (for alerts)

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

# --- GITHUB CONFIGURATION ---
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
        if 'github_repo' not in proj_config:
            raise ValueError(f"PROJECT_CONFIG['{project}'] missing 'github_repo' key")


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
            "copilot_cli_path": os.getenv("COPILOT_CLI_PATH", "copilot"),
            "tool_preferences": AI_TOOL_PREFERENCES._ensure_loaded(),
            "fallback_enabled": os.getenv("AI_FALLBACK_ENABLED", "true").lower() == "true",
            "rate_limit_ttl": int(os.getenv("AI_RATE_LIMIT_TTL", "3600")),
            "max_retries": int(os.getenv("AI_MAX_RETRIES", "2")),
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
def get_github_repo(project: str) -> str:
    """Get GitHub repo for a project from PROJECT_CONFIG.
    
    Args:
        project: Project name (e.g., "case_italia", "nexus")
        
    Returns:
        GitHub repo string (e.g., "Ghabs95/agents")
        
    Raises:
        KeyError: If project not found in PROJECT_CONFIG
    """
    config = _get_project_config()
    if project not in config:
        raise KeyError(
            f"Project '{project}' not found in PROJECT_CONFIG. "
            f"Available projects: {[k for k in config.keys() if k != 'workflow_definition_path']}"
        )
    repo = config[project].get("github_repo")
    if not repo:
        raise ValueError(
            f"Project '{project}' is missing 'github_repo' in PROJECT_CONFIG"
        )
    return repo


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


def get_inbox_dir(workspace: str = None) -> str:
    """Get inbox directory path for workflow tasks.
    
    Returns:
        Path to {nexus_dir}/inbox directory
    """
    nexus_dir = get_nexus_dir(workspace)
    return os.path.join(nexus_dir, "inbox")


def get_tasks_active_dir(workspace: str = None) -> str:
    """Get active tasks directory path for in-progress work.
    
    Returns:
        Path to {nexus_dir}/tasks/active directory
    """
    nexus_dir = get_nexus_dir(workspace)
    return os.path.join(nexus_dir, "tasks", "active")


def get_tasks_closed_dir(workspace: str = None) -> str:
    """Get closed tasks directory path for archived work.

    Returns:
        Path to {nexus_dir}/tasks/closed directory
    """
    nexus_dir = get_nexus_dir(workspace)
    return os.path.join(nexus_dir, "tasks", "closed")


def get_tasks_logs_dir(workspace: str = None, project: str = None) -> str:
    """Get task logs directory path for agent execution logs.
    
    Args:
        workspace: Workspace directory
        project: Optional project subdirectory within logs
        
    Returns:
        Path to {nexus_dir}/tasks/logs or {nexus_dir}/tasks/logs/{project}
    """
    nexus_dir = get_nexus_dir(workspace)
    logs_dir = os.path.join(nexus_dir, "tasks", "logs")
    
    if project:
        logs_dir = os.path.join(logs_dir, project)
    
    return logs_dir




# --- TIMING CONFIGURATION ---
INBOX_CHECK_INTERVAL = 10  # seconds - how often to check for new completions
SLEEP_INTERVAL = INBOX_CHECK_INTERVAL  # Alias for backward compatibility
STUCK_AGENT_THRESHOLD = 60  # seconds - alert if no log activity for this long
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
    
    if not ALLOWED_USER_ID:
        warnings.append("ALLOWED_USER is missing! Bot will not respond to anyone.")
    
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
                    if 'github_repo' not in proj_config:
                        errors.append(f"PROJECT_CONFIG['{project}'] missing 'github_repo' key")
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

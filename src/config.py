"""Centralized configuration for Nexus bot and processor."""
import os
import sys
import logging
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
GITHUB_AGENTS_REPO = os.getenv("GITHUB_AGENTS_REPO", "Ghabs95/agents")
GITHUB_NEXUS_REPO = "Ghabs95/nexus"

# --- WEBHOOK CONFIGURATION ---
WEBHOOK_PORT = int(os.getenv("WEBHOOK_PORT", "8081"))
WEBHOOK_SECRET = os.getenv("WEBHOOK_SECRET")  # GitHub webhook secret for signature verification

# --- AI ORCHESTRATOR CONFIGURATION ---
# Tool selection strategy: which AI tool to use for which agent
# Options: "copilot", "gemini"
AI_TOOL_PREFERENCES = {
    # Code generation & complex reasoning → Copilot (better reasoning, multi-file context)
    "ProjectLead": "copilot",
    "Atlas": "copilot",
    "Architect": "copilot",
    "Tier2Lead": "copilot",
    
    # Fast & simple analysis → Gemini (faster, good for classification)
    "ProductDesigner": "gemini",
    "QAGuard": "gemini",
    "Scribe": "gemini",
    "OpsCommander": "gemini",
    "Privacy": "gemini",
    
    # Frontend/Backend specific (can prefer platform-aligned tool)
    "FrontendLead": "copilot",
    "BackendLead": "copilot",
    "MobileLead": "copilot",
}

# Orchestrator configuration
ORCHESTRATOR_CONFIG = {
    "gemini_cli_path": os.getenv("GEMINI_CLI_PATH", "gemini"),  # Path to gemini-cli executable
    "copilot_cli_path": os.getenv("COPILOT_CLI_PATH", "copilot"),  # Path to copilot executable
    "tool_preferences": AI_TOOL_PREFERENCES,
    "fallback_enabled": os.getenv("AI_FALLBACK_ENABLED", "true").lower() == "true",  # Enable fallback
    "rate_limit_ttl": int(os.getenv("AI_RATE_LIMIT_TTL", "3600")),  # 1 hour default
    "max_retries": int(os.getenv("AI_MAX_RETRIES", "2")),
}

# --- WORKFLOW CONFIGURATION ---
WORKFLOW_CHAIN = {
    "full": [  # new_feature workflow
        ("ProjectLead", "Vision & Scope"),          # Step 1
        ("Atlas", "Technical Feasibility"),         # Step 2
        ("Architect", "Architecture Design"),       # Step 3
        ("ProductDesigner", "UX Design"),           # Step 4
        ("Tier2Lead", "Implementation"),            # Step 5
        ("QAGuard", "Quality Gate"),                # Step 6
        ("Privacy", "Compliance Gate"),             # Step 7
        ("OpsCommander", "Deployment"),             # Step 8
        ("Scribe", "Documentation")                 # Step 9 (final)
    ],
    "shortened": [  # bug_fix workflow
        ("ProjectLead", "Triage"),                  # Step 1
        ("Tier2Lead", "Root Cause Analysis"),       # Step 2
        ("Tier2Lead", "Fix"),                       # Step 3
        ("QAGuard", "Verify"),                      # Step 4
        ("OpsCommander", "Deploy"),                 # Step 5
        ("Scribe", "Document")                      # Step 6 (final)
    ],
    "fast-track": [  # hotfix/chore workflow
        ("ProjectLead", "Triage"),                  # Step 1
        ("Copilot", "Implementation"),              # Step 2
        ("QAGuard", "Verify"),                      # Step 3
        ("OpsCommander", "Deploy")                  # Step 4 (final)
    ]
}

# Map tier names to their final agent (for issue closing)
FINAL_AGENTS = {
    "full": "Scribe",
    "shortened": "Scribe",
    "fast-track": "OpsCommander"
}

# --- NEXUS-CORE FRAMEWORK CONFIGURATION ---
# Enable nexus-core workflow engine (set to False to use legacy StateManager)
USE_NEXUS_CORE = os.getenv("USE_NEXUS_CORE", "true").lower() == "true"
NEXUS_CORE_STORAGE_DIR = os.path.join(DATA_DIR, "nexus-core-workflows")
WORKFLOW_ID_MAPPING_FILE = os.path.join(DATA_DIR, "workflow_id_mapping.json")

# Nexus-Core storage backend configuration
NEXUS_CORE_STORAGE_BACKEND = os.getenv("NEXUS_CORE_STORAGE", "file")  # Options: file, postgres, redis

# --- PROJECT CONFIGURATION ---
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
        "agents_dir": None,  # Nexus tasks handled directly
        "workspace": "ghabs/nexus",
        "github_repo": GITHUB_NEXUS_REPO,
    }
}

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
    """Validate all configuration on startup with detailed error messages."""
    errors = []
    warnings = []
    
    # Check required environment variables
    if not TELEGRAM_TOKEN:
        errors.append("TELEGRAM_TOKEN is missing! Set it in vars.secret or environment.")
    
    if not ALLOWED_USER_ID:
        warnings.append("ALLOWED_USER is missing! Bot will not respond to anyone.")
    
    # Validate WORKFLOW_CHAIN structure
    try:
        required_tiers = ['full', 'shortened', 'fast-track']
        for tier in required_tiers:
            if tier not in WORKFLOW_CHAIN:
                errors.append(f"WORKFLOW_CHAIN missing required tier: '{tier}'")
            elif not isinstance(WORKFLOW_CHAIN[tier], list) or len(WORKFLOW_CHAIN[tier]) == 0:
                errors.append(f"WORKFLOW_CHAIN['{tier}'] must be a non-empty list")
            else:
                # Validate each step is a tuple
                for idx, step in enumerate(WORKFLOW_CHAIN[tier]):
                    if not isinstance(step, tuple) or len(step) != 2:
                        errors.append(
                            f"WORKFLOW_CHAIN['{tier}'][{idx}] must be tuple (agent_name, description), "
                            f"got {type(step).__name__}"
                        )
    except Exception as e:
        errors.append(f"WORKFLOW_CHAIN validation error: {e}")
    
    # Validate PROJECT_CONFIG
    if not PROJECT_CONFIG or len(PROJECT_CONFIG) == 0:
        warnings.append("PROJECT_CONFIG is empty. No projects configured.")
    else:
        for project, config in PROJECT_CONFIG.items():
            if not isinstance(config, dict):
                errors.append(f"PROJECT_CONFIG['{project}'] must be a dict")
            else:
                if 'workspace' not in config:
                    errors.append(f"PROJECT_CONFIG['{project}'] missing 'workspace' key")
                if 'github_repo' not in config:
                    errors.append(f"PROJECT_CONFIG['{project}'] missing 'github_repo' key")
    
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
    logger.info(f"✅ Data directory ready: {DATA_DIR}")


def ensure_logs_dir():
    """Ensure logs directory exists."""
    os.makedirs(LOGS_DIR, exist_ok=True)
    logger.info(f"✅ Logs directory ready: {LOGS_DIR}")


# Run validation on import
validate_configuration()
ensure_data_dir()
ensure_logs_dir()

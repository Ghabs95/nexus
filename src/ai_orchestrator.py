"""Compatibility wrapper for AI orchestrator backed by nexus-core plugin."""

from typing import Any, Dict, Mapping, Optional

from plugin_runtime import clear_cached_plugin, get_profiled_plugin
from nexus.plugins.builtin.ai_runtime_plugin import (
    AIOrchestrator,
    AIProvider,
    RateLimitedError,
    ToolUnavailableError,
)

_orchestrator: Optional[AIOrchestrator] = None


def _resolve_tasks_logs_dir(workspace: str, project: Optional[str] = None) -> str:
    """Resolve tasks logs directory via config."""
    from config import get_tasks_logs_dir

    return get_tasks_logs_dir(workspace, project)


def get_orchestrator(config: Optional[Any] = None) -> AIOrchestrator:
    """Get or create global orchestrator instance."""
    global _orchestrator
    if _orchestrator is None:
        if config is None:
            overrides: Dict[str, Any] = {}
        elif isinstance(config, dict):
            overrides = dict(config)
        elif isinstance(config, Mapping):
            overrides = dict(config.items())
        elif hasattr(config, "items"):
            overrides = dict(config.items())
        elif hasattr(config, "get"):
            keys = (
                "gemini_cli_path",
                "copilot_cli_path",
                "tool_preferences",
                "fallback_enabled",
                "rate_limit_ttl",
                "max_retries",
                "analysis_timeout",
                "refine_description_timeout",
            )
            overrides = {
                key: config.get(key)
                for key in keys
                if config.get(key) is not None
            }
        else:
            overrides = dict(config)
        overrides["tasks_logs_dir_resolver"] = _resolve_tasks_logs_dir
        plugin = get_profiled_plugin(
            "ai_runtime_default",
            overrides=overrides,
            cache_key="ai:orchestrator",
        )
        _orchestrator = plugin or AIOrchestrator(overrides)
    return _orchestrator


def reset_orchestrator() -> None:
    """Reset global orchestrator (for testing)."""
    global _orchestrator
    _orchestrator = None
    clear_cached_plugin("ai:orchestrator")

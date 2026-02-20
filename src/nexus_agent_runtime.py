"""NexusAgentRuntime — nexus-specific implementation of AgentRuntime.

Bridges ProcessOrchestrator (nexus-core) with the concrete nexus host:
  - Copilot / Gemini CLI invocations (via agent_launcher.launch_next_agent)
  - StateManager-backed process tracking
  - AgentMonitor stuck/dead detection hooks
  - Telegram alerting
  - Workflow finalisation (close issue + PR)
"""

import logging
from typing import Callable, Dict, List, Optional, Tuple

from nexus.core.process_orchestrator import AgentRuntime

logger = logging.getLogger(__name__)


class NexusAgentRuntime(AgentRuntime):
    """AgentRuntime implementation for the nexus host application.

    Args:
        finalize_fn: Callable ``(issue_number, repo, last_agent, project_name) -> None``
            that closes the issue and creates a PR when a workflow completes.
            Typically :func:`inbox_processor._finalize_workflow`.
        resolve_project: ``(file_path) -> project_name | None`` resolver used
            in :meth:`is_process_running` and forwarded via the orchestrator
            callbacks.  Can be ``None`` when not needed.
        resolve_repo: ``(project_name, issue_number) -> repo_string`` resolver.
            Falls back to the ``"nexus"`` project repo when omitted.
    """

    def __init__(
        self,
        finalize_fn: Callable,
        resolve_project: Optional[Callable] = None,
        resolve_repo: Optional[Callable] = None,
    ) -> None:
        self._finalize_fn = finalize_fn
        self._resolve_project = resolve_project
        self._resolve_repo = resolve_repo

    # ------------------------------------------------------------------
    # AgentRuntime interface
    # ------------------------------------------------------------------

    def launch_agent(
        self,
        issue_number: str,
        agent_type: str,
        *,
        trigger_source: str = "orchestrator",
        exclude_tools: Optional[List[str]] = None,
    ) -> Tuple[Optional[int], Optional[str]]:
        from agent_launcher import launch_next_agent

        return launch_next_agent(
            issue_number=issue_number,
            next_agent=agent_type,
            trigger_source=trigger_source,
            exclude_tools=exclude_tools,
        )

    def load_launched_agents(self, recent_only: bool = True) -> Dict[str, dict]:
        from state_manager import StateManager

        return StateManager.load_launched_agents(recent_only=recent_only)

    def save_launched_agents(self, data: Dict[str, dict]) -> None:
        from state_manager import StateManager

        StateManager.save_launched_agents(data)

    def clear_launch_guard(self, issue_number: str) -> None:
        from agent_launcher import clear_launch_guard

        clear_launch_guard(issue_number)

    def should_retry(self, issue_number: str, agent_type: str) -> bool:
        from agent_monitor import AgentMonitor

        return bool(AgentMonitor.should_retry(issue_number, agent_type))

    def send_alert(self, message: str) -> bool:
        from notifications import send_telegram_alert

        return bool(send_telegram_alert(message))

    def audit_log(self, issue_number: str, event: str, details: str = "") -> None:
        from state_manager import StateManager

        StateManager.audit_log(int(issue_number), event, details or None)

    def finalize_workflow(
        self,
        issue_number: str,
        repo: str,
        last_agent: str,
        project_name: str,
    ) -> dict:
        self._finalize_fn(issue_number, repo, last_agent, project_name)
        return {}

    # ------------------------------------------------------------------
    # Optional hooks (override base-class no-ops)
    # ------------------------------------------------------------------

    def get_workflow_state(self, issue_number: str) -> Optional[str]:
        """Read workflow state directly from nexus-core FileStorage on disk."""
        import json
        import os
        from config import NEXUS_CORE_STORAGE_DIR
        from state_manager import StateManager

        workflow_id = StateManager.get_workflow_id_for_issue(str(issue_number))
        if not workflow_id:
            return None
        wf_file = os.path.join(
            NEXUS_CORE_STORAGE_DIR, "workflows", f"{workflow_id}.json"
        )
        try:
            with open(wf_file, "r") as f:
                state_str = json.load(f).get("state", "")
        except (FileNotFoundError, json.JSONDecodeError):
            return None
        # WorkflowEngine stores: "active", "paused", "cancelled", "completed", "pending"
        if state_str in ("paused", "cancelled"):
            return state_str.upper()
        return None

    def is_process_running(self, issue_number: str) -> bool:
        """Return True if an agent process is still active for this issue."""
        try:
            from plugin_runtime import get_runtime_ops_plugin

            ops = get_runtime_ops_plugin(cache_key="runtime-ops:inbox")
            if ops:
                return bool(ops.is_issue_process_running(issue_number))
        except Exception as exc:
            logger.debug(f"is_process_running check failed for #{issue_number}: {exc}")
        return False

    def check_log_timeout(
        self, issue_number: str, log_file: str
    ) -> Tuple[bool, Optional[int]]:
        from agent_monitor import AgentMonitor

        return AgentMonitor.check_timeout(issue_number, log_file)

    def kill_process(self, pid: int) -> bool:
        """Delegate to AgentMonitor.kill_agent for consistent kill + cleanup."""
        from agent_monitor import AgentMonitor

        # AgentMonitor.kill_agent expects an issue_num string but uses it only
        # for logging; pass empty string when we don't have it readily.
        return bool(AgentMonitor.kill_agent(pid, ""))

    def notify_timeout(
        self, issue_number: str, agent_type: str, will_retry: bool
    ) -> None:
        try:
            from notifications import notify_agent_timeout

            notify_agent_timeout(issue_number, agent_type, will_retry, project="nexus")
        except Exception as exc:
            logger.warning(
                f"notify_timeout failed for #{issue_number} / {agent_type}: {exc}"
            )

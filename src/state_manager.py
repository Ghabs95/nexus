"""State management for Nexus - handles persistent storage."""
import logging
import time
from typing import Dict, Optional, List
from datetime import datetime
from config import (
    LAUNCHED_AGENTS_FILE, TRACKED_ISSUES_FILE,
    DATA_DIR, AGENT_RECENT_WINDOW, ensure_data_dir, ensure_logs_dir
)
from plugin_runtime import get_profiled_plugin

logger = logging.getLogger(__name__)


def _get_state_store_plugin():
    """Return shared JSON state store plugin instance."""
    return get_profiled_plugin(
        "state_store_default",
        cache_key="state:json-store",
    )


class StateManager:
    """Manages all persistent state for the Nexus system."""

    @staticmethod
    def _load_json_state(path: str, default, ensure_logs: bool = False):
        """Load JSON state via storage plugin."""
        if ensure_logs:
            ensure_logs_dir()
        else:
            ensure_data_dir()

        plugin = _get_state_store_plugin()
        if not plugin:
            return default
        return plugin.load_json(path, default=default)

    @staticmethod
    def _save_json_state(path: str, data, *, context: str, ensure_logs: bool = False) -> None:
        """Save JSON state via storage plugin."""
        if ensure_logs:
            ensure_logs_dir()
        else:
            ensure_data_dir()

        plugin = _get_state_store_plugin()
        if not plugin:
            logger.error(f"State storage plugin unavailable; cannot save {context}")
            return
        plugin.save_json(path, data)

    @staticmethod
    def _append_line(path: str, line: str, *, context: str) -> bool:
        """Append line to file via storage plugin."""
        ensure_logs_dir()
        plugin = _get_state_store_plugin()
        if not plugin:
            logger.error(f"State storage plugin unavailable; cannot write {context}")
            return False
        return plugin.append_line(path, line)

    @staticmethod
    def _read_lines(path: str) -> List[str]:
        """Read lines from file via storage plugin."""
        ensure_logs_dir()
        plugin = _get_state_store_plugin()
        if not plugin:
            return []
        return plugin.read_lines(path)

    @staticmethod
    def load_launched_agents(recent_only: bool = True) -> Dict[str, dict]:
        """Load launched agents from persistent storage.

        Args:
            recent_only: When True (default), filter to entries within
                AGENT_RECENT_WINDOW. Pass False in dead-agent detection so
                that crashed agents older than the window are still caught.
        """
        data = StateManager._load_json_state(LAUNCHED_AGENTS_FILE, default={}) or {}
        if not recent_only:
            return data
        cutoff = time.time() - AGENT_RECENT_WINDOW
        return {k: v for k, v in data.items() if v.get("timestamp", 0) > cutoff}

    @staticmethod
    def save_launched_agents(data: Dict[str, dict]) -> None:
        """Save launched agents to persistent storage."""
        StateManager._save_json_state(
            LAUNCHED_AGENTS_FILE,
            data,
            context="launched agents",
        )

    @staticmethod
    def get_last_tier_for_issue(issue_num: str) -> Optional[str]:
        """Get the last known workflow tier for an issue from launched_agents.

        Unlike :meth:`load_launched_agents`, this reads without the recency
        cutoff so that tier information persists across slow agent executions.

        Returns:
            Tier name (e.g. ``"full"``, ``"fast-track"``) or ``None``.
        """
        data = StateManager._load_json_state(LAUNCHED_AGENTS_FILE, default={}) or {}
        entry = data.get(str(issue_num))
        if entry and isinstance(entry, dict):
            return entry.get("tier")
        return None

    @staticmethod
    def register_launched_agent(issue_num: str, agent_name: str, pid: int) -> None:
        """Register a newly launched agent."""
        data = StateManager.load_launched_agents()
        key = f"{issue_num}_{agent_name}"
        data[key] = {
            "issue": issue_num,
            "agent": agent_name,
            "pid": pid,
            "timestamp": time.time()
        }
        StateManager.save_launched_agents(data)
        logger.info(f"Registered launched agent: {agent_name} (PID: {pid}) for issue #{issue_num}")

    @staticmethod
    def was_recently_launched(issue_num: str, agent_name: str) -> bool:
        """Check if agent was recently launched (within 2-minute window)."""
        data = StateManager.load_launched_agents()
        key = f"{issue_num}_{agent_name}"
        return key in data

    @staticmethod
    def load_tracked_issues() -> Dict[int, dict]:
        """Load tracked issues from file."""
        return StateManager._load_json_state(TRACKED_ISSUES_FILE, default={})

    @staticmethod
    def save_tracked_issues(data: Dict[int, dict]) -> None:
        """Save tracked issues to file."""
        StateManager._save_json_state(
            TRACKED_ISSUES_FILE,
            data,
            context="tracked issues",
        )

    @staticmethod
    def add_tracked_issue(issue_num: int, project: str, description: str) -> None:
        """Add an issue to tracking."""
        data = StateManager.load_tracked_issues()
        data[str(issue_num)] = {
            "project": project,
            "description": description,
            "created_at": time.time(),
            "status": "active"
        }
        StateManager.save_tracked_issues(data)
        logger.info(f"Added tracked issue: #{issue_num} ({project})")

    @staticmethod
    def remove_tracked_issue(issue_num: int) -> None:
        """Remove an issue from tracking."""
        data = StateManager.load_tracked_issues()
        data.pop(str(issue_num), None)
        StateManager.save_tracked_issues(data)
        logger.info(f"Removed tracked issue: #{issue_num}")

    @staticmethod
    def audit_log(issue_num: int, event: str, details: Optional[str] = None) -> None:
        """Log an audit event in nexus-core JSONL format.

        Writes to ``{NEXUS_CORE_STORAGE_DIR}/audit/{workflow_id}.jsonl``, the
        same directory and format used by :class:`WorkflowEngine`.  Falls back
        to ``_nexus_system.jsonl`` when no workflow mapping exists.
        """
        import json
        import os
        from config import NEXUS_CORE_STORAGE_DIR

        try:
            timestamp = datetime.now().isoformat()
            workflow_id = StateManager.get_workflow_id_for_issue(str(issue_num))
            if not workflow_id:
                workflow_id = "_nexus_system"

            audit_dir = os.path.join(NEXUS_CORE_STORAGE_DIR, "audit")
            os.makedirs(audit_dir, exist_ok=True)
            audit_file = os.path.join(audit_dir, f"{workflow_id}.jsonl")

            entry = {
                "workflow_id": workflow_id,
                "timestamp": timestamp,
                "event_type": event,
                "data": {
                    "issue_number": issue_num,
                    "details": details,
                },
                "user_id": None,
            }
            with open(audit_file, "a", encoding="utf-8") as f:
                f.write(json.dumps(entry) + "\n")

            logger.debug(f"Audit: #{issue_num} {event} -> {audit_file}")
        except Exception as e:
            logger.error(f"Failed to write audit log: {e}")

    @staticmethod
    def get_audit_history(issue_num: int, limit: int = 50) -> List[dict]:
        """Get recent audit events for an issue from JSONL audit files.

        Returns a list of parsed dicts (newest last, capped at *limit*).
        Each dict has keys: ``workflow_id``, ``timestamp``, ``event_type``,
        ``data``, ``user_id``.
        """
        import json
        import os
        from config import NEXUS_CORE_STORAGE_DIR

        try:
            workflow_id = StateManager.get_workflow_id_for_issue(str(issue_num))
            if not workflow_id:
                return []
            audit_file = os.path.join(
                NEXUS_CORE_STORAGE_DIR, "audit", f"{workflow_id}.jsonl"
            )
            if not os.path.exists(audit_file):
                return []
            entries: List[dict] = []
            with open(audit_file, "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        evt = json.loads(line)
                        entries.append(evt)
                    except json.JSONDecodeError:
                        continue
            return entries[-limit:]
        except Exception as e:
            logger.error(f"Failed to read audit log: {e}")
            return []

    @staticmethod
    def read_all_audit_events(
        since_hours: Optional[int] = None,
    ) -> List[dict]:
        """Read ALL audit events across every JSONL file in the audit dir.

        Optional *since_hours* restricts to events within the last N hours.
        Returns a flat list of dicts sorted by timestamp (oldest first).
        """
        import json
        import os
        from config import NEXUS_CORE_STORAGE_DIR

        audit_dir = os.path.join(NEXUS_CORE_STORAGE_DIR, "audit")
        if not os.path.isdir(audit_dir):
            return []

        cutoff = None
        if since_hours is not None:
            from datetime import timedelta
            cutoff = datetime.now() - timedelta(hours=since_hours)

        events: List[dict] = []
        for fname in os.listdir(audit_dir):
            if not fname.endswith(".jsonl"):
                continue
            fpath = os.path.join(audit_dir, fname)
            try:
                with open(fpath, "r", encoding="utf-8") as f:
                    for line in f:
                        line = line.strip()
                        if not line:
                            continue
                        try:
                            evt = json.loads(line)
                        except json.JSONDecodeError:
                            continue
                        if cutoff:
                            try:
                                ts = datetime.fromisoformat(evt.get("timestamp", ""))
                                if ts < cutoff:
                                    continue
                            except (ValueError, TypeError):
                                continue
                        events.append(evt)
            except Exception:
                continue

        events.sort(key=lambda e: e.get("timestamp", ""))
        return events
    # --- NEXUS-CORE INTEGRATION ---
    
    @staticmethod
    def load_workflow_mapping() -> Dict[str, str]:
        """Load issue_number -> workflow_id mapping."""
        from config import WORKFLOW_ID_MAPPING_FILE
        return StateManager._load_json_state(WORKFLOW_ID_MAPPING_FILE, default={})

    @staticmethod
    def save_workflow_mapping(data: Dict[str, str]) -> None:
        """Save issue_number -> workflow_id mapping."""
        from config import WORKFLOW_ID_MAPPING_FILE
        StateManager._save_json_state(
            WORKFLOW_ID_MAPPING_FILE,
            data,
            context="workflow mapping",
        )

    @staticmethod
    def map_issue_to_workflow(issue_num: str, workflow_id: str) -> None:
        """Map an issue number to a workflow ID."""
        data = StateManager.load_workflow_mapping()
        data[str(issue_num)] = workflow_id
        StateManager.save_workflow_mapping(data)
        logger.info(f"Mapped issue #{issue_num} -> workflow {workflow_id}")

    @staticmethod
    def get_workflow_id_for_issue(issue_num: str) -> Optional[str]:
        """Get workflow ID for an issue number."""
        data = StateManager.load_workflow_mapping()
        return data.get(str(issue_num))

    @staticmethod
    def remove_workflow_mapping(issue_num: str) -> None:
        """Remove workflow mapping for an issue."""
        data = StateManager.load_workflow_mapping()
        data.pop(str(issue_num), None)
        StateManager.save_workflow_mapping(data)
        logger.info(f"Removed workflow mapping for issue #{issue_num}")

    # --- APPROVAL GATE STATE ---

    @staticmethod
    def load_approval_state() -> Dict[str, dict]:
        """Load pending approval state from persistent storage."""
        from config import APPROVAL_STATE_FILE
        return StateManager._load_json_state(APPROVAL_STATE_FILE, default={})

    @staticmethod
    def save_approval_state(data: Dict[str, dict]) -> None:
        """Save approval state to persistent storage."""
        from config import APPROVAL_STATE_FILE
        StateManager._save_json_state(
            APPROVAL_STATE_FILE,
            data,
            context="approval state",
        )

    @staticmethod
    def set_pending_approval(
        issue_num: str,
        step_num: int,
        step_name: str,
        approvers: List[str],
        approval_timeout: int,
    ) -> None:
        """Record that a workflow step is waiting for approval."""
        data = StateManager.load_approval_state()
        data[str(issue_num)] = {
            "step_num": step_num,
            "step_name": step_name,
            "approvers": approvers,
            "approval_timeout": approval_timeout,
            "requested_at": time.time(),
        }
        StateManager.save_approval_state(data)
        logger.info(
            f"Set pending approval for issue #{issue_num} step {step_num} ({step_name})"
        )

    @staticmethod
    def clear_pending_approval(issue_num: str) -> None:
        """Remove approval gate record once resolved."""
        data = StateManager.load_approval_state()
        data.pop(str(issue_num), None)
        StateManager.save_approval_state(data)
        logger.info(f"Cleared pending approval for issue #{issue_num}")

    @staticmethod
    def get_pending_approval(issue_num: str) -> Optional[dict]:
        """Return pending approval info for an issue, or None if not awaiting approval."""
        data = StateManager.load_approval_state()
        return data.get(str(issue_num))
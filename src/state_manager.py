"""State management for Nexus - handles persistent storage."""
import json
import logging
import os
import time
from typing import Dict, Optional, List
from datetime import datetime
from models import WorkflowState, Issue, CompletionMarker
from config import (
    WORKFLOW_STATE_FILE, LAUNCHED_AGENTS_FILE, TRACKED_ISSUES_FILE,
    AUDIT_LOG_FILE, DATA_DIR, AGENT_RECENT_WINDOW, ensure_data_dir
)

logger = logging.getLogger(__name__)


class StateManager:
    """Manages all persistent state for the Nexus system."""

    @staticmethod
    def load_workflow_state() -> Dict[str, dict]:
        """Load workflow state (paused/stopped issues) from persistent storage."""
        ensure_data_dir()
        if os.path.exists(WORKFLOW_STATE_FILE):
            try:
                with open(WORKFLOW_STATE_FILE) as f:
                    return json.load(f)
            except Exception as e:
                logger.error(f"Failed to load workflow state: {e}")
        return {}

    @staticmethod
    def save_workflow_state(data: Dict[str, dict]) -> None:
        """Save workflow state to persistent storage."""
        ensure_data_dir()
        try:
            with open(WORKFLOW_STATE_FILE, "w") as f:
                json.dump(data, f, indent=2)
        except Exception as e:
            logger.error(f"Failed to save workflow state: {e}")

    @staticmethod
    def set_workflow_state(issue_num: str, state: WorkflowState) -> None:
        """Set workflow state for an issue (ACTIVE, PAUSED, or STOPPED)."""
        data = StateManager.load_workflow_state()
        if state == WorkflowState.ACTIVE:
            data.pop(str(issue_num), None)  # Remove entry to mark as active
        else:
            data[str(issue_num)] = {
                "state": state.value,
                "timestamp": time.time()
            }
        StateManager.save_workflow_state(data)
        logger.info(f"Set workflow state for issue #{issue_num}: {state.value}")

    @staticmethod
    def get_workflow_state(issue_num: str) -> WorkflowState:
        """Get workflow state for an issue. Returns ACTIVE, PAUSED, STOPPED, or None."""
        data = StateManager.load_workflow_state()
        state_str = data.get(str(issue_num), {}).get("state")
        if state_str:
            try:
                return WorkflowState[state_str.upper()]
            except KeyError:
                return WorkflowState.ACTIVE
        return WorkflowState.ACTIVE

    @staticmethod
    def load_launched_agents() -> Dict[str, dict]:
        """Load recently launched agents from persistent storage."""
        ensure_data_dir()
        if os.path.exists(LAUNCHED_AGENTS_FILE):
            try:
                with open(LAUNCHED_AGENTS_FILE) as f:
                    data = json.load(f)
                    # Clean up entries older than 2-minute window
                    cutoff = time.time() - AGENT_RECENT_WINDOW
                    return {k: v for k, v in data.items() if v.get('timestamp', 0) > cutoff}
            except Exception as e:
                logger.error(f"Failed to load launched agents: {e}")
        return {}

    @staticmethod
    def save_launched_agents(data: Dict[str, dict]) -> None:
        """Save launched agents to persistent storage."""
        ensure_data_dir()
        try:
            with open(LAUNCHED_AGENTS_FILE, "w") as f:
                json.dump(data, f, indent=2)
        except Exception as e:
            logger.error(f"Failed to save launched agents: {e}")

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
        ensure_data_dir()
        if os.path.exists(TRACKED_ISSUES_FILE):
            try:
                with open(TRACKED_ISSUES_FILE) as f:
                    return json.load(f)
            except Exception as e:
                logger.error(f"Failed to load tracked issues: {e}")
        return {}

    @staticmethod
    def save_tracked_issues(data: Dict[int, dict]) -> None:
        """Save tracked issues to file."""
        ensure_data_dir()
        try:
            with open(TRACKED_ISSUES_FILE, "w") as f:
                json.dump(data, f, indent=2)
        except Exception as e:
            logger.error(f"Failed to save tracked issues: {e}")

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
        """Log an audit event for workflow tracking."""
        ensure_data_dir()
        try:
            timestamp = datetime.now().isoformat()
            log_entry = f"{timestamp} | Issue #{issue_num} | {event}"
            if details:
                log_entry += f" | {details}"
            log_entry += "\n"

            with open(AUDIT_LOG_FILE, "a") as f:
                f.write(log_entry)
            logger.debug(f"Audit: {log_entry.strip()}")
        except Exception as e:
            logger.error(f"Failed to write audit log: {e}")

    @staticmethod
    def get_audit_history(issue_num: int, limit: int = 50) -> List[str]:
        """Get recent audit history for an issue."""
        ensure_data_dir()
        if not os.path.exists(AUDIT_LOG_FILE):
            return []

        try:
            entries = []
            with open(AUDIT_LOG_FILE, "r") as f:
                for line in f:
                    if f"Issue #{issue_num}" in line:
                        entries.append(line.strip())
            return entries[-limit:]  # Return last 'limit' entries
        except Exception as e:
            logger.error(f"Failed to read audit log: {e}")
            return []

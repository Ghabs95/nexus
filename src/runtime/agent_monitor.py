"""Agent monitoring and recovery - handles timeouts, retries, and failures."""
import logging
import time
import os
from typing import Optional, Tuple
from datetime import datetime
from audit_store import AuditStore
from config import STUCK_AGENT_THRESHOLD
from orchestration.plugin_runtime import get_runtime_ops_plugin

logger = logging.getLogger(__name__)


class AgentMonitor:
    """Monitors agent execution and handles timeouts/failures."""

    # Track retry counts per issue+agent
    retry_counters = {}
    MAX_RETRIES = 2  # Retry up to 2 times before giving up

    @staticmethod
    def _retry_key(issue_num: str, agent_name: str) -> str:
        normalized_issue = str(issue_num or "").strip()
        normalized_agent = str(agent_name or "").strip().lstrip("@").strip().lower()
        return f"{normalized_issue}_{normalized_agent}"

    @staticmethod
    def check_timeout(issue_num: str, log_file: str) -> Tuple[bool, Optional[int]]:
        """
        Check if an agent has timed out.
        
        Returns: (timed_out, pid)
        """
        try:
            current_time = time.time()
            last_modified = os.path.getmtime(log_file)
            time_since_update = current_time - last_modified

            if time_since_update > STUCK_AGENT_THRESHOLD:
                # Check if process is still running
                runtime_ops = get_runtime_ops_plugin(cache_key="runtime-ops:monitor")
                pid = runtime_ops.find_agent_pid_for_issue(issue_num) if runtime_ops else None
                if pid:
                    logger.warning(
                        f"Issue #{issue_num}: Agent timeout detected "
                        f"(no activity for {int(time_since_update/60)} min, PID: {pid})"
                    )
                    return (True, pid)
        except Exception as e:
            logger.error(f"Error checking timeout for issue #{issue_num}: {e}")

        return (False, None)

    @staticmethod
    def kill_agent(pid: int, issue_num: str) -> bool:
        """Kill a stuck agent process."""
        try:
            runtime_ops = get_runtime_ops_plugin(cache_key="runtime-ops:monitor")
            if not runtime_ops or not runtime_ops.kill_process(pid, force=True):
                logger.error(f"Failed to kill agent PID {pid}")
                return False
            issue_text = str(issue_num or "").strip()
            issue_label = issue_text if issue_text else "unknown"
            logger.warning(f"Killed stuck agent PID {pid} for issue #{issue_label}")
            if issue_text.isdigit():
                AuditStore.audit_log(
                    int(issue_text),
                    "AGENT_TIMEOUT_KILL",
                    f"Killed agent process PID {pid} after timeout"
                )
            return True
        except Exception as e:
            logger.error(f"Failed to kill agent PID {pid}: {e}")
            return False

    @staticmethod
    def should_retry(issue_num: str, agent_name: str) -> bool:
        """Check if we should retry this agent."""
        key = AgentMonitor._retry_key(issue_num, agent_name)
        retry_count = AgentMonitor.retry_counters.get(key, 0)

        if retry_count < AgentMonitor.MAX_RETRIES:
            AgentMonitor.retry_counters[key] = retry_count + 1
            logger.info(f"Retry #{retry_count + 1} for {agent_name} on issue #{issue_num}")
            AuditStore.audit_log(
                int(issue_num),
                "AGENT_RETRY",
                f"Retrying {agent_name} (attempt {retry_count + 1}/{AgentMonitor.MAX_RETRIES})"
            )
            return True
        else:
            logger.error(f"Max retries reached for {agent_name} on issue #{issue_num}")
            AuditStore.audit_log(
                int(issue_num),
                "AGENT_FAILED",
                f"Agent {agent_name} failed after {AgentMonitor.MAX_RETRIES} retries"
            )
            return False

    @staticmethod
    def mark_failed(issue_num: str, agent_name: str, reason: str) -> None:
        """Mark an agent as permanently failed."""
        AuditStore.audit_log(int(issue_num), "AGENT_FAILED", reason)
        key = AgentMonitor._retry_key(issue_num, agent_name)
        AgentMonitor.retry_counters.pop(key, None)

    @staticmethod
    def reset_retries(issue_num: str, agent_name: str) -> None:
        """Reset retry counter for an issue+agent (called on success)."""
        key = AgentMonitor._retry_key(issue_num, agent_name)
        AgentMonitor.retry_counters.pop(key, None)


class WorkflowRouter:
    """Routes workflows based on issue labels and automatic tier selection."""

    @staticmethod
    def detect_workflow_tier(labels: list) -> Optional[str]:
        """
        Detect workflow tier from issue labels.
        
        Label mappings:
        - workflow:* → use explicit tier
        - priority:critical → fast-track (quick fixes)
        - bug → shortened (bug fix)
        - feature, enhancement → full (new feature)
        
        Returns: "full", "shortened", "fast-track", or None
        """
        # Check for explicit workflow labels
        for label in labels:
            if label == "workflow:full":
                return "full"
            elif label == "workflow:shortened":
                return "shortened"
            elif label == "workflow:fast-track":
                return "fast-track"

        # Auto-detect based on other labels
        labels_lower = [l.lower() for l in labels]

        if any("critical" in l or "hotfix" in l or "urgent" in l for l in labels_lower):
            return "fast-track"
        elif any("bug" in l or "fix" in l for l in labels_lower):
            return "shortened"
        elif any(
            "feature" in l or "enhancement" in l or "improvement" in l
            for l in labels_lower
        ):
            return "full"

        # Default to full for unclassified
        return "full"

    @staticmethod
    def suggest_tier_label(issue_title: str, issue_body: str) -> Optional[str]:
        """
        Suggest a workflow tier label based on issue title/body.
        
        Returns: Suggested label ("workflow:full", etc.) or None
        """
        content = f"{issue_title} {issue_body}".lower()

        if any(word in content for word in ["critical", "urgent", "hotfix", "asap"]):
            return "workflow:fast-track"
        elif any(word in content for word in ["bug", "fix", "problem"]):
            return "workflow:shortened"
        elif any(
            word in content for word in ["feature", "add", "enhancement", "improvement", "new"]
        ):
            return "workflow:full"

        return None

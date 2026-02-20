"""Dedicated audit storage utilities for Nexus.

Keeps audit read/write concerns separate from generic state management.
"""

import json
import logging
import os
from datetime import datetime, timedelta
from typing import List, Optional

from config import NEXUS_CORE_STORAGE_DIR

logger = logging.getLogger(__name__)


class AuditStore:
    """Read/write audit events in nexus-core JSONL format."""

    @staticmethod
    def _get_workflow_id(issue_num: int) -> str:
        """Resolve workflow ID for an issue, with system fallback."""
        from state_manager import StateManager

        workflow_id = StateManager.get_workflow_id_for_issue(str(issue_num))
        return workflow_id or "_nexus_system"

    @staticmethod
    def audit_log(issue_num: int, event: str, details: Optional[str] = None) -> None:
        """Log an audit event in nexus-core JSONL format."""
        try:
            timestamp = datetime.now().isoformat()
            workflow_id = AuditStore._get_workflow_id(issue_num)

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
        """Get recent audit events for an issue from JSONL audit files."""
        from state_manager import StateManager

        try:
            workflow_id = StateManager.get_workflow_id_for_issue(str(issue_num))
            if not workflow_id:
                return []

            audit_file = os.path.join(NEXUS_CORE_STORAGE_DIR, "audit", f"{workflow_id}.jsonl")
            if not os.path.exists(audit_file):
                return []

            entries: List[dict] = []
            with open(audit_file, "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        entries.append(json.loads(line))
                    except json.JSONDecodeError:
                        continue

            return entries[-limit:]
        except Exception as e:
            logger.error(f"Failed to read audit log: {e}")
            return []

    @staticmethod
    def read_all_audit_events(since_hours: Optional[int] = None) -> List[dict]:
        """Read all audit events across workflow audit JSONL files."""
        audit_dir = os.path.join(NEXUS_CORE_STORAGE_DIR, "audit")
        if not os.path.isdir(audit_dir):
            return []

        cutoff = datetime.now() - timedelta(hours=since_hours) if since_hours is not None else None

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

                        if cutoff is not None:
                            try:
                                ts = datetime.fromisoformat(evt.get("timestamp", ""))
                                if ts < cutoff:
                                    continue
                            except (ValueError, TypeError):
                                continue

                        events.append(evt)
            except Exception:
                continue

        events.sort(key=lambda event: event.get("timestamp", ""))
        return events

#!/usr/bin/env python3
"""Normalize tracked issues state for digest/report compatibility."""

import os
import sys
from typing import Any, Dict

sys.path.append(os.path.join(os.path.dirname(os.path.dirname(__file__)), "src"))

from state_manager import StateManager


def _normalize(issue_id: str, payload: Dict[str, Any]) -> Dict[str, Any]:
    normalized = dict(payload or {})

    status = str(normalized.get("status") or "").strip().lower()
    if not status:
        legacy_state = str(normalized.get("last_seen_state") or "").strip().lower()
        if legacy_state in {"closed", "resolved", "done", "completed", "implemented", "rejected"}:
            status = legacy_state
        else:
            status = "active"
    normalized["status"] = status

    project = str(normalized.get("project") or "").strip().lower()
    if not project:
        project = "global"
    normalized["project"] = project

    description = str(normalized.get("description") or "").strip()
    if not description:
        normalized["description"] = f"Issue #{issue_id}"

    return normalized


def main() -> int:
    tracked = StateManager.load_tracked_issues() or {}
    if not isinstance(tracked, dict):
        print("Tracked issues state is not a mapping; aborting.")
        return 1

    updated = 0
    normalized_all = {}
    for issue_id, payload in tracked.items():
        source = payload if isinstance(payload, dict) else {}
        normalized = _normalize(str(issue_id), source)
        normalized_all[str(issue_id)] = normalized
        if normalized != source:
            updated += 1

    if updated:
        StateManager.save_tracked_issues(normalized_all)

    print(f"Tracked issues backfill complete: total={len(tracked)}, updated={updated}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

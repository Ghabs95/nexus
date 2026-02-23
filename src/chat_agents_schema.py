"""Canonical schema helpers for project chat_agents configuration."""

from __future__ import annotations

from typing import Any, Dict, List


def normalize_chat_agents(raw_chat_agents: Any) -> List[Dict[str, Any]]:
    """Normalize chat_agents config payload into ordered entries with `agent_type`."""
    entries: List[Dict[str, Any]] = []

    if isinstance(raw_chat_agents, dict):
        for agent_type, payload in raw_chat_agents.items():
            normalized = str(agent_type or "").strip().lower()
            if not normalized:
                continue
            item: Dict[str, Any] = {"agent_type": normalized}
            if isinstance(payload, dict):
                item.update(payload)
            entries.append(item)
        return entries

    if isinstance(raw_chat_agents, list):
        for item in raw_chat_agents:
            if not isinstance(item, dict):
                continue

            if "agent_type" in item:
                normalized = str(item.get("agent_type") or "").strip().lower()
                if not normalized:
                    continue
                payload = dict(item)
                payload["agent_type"] = normalized
                entries.append(payload)
                continue

            if len(item) != 1:
                continue
            key, value = next(iter(item.items()))
            normalized = str(key or "").strip().lower()
            if not normalized:
                continue
            payload: Dict[str, Any] = {"agent_type": normalized}
            if isinstance(value, dict):
                payload.update(value)
            entries.append(payload)

    return entries


def get_project_chat_agents(project_cfg: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Return normalized ordered chat agent entries from a project config payload."""
    if not isinstance(project_cfg, dict):
        return []
    return normalize_chat_agents(project_cfg.get("chat_agents"))


def get_project_chat_agent_types(project_cfg: Dict[str, Any]) -> List[str]:
    """Return ordered agent_type values from project chat_agents."""
    return [entry["agent_type"] for entry in get_project_chat_agents(project_cfg)]


def get_default_project_chat_agent_type(project_cfg: Dict[str, Any]) -> str:
    """Return first configured chat agent type for the project, if any."""
    types = get_project_chat_agent_types(project_cfg)
    return types[0] if types else ""


def get_project_chat_agent_config(project_cfg: Dict[str, Any], agent_type: str) -> Dict[str, Any]:
    """Return per-agent chat config payload for a specific agent_type."""
    normalized = str(agent_type or "").strip().lower()
    if not normalized:
        return {}

    for entry in get_project_chat_agents(project_cfg):
        if entry.get("agent_type") != normalized:
            continue
        payload = dict(entry)
        payload.pop("agent_type", None)
        return payload

    return {}

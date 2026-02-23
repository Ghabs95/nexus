"""Shared project-key normalization helpers."""

from __future__ import annotations

from typing import Optional

from config import normalize_project_key


def normalize_project_key_str(value: str) -> str:
    """Normalize project key and coerce missing values to empty string."""
    normalized = normalize_project_key(value)
    return str(normalized or "")


def normalize_project_key_optional(value: str) -> Optional[str]:
    """Normalize project key and preserve optional output semantics."""
    return normalize_project_key(value)

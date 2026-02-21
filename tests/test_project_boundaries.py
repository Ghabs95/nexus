"""Regression tests for strict project/repository boundaries."""

from pathlib import Path
from unittest.mock import patch

import pytest


def test_extract_repo_from_issue_url_parses_owner_repo():
    from inbox_processor import _extract_repo_from_issue_url

    repo = _extract_repo_from_issue_url("https://github.com/Ghabs95/nexus-core/issues/43")

    assert repo == "Ghabs95/nexus-core"


def test_extract_repo_from_gitlab_issue_url_parses_namespace_repo():
    from inbox_processor import _extract_repo_from_issue_url

    repo = _extract_repo_from_issue_url(
        "https://gitlab.com/wallible/mobile-app/-/issues/77"
    )

    assert repo == "wallible/mobile-app"


def test_resolve_repo_strict_raises_on_mismatch(monkeypatch):
    import inbox_processor

    monkeypatch.setattr(inbox_processor, "get_github_repo", lambda project: "Ghabs95/nexus-core")
    monkeypatch.setattr(inbox_processor, "_resolve_repo_for_issue", lambda issue, default_project=None: "Ghabs95/nexus")

    with patch("inbox_processor.send_telegram_alert") as mock_alert:
        with pytest.raises(ValueError):
            inbox_processor._resolve_repo_strict("nexus", "43")

    mock_alert.assert_called_once()


def test_reroute_webhook_task_moves_file(tmp_path, monkeypatch):
    from inbox_processor import _reroute_webhook_task_to_project

    base_dir = tmp_path / "root"
    source_dir = base_dir / "workspace-a" / ".nexus" / "inbox" / "project-a"
    source_dir.mkdir(parents=True)
    source_file = source_dir / "issue_43.md"
    source_file.write_text("test")

    monkeypatch.setattr("inbox_processor.BASE_DIR", str(base_dir))
    monkeypatch.setattr(
        "inbox_processor.PROJECT_CONFIG",
        {
            "project-b": {
                "workspace": "workspace-b",
                "git_repo": "Ghabs95/nexus-core",
            }
        },
    )

    moved_path = _reroute_webhook_task_to_project(str(source_file), "project-b")

    assert moved_path is not None
    assert not source_file.exists()
    assert Path(moved_path).exists()
    assert str(Path(moved_path)).endswith("/.nexus/inbox/project-b/issue_43.md")


@patch("webhook_server.send_telegram_alert", return_value=True)
def test_webhook_blocks_unmapped_repository(mock_alert):
    from webhook_server import handle_issue_opened, _get_webhook_policy

    payload = {
        "action": "opened",
        "issue": {
            "number": 55,
            "title": "Boundary test",
            "body": "Body",
            "html_url": "https://github.com/unknown/repo/issues/55",
            "user": {"login": "alice"},
            "labels": [],
        },
        "repository": {"full_name": "unknown/repo"},
        "sender": {"login": "alice"},
    }

    event = _get_webhook_policy().parse_issue_event(payload)
    result = handle_issue_opened(payload, event)

    assert result["status"] == "ignored"
    assert result["reason"] == "unmapped_repository"
    mock_alert.assert_called_once()


def test_agent_launcher_resolves_issue_body_from_matching_project_repo(monkeypatch):
    import runtime.agent_launcher as agent_launcher

    class PluginA:
        def get_issue(self, issue_number, fields):
            return {
                "body": "**Task File:** `/tmp/base/workspace-b/.nexus/tasks/project-b/active/issue_43.md`"
            }

    class PluginB:
        def get_issue(self, issue_number, fields):
            return {
                "body": "**Task File:** `/tmp/base/workspace-b/.nexus/tasks/project-b/active/issue_43.md`"
            }

    plugins = {
        "org/repo-a": PluginA(),
        "org/repo-b": PluginB(),
    }

    monkeypatch.setattr(agent_launcher, "BASE_DIR", "/tmp/base")
    monkeypatch.setattr(
        agent_launcher,
        "PROJECT_CONFIG",
        {
            "project-a": {
                "workspace": "workspace-a",
                "git_repo": "org/repo-a",
                "agents_dir": "agents/a",
            },
            "project-b": {
                "workspace": "workspace-b",
                "git_repo": "org/repo-b",
                "agents_dir": "agents/b",
            },
        },
    )
    monkeypatch.setattr(agent_launcher, "_get_issue_plugin", lambda repo: plugins.get(repo))

    body, repo, task_file = agent_launcher._load_issue_body_from_project_repo("43")

    assert "Task File" in body
    assert repo == "org/repo-b"
    assert task_file.endswith("issue_43.md")


def test_resolve_project_for_repo_matches_secondary_repo(monkeypatch):
    import inbox_processor

    monkeypatch.setattr(
        inbox_processor,
        "PROJECT_CONFIG",
        {
            "wallible": {
                "workspace": "wallible",
                "git_repo": "acme/wallible-backend",
                "git_repos": ["acme/wallible-backend", "acme/wallible-mobile"],
            }
        },
    )

    assert inbox_processor._resolve_project_for_repo("acme/wallible-mobile") == "wallible"


def test_resolve_project_for_repo_matches_gitlab_secondary_repo(monkeypatch):
    import inbox_processor

    monkeypatch.setattr(
        inbox_processor,
        "PROJECT_CONFIG",
        {
            "wallible": {
                "workspace": "wallible",
                "git_platform": "gitlab",
                "git_repo": "wallible/backend",
                "git_repos": ["wallible/backend", "wallible/mobile-app"],
            }
        },
    )

    assert inbox_processor._resolve_project_for_repo("wallible/mobile-app") == "wallible"


@patch("webhook_server._notify_lifecycle", return_value=True)
@patch("webhook_server.send_telegram_alert", return_value=True)
def test_webhook_maps_secondary_repo_to_same_project(_mock_alert, _mock_notify, tmp_path, monkeypatch):
    import webhook_server

    base_dir = tmp_path / "workspace-root"
    base_dir.mkdir(parents=True)
    monkeypatch.setattr(webhook_server, "BASE_DIR", str(base_dir))
    monkeypatch.setattr(
        webhook_server,
        "PROJECT_CONFIG",
        {
            "wallible": {
                "workspace": "wallible-workspace",
                "git_repo": "acme/wallible-backend",
                "git_repos": ["acme/wallible-backend", "acme/wallible-mobile"],
            },
            "github_issue_triage": {
                "default_agent_type": "triage",
            },
        },
    )

    payload = {
        "action": "opened",
        "issue": {
            "number": 77,
            "title": "Cross-repo feature",
            "body": "Implement backend + mobile",
            "html_url": "https://github.com/acme/wallible-mobile/issues/77",
            "user": {"login": "alice"},
            "labels": [],
        },
        "repository": {"full_name": "acme/wallible-mobile"},
        "sender": {"login": "alice"},
    }

    event = webhook_server._get_webhook_policy().parse_issue_event(payload)
    result = webhook_server.handle_issue_opened(payload, event)

    assert result["status"] == "task_created"
    assert "wallible" in result["task_file"]

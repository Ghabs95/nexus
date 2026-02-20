"""Tests for project-aware git platform selection."""


def test_get_git_platform_returns_github_by_default(monkeypatch):
    import nexus_core_helpers
    from nexus.adapters.git.github import GitHubPlatform

    monkeypatch.setattr(nexus_core_helpers, "get_project_platform", lambda _project: "github")
    monkeypatch.setattr(nexus_core_helpers, "get_github_repo", lambda _project: "org/repo")

    platform = nexus_core_helpers.get_git_platform(project_name="nexus")

    assert isinstance(platform, GitHubPlatform)


def test_get_git_platform_returns_gitlab_for_gitlab_project(monkeypatch):
    import nexus_core_helpers
    from nexus.adapters.git.gitlab import GitLabPlatform

    monkeypatch.setattr(nexus_core_helpers, "get_project_platform", lambda _project: "gitlab")
    monkeypatch.setattr(nexus_core_helpers, "get_github_repo", lambda _project: "wallible/backend")
    monkeypatch.setattr(nexus_core_helpers, "get_gitlab_base_url", lambda _project: "https://gitlab.com")
    monkeypatch.setenv("GITLAB_TOKEN", "glpat-test")

    platform = nexus_core_helpers.get_git_platform(project_name="wallible")

    assert isinstance(platform, GitLabPlatform)


def test_get_git_platform_raises_when_gitlab_token_missing(monkeypatch):
    import pytest
    import nexus_core_helpers

    monkeypatch.setattr(nexus_core_helpers, "get_project_platform", lambda _project: "gitlab")
    monkeypatch.setattr(nexus_core_helpers, "get_github_repo", lambda _project: "wallible/backend")
    monkeypatch.delenv("GITLAB_TOKEN", raising=False)

    with pytest.raises(ValueError, match="GITLAB_TOKEN"):
        nexus_core_helpers.get_git_platform(project_name="wallible")

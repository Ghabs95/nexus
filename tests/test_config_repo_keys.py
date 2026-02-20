"""Tests for provider-neutral repository keys in project config."""


def test_get_github_repos_auto_discovers_from_workspace(monkeypatch, tmp_path):
    import config

    workspace_root = tmp_path / "wallible"
    backend_dir = workspace_root / "backend"
    mobile_dir = workspace_root / "mobile"
    backend_dir.mkdir(parents=True)
    mobile_dir.mkdir(parents=True)
    (backend_dir / ".git").mkdir()
    (mobile_dir / ".git").mkdir()

    monkeypatch.setattr(config, "BASE_DIR", str(tmp_path))
    monkeypatch.setattr(
        config,
        "_get_project_config",
        lambda: {
            "wallible": {
                "workspace": "wallible",
                "git_platform": "gitlab",
            }
        },
    )

    class _Result:
        def __init__(self, returncode: int, stdout: str):
            self.returncode = returncode
            self.stdout = stdout

    def fake_run(cmd, capture_output, text, timeout, check):
        target = cmd[2]
        if target.endswith("/backend"):
            return _Result(0, "git@gitlab.com:wallible/backend.git\n")
        if target.endswith("/mobile"):
            return _Result(0, "https://gitlab.com/wallible/mobile-app.git\n")
        return _Result(1, "")

    monkeypatch.setattr(config.subprocess, "run", fake_run)

    repos = config.get_github_repos("wallible")

    assert sorted(repos) == ["wallible/backend", "wallible/mobile-app"]


def test_get_github_repos_supports_git_repo_and_git_repos(monkeypatch):
    import config

    monkeypatch.setattr(
        config,
        "_get_project_config",
        lambda: {
            "wallible": {
                "workspace": "wallible",
                "git_platform": "gitlab",
                "git_repo": "wallible/backend",
                "git_repos": ["wallible/backend", "wallible/mobile-app"],
            }
        },
    )

    repos = config.get_github_repos("wallible")

    assert repos == ["wallible/backend", "wallible/mobile-app"]

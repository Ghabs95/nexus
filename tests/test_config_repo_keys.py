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


def test_get_workflow_profile_prefers_project_specific(monkeypatch):
    import config

    monkeypatch.setattr(
        config,
        "_get_project_config",
        lambda: {
            "workflow_definition_path": "ghabs/agents/workflows/ghabs_org_workflow.yaml",
            "wallible": {
                "workspace": "wallible",
                "workflow_definition_path": "wallible/wlbl-workflow-os/workflows/master.yaml",
            },
        },
    )

    assert config.get_workflow_profile("wallible") == "wallible/wlbl-workflow-os/workflows/master.yaml"


def test_get_workflow_profile_falls_back_to_global(monkeypatch):
    import config

    monkeypatch.setattr(
        config,
        "_get_project_config",
        lambda: {
            "workflow_definition_path": "ghabs/agents/workflows/ghabs_org_workflow.yaml",
            "nexus": {"workspace": "ghabs"},
        },
    )

    assert config.get_workflow_profile("nexus") == "ghabs/agents/workflows/ghabs_org_workflow.yaml"


def test_normalize_project_key_uses_config_aliases(monkeypatch):
    import config

    monkeypatch.setattr(
        config,
        "_get_project_config",
        lambda: {
            "projects": {
                "casit": {"code": "case_italia", "aliases": []},
                "wlbl": {"code": "wallible", "aliases": []},
                "nxs": {"code": "nexus", "aliases": ["nexus core", "nexus-core"]},
            },
            "case_italia": {"workspace": "case_italia"},
            "wallible": {"workspace": "wallible"},
            "nexus": {"workspace": "ghabs"},
        },
    )

    assert config.normalize_project_key("casit") == "case_italia"
    assert config.normalize_project_key("nxs") == "nexus"
    assert config.normalize_project_key("wallible") == "wallible"
    assert config.normalize_project_key("unknown") == "unknown"


def test_get_track_short_projects_derives_from_aliases(monkeypatch):
    import config

    monkeypatch.setattr(
        config,
        "_get_project_config",
        lambda: {
            "projects": {
                "casit": {"code": "case_italia", "aliases": []},
                "wlbl": {"code": "wallible", "aliases": []},
                "bm": {"code": "biome", "aliases": []},
                "nxs": {"code": "nexus", "aliases": ["nexus core", "nexus-core"]},
            },
            "case_italia": {"workspace": "case_italia"},
            "wallible": {"workspace": "wallible"},
            "biome": {"workspace": "biome"},
            "nexus": {"workspace": "ghabs"},
        },
    )

    assert config.get_track_short_projects() == ["casit", "wlbl", "bm", "nxs"]


def test_get_chat_agents_reads_mapping_shape(monkeypatch):
    import config

    monkeypatch.setattr(
        config,
        "_get_project_config",
        lambda: {
            "wallible": {
                "workspace": "wallible",
                "chat_agents": {
                    "business": {"context_path": "wlbl-business-os", "label": "Business"},
                    "marketing": {"context_path": "wlbl-marketing-os", "label": "Marketing"},
                },
            }
        },
    )

    agents = config.get_chat_agents("wallible")

    assert [item["agent_type"] for item in agents] == ["business", "marketing"]
    assert agents[0]["context_path"] == "wlbl-business-os"
    assert agents[0]["label"] == "Business"


def test_get_chat_agents_reads_list_shape(monkeypatch):
    import config

    monkeypatch.setattr(
        config,
        "_get_project_config",
        lambda: {
            "wallible": {
                "workspace": "wallible",
                "chat_agents": [
                    {"business": {"context_path": "wlbl-business-os"}},
                    {"agent_type": "marketing", "context_path": "wlbl-marketing-os"},
                ],
            }
        },
    )

    agents = config.get_chat_agents("wallible")

    assert [item["agent_type"] for item in agents] == ["business", "marketing"]
    assert agents[0]["context_path"] == "wlbl-business-os"
    assert agents[1]["context_path"] == "wlbl-marketing-os"

from handlers import feature_ideation_handlers as handlers


def test_detect_feature_project_uses_config_aliases(monkeypatch):
    import config

    monkeypatch.setattr(
        config,
        "get_project_aliases",
        lambda: {
            "nxs": "nexus",
            "wlbl": "wallible",
        },
    )

    detected = handlers.detect_feature_project(
        "Can you propose top 3 features for nxs this week?",
        projects={"nexus": "Nexus", "wallible": "Wallible"},
    )

    assert detected == "nexus"


def test_detect_feature_project_falls_back_to_project_keys(monkeypatch):
    import config

    monkeypatch.setattr(config, "get_project_aliases", lambda: {})

    detected = handlers.detect_feature_project(
        "What features should we add to wallible?",
        projects={"wallible": "Wallible", "nexus": "Nexus"},
    )

    assert detected == "wallible"

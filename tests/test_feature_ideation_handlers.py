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


class _CaptureOrchestrator:
    def __init__(self):
        self.persona = ""

    def run_text_to_speech_analysis(self, **kwargs):
        self.persona = str(kwargs.get("persona", ""))
        return {
            "items": [
                {
                    "title": "Improve onboarding conversion",
                    "summary": "Reduce onboarding drop-off with guided checklist.",
                    "why": "Higher activation and retention.",
                    "steps": ["Audit funnel", "Implement checklist", "Track activation KPI"],
                }
            ]
        }


class _ArrayTextOrchestrator:
    def run_text_to_speech_analysis(self, **_kwargs):
        return {
            "text": (
                "```json\n"
                "["
                "{\"title\":\"Improve retention loops\","
                "\"summary\":\"Add habit reminders tied to active goals.\","
                "\"why\":\"Improves weekly active usage.\","
                "\"steps\":[\"Define trigger points\",\"Implement reminder jobs\",\"Track WAU uplift\"]}"
                "]\n"
                "```"
            )
        }


class _NonJsonTextOrchestrator:
    def run_text_to_speech_analysis(self, **_kwargs):
        return {"text": "This is plain text and not valid JSON for feature items."}


class _CaptureLogger:
    def __init__(self):
        self.messages = []

    def info(self, message, *args):
        if args:
            self.messages.append(str(message) % args)
        else:
            self.messages.append(str(message))

    def warning(self, message, *args):
        if args:
            self.messages.append(str(message) % args)
        else:
            self.messages.append(str(message))


class _SingleItemDictOrchestrator:
    def run_text_to_speech_analysis(self, **_kwargs):
        return {
            "title": "Improve retention loops",
            "summary": "Add habit reminders tied to active goals.",
            "why": "Improves weekly active usage.",
            "steps": ["Define trigger points", "Implement reminder jobs", "Track WAU uplift"],
        }


class _WrappedResponseOrchestrator:
    def run_text_to_speech_analysis(self, **_kwargs):
        return {
            "session_id": "abc-123",
            "stats": {"tokens": 42},
            "response": (
                "{\n"
                "  \"items\": [\n"
                "    {\n"
                "      \"title\": \"Multi-Asset Performance Benchmarking\",\n"
                "      \"summary\": \"Overlay diversified portfolio performance against market indices.\",\n"
                "      \"why\": \"Improves long-term performance visibility and retention.\",\n"
                "      \"steps\": [\"Map asset weights\", \"Add benchmark layer\", \"Ship comparison dashboard\"]\n"
                "    }\n"
                "  ]\n"
                "}"
            ),
        }


class _CopilotFallbackSuccessOrchestrator:
    def run_text_to_speech_analysis(self, **_kwargs):
        return {"text": "not-json"}

    def _run_copilot_analysis(self, *_args, **_kwargs):
        return {
            "items": [
                {
                    "title": "Copilot-generated roadmap slice",
                    "summary": "Break roadmap into measurable monthly increments.",
                    "why": "Improves execution predictability and visibility.",
                    "steps": ["Define milestones", "Map owner per milestone", "Track completion"],
                }
            ]
        }


def test_build_feature_suggestions_requires_agent_prompt(tmp_path):
    workspace_root = tmp_path / "workspace"
    business_dir = workspace_root / "business-os"
    business_dir.mkdir(parents=True)
    (business_dir / "README.md").write_text(
        "Business context that should not be used without prompt definition.",
        encoding="utf-8",
    )

    orchestrator = _CaptureOrchestrator()
    deps = handlers.FeatureIdeationHandlerDeps(
        logger=None,
        allowed_user_ids=[],
        projects={"acme": "Acme"},
        get_project_label=lambda key: "Acme" if key == "acme" else key,
        orchestrator=orchestrator,
        base_dir=str(tmp_path),
        project_config={
            "acme": {
                "workspace": "workspace",
                "chat_agents": {
                    "business": {
                        "context_path": "business-os",
                        "context_files": ["README.md"],
                    }
                },
            }
        },
    )

    items = handlers._build_feature_suggestions(
        project_key="acme",
        text="Propose top 1 feature",
        deps=deps,
        preferred_agent_type="business",
        feature_count=1,
    )

    assert items == []
    assert orchestrator.persona == ""


def test_build_feature_suggestions_uses_business_context_folder(tmp_path):
    workspace_root = tmp_path / "workspace"
    business_dir = workspace_root / "business-os"
    agents_dir = workspace_root / "agents"
    business_dir.mkdir(parents=True)
    agents_dir.mkdir(parents=True)
    (business_dir / "README.md").write_text(
        "Business OS context: prioritize revenue and retention.",
        encoding="utf-8",
    )
    (agents_dir / "business.yaml").write_text(
        "spec:\n"
        "  agent_type: business\n"
        "  prompt_template: |\n"
        "    Dedicated Advisor Prompt\n"
        "    Strategic constraints and principles.\n",
        encoding="utf-8",
    )

    orchestrator = _CaptureOrchestrator()
    deps = handlers.FeatureIdeationHandlerDeps(
        logger=None,
        allowed_user_ids=[],
        projects={"acme": "Acme"},
        get_project_label=lambda key: "Acme" if key == "acme" else key,
        orchestrator=orchestrator,
        base_dir=str(tmp_path),
        project_config={
            "acme": {
                "workspace": "workspace",
                "agents_dir": "workspace/agents",
                "chat_agents": {
                    "business": {
                        "context_path": "business-os",
                        "context_files": ["README.md"],
                    }
                },
            }
        },
    )

    items = handlers._build_feature_suggestions(
        project_key="acme",
        text="Propose top 1 feature",
        deps=deps,
        preferred_agent_type="business",
        feature_count=1,
    )

    assert len(items) == 1
    assert "Dedicated Advisor Prompt" in orchestrator.persona
    assert "Context folders: business-os" in orchestrator.persona
    assert "prioritize revenue and retention" in orchestrator.persona


def test_build_feature_suggestions_uses_marketing_context_folder(tmp_path):
    workspace_root = tmp_path / "workspace"
    marketing_dir = workspace_root / "marketing-os"
    agents_dir = workspace_root / "agents"
    marketing_dir.mkdir(parents=True)
    agents_dir.mkdir(parents=True)
    (marketing_dir / "README.md").write_text(
        "Marketing OS context: focus on channel strategy and activation.",
        encoding="utf-8",
    )
    (agents_dir / "marketing.yaml").write_text(
        """
spec:
  agent_type: marketing
  prompt_template: |
    Dedicated Marketing Prompt
    Focus on acquisition and activation outcomes.
""".strip(),
        encoding="utf-8",
    )

    orchestrator = _CaptureOrchestrator()
    deps = handlers.FeatureIdeationHandlerDeps(
        logger=None,
        allowed_user_ids=[],
        projects={"acme": "Acme"},
        get_project_label=lambda key: "Acme" if key == "acme" else key,
        orchestrator=orchestrator,
        base_dir=str(tmp_path),
        project_config={
            "acme": {
                "workspace": "workspace",
                "agents_dir": "workspace/agents",
                "chat_agents": {
                    "marketing": {
                        "context_path": "marketing-os",
                        "context_files": ["README.md"],
                    }
                },
            }
        },
    )

    items = handlers._build_feature_suggestions(
        project_key="acme",
        text="Propose top 1 marketing feature",
        deps=deps,
        preferred_agent_type="marketing",
        feature_count=1,
    )

    assert len(items) == 1
    assert "Dedicated Marketing Prompt" in orchestrator.persona
    assert "Context folders: marketing-os" in orchestrator.persona
    assert "focus on channel strategy and activation" in orchestrator.persona


def test_agent_prompt_discovery_matches_spec_agent_type_without_prompt_map(tmp_path):
    workspace_root = tmp_path / "workspace"
    agents_dir = workspace_root / "agents"
    agents_dir.mkdir(parents=True)
    (agents_dir / "business.yaml").write_text(
        "spec:\n"
        "  agent_type: business\n"
        "  prompt_template: |\n"
        "    Business Prompt From AgentType Match\n",
        encoding="utf-8",
    )

    orchestrator = _CaptureOrchestrator()
    deps = handlers.FeatureIdeationHandlerDeps(
        logger=None,
        allowed_user_ids=[],
        projects={"acme": "Acme"},
        get_project_label=lambda key: "Acme" if key == "acme" else key,
        orchestrator=orchestrator,
        base_dir=str(tmp_path),
        project_config={
            "acme": {
                "workspace": "workspace",
                "agents_dir": "workspace/agents",
                "chat_agents": {
                    "business": {
                        "context_path": "business-os",
                        "context_files": ["README.md"],
                    }
                },
            }
        },
    )

    handlers._build_feature_suggestions(
        project_key="acme",
        text="Propose top 1 feature",
        deps=deps,
        preferred_agent_type="business",
        feature_count=1,
    )

    assert "Business Prompt From AgentType Match" in orchestrator.persona


def test_build_feature_suggestions_omitted_context_files_disables_context_loading(tmp_path):
    workspace_root = tmp_path / "workspace"
    marketing_dir = workspace_root / "marketing-os"
    agents_dir = workspace_root / "agents"
    marketing_dir.mkdir(parents=True)
    agents_dir.mkdir(parents=True)
    (marketing_dir / "README.md").write_text(
        "This should not be loaded when context_files is omitted.",
        encoding="utf-8",
    )
    (agents_dir / "marketing.yaml").write_text(
        """
spec:
  agent_type: marketing
  prompt_template: |
    Dedicated Marketing Prompt
""".strip(),
        encoding="utf-8",
    )

    orchestrator = _CaptureOrchestrator()
    deps = handlers.FeatureIdeationHandlerDeps(
        logger=None,
        allowed_user_ids=[],
        projects={"acme": "Acme"},
        get_project_label=lambda key: "Acme" if key == "acme" else key,
        orchestrator=orchestrator,
        base_dir=str(tmp_path),
        project_config={
            "acme": {
                "workspace": "workspace",
                "agents_dir": "workspace/agents",
                "chat_agents": {
                    "marketing": {
                        "context_path": "marketing-os",
                    }
                }
            }
        },
    )

    items = handlers._build_feature_suggestions(
        project_key="acme",
        text="Propose top 1 marketing feature",
        deps=deps,
        preferred_agent_type="marketing",
        feature_count=1,
    )

    assert len(items) == 1
    assert "This should not be loaded" not in orchestrator.persona
    assert "Context folders:" not in orchestrator.persona


def test_chat_agents_list_shape_is_supported_for_context(tmp_path):
    workspace_root = tmp_path / "workspace"
    business_dir = workspace_root / "business-os"
    agents_dir = workspace_root / "agents"
    business_dir.mkdir(parents=True)
    agents_dir.mkdir(parents=True)
    (business_dir / "README.md").write_text(
        "List-shape context should be loaded.",
        encoding="utf-8",
    )
    (agents_dir / "business.yaml").write_text(
        "spec:\n"
        "  agent_type: business\n"
        "  prompt_template: |\n"
        "    Dedicated Advisor Prompt\n",
        encoding="utf-8",
    )

    orchestrator = _CaptureOrchestrator()
    deps = handlers.FeatureIdeationHandlerDeps(
        logger=None,
        allowed_user_ids=[],
        projects={"acme": "Acme"},
        get_project_label=lambda key: "Acme" if key == "acme" else key,
        orchestrator=orchestrator,
        base_dir=str(tmp_path),
        project_config={
            "acme": {
                "workspace": "workspace",
                "agents_dir": "workspace/agents",
                "chat_agents": [
                    {
                        "business": {
                            "context_path": "business-os",
                            "context_files": ["README.md"],
                        }
                    }
                ],
            }
        },
    )

    items = handlers._build_feature_suggestions(
        project_key="acme",
        text="Propose top 1 feature",
        deps=deps,
        preferred_agent_type="business",
        feature_count=1,
    )

    assert len(items) == 1
    assert "List-shape context should be loaded." in orchestrator.persona


def test_build_feature_suggestions_accepts_top_level_json_array_text(tmp_path):
    workspace_root = tmp_path / "workspace"
    agents_dir = workspace_root / "agents"
    agents_dir.mkdir(parents=True)
    (agents_dir / "business.yaml").write_text(
        "spec:\n"
        "  agent_type: business\n"
        "  prompt_template: |\n"
        "    Dedicated Advisor Prompt\n",
        encoding="utf-8",
    )

    deps = handlers.FeatureIdeationHandlerDeps(
        logger=None,
        allowed_user_ids=[],
        projects={"acme": "Acme"},
        get_project_label=lambda key: "Acme" if key == "acme" else key,
        orchestrator=_ArrayTextOrchestrator(),
        base_dir=str(tmp_path),
        project_config={
            "acme": {
                "workspace": "workspace",
                "agents_dir": "workspace/agents",
                "chat_agents": {
                    "business": {
                        "context_path": "business-os",
                        "context_files": ["README.md"],
                    }
                },
            }
        },
    )

    items = handlers._build_feature_suggestions(
        project_key="acme",
        text="Propose top 1 feature",
        deps=deps,
        preferred_agent_type="business",
        feature_count=1,
    )

    assert len(items) == 1
    assert items[0]["title"] == "Improve retention loops"


def test_build_feature_suggestions_logs_primary_non_json_response(tmp_path):
    workspace_root = tmp_path / "workspace"
    agents_dir = workspace_root / "agents"
    agents_dir.mkdir(parents=True)
    (agents_dir / "business.yaml").write_text(
        "spec:\n"
        "  agent_type: business\n"
        "  prompt_template: |\n"
        "    Dedicated Advisor Prompt\n",
        encoding="utf-8",
    )

    logger = _CaptureLogger()
    deps = handlers.FeatureIdeationHandlerDeps(
        logger=logger,
        allowed_user_ids=[],
        projects={"acme": "Acme"},
        get_project_label=lambda key: "Acme" if key == "acme" else key,
        orchestrator=_NonJsonTextOrchestrator(),
        base_dir=str(tmp_path),
        project_config={
            "acme": {
                "workspace": "workspace",
                "agents_dir": "workspace/agents",
                "chat_agents": {
                    "business": {
                        "context_path": "business-os",
                        "context_files": ["README.md"],
                    }
                },
            }
        },
    )

    items = handlers._build_feature_suggestions(
        project_key="acme",
        text="Propose top 1 feature",
        deps=deps,
        preferred_agent_type="business",
        feature_count=1,
    )

    assert items == []
    assert any("Primary feature ideation raw response (truncated):" in msg for msg in logger.messages)
    assert any("not valid JSON" in msg for msg in logger.messages)


def test_build_feature_suggestions_accepts_structured_single_item_dict(tmp_path):
    workspace_root = tmp_path / "workspace"
    agents_dir = workspace_root / "agents"
    agents_dir.mkdir(parents=True)
    (agents_dir / "business.yaml").write_text(
        "spec:\n"
        "  agent_type: business\n"
        "  prompt_template: |\n"
        "    Dedicated Advisor Prompt\n",
        encoding="utf-8",
    )

    deps = handlers.FeatureIdeationHandlerDeps(
        logger=_CaptureLogger(),
        allowed_user_ids=[],
        projects={"acme": "Acme"},
        get_project_label=lambda key: "Acme" if key == "acme" else key,
        orchestrator=_SingleItemDictOrchestrator(),
        base_dir=str(tmp_path),
        project_config={
            "acme": {
                "workspace": "workspace",
                "agents_dir": "workspace/agents",
                "chat_agents": {
                    "business": {
                        "context_path": "business-os",
                        "context_files": ["README.md"],
                    }
                },
            }
        },
    )

    items = handlers._build_feature_suggestions(
        project_key="acme",
        text="Propose top 1 feature",
        deps=deps,
        preferred_agent_type="business",
        feature_count=1,
    )

    assert len(items) == 1
    assert items[0]["title"] == "Improve retention loops"


def test_build_feature_suggestions_accepts_wrapped_response_json_string(tmp_path):
    workspace_root = tmp_path / "workspace"
    agents_dir = workspace_root / "agents"
    agents_dir.mkdir(parents=True)
    (agents_dir / "business.yaml").write_text(
        "spec:\n"
        "  agent_type: business\n"
        "  prompt_template: |\n"
        "    Dedicated Advisor Prompt\n",
        encoding="utf-8",
    )

    logger = _CaptureLogger()
    deps = handlers.FeatureIdeationHandlerDeps(
        logger=logger,
        allowed_user_ids=[],
        projects={"acme": "Acme"},
        get_project_label=lambda key: "Acme" if key == "acme" else key,
        orchestrator=_WrappedResponseOrchestrator(),
        base_dir=str(tmp_path),
        project_config={
            "acme": {
                "workspace": "workspace",
                "agents_dir": "workspace/agents",
                "chat_agents": {
                    "business": {
                        "context_path": "business-os",
                        "context_files": ["README.md"],
                    }
                },
            }
        },
    )

    items = handlers._build_feature_suggestions(
        project_key="acme",
        text="Propose top 1 feature",
        deps=deps,
        preferred_agent_type="business",
        feature_count=1,
    )

    assert len(items) == 1
    assert items[0]["title"] == "Multi-Asset Performance Benchmarking"
    assert not any("retrying with Copilot" in msg for msg in logger.messages)


def test_build_feature_suggestions_logs_success_when_copilot_fallback_succeeds(tmp_path):
    workspace_root = tmp_path / "workspace"
    agents_dir = workspace_root / "agents"
    agents_dir.mkdir(parents=True)
    (agents_dir / "business.yaml").write_text(
        "spec:\n"
        "  agent_type: business\n"
        "  prompt_template: |\n"
        "    Dedicated Advisor Prompt\n",
        encoding="utf-8",
    )

    logger = _CaptureLogger()
    deps = handlers.FeatureIdeationHandlerDeps(
        logger=logger,
        allowed_user_ids=[],
        projects={"acme": "Acme"},
        get_project_label=lambda key: "Acme" if key == "acme" else key,
        orchestrator=_CopilotFallbackSuccessOrchestrator(),
        base_dir=str(tmp_path),
        project_config={
            "acme": {
                "workspace": "workspace",
                "agents_dir": "workspace/agents",
                "chat_agents": {
                    "business": {
                        "context_path": "business-os",
                        "context_files": ["README.md"],
                    }
                },
            }
        },
    )

    items = handlers._build_feature_suggestions(
        project_key="acme",
        text="Propose top 1 feature",
        deps=deps,
        preferred_agent_type="business",
        feature_count=1,
    )

    assert len(items) == 1
    assert items[0]["title"] == "Copilot-generated roadmap slice"
    assert any(
        "Feature ideation success: provider=copilot primary_success=false fallback_used=true items=1"
        in msg
        for msg in logger.messages
    )

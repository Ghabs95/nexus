import pytest

from handlers import inbox_routing_handler as routing


class _FakeOrchestrator:
    def __init__(self, payload):
        self.payload = payload

    def run_text_to_speech_analysis(self, **_kwargs):
        return self.payload


class _FailingOrchestrator:
    def run_text_to_speech_analysis(self, **_kwargs):
        raise AssertionError("classifier should not be called when project context is set")


@pytest.mark.asyncio
async def test_process_inbox_task_parses_project_from_response_payload(tmp_path, monkeypatch):
    monkeypatch.setattr(routing, "BASE_DIR", str(tmp_path))
    monkeypatch.setattr(routing, "PROJECT_CONFIG", {"nexus": {"workspace": "nexus"}})
    monkeypatch.setattr(
        routing,
        "get_inbox_dir",
        lambda workspace_root, project: str(tmp_path / workspace_root.split("/")[-1] / project / "inbox"),
    )

    orchestrator = _FakeOrchestrator(
        {
            "session_id": "abc",
            "response": (
                '{"project": "nexus", "type": "feature", '
                '"task_name": "evaluate-feature-alignment-knowledge-base"}'
            ),
        }
    )

    result = await routing.process_inbox_task(
        text="evaluate this feature",
        orchestrator=orchestrator,
        message_id_or_unique_id="123",
    )

    assert result["success"] is True
    assert result["project"] == "nexus"


@pytest.mark.asyncio
async def test_process_inbox_task_uses_project_hint_when_classifier_project_missing(tmp_path, monkeypatch):
    monkeypatch.setattr(routing, "BASE_DIR", str(tmp_path))
    monkeypatch.setattr(routing, "PROJECT_CONFIG", {"wallible": {"workspace": "wallible"}})
    monkeypatch.setattr(
        routing,
        "get_inbox_dir",
        lambda workspace_root, project: str(tmp_path / workspace_root.split("/")[-1] / project / "inbox"),
    )

    orchestrator = _FakeOrchestrator({"response": '{"type": "feature", "task_name": "missing-project"}'})

    result = await routing.process_inbox_task(
        text="please route this",
        orchestrator=orchestrator,
        message_id_or_unique_id="456",
        project_hint="wallible",
    )

    assert result["success"] is True
    assert result["project"] == "wallible"


@pytest.mark.asyncio
async def test_process_inbox_task_skips_classification_when_project_hint_set(tmp_path, monkeypatch):
    monkeypatch.setattr(routing, "BASE_DIR", str(tmp_path))
    monkeypatch.setattr(routing, "PROJECT_CONFIG", {"wallible": {"workspace": "wallible"}})
    monkeypatch.setattr(
        routing,
        "get_inbox_dir",
        lambda workspace_root, project: str(tmp_path / workspace_root.split("/")[-1] / project / "inbox"),
    )

    result = await routing.process_inbox_task(
        text="route directly with context",
        orchestrator=_FailingOrchestrator(),
        message_id_or_unique_id="789",
        project_hint="wallible",
    )

    assert result["success"] is True
    assert result["project"] == "wallible"

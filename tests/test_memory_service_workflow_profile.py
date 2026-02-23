from services import memory_service


def test_default_chat_metadata_uses_project_workflow_profile(monkeypatch):
    monkeypatch.setattr(memory_service, "get_workflow_profile", lambda project: "wallible/wlbl-workflow-os/workflows/master.yaml")

    metadata = memory_service._default_chat_metadata("wallible")

    assert metadata["workflow_profile"] == "wallible/wlbl-workflow-os/workflows/master.yaml"


def test_normalize_chat_data_replaces_generic_profile_with_project_specific(monkeypatch):
    monkeypatch.setattr(memory_service, "get_workflow_profile", lambda project: "wallible/wlbl-workflow-os/workflows/master.yaml")
    monkeypatch.setattr(memory_service, "get_chat_agent_types", lambda project: ["business", "marketing"])

    normalized = memory_service._normalize_chat_data(
        {
            "id": "chat1",
            "metadata": {
                "project_key": "wallible",
                "workflow_profile": "ghabs_org_workflow",
            },
        }
    )

    metadata = normalized["metadata"]
    assert metadata["project_key"] == "wallible"
    assert metadata["workflow_profile"] == "wallible/wlbl-workflow-os/workflows/master.yaml"
    assert metadata["primary_agent_type"] == "business"

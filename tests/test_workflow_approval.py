"""Tests for workflow approval gate feature."""
import asyncio
import json
import sys
from pathlib import Path
from datetime import datetime
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

# Ensure src is on the path (conftest.py handles this, but be explicit)
src_path = Path(__file__).parent.parent / "src"
if str(src_path) not in sys.path:
    sys.path.insert(0, str(src_path))

# nexus-core path
nexus_core_path = Path(__file__).parent.parent.parent / "nexus-core"
if str(nexus_core_path) not in sys.path:
    sys.path.insert(0, str(nexus_core_path))

from nexus.core.models import (
    Agent,
    StepStatus,
    Workflow,
    WorkflowState,
    WorkflowStep,
)
from nexus.core.workflow import WorkflowDefinition, WorkflowEngine


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_agent(name: str = "TestAgent") -> Agent:
    return Agent(name=name, display_name=name, description="test agent", timeout=60)


def _make_step(step_num: int, approval_required: bool = False, approvers=None) -> WorkflowStep:
    return WorkflowStep(
        step_num=step_num,
        name=f"step_{step_num}",
        agent=_make_agent(),
        prompt_template="Do the thing",
        approval_required=approval_required,
        approvers=approvers or [],
        approval_timeout=3600,
    )


def _make_workflow(steps=None) -> Workflow:
    if steps is None:
        steps = [_make_step(1), _make_step(2)]
    return Workflow(
        id="test-wf-1",
        name="Test Workflow",
        version="1.0",
        steps=steps,
        state=WorkflowState.RUNNING,
        current_step=1,
    )


def _make_storage(workflow: Workflow):
    """Return a mock StorageBackend pre-loaded with `workflow`."""
    storage = AsyncMock()
    storage.load_workflow.return_value = workflow
    storage.save_workflow.return_value = None
    storage.append_audit_event.return_value = None
    return storage


# ---------------------------------------------------------------------------
# Model tests
# ---------------------------------------------------------------------------

class TestWorkflowStepApprovalFields:
    def test_default_approval_required_is_false(self):
        step = _make_step(1)
        assert step.approval_required is False

    def test_approval_required_true(self):
        step = _make_step(1, approval_required=True)
        assert step.approval_required is True

    def test_default_approval_timeout(self):
        step = WorkflowStep(
            step_num=1,
            name="s",
            agent=_make_agent(),
            prompt_template="x",
        )
        assert step.approval_timeout == 86400  # 24 hours default

    def test_approvers_list(self):
        step = _make_step(1, approvers=["tech-lead", "devops-team"])
        assert step.approvers == ["tech-lead", "devops-team"]


class TestWorkflowStateEnum:
    def test_awaiting_approval_in_enum(self):
        assert WorkflowState.AWAITING_APPROVAL.value == "awaiting_approval"

    def test_is_complete_does_not_include_awaiting(self):
        wf = _make_workflow()
        wf.state = WorkflowState.AWAITING_APPROVAL
        assert not wf.is_complete()


# ---------------------------------------------------------------------------
# WorkflowDefinition YAML / dict parsing tests
# ---------------------------------------------------------------------------

class TestWorkflowDefinitionApprovalParsing:
    def test_from_dict_parses_approval_required(self):
        data = {
            "name": "My Workflow",
            "steps": [
                {"name": "design", "agent_type": "Architect", "approval_required": True},
                {"name": "deploy", "agent_type": "OpsCommander", "approval_required": False},
            ],
        }
        wf = WorkflowDefinition.from_dict(data)
        assert wf.steps[0].approval_required is True
        assert wf.steps[1].approval_required is False

    def test_from_dict_parses_approval_timeout(self):
        data = {
            "name": "My Workflow",
            "steps": [
                {
                    "name": "deploy",
                    "agent_type": "OpsCommander",
                    "approval_required": True,
                    "approval_timeout": 7200,
                }
            ],
        }
        wf = WorkflowDefinition.from_dict(data)
        assert wf.steps[0].approval_timeout == 7200

    def test_from_dict_parses_approvers(self):
        data = {
            "name": "My Workflow",
            "steps": [
                {
                    "name": "deploy",
                    "agent_type": "OpsCommander",
                    "approval_required": True,
                    "approvers": ["tech-lead", "devops-team"],
                }
            ],
        }
        wf = WorkflowDefinition.from_dict(data)
        assert wf.steps[0].approvers == ["tech-lead", "devops-team"]

    def test_from_dict_defaults_when_not_specified(self):
        data = {
            "name": "My Workflow",
            "steps": [{"name": "plain", "agent_type": "Copilot"}],
        }
        wf = WorkflowDefinition.from_dict(data)
        assert wf.steps[0].approval_required is False
        assert wf.steps[0].approval_timeout == 86400
        assert wf.steps[0].approvers == []

    def test_from_yaml(self, tmp_path):
        yaml_content = """\
name: Deploy Flow
steps:
  - name: design
    agent_type: Architect
    approval_required: true
    approval_timeout: 86400
    approvers:
      - tech-lead
  - name: deploy
    agent_type: OpsCommander
    approval_required: true
    approvers:
      - tech-lead
      - devops-team
"""
        yaml_file = tmp_path / "workflow.yaml"
        yaml_file.write_text(yaml_content)

        wf = WorkflowDefinition.from_yaml(str(yaml_file))
        assert len(wf.steps) == 2

        design = wf.steps[0]
        assert design.approval_required is True
        assert design.approval_timeout == 86400
        assert design.approvers == ["tech-lead"]

        deploy = wf.steps[1]
        assert deploy.approval_required is True
        assert deploy.approvers == ["tech-lead", "devops-team"]


# ---------------------------------------------------------------------------
# WorkflowEngine approval gate tests
# ---------------------------------------------------------------------------

class TestWorkflowEngineApprovalGate:
    def test_complete_step_transitions_to_awaiting_approval(self):
        """When the next step has approval_required=True, state becomes AWAITING_APPROVAL."""
        step1 = _make_step(1)
        step1.status = StepStatus.RUNNING
        step2 = _make_step(2, approval_required=True, approvers=["tech-lead"])

        wf = Workflow(
            id="wf-approval",
            name="Test",
            version="1.0",
            steps=[step1, step2],
            state=WorkflowState.RUNNING,
            current_step=1,
        )

        storage = _make_storage(wf)
        engine = WorkflowEngine(storage=storage)

        result = asyncio.get_event_loop().run_until_complete(
            engine.complete_step("wf-approval", step_num=1, outputs={})
        )

        assert result.state == WorkflowState.AWAITING_APPROVAL
        assert result.current_step == 2
        assert wf.steps[1].status == StepStatus.PENDING

    def test_complete_step_no_approval_transitions_to_running(self):
        """When next step has no approval gate, execution continues normally."""
        step1 = _make_step(1)
        step1.status = StepStatus.RUNNING
        step2 = _make_step(2, approval_required=False)

        wf = Workflow(
            id="wf-no-approval",
            name="Test",
            version="1.0",
            steps=[step1, step2],
            state=WorkflowState.RUNNING,
            current_step=1,
        )

        storage = _make_storage(wf)
        engine = WorkflowEngine(storage=storage)

        result = asyncio.get_event_loop().run_until_complete(
            engine.complete_step("wf-no-approval", step_num=1, outputs={})
        )

        assert result.state == WorkflowState.RUNNING
        assert wf.steps[1].status == StepStatus.RUNNING

    def test_approve_step_resumes_workflow(self):
        """approve_step() transitions AWAITING_APPROVAL -> RUNNING."""
        step1 = _make_step(1, approval_required=True)
        wf = Workflow(
            id="wf-approve",
            name="Test",
            version="1.0",
            steps=[step1],
            state=WorkflowState.AWAITING_APPROVAL,
            current_step=1,
        )

        storage = _make_storage(wf)
        engine = WorkflowEngine(storage=storage)

        result = asyncio.get_event_loop().run_until_complete(
            engine.approve_step("wf-approve", approved_by="tech-lead")
        )

        assert result.state == WorkflowState.RUNNING
        assert wf.steps[0].status == StepStatus.RUNNING

    def test_approve_step_raises_if_not_awaiting(self):
        """approve_step() raises ValueError when workflow is not AWAITING_APPROVAL."""
        wf = _make_workflow()
        wf.state = WorkflowState.RUNNING
        storage = _make_storage(wf)
        engine = WorkflowEngine(storage=storage)

        with pytest.raises(ValueError, match="awaiting_approval"):
            asyncio.get_event_loop().run_until_complete(
                engine.approve_step("test-wf-1", approved_by="tech-lead")
            )

    def test_deny_step_fails_workflow(self):
        """deny_step() transitions AWAITING_APPROVAL -> FAILED."""
        step1 = _make_step(1, approval_required=True)
        wf = Workflow(
            id="wf-deny",
            name="Test",
            version="1.0",
            steps=[step1],
            state=WorkflowState.AWAITING_APPROVAL,
            current_step=1,
        )

        storage = _make_storage(wf)
        engine = WorkflowEngine(storage=storage)

        result = asyncio.get_event_loop().run_until_complete(
            engine.deny_step("wf-deny", denied_by="devops-team", reason="not ready")
        )

        assert result.state == WorkflowState.FAILED
        assert wf.steps[0].status == StepStatus.FAILED
        assert "devops-team" in (wf.steps[0].error or "")

    def test_deny_step_raises_if_not_awaiting(self):
        """deny_step() raises ValueError when workflow is not AWAITING_APPROVAL."""
        wf = _make_workflow()
        wf.state = WorkflowState.RUNNING
        storage = _make_storage(wf)
        engine = WorkflowEngine(storage=storage)

        with pytest.raises(ValueError, match="awaiting_approval"):
            asyncio.get_event_loop().run_until_complete(
                engine.deny_step("test-wf-1", denied_by="someone")
            )

    def test_audit_events_logged_on_awaiting_approval(self):
        """STEP_AWAITING_APPROVAL audit event is emitted."""
        step1 = _make_step(1)
        step1.status = StepStatus.RUNNING
        step2 = _make_step(2, approval_required=True)

        wf = Workflow(
            id="wf-audit",
            name="Test",
            version="1.0",
            steps=[step1, step2],
            state=WorkflowState.RUNNING,
            current_step=1,
        )

        storage = _make_storage(wf)
        engine = WorkflowEngine(storage=storage)

        asyncio.get_event_loop().run_until_complete(
            engine.complete_step("wf-audit", step_num=1, outputs={})
        )

        # Check that STEP_AWAITING_APPROVAL was audited
        audit_calls = [call.args[0] for call in storage.append_audit_event.call_args_list]
        event_types = [event.event_type for event in audit_calls]
        assert "STEP_AWAITING_APPROVAL" in event_types


# ---------------------------------------------------------------------------
# StateManager approval state persistence tests
# ---------------------------------------------------------------------------

class TestStateManagerApprovalState:
    def test_set_and_get_pending_approval(self, tmp_path, monkeypatch):
        import config
        monkeypatch.setattr(config, "APPROVAL_STATE_FILE", str(tmp_path / "approval_state.json"))
        monkeypatch.setattr(config, "DATA_DIR", str(tmp_path))

        from state_manager import StateManager

        StateManager.set_pending_approval(
            issue_num="42",
            step_num=3,
            step_name="deploy",
            approvers=["tech-lead"],
            approval_timeout=3600,
        )

        pending = StateManager.get_pending_approval("42")
        assert pending is not None
        assert pending["step_num"] == 3
        assert pending["step_name"] == "deploy"
        assert pending["approvers"] == ["tech-lead"]
        assert pending["approval_timeout"] == 3600

    def test_get_pending_approval_returns_none_when_absent(self, tmp_path, monkeypatch):
        import config
        monkeypatch.setattr(config, "APPROVAL_STATE_FILE", str(tmp_path / "approval_state.json"))
        monkeypatch.setattr(config, "DATA_DIR", str(tmp_path))

        from state_manager import StateManager

        assert StateManager.get_pending_approval("99") is None

    def test_clear_pending_approval(self, tmp_path, monkeypatch):
        import config
        monkeypatch.setattr(config, "APPROVAL_STATE_FILE", str(tmp_path / "approval_state.json"))
        monkeypatch.setattr(config, "DATA_DIR", str(tmp_path))

        from state_manager import StateManager

        StateManager.set_pending_approval(
            issue_num="55",
            step_num=1,
            step_name="review",
            approvers=[],
            approval_timeout=86400,
        )
        StateManager.clear_pending_approval("55")
        assert StateManager.get_pending_approval("55") is None

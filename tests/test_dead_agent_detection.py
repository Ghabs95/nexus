"""Tests for dead/stuck-agent detection after Phase-3 refactor.

The dead- and stuck-agent detection logic has moved to
``nexus.core.ProcessOrchestrator`` (nexus-core repo).  The full behavioural
test suite lives in nexus-core's ``tests/test_process_orchestrator.py``.

This file verifies the nexus-side integration:
  - ``NexusAgentRuntime`` correctly delegates to ``AgentMonitor``,
    ``StateManager``, and ``notifications``
  - ``check_stuck_agents()`` in inbox_processor delegates to the orchestrator
    and records polling failures on exception
"""

import time
import pytest
from unittest.mock import MagicMock, patch


# ---------------------------------------------------------------------------
# NexusAgentRuntime hook tests
# ---------------------------------------------------------------------------


class TestNexusAgentRuntimeShouldRetry:
    def test_delegates_to_agent_monitor(self):
        from nexus_agent_runtime import NexusAgentRuntime

        runtime = NexusAgentRuntime(finalize_fn=lambda *a, **kw: None)

        with patch("agent_monitor.AgentMonitor") as MockMonitor:
            MockMonitor.should_retry.return_value = True
            result = runtime.should_retry("42", "developer")

        MockMonitor.should_retry.assert_called_once_with("42", "developer")
        assert result is True

    def test_max_retries_returns_false(self):
        from nexus_agent_runtime import NexusAgentRuntime

        runtime = NexusAgentRuntime(finalize_fn=lambda *a, **kw: None)

        with patch("agent_monitor.AgentMonitor") as MockMonitor:
            MockMonitor.should_retry.return_value = False
            result = runtime.should_retry("42", "developer")

        assert result is False


class TestNexusAgentRuntimeGetWorkflowState:
    def test_returns_cancelled_string(self):
        from nexus_agent_runtime import NexusAgentRuntime

        runtime = NexusAgentRuntime(finalize_fn=lambda *a, **kw: None)

        with patch("state_manager.StateManager.get_workflow_id_for_issue", return_value="nexus-10-full"):
            with patch("builtins.open", create=True):
                with patch("json.load", return_value={"state": "cancelled"}):
                    result = runtime.get_workflow_state("10")

        assert result == "CANCELLED"

    def test_returns_paused_string(self):
        from nexus_agent_runtime import NexusAgentRuntime

        runtime = NexusAgentRuntime(finalize_fn=lambda *a, **kw: None)

        with patch("state_manager.StateManager.get_workflow_id_for_issue", return_value="nexus-11-full"):
            with patch("builtins.open", create=True):
                with patch("json.load", return_value={"state": "paused"}):
                    result = runtime.get_workflow_state("11")

        assert result == "PAUSED"

    def test_returns_none_for_active(self):
        from nexus_agent_runtime import NexusAgentRuntime

        runtime = NexusAgentRuntime(finalize_fn=lambda *a, **kw: None)

        with patch("state_manager.StateManager.get_workflow_id_for_issue", return_value="nexus-12-full"):
            with patch("builtins.open", create=True):
                with patch("json.load", return_value={"state": "active"}):
                    result = runtime.get_workflow_state("12")

        assert result is None

    def test_returns_none_for_missing_mapping(self):
        from nexus_agent_runtime import NexusAgentRuntime

        runtime = NexusAgentRuntime(finalize_fn=lambda *a, **kw: None)

        with patch("state_manager.StateManager.get_workflow_id_for_issue", return_value=None):
            result = runtime.get_workflow_state("10")

        assert result is None


class TestNexusAgentRuntimeAuditLog:
    def test_delegates_to_audit_store(self):
        from nexus_agent_runtime import NexusAgentRuntime

        runtime = NexusAgentRuntime(finalize_fn=lambda *a, **kw: None)

        with patch("audit_store.AuditStore") as MockAudit:
            runtime.audit_log("55", "AGENT_DEAD", "PID 1234 exited")

        MockAudit.audit_log.assert_called_once_with(55, "AGENT_DEAD", "PID 1234 exited")

    def test_empty_details_passes_none(self):
        from nexus_agent_runtime import NexusAgentRuntime

        runtime = NexusAgentRuntime(finalize_fn=lambda *a, **kw: None)

        with patch("audit_store.AuditStore") as MockAudit:
            runtime.audit_log("55", "AGENT_DEAD")

        MockAudit.audit_log.assert_called_once_with(55, "AGENT_DEAD", None)


class TestNexusAgentRuntimeSendAlert:
    def test_delegates_to_telegram(self):
        from nexus_agent_runtime import NexusAgentRuntime

        runtime = NexusAgentRuntime(finalize_fn=lambda *a, **kw: None)

        with patch("notifications.send_telegram_alert", return_value=True) as mock_tg:
            result = runtime.send_alert("hello")

        mock_tg.assert_called_once_with("hello")
        assert result is True

    def test_returns_false_on_failure(self):
        from nexus_agent_runtime import NexusAgentRuntime

        runtime = NexusAgentRuntime(finalize_fn=lambda *a, **kw: None)

        with patch("notifications.send_telegram_alert", return_value=False):
            result = runtime.send_alert("hello")

        assert result is False


# ---------------------------------------------------------------------------
# check_stuck_agents delegation smoke test
# ---------------------------------------------------------------------------


class TestCheckStuckAgentsDelegates:
    def test_delegates_to_process_orchestrator(self):
        """check_stuck_agents() must delegate to ProcessOrchestrator."""
        from inbox_processor import check_stuck_agents

        with patch("inbox_processor._get_process_orchestrator") as mock_factory:
            mock_orc = MagicMock()
            mock_factory.return_value = mock_orc
            check_stuck_agents()

        mock_orc.check_stuck_agents.assert_called_once()

    def test_records_polling_failure_on_exception(self):
        """An exception in check_stuck_agents must record a polling failure."""
        from inbox_processor import check_stuck_agents, polling_failure_counts

        with patch("inbox_processor._get_process_orchestrator") as mock_factory:
            mock_orc = MagicMock()
            mock_orc.check_stuck_agents.side_effect = RuntimeError("boom")
            mock_factory.return_value = mock_orc

            check_stuck_agents()

        assert polling_failure_counts.get("stuck-agents:loop", 0) >= 1



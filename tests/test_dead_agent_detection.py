"""Tests for dead-agent detection in check_stuck_agents / _check_dead_agents."""

import time
import pytest
from unittest.mock import patch, MagicMock


class TestIsPidAlive:
    """Tests for _is_pid_alive helper."""

    def test_pid_alive(self):
        from inbox_processor import _is_pid_alive

        with patch("inbox_processor.os.kill") as mock_kill:
            mock_kill.return_value = None  # no exception => alive
            assert _is_pid_alive(1234) is True
            mock_kill.assert_called_once_with(1234, 0)

    def test_pid_dead(self):
        from inbox_processor import _is_pid_alive

        with patch("inbox_processor.os.kill", side_effect=ProcessLookupError):
            assert _is_pid_alive(9999) is False

    def test_pid_permission_error(self):
        """PermissionError means process exists but we can't signal it."""
        from inbox_processor import _is_pid_alive

        with patch("inbox_processor.os.kill", side_effect=PermissionError):
            assert _is_pid_alive(1) is True


class TestCheckDeadAgents:
    """Tests for _check_dead_agents detecting crashed agent processes."""

    @patch("inbox_processor.save_launched_agents")
    @patch("inbox_processor.send_telegram_alert")
    @patch("inbox_processor._is_pid_alive", return_value=False)
    @patch("inbox_processor.load_launched_agents")
    @patch("inbox_processor.get_github_repo", return_value="test/repo")
    @patch("inbox_processor._resolve_project_for_issue", return_value="nexus")
    def test_dead_agent_sends_alert(
        self, mock_resolve, mock_repo, mock_load, mock_alive, mock_alert, mock_save
    ):
        """Dead PID past grace period triggers a Telegram alert."""
        from inbox_processor import _check_dead_agents, _dead_agent_alerted

        _dead_agent_alerted.clear()
        AgentMonitor_retry = MagicMock()

        mock_load.return_value = {
            "41": {
                "timestamp": time.time() - 300,  # 5 min ago
                "pid": 99999,
                "tier": "shortened",
                "agent_type": "developer",
            }
        }

        with patch("inbox_processor.AgentMonitor") as MockMonitor:
            MockMonitor.should_retry.return_value = True
            _check_dead_agents()

        mock_alert.assert_called_once()
        alert_text = mock_alert.call_args[0][0]
        assert "Agent Crashed" in alert_text
        assert "#41" in alert_text
        assert "developer" in alert_text
        mock_save.assert_called_once()

    @patch("inbox_processor.save_launched_agents")
    @patch("inbox_processor.send_telegram_alert")
    @patch("inbox_processor._is_pid_alive", return_value=True)
    @patch("inbox_processor.load_launched_agents")
    def test_alive_agent_no_alert(self, mock_load, mock_alive, mock_alert, mock_save):
        """Still-running agents should not trigger dead-agent alerts."""
        from inbox_processor import _check_dead_agents, _dead_agent_alerted

        _dead_agent_alerted.clear()

        mock_load.return_value = {
            "41": {
                "timestamp": time.time() - 300,
                "pid": 99999,
                "tier": "shortened",
                "agent_type": "developer",
            }
        }

        _check_dead_agents()
        mock_alert.assert_not_called()
        mock_save.assert_not_called()

    @patch("inbox_processor.save_launched_agents")
    @patch("inbox_processor.send_telegram_alert")
    @patch("inbox_processor._is_pid_alive", return_value=False)
    @patch("inbox_processor.load_launched_agents")
    def test_grace_period_respected(self, mock_load, mock_alive, mock_alert, mock_save):
        """Agents within grace period should not trigger alerts."""
        from inbox_processor import _check_dead_agents, _dead_agent_alerted

        _dead_agent_alerted.clear()

        mock_load.return_value = {
            "41": {
                "timestamp": time.time() - 10,  # Only 10 seconds ago
                "pid": 99999,
                "tier": "shortened",
                "agent_type": "developer",
            }
        }

        _check_dead_agents()
        mock_alert.assert_not_called()
        mock_save.assert_not_called()

    @patch("inbox_processor.save_launched_agents")
    @patch("inbox_processor.send_telegram_alert")
    @patch("inbox_processor._is_pid_alive", return_value=False)
    @patch("inbox_processor.load_launched_agents")
    @patch("inbox_processor.get_github_repo", return_value="test/repo")
    @patch("inbox_processor._resolve_project_for_issue", return_value="nexus")
    def test_no_duplicate_alerts(
        self, mock_resolve, mock_repo, mock_load, mock_alive, mock_alert, mock_save
    ):
        """Same dead agent should only be alerted once."""
        from inbox_processor import _check_dead_agents, _dead_agent_alerted

        _dead_agent_alerted.clear()

        agent_data = {
            "41": {
                "timestamp": time.time() - 300,
                "pid": 99999,
                "tier": "shortened",
                "agent_type": "developer",
            }
        }

        with patch("inbox_processor.AgentMonitor") as MockMonitor:
            MockMonitor.should_retry.return_value = False
            MockMonitor.mark_failed = MagicMock()

            # First call — should alert
            mock_load.return_value = dict(agent_data)
            _check_dead_agents()
            assert mock_alert.call_count == 1

            # Second call — same pid, should NOT alert again
            mock_load.return_value = dict(agent_data)
            mock_save.reset_mock()
            _check_dead_agents()
            assert mock_alert.call_count == 1  # still 1

    @patch("inbox_processor.save_launched_agents")
    @patch("inbox_processor.send_telegram_alert")
    @patch("inbox_processor._is_pid_alive", return_value=False)
    @patch("inbox_processor.load_launched_agents")
    @patch("inbox_processor.get_github_repo", return_value="test/repo")
    @patch("inbox_processor._resolve_project_for_issue", return_value="nexus")
    def test_max_retries_shows_manual_intervention(
        self, mock_resolve, mock_repo, mock_load, mock_alive, mock_alert, mock_save
    ):
        """When retries exhausted, alert should say manual intervention needed."""
        from inbox_processor import _check_dead_agents, _dead_agent_alerted

        _dead_agent_alerted.clear()

        mock_load.return_value = {
            "42": {
                "timestamp": time.time() - 300,
                "pid": 88888,
                "tier": "full",
                "agent_type": "triage",
            }
        }

        with patch("inbox_processor.AgentMonitor") as MockMonitor:
            MockMonitor.should_retry.return_value = False
            MockMonitor.mark_failed = MagicMock()

            _check_dead_agents()

        alert_text = mock_alert.call_args[0][0]
        assert "Manual Intervention" in alert_text
        assert "/reprocess" in alert_text

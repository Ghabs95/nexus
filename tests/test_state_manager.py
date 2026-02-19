"""Tests for state_manager module."""
import pytest
import time
from unittest.mock import patch, MagicMock
import sys
sys.path.insert(0, '/home/ubuntu/git/ghabs/nexus/src')
from state_manager import StateManager
from models import WorkflowState


class TestTrackedIssues:
    """Tests for tracked issues persistence."""
    
    def test_load_tracked_issues_empty_file(self):
        """Test loading tracked issues when store is empty."""
        plugin = MagicMock()
        plugin.load_json.return_value = {}
        with patch('state_manager._get_state_store_plugin', return_value=plugin):
            result = StateManager.load_tracked_issues()
            assert result == {}
    
    def test_load_tracked_issues_valid_data(self):
        """Test loading valid tracked issues."""
        test_data = {"123": {"project": "test", "status": "active"}}
        plugin = MagicMock()
        plugin.load_json.return_value = test_data
        with patch('state_manager._get_state_store_plugin', return_value=plugin):
            result = StateManager.load_tracked_issues()
            assert result == test_data
    
    def test_load_tracked_issues_plugin_missing(self):
        """Test loading when plugin is unavailable."""
        with patch('state_manager._get_state_store_plugin', return_value=None):
            result = StateManager.load_tracked_issues()
            assert result == {}
    
    def test_save_tracked_issues(self):
        """Test saving tracked issues."""
        test_data = {"111": {"project": "test", "status": "active"}}
        plugin = MagicMock()
        with patch('state_manager._get_state_store_plugin', return_value=plugin):
            StateManager.save_tracked_issues(test_data)
            plugin.save_json.assert_called_once()
            assert plugin.save_json.call_args.args[1] == test_data
    
    def test_add_tracked_issue(self):
        """Test adding a tracked issue."""
        with patch.object(StateManager, 'load_tracked_issues', return_value={}):
            with patch.object(StateManager, 'save_tracked_issues') as mock_save:
                StateManager.add_tracked_issue(123, "test-project", "Test description")
                
                mock_save.assert_called_once()
                saved_data = mock_save.call_args[0][0]
                assert "123" in saved_data
                assert saved_data["123"]["project"] == "test-project"
                assert saved_data["123"]["description"] == "Test description"
                assert saved_data["123"]["status"] == "active"
    
    def test_remove_tracked_issue(self):
        """Test removing a tracked issue."""
        existing_data = {"123": {"project": "test", "status": "active"}}
        with patch.object(StateManager, 'load_tracked_issues', return_value=existing_data):
            with patch.object(StateManager, 'save_tracked_issues') as mock_save:
                StateManager.remove_tracked_issue(123)
                
                mock_save.assert_called_once()
                saved_data = mock_save.call_args[0][0]
                assert "123" not in saved_data


class TestWorkflowState:
    """Tests for workflow state persistence."""
    
    def test_load_workflow_state_empty(self):
        """Test loading workflow state when store is empty."""
        plugin = MagicMock()
        plugin.load_json.return_value = {}
        with patch('state_manager._get_state_store_plugin', return_value=plugin):
            result = StateManager.load_workflow_state()
            assert result == {}
    
    def test_load_workflow_state_valid_data(self):
        """Test loading valid workflow state."""
        test_data = {
            "123": {
                "state": "PAUSED",
                "timestamp": 1234567890.0
            }
        }
        plugin = MagicMock()
        plugin.load_json.return_value = test_data
        with patch('state_manager._get_state_store_plugin', return_value=plugin):
            result = StateManager.load_workflow_state()
            assert result == test_data
    
    def test_save_workflow_state(self):
        """Test saving workflow state."""
        test_data = {
            "456": {
                "state": "STOPPED",
                "timestamp": 1234567890.0
            }
        }
        plugin = MagicMock()
        with patch('state_manager._get_state_store_plugin', return_value=plugin):
            StateManager.save_workflow_state(test_data)
            plugin.save_json.assert_called_once()
            assert plugin.save_json.call_args.args[1] == test_data
    
    def test_set_workflow_state_paused(self):
        """Test setting workflow to PAUSED."""
        with patch.object(StateManager, 'load_workflow_state', return_value={}):
            with patch.object(StateManager, 'save_workflow_state') as mock_save:
                StateManager.set_workflow_state("123", WorkflowState.PAUSED)
                
                mock_save.assert_called_once()
                saved_state = mock_save.call_args[0][0]
                assert "123" in saved_state
                assert saved_state["123"]["state"] == "paused"
    
    def test_set_workflow_state_active(self):
        """Test setting workflow to ACTIVE removes entry."""
        existing = {"123": {"state": "PAUSED", "timestamp": 123.0}}
        with patch.object(StateManager, 'load_workflow_state', return_value=existing):
            with patch.object(StateManager, 'save_workflow_state') as mock_save:
                StateManager.set_workflow_state("123", WorkflowState.ACTIVE)
                
                mock_save.assert_called_once()
                saved_state = mock_save.call_args[0][0]
                assert "123" not in saved_state
    
    def test_get_workflow_state_paused(self):
        """Test getting PAUSED workflow state."""
        test_data = {"123": {"state": "PAUSED", "timestamp": 123.0}}
        with patch.object(StateManager, 'load_workflow_state', return_value=test_data):
            state = StateManager.get_workflow_state("123")
            assert state == WorkflowState.PAUSED
    
    def test_get_workflow_state_active_default(self):
        """Test getting workflow state defaults to ACTIVE."""
        with patch.object(StateManager, 'load_workflow_state', return_value={}):
            state = StateManager.get_workflow_state("999")
            assert state == WorkflowState.ACTIVE


class TestAuditLog:
    """Tests for audit logging."""
    
    def test_audit_log_creates_entry(self):
        """Test that audit log appends an entry through the state store plugin."""
        plugin = MagicMock()
        plugin.append_line.return_value = True
        with patch('state_manager._get_state_store_plugin', return_value=plugin):
            with patch('state_manager.datetime') as mock_dt:
                mock_dt.now.return_value.isoformat.return_value = "2024-01-01T12:00:00"
                
                StateManager.audit_log(123, "test_event", "test details")

                written = plugin.append_line.call_args.args[1]
                
                assert "Issue #123" in written
                assert "test_event" in written
                assert "test details" in written
                assert "2024-01-01T12:00:00" in written
    
    def test_audit_log_no_details(self):
        """Test audit log with no additional details."""
        plugin = MagicMock()
        plugin.append_line.return_value = True
        with patch('state_manager._get_state_store_plugin', return_value=plugin):
            with patch('state_manager.datetime') as mock_dt:
                mock_dt.now.return_value.isoformat.return_value = "2024-01-01T12:00:00"
                
                StateManager.audit_log(456, "simple_event")

                written = plugin.append_line.call_args.args[1]
                
                assert "Issue #456" in written
                assert "simple_event" in written


class TestLaunchedAgents:
    """Tests for launched agents tracking."""
    
    def test_load_launched_agents_empty(self):
        """Test loading launched agents when store is empty."""
        plugin = MagicMock()
        plugin.load_json.return_value = {}
        with patch('state_manager._get_state_store_plugin', return_value=plugin):
            result = StateManager.load_launched_agents()
            assert result == {}
    
    def test_load_launched_agents_filters_old(self):
        """Test that old entries are filtered during load."""
        old_time = time.time() - 300  # 5 minutes ago
        recent_time = time.time() - 60  # 1 minute ago
        test_data = {
            "123_OldAgent": {"timestamp": old_time, "issue": "123"},
            "456_RecentAgent": {"timestamp": recent_time, "issue": "456"}
        }
        plugin = MagicMock()
        plugin.load_json.return_value = test_data
        with patch('state_manager._get_state_store_plugin', return_value=plugin):
            result = StateManager.load_launched_agents()

            # Old entry should be filtered out (>2 minute window)
            assert "123_OldAgent" not in result
            assert "456_RecentAgent" in result
    
    def test_register_launched_agent(self):
        """Test registering a newly launched agent."""
        with patch.object(StateManager, 'load_launched_agents', return_value={}):
            with patch.object(StateManager, 'save_launched_agents') as mock_save:
                StateManager.register_launched_agent("123", "TestAgent", 12345)
                
                mock_save.assert_called_once()
                saved_data = mock_save.call_args[0][0]
                assert "123_TestAgent" in saved_data
                assert saved_data["123_TestAgent"]["pid"] == 12345
    
    def test_was_recently_launched(self):
        """Test checking if agent was recently launched."""
        test_data = {"123_TestAgent": {"issue": "123", "timestamp": time.time()}}
        with patch.object(StateManager, 'load_launched_agents', return_value=test_data):
            result = StateManager.was_recently_launched("123", "TestAgent")
            assert result is True
    
    def test_was_not_recently_launched(self):
        """Test checking agent that was not recently launched."""
        with patch.object(StateManager, 'load_launched_agents', return_value={}):
            result = StateManager.was_recently_launched("999", "NonExistentAgent")
            assert result is False

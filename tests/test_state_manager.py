"""Tests for state_manager module."""
import pytest
import time
from unittest.mock import patch, MagicMock
import sys
sys.path.insert(0, '/home/ubuntu/git/ghabs/nexus/src')
from state_manager import StateManager, set_socketio_emitter
import state_manager as sm_module


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


class TestEmitTransition:
    """Tests for SocketIO transition broadcasting."""

    def setup_method(self):
        """Reset the module-level emitter before each test."""
        set_socketio_emitter(None)

    def teardown_method(self):
        """Reset the module-level emitter after each test."""
        set_socketio_emitter(None)

    def test_emit_noop_when_no_emitter(self):
        """emit_transition is a no-op when no emitter is registered."""
        # Should not raise
        StateManager.emit_transition("agent_registered", {"issue": "1", "agent": "TestAgent"})

    def test_emit_calls_registered_emitter(self):
        """emit_transition forwards events to the registered emitter."""
        mock_emitter = MagicMock()
        set_socketio_emitter(mock_emitter)
        StateManager.emit_transition("agent_registered", {"issue": "42", "agent": "Bot"})
        mock_emitter.assert_called_once_with("agent_registered", {"issue": "42", "agent": "Bot"})

    def test_emit_swallows_emitter_exceptions(self):
        """emit_transition swallows exceptions raised by the emitter."""
        failing_emitter = MagicMock(side_effect=RuntimeError("socket error"))
        set_socketio_emitter(failing_emitter)
        # Should not raise
        StateManager.emit_transition("workflow_mapped", {"issue": "1", "workflow_id": "wf-1"})

    def test_register_launched_agent_emits(self):
        """register_launched_agent calls emit_transition with agent_registered."""
        mock_emitter = MagicMock()
        set_socketio_emitter(mock_emitter)
        with patch.object(StateManager, 'load_launched_agents', return_value={}):
            with patch.object(StateManager, 'save_launched_agents'):
                StateManager.register_launched_agent("10", "TestAgent", 9999)
        assert mock_emitter.call_count == 1
        event_name, event_data = mock_emitter.call_args[0]
        assert event_name == "agent_registered"
        assert event_data["issue"] == "10"
        assert event_data["agent"] == "TestAgent"

    def test_map_issue_to_workflow_emits(self):
        """map_issue_to_workflow calls emit_transition with workflow_mapped."""
        mock_emitter = MagicMock()
        set_socketio_emitter(mock_emitter)
        with patch.object(StateManager, 'load_workflow_mapping', return_value={}):
            with patch.object(StateManager, 'save_workflow_mapping'):
                StateManager.map_issue_to_workflow("20", "wf-abc")
        assert mock_emitter.call_count == 1
        event_name, event_data = mock_emitter.call_args[0]
        assert event_name == "workflow_mapped"
        assert event_data["issue"] == "20"
        assert event_data["workflow_id"] == "wf-abc"

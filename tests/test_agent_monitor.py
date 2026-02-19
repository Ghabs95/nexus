"""Unit tests for agent_monitor module."""

import pytest
from unittest.mock import patch, MagicMock
from agent_monitor import AgentMonitor, WorkflowRouter


class TestAgentMonitor:
    """Tests for AgentMonitor class."""
    
    def test_should_retry_within_limit(self):
        """Test should_retry returns True when under retry limit."""
        # Reset retry counters
        AgentMonitor.retry_counters.clear()
        
        result = AgentMonitor.should_retry("42", "@ProjectLead")
        assert result is True
        assert AgentMonitor.retry_counters["42_@ProjectLead"] == 1
    
    def test_should_retry_at_limit(self):
        """Test should_retry returns False when at retry limit."""
        # Reset and set to max retries
        AgentMonitor.retry_counters.clear()
        AgentMonitor.retry_counters["42_@Copilot"] = AgentMonitor.MAX_RETRIES
        
        result = AgentMonitor.should_retry("42", "@Copilot")
        assert result is False
    
    def test_should_retry_increments_counter(self):
        """Test should_retry increments retry counter."""
        AgentMonitor.retry_counters.clear()
        
        AgentMonitor.should_retry("42", "@Architect")
        assert AgentMonitor.retry_counters["42_@Architect"] == 1
        
        AgentMonitor.should_retry("42", "@Architect")
        assert AgentMonitor.retry_counters["42_@Architect"] == 2
    
    def test_reset_retries(self):
        """Test reset_retries clears counter for issue+agent."""
        AgentMonitor.retry_counters.clear()
        AgentMonitor.retry_counters["42_@ProjectLead"] = 2
        
        AgentMonitor.reset_retries("42", "@ProjectLead")
        assert "42_@ProjectLead" not in AgentMonitor.retry_counters
    
    def test_mark_failed(self):
        """Test mark_failed removes retry counter."""
        AgentMonitor.retry_counters.clear()
        AgentMonitor.retry_counters["42_@Copilot"] = 1
        
        AgentMonitor.mark_failed("42", "@Copilot", "Test failure")
        assert "42_@Copilot" not in AgentMonitor.retry_counters
    
    @patch('agent_monitor.get_runtime_ops_plugin')
    def test_kill_agent_success(self, mock_get_runtime_ops):
        """Test kill_agent successfully terminates via runtime-ops plugin."""
        runtime_ops = MagicMock()
        runtime_ops.kill_process.return_value = True
        mock_get_runtime_ops.return_value = runtime_ops
        
        result = AgentMonitor.kill_agent(12345, "42")
        assert result is True
        runtime_ops.kill_process.assert_called_once_with(12345, force=True)
    
    @patch('agent_monitor.get_runtime_ops_plugin')
    def test_kill_agent_failure(self, mock_get_runtime_ops):
        """Test kill_agent handles plugin kill failures gracefully."""
        runtime_ops = MagicMock()
        runtime_ops.kill_process.return_value = False
        mock_get_runtime_ops.return_value = runtime_ops
        
        result = AgentMonitor.kill_agent(12345, "42")
        assert result is False
    
    @patch('agent_monitor.get_runtime_ops_plugin')
    @patch('os.path.getmtime')
    @patch('time.time')
    def test_check_timeout_detected(self, mock_time, mock_getmtime, mock_get_runtime_ops):
        """Test check_timeout detects timeout when plugin reports active PID."""
        # Mock current time and file modification time (16 minutes ago)
        mock_time.return_value = 1000.0
        mock_getmtime.return_value = 1000.0 - (16 * 60)  # 16 minutes ago
        
        runtime_ops = MagicMock()
        runtime_ops.find_agent_pid_for_issue.return_value = 12345
        mock_get_runtime_ops.return_value = runtime_ops
        
        timed_out, pid = AgentMonitor.check_timeout("42", "/tmp/test.log")
        
        assert timed_out is True
        assert pid == 12345
    
    @patch('agent_monitor.get_runtime_ops_plugin')
    @patch('os.path.getmtime')
    @patch('time.time')
    def test_check_timeout_not_detected(self, mock_time, mock_getmtime, mock_get_runtime_ops):
        """Test check_timeout when no timeout has occurred."""
        # Mock current time and recent file modification (1 minute ago)
        mock_time.return_value = 1000.0
        mock_getmtime.return_value = 1000.0 - 60  # 1 minute ago

        runtime_ops = MagicMock()
        runtime_ops.find_agent_pid_for_issue.return_value = None
        mock_get_runtime_ops.return_value = runtime_ops
        
        timed_out, pid = AgentMonitor.check_timeout("42", "/tmp/test.log")
        
        assert timed_out is False
        assert pid is None


class TestWorkflowRouter:
    """Tests for WorkflowRouter class."""
    
    def test_detect_workflow_tier_explicit_full(self):
        """Test explicit workflow:full label."""
        labels = ["workflow:full", "feature"]
        tier = WorkflowRouter.detect_workflow_tier(labels)
        assert tier == "full"
    
    def test_detect_workflow_tier_explicit_shortened(self):
        """Test explicit workflow:shortened label."""
        labels = ["workflow:shortened", "bug"]
        tier = WorkflowRouter.detect_workflow_tier(labels)
        assert tier == "shortened"
    
    def test_detect_workflow_tier_explicit_fast_track(self):
        """Test explicit workflow:fast-track label."""
        labels = ["workflow:fast-track", "critical"]
        tier = WorkflowRouter.detect_workflow_tier(labels)
        assert tier == "fast-track"
    
    def test_detect_workflow_tier_auto_critical(self):
        """Test auto-detection for critical priority."""
        labels = ["priority:critical"]
        tier = WorkflowRouter.detect_workflow_tier(labels)
        assert tier == "fast-track"
    
    def test_detect_workflow_tier_auto_bug(self):
        """Test auto-detection for bug label."""
        labels = ["bug", "backend"]
        tier = WorkflowRouter.detect_workflow_tier(labels)
        assert tier == "shortened"
    
    def test_detect_workflow_tier_auto_feature(self):
        """Test auto-detection for feature label."""
        labels = ["feature", "enhancement"]
        tier = WorkflowRouter.detect_workflow_tier(labels)
        assert tier == "full"
    
    def test_detect_workflow_tier_default(self):
        """Test default tier when no matching labels."""
        labels = ["documentation", "question"]
        tier = WorkflowRouter.detect_workflow_tier(labels)
        assert tier == "full"
    
    def test_suggest_tier_label_critical(self):
        """Test suggesting tier label for critical issue."""
        suggestion = WorkflowRouter.suggest_tier_label(
            "URGENT: Production down",
            "Critical hotfix needed ASAP"
        )
        assert suggestion == "workflow:fast-track"
    
    def test_suggest_tier_label_bug(self):
        """Test suggesting tier label for bug."""
        suggestion = WorkflowRouter.suggest_tier_label(
            "Fix broken login",
            "There's a bug in the authentication system"
        )
        assert suggestion == "workflow:shortened"
    
    def test_suggest_tier_label_feature(self):
        """Test suggesting tier label for feature."""
        suggestion = WorkflowRouter.suggest_tier_label(
            "Add new dashboard",
            "We need a new feature for analytics"
        )
        assert suggestion == "workflow:full"
    
    def test_suggest_tier_label_no_match(self):
        """Test suggesting tier label when no keywords match."""
        suggestion = WorkflowRouter.suggest_tier_label(
            "Question about docs",
            "Just wondering about something"
        )
        assert suggestion is None

"""Unit tests for analytics module.

Tests audit log parsing and metrics calculation.
"""

import pytest
from datetime import datetime, timedelta
from analytics import (
    AuditLogParser,
    WorkflowMetrics,
    AgentMetrics,
    SystemMetrics,
    get_stats_report
)


def get_recent_date(hours_ago=0):
    """Helper to generate recent timestamps for testing."""
    return (datetime.now() - timedelta(hours=hours_ago)).strftime("%Y-%m-%dT%H:%M:%S")


class TestAuditLogParser:
    """Tests for AuditLogParser class."""
    
    def test_parse_log_line_valid(self):
        """Test parsing a valid audit log line."""
        parser = AuditLogParser("dummy.log")
        line = "2026-02-16T10:30:00 | Issue #5 | AGENT_LAUNCHED | Launched Copilot agent"
        
        result = parser.parse_log_line(line)
        assert result is not None
        timestamp, issue_num, event_type, details = result
        
        assert isinstance(timestamp, datetime)
        assert issue_num == 5
        assert event_type == "AGENT_LAUNCHED"
        assert "Launched Copilot agent" in details
    
    def test_parse_log_line_invalid(self):
        """Test parsing an invalid log line returns None."""
        parser = AuditLogParser("dummy.log")
        line = "Invalid log line without proper format"
        
        result = parser.parse_log_line(line)
        assert result is None
    
    def test_parse_workflow_started_event(self, tmp_path):
        """Test parsing WORKFLOW_STARTED event."""
        log_file = tmp_path / "audit.log"
        log_file.write_text(
            f"{get_recent_date(1)} | Issue #10 | WORKFLOW_STARTED | Started workflow tier: full\n"
        )
        
        parser = AuditLogParser(str(log_file))
        parser.parse_log_file(lookback_days=1)
        
        assert 10 in parser.workflow_metrics
        wm = parser.workflow_metrics[10]
        assert wm.start_time is not None
        assert wm.workflow_tier == "full"
    
    def test_parse_agent_launched_event(self, tmp_path):
        """Test parsing AGENT_LAUNCHED event."""
        log_file = tmp_path / "audit.log"
        log_file.write_text(
            f"{get_recent_date(1)} | Issue #10 | AGENT_LAUNCHED | @Copilot agent launched\n"
        )
        
        parser = AuditLogParser(str(log_file))
        parser.parse_log_file(lookback_days=1)
        
        assert 10 in parser.workflow_metrics
        assert parser.workflow_metrics[10].agents_launched == 1
        assert "Copilot" in parser.agent_metrics
        assert parser.agent_metrics["Copilot"].launches == 1
    
    def test_parse_multiple_events(self, tmp_path):
        """Test parsing multiple events for same issue."""
        log_file = tmp_path / "audit.log"
        log_content = f"""{get_recent_date(10)} | Issue #10 | WORKFLOW_STARTED | Started full tier workflow
{get_recent_date(9)} | Issue #10 | AGENT_LAUNCHED | Launched @ProjectLead agent
{get_recent_date(5)} | Issue #10 | AGENT_TIMEOUT_KILL | @ProjectLead timed out
{get_recent_date(4)} | Issue #10 | AGENT_RETRY | Retrying @ProjectLead
{get_recent_date(1)} | Issue #10 | WORKFLOW_COMPLETED | Workflow finished
"""
        log_file.write_text(log_content)
        
        parser = AuditLogParser(str(log_file))
        parser.parse_log_file(lookback_days=1)
        
        wm = parser.workflow_metrics[10]
        assert wm.agents_launched == 1
        assert wm.timeouts == 1
        assert wm.retries == 1
        assert wm.completed is True
        assert wm.duration_seconds is not None


class TestSystemMetrics:
    """Tests for system-wide metrics calculation."""
    
    def test_completion_rate_calculation(self, tmp_path):
        """Test completion rate calculation."""
        log_file = tmp_path / "audit.log"
        log_content = f"""{get_recent_date(10)} | Issue #1 | WORKFLOW_STARTED | Started
{get_recent_date(9)} | Issue #1 | WORKFLOW_COMPLETED | Finished
{get_recent_date(8)} | Issue #2 | WORKFLOW_STARTED | Started
{get_recent_date(7)} | Issue #2 | WORKFLOW_COMPLETED | Finished
{get_recent_date(6)} | Issue #3 | WORKFLOW_STARTED | Started
"""
        log_file.write_text(log_content)
        
        parser = AuditLogParser(str(log_file))
        parser.parse_log_file(lookback_days=1)
        metrics = parser.get_system_metrics()
        
        assert metrics.total_issues == 3
        assert metrics.completed_issues == 2
        assert metrics.completion_rate == pytest.approx(66.67, rel=0.1)
    
    def test_tier_distribution(self, tmp_path):
        """Test issues per tier counting."""
        log_file = tmp_path / "audit.log"
        log_content = f"""{get_recent_date(10)} | Issue #1 | WORKFLOW_STARTED | tier: full
{get_recent_date(8)} | Issue #2 | WORKFLOW_STARTED | tier: shortened
{get_recent_date(6)} | Issue #3 | WORKFLOW_STARTED | tier: full
"""
        log_file.write_text(log_content)
        
        parser = AuditLogParser(str(log_file))
        parser.parse_log_file(lookback_days=1)
        metrics = parser.get_system_metrics()
        
        assert metrics.issues_per_tier["full"] == 2
        assert metrics.issues_per_tier["shortened"] == 1


class TestAgentLeaderboard:
    """Tests for agent performance ranking."""
    
    def test_agent_ranking_by_activity(self, tmp_path):
        """Test agents are ranked by launch count."""
        log_file = tmp_path / "audit.log"
        log_content = f"""{get_recent_date(10)} | Issue #1 | AGENT_LAUNCHED | @Copilot
{get_recent_date(9)} | Issue #2 | AGENT_LAUNCHED | @Copilot
{get_recent_date(8)} | Issue #3 | AGENT_LAUNCHED | @Copilot
{get_recent_date(7)} | Issue #4 | AGENT_LAUNCHED | @ProjectLead
"""
        log_file.write_text(log_content)
        
        parser = AuditLogParser(str(log_file))
        parser.parse_log_file(lookback_days=1)
        leaderboard = parser.get_agent_leaderboard(top_n=10)
        
        assert len(leaderboard) == 2
        assert leaderboard[0].agent_name == "Copilot"
        assert leaderboard[0].launches == 3
        assert leaderboard[1].agent_name == "ProjectLead"
        assert leaderboard[1].launches == 1


class TestStatsReport:
    """Tests for formatted stats report generation."""
    
    def test_stats_report_format(self, tmp_path):
        """Test that stats report is properly formatted."""
        log_file = tmp_path / "audit.log"
        log_content = f"""{get_recent_date(10)} | Issue #1 | WORKFLOW_STARTED | tier: full
{get_recent_date(9)} | Issue #1 | AGENT_LAUNCHED | @ProjectLead
{get_recent_date(1)} | Issue #1 | WORKFLOW_COMPLETED | Finished
"""
        log_file.write_text(log_content)
        
        report = get_stats_report(str(log_file), lookback_days=1)
        
        assert "📊 **Nexus System Analytics**" in report
        assert "Total Issues: 1" in report
        assert "Completed: 1" in report
        assert "@ProjectLead" in report
        assert "Completion Rate" in report
    
    def test_empty_log_report(self, tmp_path):
        """Test report with no events."""
        log_file = tmp_path / "audit.log"
        log_file.write_text("")
        
        report = get_stats_report(str(log_file), lookback_days=1)
        
        assert "📊 **Nexus System Analytics**" in report
        assert "Total Issues: 0" in report

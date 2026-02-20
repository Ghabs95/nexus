"""Unit tests for analytics module.

Tests JSONL audit-event parsing and metrics calculation.
"""

import pytest
from datetime import datetime, timedelta
from unittest.mock import patch

from analytics import AuditLogParser, get_stats_report


def iso_hours_ago(hours_ago: int = 0) -> str:
    """Helper to generate ISO timestamps for testing."""
    return (datetime.now() - timedelta(hours=hours_ago)).isoformat()


class TestAuditLogParser:
    """Tests for AuditLogParser class."""

    def test_extract_issue_num_from_data(self):
        parser = AuditLogParser()
        evt = {
            "workflow_id": "nexus-99-full",
            "timestamp": iso_hours_ago(1),
            "event_type": "AGENT_LAUNCHED",
            "data": {"issue_number": 5, "details": "Launched Copilot"},
        }

        assert parser._extract_issue_num(evt) == 5

    def test_extract_issue_num_from_workflow_id(self):
        parser = AuditLogParser()
        evt = {
            "workflow_id": "nexus-42-shortened",
            "timestamp": iso_hours_ago(1),
            "event_type": "WORKFLOW_STARTED",
            "data": {},
        }

        assert parser._extract_issue_num(evt) == 42

    def test_parse_workflow_started_event(self):
        events = [
            {
                "workflow_id": "nexus-10-full",
                "timestamp": iso_hours_ago(1),
                "event_type": "WORKFLOW_STARTED",
                "data": {"issue_number": 10, "details": "Started workflow tier: full"},
            }
        ]

        parser = AuditLogParser()
        with patch("audit_store.AuditStore.read_all_audit_events", return_value=events):
            parser.parse_events(lookback_days=1)

        assert 10 in parser.workflow_metrics
        wm = parser.workflow_metrics[10]
        assert wm.start_time is not None
        assert wm.workflow_tier == "full"

    def test_parse_agent_launched_event(self):
        events = [
            {
                "workflow_id": "nexus-10-full",
                "timestamp": iso_hours_ago(1),
                "event_type": "AGENT_LAUNCHED",
                "data": {"issue_number": 10, "details": "@Copilot agent launched"},
            }
        ]

        parser = AuditLogParser()
        with patch("audit_store.AuditStore.read_all_audit_events", return_value=events):
            parser.parse_events(lookback_days=1)

        assert 10 in parser.workflow_metrics
        assert parser.workflow_metrics[10].agents_launched == 1
        assert "Copilot" in parser.agent_metrics
        assert parser.agent_metrics["Copilot"].launches == 1

    def test_parse_multiple_events(self):
        events = [
            {
                "workflow_id": "nexus-10-full",
                "timestamp": iso_hours_ago(10),
                "event_type": "WORKFLOW_STARTED",
                "data": {"issue_number": 10, "details": "Started full tier workflow"},
            },
            {
                "workflow_id": "nexus-10-full",
                "timestamp": iso_hours_ago(9),
                "event_type": "AGENT_LAUNCHED",
                "data": {"issue_number": 10, "details": "Launched @ProjectLead agent"},
            },
            {
                "workflow_id": "nexus-10-full",
                "timestamp": iso_hours_ago(5),
                "event_type": "AGENT_TIMEOUT_KILL",
                "data": {"issue_number": 10, "details": "@ProjectLead timed out"},
            },
            {
                "workflow_id": "nexus-10-full",
                "timestamp": iso_hours_ago(4),
                "event_type": "AGENT_RETRY",
                "data": {"issue_number": 10, "details": "Retrying @ProjectLead"},
            },
            {
                "workflow_id": "nexus-10-full",
                "timestamp": iso_hours_ago(1),
                "event_type": "WORKFLOW_COMPLETED",
                "data": {"issue_number": 10, "details": "Workflow finished"},
            },
        ]

        parser = AuditLogParser()
        with patch("audit_store.AuditStore.read_all_audit_events", return_value=events):
            parser.parse_events(lookback_days=1)

        wm = parser.workflow_metrics[10]
        assert wm.agents_launched == 1
        assert wm.timeouts == 1
        assert wm.retries == 1
        assert wm.completed is True
        assert wm.duration_seconds is not None


class TestSystemMetrics:
    """Tests for system-wide metrics calculation."""

    def test_completion_rate_calculation(self):
        events = [
            {
                "workflow_id": "nexus-1-full",
                "timestamp": iso_hours_ago(10),
                "event_type": "WORKFLOW_STARTED",
                "data": {"issue_number": 1, "details": "Started"},
            },
            {
                "workflow_id": "nexus-1-full",
                "timestamp": iso_hours_ago(9),
                "event_type": "WORKFLOW_COMPLETED",
                "data": {"issue_number": 1, "details": "Finished"},
            },
            {
                "workflow_id": "nexus-2-shortened",
                "timestamp": iso_hours_ago(8),
                "event_type": "WORKFLOW_STARTED",
                "data": {"issue_number": 2, "details": "Started"},
            },
            {
                "workflow_id": "nexus-2-shortened",
                "timestamp": iso_hours_ago(7),
                "event_type": "WORKFLOW_COMPLETED",
                "data": {"issue_number": 2, "details": "Finished"},
            },
            {
                "workflow_id": "nexus-3-full",
                "timestamp": iso_hours_ago(6),
                "event_type": "WORKFLOW_STARTED",
                "data": {"issue_number": 3, "details": "Started"},
            },
        ]

        parser = AuditLogParser()
        with patch("audit_store.AuditStore.read_all_audit_events", return_value=events):
            parser.parse_events(lookback_days=1)
        metrics = parser.get_system_metrics()

        assert metrics.total_issues == 3
        assert metrics.completed_issues == 2
        assert metrics.completion_rate == pytest.approx(66.67, rel=0.1)

    def test_tier_distribution(self):
        events = [
            {
                "workflow_id": "nexus-1-full",
                "timestamp": iso_hours_ago(10),
                "event_type": "WORKFLOW_STARTED",
                "data": {"issue_number": 1, "details": "tier: full"},
            },
            {
                "workflow_id": "nexus-2-shortened",
                "timestamp": iso_hours_ago(8),
                "event_type": "WORKFLOW_STARTED",
                "data": {"issue_number": 2, "details": "tier: shortened"},
            },
            {
                "workflow_id": "nexus-3-full",
                "timestamp": iso_hours_ago(6),
                "event_type": "WORKFLOW_STARTED",
                "data": {"issue_number": 3, "details": "tier: full"},
            },
        ]

        parser = AuditLogParser()
        with patch("audit_store.AuditStore.read_all_audit_events", return_value=events):
            parser.parse_events(lookback_days=1)
        metrics = parser.get_system_metrics()

        assert metrics.issues_per_tier["full"] == 2
        assert metrics.issues_per_tier["shortened"] == 1


class TestAgentLeaderboard:
    """Tests for agent performance ranking."""

    def test_agent_ranking_by_activity(self):
        events = [
            {
                "workflow_id": "nexus-1-full",
                "timestamp": iso_hours_ago(10),
                "event_type": "AGENT_LAUNCHED",
                "data": {"issue_number": 1, "details": "@Copilot"},
            },
            {
                "workflow_id": "nexus-2-full",
                "timestamp": iso_hours_ago(9),
                "event_type": "AGENT_LAUNCHED",
                "data": {"issue_number": 2, "details": "@Copilot"},
            },
            {
                "workflow_id": "nexus-3-full",
                "timestamp": iso_hours_ago(8),
                "event_type": "AGENT_LAUNCHED",
                "data": {"issue_number": 3, "details": "@Copilot"},
            },
            {
                "workflow_id": "nexus-4-full",
                "timestamp": iso_hours_ago(7),
                "event_type": "AGENT_LAUNCHED",
                "data": {"issue_number": 4, "details": "@ProjectLead"},
            },
        ]

        parser = AuditLogParser()
        with patch("audit_store.AuditStore.read_all_audit_events", return_value=events):
            parser.parse_events(lookback_days=1)
        leaderboard = parser.get_agent_leaderboard(top_n=10)

        assert len(leaderboard) == 2
        assert leaderboard[0].agent_name == "Copilot"
        assert leaderboard[0].launches == 3
        assert leaderboard[1].agent_name == "ProjectLead"
        assert leaderboard[1].launches == 1


class TestStatsReport:
    """Tests for formatted stats report generation."""

    def test_stats_report_format(self):
        events = [
            {
                "workflow_id": "nexus-1-full",
                "timestamp": iso_hours_ago(10),
                "event_type": "WORKFLOW_STARTED",
                "data": {"issue_number": 1, "details": "tier: full"},
            },
            {
                "workflow_id": "nexus-1-full",
                "timestamp": iso_hours_ago(9),
                "event_type": "AGENT_LAUNCHED",
                "data": {"issue_number": 1, "details": "@ProjectLead"},
            },
            {
                "workflow_id": "nexus-1-full",
                "timestamp": iso_hours_ago(1),
                "event_type": "WORKFLOW_COMPLETED",
                "data": {"issue_number": 1, "details": "Finished"},
            },
        ]

        with patch("audit_store.AuditStore.read_all_audit_events", return_value=events):
            report = get_stats_report(lookback_days=1)

        assert "📊 **Nexus System Analytics**" in report
        assert "Total Issues: 1" in report
        assert "Completed: 1" in report
        assert "@ProjectLead" in report
        assert "Completion Rate" in report

    def test_empty_log_report(self):
        with patch("audit_store.AuditStore.read_all_audit_events", return_value=[]):
            report = get_stats_report(lookback_days=1)

        assert "📊 **Nexus System Analytics**" in report
        assert "Total Issues: 0" in report

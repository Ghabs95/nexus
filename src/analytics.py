"""Analytics module for parsing audit logs and generating workflow statistics.

Provides insights into:
- Issue completion rates
- Agent performance metrics
- Timeout and retry frequencies
- Workflow duration analysis
"""

import os
import re
import logging
from datetime import datetime, timedelta
from typing import Dict, List, Tuple, Optional
from collections import defaultdict, Counter
from dataclasses import dataclass

logger = logging.getLogger(__name__)


@dataclass
class WorkflowMetrics:
    """Metrics for a single workflow execution."""
    issue_num: int
    start_time: Optional[datetime] = None
    end_time: Optional[datetime] = None
    duration_seconds: Optional[float] = None
    agents_launched: int = 0
    timeouts: int = 0
    retries: int = 0
    failures: int = 0
    workflow_tier: Optional[str] = None
    completed: bool = False


@dataclass
class AgentMetrics:
    """Performance metrics for a specific agent."""
    agent_name: str
    launches: int = 0
    timeouts: int = 0
    retries: int = 0
    failures: int = 0
    successes: int = 0
    avg_duration_seconds: Optional[float] = None


@dataclass
class SystemMetrics:
    """Overall system performance metrics."""
    total_issues: int = 0
    completed_issues: int = 0
    active_issues: int = 0
    failed_issues: int = 0
    total_timeouts: int = 0
    total_retries: int = 0
    completion_rate: float = 0.0
    avg_workflow_duration_hours: Optional[float] = None
    issues_per_tier: Dict[str, int] = None
    
    def __post_init__(self):
        if self.issues_per_tier is None:
            self.issues_per_tier = {}


class AuditLogParser:
    """Parser for JSONL audit events from nexus-core storage."""
    
    def __init__(self):
        """Initialize parser."""
        self.workflow_metrics: Dict[int, WorkflowMetrics] = {}
        self.agent_metrics: Dict[str, AgentMetrics] = defaultdict(
            lambda: AgentMetrics(agent_name="")
        )

    @staticmethod
    def _extract_issue_num(evt: dict) -> Optional[int]:
        """Extract issue number from a JSONL audit event."""
        data = evt.get("data", {})
        if isinstance(data, dict) and "issue_number" in data:
            try:
                return int(data["issue_number"])
            except (ValueError, TypeError):
                pass
        # Fallback: parse from workflow_id (format: project-N-tier)
        wf_id = evt.get("workflow_id", "")
        match = re.search(r"-(\d+)-", wf_id)
        if match:
            return int(match.group(1))
        return None

    def parse_events(self, lookback_days: int = 30) -> None:
        """Load and parse all JSONL audit events from nexus-core storage.
        
        Args:
            lookback_days: Only consider events from the last N days (default: 30)
        """
        from state_manager import StateManager

        events = StateManager.read_all_audit_events(
            since_hours=lookback_days * 24,
        )
        
        for evt in events:
            try:
                timestamp = datetime.fromisoformat(evt.get("timestamp", ""))
            except (ValueError, TypeError):
                continue

            issue_num = self._extract_issue_num(evt)
            if issue_num is None:
                continue

            event_type = evt.get("event_type", "")
            data = evt.get("data", {}) or {}
            details = data.get("details", "") if isinstance(data, str) is False else ""

            # Initialize workflow metrics if needed
            if issue_num not in self.workflow_metrics:
                self.workflow_metrics[issue_num] = WorkflowMetrics(issue_num=issue_num)

            wm = self.workflow_metrics[issue_num]

            if event_type in ("WORKFLOW_STARTED", "WORKFLOW_CREATED"):
                if wm.start_time is None:
                    wm.start_time = timestamp
                tier_match = re.search(r"tier[:\s]+(\w+)", str(details), re.IGNORECASE)
                if tier_match:
                    wm.workflow_tier = tier_match.group(1)
                # Also try to extract tier from workflow_id
                if not wm.workflow_tier:
                    wf_id = evt.get("workflow_id", "")
                    for tier in ("full", "shortened", "fast-track"):
                        if wf_id.endswith(f"-{tier}"):
                            wm.workflow_tier = tier
                            break

            elif event_type == "AGENT_LAUNCHED":
                wm.agents_launched += 1
                agent_match = re.search(r"@?(\w+)", str(details))
                if agent_match:
                    agent_name = agent_match.group(1)
                    self.agent_metrics[agent_name].agent_name = agent_name
                    self.agent_metrics[agent_name].launches += 1

            elif event_type == "AGENT_TIMEOUT_KILL":
                wm.timeouts += 1
                agent_match = re.search(r"@?(\w+)", str(details))
                if agent_match:
                    self.agent_metrics[agent_match.group(1)].timeouts += 1

            elif event_type == "AGENT_RETRY":
                wm.retries += 1
                agent_match = re.search(r"@?(\w+)", str(details))
                if agent_match:
                    self.agent_metrics[agent_match.group(1)].retries += 1

            elif event_type == "AGENT_FAILED":
                wm.failures += 1
                agent_match = re.search(r"@?(\w+)", str(details))
                if agent_match:
                    self.agent_metrics[agent_match.group(1)].failures += 1

            elif event_type == "WORKFLOW_COMPLETED":
                wm.completed = True
                wm.end_time = timestamp
                if wm.start_time:
                    wm.duration_seconds = (wm.end_time - wm.start_time).total_seconds()
    
    def get_system_metrics(self) -> SystemMetrics:
        """Calculate overall system metrics from parsed data.
        
        Returns:
            SystemMetrics object with aggregated statistics
        """
        metrics = SystemMetrics()
        
        metrics.total_issues = len(self.workflow_metrics)
        metrics.completed_issues = sum(1 for wm in self.workflow_metrics.values() if wm.completed)
        metrics.failed_issues = sum(1 for wm in self.workflow_metrics.values() if wm.failures > 0 and not wm.completed)
        metrics.active_issues = metrics.total_issues - metrics.completed_issues - metrics.failed_issues
        
        if metrics.total_issues > 0:
            metrics.completion_rate = (metrics.completed_issues / metrics.total_issues) * 100
        
        metrics.total_timeouts = sum(wm.timeouts for wm in self.workflow_metrics.values())
        metrics.total_retries = sum(wm.retries for wm in self.workflow_metrics.values())
        
        # Calculate average workflow duration (only for completed workflows)
        completed_durations = [
            wm.duration_seconds 
            for wm in self.workflow_metrics.values() 
            if wm.completed and wm.duration_seconds is not None
        ]
        if completed_durations:
            avg_seconds = sum(completed_durations) / len(completed_durations)
            metrics.avg_workflow_duration_hours = avg_seconds / 3600
        
        # Count issues per tier
        tier_counter = Counter()
        for wm in self.workflow_metrics.values():
            if wm.workflow_tier:
                tier_counter[wm.workflow_tier] += 1
        metrics.issues_per_tier = dict(tier_counter)
        
        return metrics
    
    def get_agent_leaderboard(self, top_n: int = 10) -> List[AgentMetrics]:
        """Get top performing agents ranked by success rate.
        
        Args:
            top_n: Number of top agents to return (default: 10)
        
        Returns:
            List of AgentMetrics sorted by performance
        """
        # Calculate success rate for each agent
        agent_list = []
        for agent_name, metrics in self.agent_metrics.items():
            if metrics.launches > 0:
                # Success = launches - (timeouts + failures)
                metrics.successes = max(0, metrics.launches - metrics.timeouts - metrics.failures)
                agent_list.append(metrics)
        
        # Sort by launches (most active first)
        agent_list.sort(key=lambda a: a.launches, reverse=True)
        
        return agent_list[:top_n]
    
    def format_stats_report(self) -> str:
        """Generate a formatted text report of all statistics.
        
        Returns:
            Markdown-formatted statistics report suitable for Telegram
        """
        system_metrics = self.get_system_metrics()
        agent_leaderboard = self.get_agent_leaderboard(top_n=5)
        
        report = "📊 **Nexus System Analytics**\n"
        report += "=" * 40 + "\n\n"
        
        # Overall System Stats
        report += "**📈 Overall Performance:**\n"
        report += f"• Total Issues: {system_metrics.total_issues}\n"
        report += f"• ✅ Completed: {system_metrics.completed_issues}\n"
        report += f"• 🔄 Active: {system_metrics.active_issues}\n"
        report += f"• ❌ Failed: {system_metrics.failed_issues}\n"
        report += f"• Completion Rate: {system_metrics.completion_rate:.1f}%\n"
        
        if system_metrics.avg_workflow_duration_hours:
            report += f"• Avg Workflow Time: {system_metrics.avg_workflow_duration_hours:.1f}h\n"
        
        report += "\n"
        
        # Reliability Stats
        report += "**⚙️ Reliability:**\n"
        report += f"• Total Timeouts: {system_metrics.total_timeouts}\n"
        report += f"• Total Retries: {system_metrics.total_retries}\n"
        
        if system_metrics.total_issues > 0:
            timeout_rate = (system_metrics.total_timeouts / system_metrics.total_issues)
            report += f"• Avg Timeouts per Issue: {timeout_rate:.1f}\n"
        
        report += "\n"
        
        # Workflow Tiers
        if system_metrics.issues_per_tier:
            report += "**🎯 Issues by Tier:**\n"
            for tier, count in sorted(system_metrics.issues_per_tier.items()):
                emoji = {"full": "🟡", "shortened": "🟠", "fast-track": "🟢"}.get(tier, "⚪")
                report += f"• {emoji} {tier}: {count}\n"
            report += "\n"
        
        # Top Agents
        if agent_leaderboard:
            report += "**🤖 Top 5 Most Active Agents:**\n"
            for idx, agent in enumerate(agent_leaderboard, 1):
                success_rate = (agent.successes / agent.launches * 100) if agent.launches > 0 else 0
                report += f"{idx}. **@{agent.agent_name}**\n"
                report += f"   ├ Launches: {agent.launches}\n"
                report += f"   ├ Successes: {agent.successes} ({success_rate:.0f}%)\n"
                if agent.timeouts > 0:
                    report += f"   ├ Timeouts: {agent.timeouts}\n"
                if agent.retries > 0:
                    report += f"   ├ Retries: {agent.retries}\n"
                if agent.failures > 0:
                    report += f"   └ Failures: {agent.failures}\n"
                else:
                    report += f"   └ Failures: 0\n"
            report += "\n"
        
        report += "=" * 40 + "\n"
        report += f"_Data from last 30 days_"
        
        return report


def get_stats_report(lookback_days: int = 30) -> str:
    """Generate a statistics report from JSONL audit events.
    
    Args:
        lookback_days: Number of days to include in analysis
    
    Returns:
        Formatted statistics report
    """
    parser = AuditLogParser()
    parser.parse_events(lookback_days=lookback_days)
    return parser.format_stats_report()

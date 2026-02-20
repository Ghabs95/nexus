"""Alerting system for Nexus - monitors for errors and stuck workflows.

Sends Telegram alerts for critical issues:
- High error rates
- Stuck workflows (>1 hour without progress)
- Repeated agent failures
- System degradation
"""
import logging
import os
from datetime import datetime, timedelta
from pathlib import Path
from typing import Dict, List, Optional
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.interval import IntervalTrigger
from telegram import Bot
from audit_store import AuditStore
from state_manager import StateManager
from config import LOGS_DIR

logger = logging.getLogger(__name__)


class AlertingSystem:
    """Monitors system health and sends alerts for critical issues."""
    
    def __init__(self, bot: Bot, chat_id: int):
        """
        Initialize alerting system.
        
        Args:
            bot: Telegram Bot instance
            chat_id: Chat ID to send alerts to
        """
        self.bot = bot
        self.chat_id = chat_id
        self.scheduler = AsyncIOScheduler()
        self.state_manager = StateManager()
        
        # Thresholds (configurable via environment)
        self.error_rate_threshold = int(os.getenv('ALERT_ERROR_RATE_THRESHOLD', '10'))  # errors per hour
        self.stuck_workflow_hours = int(os.getenv('ALERT_STUCK_WORKFLOW_HOURS', '2'))  # hours without progress
        self.agent_failure_threshold = int(os.getenv('ALERT_AGENT_FAILURE_THRESHOLD', '3'))  # failures in 1 hour
        
        # Alert cooldown (prevent spam)
        self.alert_cooldown_minutes = int(os.getenv('ALERT_COOLDOWN_MINUTES', '30'))
        self.last_alerts: Dict[str, datetime] = {}
    
    def start(self):
        """Start the alerting scheduler."""
        # Check every 15 minutes
        check_interval_minutes = int(os.getenv('ALERT_CHECK_INTERVAL_MINUTES', '15'))
        
        self.scheduler.add_job(
            self.check_for_alerts,
            trigger=IntervalTrigger(minutes=check_interval_minutes),
            id='alert_check',
            name='Alert System Check',
            replace_existing=True
        )
        
        self.scheduler.start()
        logger.info(f"Alerting system started. Checking every {check_interval_minutes} minutes")
    
    def stop(self):
        """Stop the alerting scheduler."""
        if self.scheduler.running:
            self.scheduler.shutdown()
            logger.info("Alerting system stopped")
    
    async def check_for_alerts(self):
        """Main alert checking loop - runs periodically."""
        try:
            logger.debug("Running alert checks...")
            
            # Check for high error rates
            await self._check_error_rates()
            
            # Check for stuck workflows
            await self._check_stuck_workflows()
            
            # Check for repeated agent failures
            await self._check_agent_failures()
            
        except Exception as e:
            logger.error(f"Error in alert check: {e}")
    
    async def _check_error_rates(self):
        """Check for high error rates in logs."""
        try:
            error_count = self._count_recent_errors(hours=1)
            
            if error_count >= self.error_rate_threshold:
                alert_key = "high_error_rate"
                if self._should_send_alert(alert_key):
                    await self._send_alert(
                        title="🚨 High Error Rate Detected",
                        message=f"Detected {error_count} errors in the last hour.\n"
                                f"Threshold: {self.error_rate_threshold} errors/hour\n\n"
                                f"Check /logs and /audit for details.",
                        alert_key=alert_key
                    )
        
        except Exception as e:
            logger.error(f"Error checking error rates: {e}")
    
    async def _check_stuck_workflows(self):
        """Check for workflows stuck without progress."""
        try:
            stuck_workflows = self._find_stuck_workflows()
            
            if stuck_workflows:
                alert_key = "stuck_workflows"
                if self._should_send_alert(alert_key):
                    message = f"Found {len(stuck_workflows)} stuck workflow(s):\n\n"
                    
                    for workflow in stuck_workflows[:5]:  # Show max 5
                        issue = workflow['issue_number']
                        status = workflow['status']
                        hours_stuck = workflow['hours_stuck']
                        message += f"• Issue #{issue} - {status} ({hours_stuck:.1f}h)\n"
                    
                    if len(stuck_workflows) > 5:
                        message += f"\n... and {len(stuck_workflows) - 5} more"
                    
                    message += "\n\nUse /continue <issue#> to check status\n"
                    message += "Use /kill <issue#> to stop stuck agents"
                    
                    await self._send_alert(
                        title="⏰ Stuck Workflows Detected",
                        message=message,
                        alert_key=alert_key
                    )
        
        except Exception as e:
            logger.error(f"Error checking stuck workflows: {e}")
    
    async def _check_agent_failures(self):
        """Check for repeated agent failures."""
        try:
            failures = self._count_agent_failures(hours=1)
            
            if failures >= self.agent_failure_threshold:
                alert_key = "agent_failures"
                if self._should_send_alert(alert_key):
                    await self._send_alert(
                        title="⚠️ Repeated Agent Failures",
                        message=f"Detected {failures} agent failures in the last hour.\n"
                                f"Threshold: {self.agent_failure_threshold} failures/hour\n\n"
                                f"Check /audit and /logs for details.",
                        alert_key=alert_key
                    )
        
        except Exception as e:
            logger.error(f"Error checking agent failures: {e}")
    
    def _count_recent_errors(self, hours: int) -> int:
        """
        Count errors in audit log within time window.
        
        Args:
            hours: Number of hours to look back
        
        Returns:
            Number of errors found
        """
        try:
            error_events = {'AGENT_FAILED', 'AGENT_TIMEOUT_KILL', 'ERROR', 'WORKFLOW_ERROR'}
            events = AuditStore.read_all_audit_events(since_hours=hours)
            return sum(1 for e in events if e.get("event_type") in error_events)
        except Exception as e:
            logger.error(f"Error counting recent errors: {e}")
            return 0
    
    def _find_stuck_workflows(self) -> List[Dict]:
        """
        Find workflows that haven't made progress recently.
        
        Returns:
            List of stuck workflow info dicts
        """
        try:
            stuck = []
            cutoff_time = datetime.now() - timedelta(hours=self.stuck_workflow_hours)
            
            # Get all tracked issues
            tracked_issues = self.state_manager.load_tracked_issues()
            
            for issue_key, issue_data in tracked_issues.items():
                status = issue_data.get('status', 'unknown')
                
                # Skip completed/stopped workflows
                if status in ['implemented', 'rejected', 'stopped']:
                    continue
                
                # Check last update time
                last_update_str = issue_data.get('updated_at')
                if last_update_str:
                    try:
                        last_update = datetime.fromisoformat(last_update_str)
                        
                        if last_update < cutoff_time:
                            hours_stuck = (datetime.now() - last_update).total_seconds() / 3600
                            
                            stuck.append({
                                'issue_number': issue_key,
                                'status': status,
                                'last_update': last_update_str,
                                'hours_stuck': hours_stuck
                            })
                    except:
                        continue
            
            return stuck
        
        except Exception as e:
            logger.error(f"Error finding stuck workflows: {e}")
            return []
    
    def _count_agent_failures(self, hours: int) -> int:
        """
        Count agent failures in time window.
        
        Args:
            hours: Number of hours to look back
        
        Returns:
            Number of agent failures
        """
        try:
            failure_events = {'AGENT_FAILED', 'AGENT_TIMEOUT_KILL'}
            events = AuditStore.read_all_audit_events(since_hours=hours)
            return sum(1 for e in events if e.get("event_type") in failure_events)
        except Exception as e:
            logger.error(f"Error counting agent failures: {e}")
            return 0
    
    def _should_send_alert(self, alert_key: str) -> bool:
        """
        Check if enough time has passed since last alert of this type.
        
        Args:
            alert_key: Unique key for this alert type
        
        Returns:
            True if alert should be sent
        """
        if alert_key not in self.last_alerts:
            return True
        
        last_alert_time = self.last_alerts[alert_key]
        cooldown_duration = timedelta(minutes=self.alert_cooldown_minutes)
        
        return datetime.now() - last_alert_time >= cooldown_duration
    
    async def _send_alert(self, title: str, message: str, alert_key: str):
        """
        Send alert to Telegram.
        
        Args:
            title: Alert title
            message: Alert message body
            alert_key: Unique key for cooldown tracking
        """
        try:
            full_message = f"<b>{title}</b>\n\n{message}\n\n<i>{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}</i>"
            
            await self.bot.send_message(
                chat_id=self.chat_id,
                text=full_message,
                parse_mode='HTML'
            )
            
            # Update last alert time
            self.last_alerts[alert_key] = datetime.now()
            
            logger.info(f"Alert sent: {alert_key}")
        
        except Exception as e:
            logger.error(f"Error sending alert: {e}")
    
    async def send_custom_alert(self, message: str, title: str = "🔔 Alert"):
        """
        Send a custom alert (can be called from other modules).
        
        Args:
            message: Alert message
            title: Alert title
        """
        try:
            full_message = f"<b>{title}</b>\n\n{message}\n\n<i>{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}</i>"
            
            await self.bot.send_message(
                chat_id=self.chat_id,
                text=full_message,
                parse_mode='HTML'
            )
            
            logger.info(f"Custom alert sent: {title}")
        
        except Exception as e:
            logger.error(f"Error sending custom alert: {e}")


# Global singleton
_alerting_system: Optional[AlertingSystem] = None


def get_alerting_system() -> Optional[AlertingSystem]:
    """Get the global AlertingSystem instance."""
    return _alerting_system


def init_alerting_system(bot: Bot, chat_id: int) -> AlertingSystem:
    """
    Initialize the global alerting system.
    
    Args:
        bot: Telegram Bot instance
        chat_id: Chat ID to send alerts to
    
    Returns:
        AlertingSystem instance
    """
    global _alerting_system
    _alerting_system = AlertingSystem(bot, chat_id)
    return _alerting_system

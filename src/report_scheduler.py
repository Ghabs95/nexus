"""Scheduled reports and daily digests for Nexus.

Sends automated reports to Telegram at configured times.
"""
import logging
import os
from datetime import datetime, timedelta
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from telegram import Bot
from state_manager import StateManager
from user_manager import get_user_manager
from config import DATA_DIR, AUDIT_LOG_FILE

logger = logging.getLogger(__name__)


class ReportScheduler:
    """Manages scheduled reports."""
    
    def __init__(self, bot: Bot, chat_id: int):
        """
        Initialize report scheduler.
        
        Args:
            bot: Telegram Bot instance
            chat_id: Chat ID to send reports to
        """
        self.bot = bot
        self.chat_id = chat_id
        self.scheduler = AsyncIOScheduler()
        self.state_manager = StateManager()
        self.user_manager = get_user_manager()
    
    def start(self):
        """Start the scheduler."""
        # Daily digest at 9:00 AM
        daily_digest_hour = int(os.getenv('DAILY_DIGEST_HOUR', '9'))
        daily_digest_minute = int(os.getenv('DAILY_DIGEST_MINUTE', '0'))
        
        self.scheduler.add_job(
            self.send_daily_digest,
            trigger=CronTrigger(
                hour=daily_digest_hour,
                minute=daily_digest_minute
            ),
            id='daily_digest',
            name='Daily Digest Report',
            replace_existing=True
        )
        
        # Weekly summary on Monday at 9:00 AM
        weekly_summary_enabled = os.getenv('WEEKLY_SUMMARY_ENABLED', 'false').lower() == 'true'
        if weekly_summary_enabled:
            self.scheduler.add_job(
                self.send_weekly_summary,
                trigger=CronTrigger(
                    day_of_week='mon',
                    hour=9,
                    minute=0
                ),
                id='weekly_summary',
                name='Weekly Summary Report',
                replace_existing=True
            )
        
        self.scheduler.start()
        logger.info(f"Report scheduler started. Daily digest at {daily_digest_hour:02d}:{daily_digest_minute:02d}")
    
    def stop(self):
        """Stop the scheduler."""
        if self.scheduler.running:
            self.scheduler.shutdown()
            logger.info("Report scheduler stopped")
    
    async def send_daily_digest(self):
        """Send daily digest report."""
        try:
            logger.info("Generating daily digest...")
            
            # Get activity from last 24 hours
            activity = self._get_recent_activity(hours=24)
            
            # Get tracked issues status
            tracked_status = self._get_tracked_issues_status()
            
            # Get user stats
            user_stats = self.user_manager.get_all_users_stats()
            
            # Build message
            message = self._build_daily_digest_message(
                activity=activity,
                tracked_status=tracked_status,
                user_stats=user_stats
            )
            
            # Send to Telegram
            await self.bot.send_message(
                chat_id=self.chat_id,
                text=message,
                parse_mode='HTML'
            )
            
            logger.info("Daily digest sent successfully")
        
        except Exception as e:
            logger.error(f"Error sending daily digest: {e}")
    
    async def send_weekly_summary(self):
        """Send weekly summary report."""
        try:
            logger.info("Generating weekly summary...")
            
            # Get activity from last 7 days
            activity = self._get_recent_activity(hours=24*7)
            
            # Get tracked issues status
            tracked_status = self._get_tracked_issues_status()
            
            # Get user stats
            user_stats = self.user_manager.get_all_users_stats()
            
            # Build message
            message = self._build_weekly_summary_message(
                activity=activity,
                tracked_status=tracked_status,
                user_stats=user_stats
            )
            
            # Send to Telegram
            await self.bot.send_message(
                chat_id=self.chat_id,
                text=message,
                parse_mode='HTML'
            )
            
            logger.info("Weekly summary sent successfully")
        
        except Exception as e:
            logger.error(f"Error sending weekly summary: {e}")
    
    def _get_recent_activity(self, hours: int) -> dict:
        """
        Get recent activity from audit log.
        
        Args:
            hours: Number of hours to look back
        
        Returns:
            Dict with activity statistics
        """
        try:
            if not os.path.exists(AUDIT_LOG_FILE):
                return {"error": "Audit log not found"}
            
            cutoff_time = datetime.now() - timedelta(hours=hours)
            event_counts = {}
            total_events = 0
            
            with open(AUDIT_LOG_FILE, 'r') as f:
                for line in f:
                    try:
                        # Parse timestamp
                        timestamp_str = line.split('|')[0].strip()
                        timestamp = datetime.fromisoformat(timestamp_str)
                        
                        if timestamp >= cutoff_time:
                            # Count event types
                            parts = line.split('|')
                            if len(parts) >= 3:
                                event_type = parts[2].strip()
                                event_counts[event_type] = event_counts.get(event_type, 0) + 1
                                total_events += 1
                    except:
                        continue
            
            return {
                "total_events": total_events,
                "event_types": event_counts,
                "time_window_hours": hours
            }
        
        except Exception as e:
            logger.error(f"Error reading audit log: {e}")
            return {"error": str(e)}
    
    def _get_tracked_issues_status(self) -> dict:
        """
        Get status of all tracked issues.
        
        Returns:
            Dict with tracked issues statistics
        """
        try:
            tracked_issues = self.state_manager.get_all_tracked_issues()
            
            total_issues = len(tracked_issues)
            status_counts = {}
            
            for issue_key, issue_data in tracked_issues.items():
                status = issue_data.get('status', 'unknown')
                status_counts[status] = status_counts.get(status, 0) + 1
            
            return {
                "total_issues": total_issues,
                "status_counts": status_counts
            }
        
        except Exception as e:
            logger.error(f"Error getting tracked issues status: {e}")
            return {"error": str(e)}
    
    def _build_daily_digest_message(
        self,
        activity: dict,
        tracked_status: dict,
        user_stats: dict
    ) -> str:
        """Build daily digest message."""
        now = datetime.now()
        
        message = f"📊 <b>Daily Digest</b>\n"
        message += f"📅 {now.strftime('%A, %B %d, %Y')}\n\n"
        
        # Activity section
        message += f"<b>📈 Activity (Last 24 Hours)</b>\n"
        if "error" in activity:
            message += f"⚠️ {activity['error']}\n"
        else:
            total = activity.get('total_events', 0)
            message += f"Total Events: {total}\n"
            
            if total > 0:
                event_types = activity.get('event_types', {})
                for event_type, count in sorted(event_types.items(), key=lambda x: x[1], reverse=True)[:5]:
                    message += f"  • {event_type}: {count}\n"
        
        message += "\n"
        
        # Tracked issues section
        message += f"<b>🎯 Tracked Issues</b>\n"
        if "error" in tracked_status:
            message += f"⚠️ {tracked_status['error']}\n"
        else:
            total_issues = tracked_status.get('total_issues', 0)
            message += f"Total: {total_issues}\n"
            
            status_counts = tracked_status.get('status_counts', {})
            for status, count in sorted(status_counts.items()):
                emoji = self._get_status_emoji(status)
                message += f"  {emoji} {status}: {count}\n"
        
        message += "\n"
        
        # User section
        message += f"<b>👥 Users</b>\n"
        total_users = user_stats.get('total_users', 0)
        total_tracked = user_stats.get('total_tracked_issues', 0)
        message += f"Active Users: {total_users}\n"
        message += f"User-Tracked Issues: {total_tracked}\n"
        
        return message
    
    def _build_weekly_summary_message(
        self,
        activity: dict,
        tracked_status: dict,
        user_stats: dict
    ) -> str:
        """Build weekly summary message."""
        now = datetime.now()
        week_start = now - timedelta(days=7)
        
        message = f"📊 <b>Weekly Summary</b>\n"
        message += f"📅 {week_start.strftime('%b %d')} - {now.strftime('%b %d, %Y')}\n\n"
        
        # Activity section
        message += f"<b>📈 Activity (Last 7 Days)</b>\n"
        if "error" in activity:
            message += f"⚠️ {activity['error']}\n"
        else:
            total = activity.get('total_events', 0)
            message += f"Total Events: {total}\n"
            
            if total > 0:
                event_types = activity.get('event_types', {})
                message += f"\nTop Events:\n"
                for event_type, count in sorted(event_types.items(), key=lambda x: x[1], reverse=True)[:10]:
                    message += f"  • {event_type}: {count}\n"
        
        message += "\n"
        
        # Tracked issues section
        message += f"<b>🎯 Tracked Issues</b>\n"
        if "error" in tracked_status:
            message += f"⚠️ {tracked_status['error']}\n"
        else:
            total_issues = tracked_status.get('total_issues', 0)
            message += f"Total: {total_issues}\n"
            
            status_counts = tracked_status.get('status_counts', {})
            for status, count in sorted(status_counts.items()):
                emoji = self._get_status_emoji(status)
                message += f"  {emoji} {status}: {count}\n"
        
        message += "\n"
        
        # User section
        message += f"<b>👥 User Engagement</b>\n"
        total_users = user_stats.get('total_users', 0)
        total_tracked = user_stats.get('total_tracked_issues', 0)
        total_projects = user_stats.get('total_projects', 0)
        message += f"Active Users: {total_users}\n"
        message += f"Projects: {total_projects}\n"
        message += f"User-Tracked Issues: {total_tracked}\n"
        
        return message
    
    def _get_status_emoji(self, status: str) -> str:
        """Get emoji for status."""
        emoji_map = {
            'pending': '⏳',
            'processing': '🔄',
            'approved': '✅',
            'rejected': '❌',
            'implemented': '🎉',
            'error': '⚠️',
            'paused': '⏸️'
        }
        return emoji_map.get(status.lower(), '📌')

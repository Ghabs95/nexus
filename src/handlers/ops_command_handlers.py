"""Operational command handlers extracted from telegram_bot."""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Dict, List, Optional, Tuple

from telegram import Update
from telegram.ext import ContextTypes


@dataclass
class OpsHandlerDeps:
    logger: Any
    allowed_user_id: Optional[int]
    base_dir: str
    project_config: Dict[str, Dict[str, Any]]
    prompt_project_selection: Callable[[Update, ContextTypes.DEFAULT_TYPE, str], Awaitable[None]]
    ensure_project_issue: Callable[
        [Update, ContextTypes.DEFAULT_TYPE, str], Awaitable[Tuple[Optional[str], Optional[str], List[str]]]
    ]
    get_project_label: Callable[[str], str]
    get_stats_report: Callable[[int], str]
    format_error_for_user: Callable[[Exception, str], str]
    get_audit_history: Callable[[str, int], List[Dict[str, Any]]]
    get_agents_for_project: Callable[[str], Dict[str, str]]
    get_github_repo: Callable[[str], str]
    get_direct_issue_plugin: Callable[[str], Any]


async def audit_handler(update: Update, context: ContextTypes.DEFAULT_TYPE, deps: OpsHandlerDeps) -> None:
    deps.logger.info(f"Audit trail requested by user: {update.effective_user.id}")
    if deps.allowed_user_id and update.effective_user.id != deps.allowed_user_id:
        deps.logger.warning(f"Unauthorized access attempt by ID: {update.effective_user.id}")
        return

    if not context.args:
        await deps.prompt_project_selection(update, context, "audit")
        return

    project_key, issue_num, _ = await deps.ensure_project_issue(update, context, "audit")
    if not project_key:
        return

    msg = await update.effective_message.reply_text(
        f"📊 Fetching audit trail for issue #{issue_num}...",
        parse_mode="Markdown",
    )

    try:
        audit_history = deps.get_audit_history(issue_num, limit=100)

        if not audit_history:
            await context.bot.edit_message_text(
                chat_id=update.effective_chat.id,
                message_id=msg.message_id,
                text=f"📊 **Audit Trail for Issue #{issue_num}**\n\nNo audit events recorded yet.",
            )
            return

        timeline = f"📊 **Audit Trail for Issue #{issue_num}**\n"
        timeline += "=" * 40 + "\n\n"

        event_emoji = {
            "AGENT_LAUNCHED": "🚀",
            "AGENT_TIMEOUT_KILL": "⏱️",
            "AGENT_RETRY": "🔄",
            "AGENT_FAILED": "❌",
            "WORKFLOW_PAUSED": "⏸️",
            "WORKFLOW_RESUMED": "▶️",
            "WORKFLOW_STOPPED": "🛑",
            "AGENT_COMPLETION": "✅",
            "WORKFLOW_STARTED": "🎬",
            "WORKFLOW_CREATED": "📋",
            "STEP_STARTED": "▶️",
            "STEP_COMPLETED": "✅",
        }

        for evt in audit_history:
            try:
                event_type = evt.get("event_type", "?")
                timestamp = evt.get("timestamp", "?")
                data = evt.get("data", {})
                details = data.get("details", "") if isinstance(data, dict) else ""
                emoji = event_emoji.get(event_type, "•")

                timeline += f"{emoji} **{event_type}** ({timestamp})\n"
                if details:
                    timeline += f"   {details}\n"
                timeline += "\n"
            except Exception as exc:
                deps.logger.warning(f"Error formatting audit event: {exc}")
                timeline += f"• {evt}\n\n"

        max_len = 3500
        if len(timeline) <= max_len:
            await context.bot.edit_message_text(
                chat_id=update.effective_chat.id,
                message_id=msg.message_id,
                text=timeline,
            )
        else:
            chunks = [timeline[i : i + max_len] for i in range(0, len(timeline), max_len)]
            await context.bot.edit_message_text(
                chat_id=update.effective_chat.id,
                message_id=msg.message_id,
                text=chunks[0],
            )
            for chunk in chunks[1:]:
                await context.bot.send_message(chat_id=update.effective_chat.id, text=chunk)
    except Exception as exc:
        deps.logger.error(f"Error in audit_handler: {exc}", exc_info=True)
        error_msg = deps.format_error_for_user(exc, "while fetching audit trail")
        await update.effective_message.reply_text(error_msg)


async def stats_handler(update: Update, context: ContextTypes.DEFAULT_TYPE, deps: OpsHandlerDeps) -> None:
    deps.logger.info(f"Stats requested by user: {update.effective_user.id}")
    if deps.allowed_user_id and update.effective_user.id != deps.allowed_user_id:
        deps.logger.warning(f"Unauthorized access attempt by ID: {update.effective_user.id}")
        return

    msg = await update.effective_message.reply_text(
        "📊 Generating analytics report...",
        parse_mode="Markdown",
    )

    try:
        lookback_days = 30
        if context.args and len(context.args) > 0:
            try:
                lookback_days = int(context.args[0])
                if lookback_days < 1 or lookback_days > 365:
                    await update.effective_message.reply_text(
                        "⚠️ Lookback days must be between 1 and 365. Using default 30 days."
                    )
                    lookback_days = 30
            except ValueError:
                await update.effective_message.reply_text("⚠️ Invalid lookback days. Using default 30 days.")
                lookback_days = 30

        report = deps.get_stats_report(lookback_days=lookback_days)

        max_len = 3500
        if len(report) <= max_len:
            await context.bot.edit_message_text(
                chat_id=update.effective_chat.id,
                message_id=msg.message_id,
                text=report,
                parse_mode="Markdown",
            )
        else:
            chunks = [report[i : i + max_len] for i in range(0, len(report), max_len)]
            await context.bot.edit_message_text(
                chat_id=update.effective_chat.id,
                message_id=msg.message_id,
                text=chunks[0],
                parse_mode="Markdown",
            )
            for chunk in chunks[1:]:
                await context.bot.send_message(
                    chat_id=update.effective_chat.id,
                    text=chunk,
                    parse_mode="Markdown",
                )

    except FileNotFoundError:
        await context.bot.edit_message_text(
            chat_id=update.effective_chat.id,
            message_id=msg.message_id,
            text="📊 No audit log found. System has not logged any workflow events yet.",
        )
    except Exception as exc:
        deps.logger.error(f"Error in stats_handler: {exc}", exc_info=True)
        error_msg = deps.format_error_for_user(exc, "while generating analytics report")
        await context.bot.edit_message_text(
            chat_id=update.effective_chat.id,
            message_id=msg.message_id,
            text=error_msg,
        )


async def agents_handler(update: Update, context: ContextTypes.DEFAULT_TYPE, deps: OpsHandlerDeps) -> None:
    deps.logger.info(f"Agents requested by user: {update.effective_user.id}")
    if deps.allowed_user_id and update.effective_user.id != deps.allowed_user_id:
        deps.logger.warning(f"Unauthorized access attempt by ID: {update.effective_user.id}")
        return

    if not context.args:
        await deps.prompt_project_selection(update, context, "agents")
        return

    project = context.args[0].lower()
    if project not in deps.project_config:
        await update.effective_message.reply_text(
            f"❌ Unknown project '{project}'\n\n"
            f"Available: " + ", ".join(deps.project_config.keys())
        )
        return

    agents_dir = os.path.join(deps.base_dir, deps.project_config[project]["agents_dir"])
    if not os.path.exists(agents_dir):
        await update.effective_message.reply_text(f"⚠️ Agents directory not found for '{project}'")
        return

    try:
        agents_map = deps.get_agents_for_project(agents_dir)

        if not agents_map:
            await update.effective_message.reply_text(f"No agents configured for '{project}'")
            return

        agents_list = "\n".join([f"• @{agent}" for agent in sorted(agents_map.keys())])
        await update.effective_message.reply_text(
            f"🤖 **Agents for {project}:**\n\n{agents_list}\n\n"
            "Use `/direct <project> <@agent> <message>` to send a direct request."
        )
    except Exception as exc:
        deps.logger.error(f"Error listing agents: {exc}")
        await update.effective_message.reply_text(f"❌ Error: {exc}")


async def direct_handler(update: Update, context: ContextTypes.DEFAULT_TYPE, deps: OpsHandlerDeps) -> None:
    deps.logger.info(f"Direct request by user: {update.effective_user.id}")
    if deps.allowed_user_id and update.effective_user.id != deps.allowed_user_id:
        deps.logger.warning(f"Unauthorized access attempt by ID: {update.effective_user.id}")
        return

    if len(context.args) < 3:
        await update.effective_message.reply_text(
            "⚠️ Usage: /direct <project> <@agent> <message>\n\n"
            "Example: /direct case_italia @BackendLead Add caching to API endpoints"
        )
        return

    project = context.args[0].lower()
    agent = context.args[1].lstrip("@")
    message = " ".join(context.args[2:])

    if project not in deps.project_config:
        await update.effective_message.reply_text(f"❌ Unknown project '{project}'")
        return

    agents_dir = os.path.join(deps.base_dir, deps.project_config[project]["agents_dir"])
    agents_map = deps.get_agents_for_project(agents_dir)

    if agent not in agents_map:
        available = ", ".join([f"@{a}" for a in sorted(agents_map.keys())])
        await update.effective_message.reply_text(
            f"❌ Unknown agent '@{agent}' for {project}\n\n"
            f"Available: {available}"
        )
        return

    msg = await update.effective_message.reply_text(f"🚀 Creating direct request for @{agent}...")

    try:
        title = f"Direct Request: {message[:50]}"
        body = f"""**Direct Request** to @{agent}

{message}

**Project:** {project}
**Assigned to:** @{agent}

---
*Created via /direct command - invoke {agent} immediately*"""

        repo = deps.get_github_repo(project)
        plugin = deps.get_direct_issue_plugin(repo)
        if not plugin:
            await context.bot.edit_message_text(
                chat_id=update.effective_chat.id,
                message_id=msg.message_id,
                text="❌ Failed to initialize GitHub issue plugin",
            )
            return

        issue_url = plugin.create_issue(
            title=title,
            body=body,
            labels=["workflow:fast-track"],
        )
        if not issue_url:
            await context.bot.edit_message_text(
                chat_id=update.effective_chat.id,
                message_id=msg.message_id,
                text="❌ Failed to create issue",
            )
            return

        match = re.search(r"/issues/(\d+)$", issue_url)
        if not match:
            await context.bot.edit_message_text(
                chat_id=update.effective_chat.id,
                message_id=msg.message_id,
                text="❌ Failed to get issue number",
            )
            return

        issue_num = match.group(1)
        comment_body = f"🎯 Direct request from @Ghabs\n\nReady for `@{agent}`"
        plugin.add_comment(issue_num, comment_body)

        await context.bot.edit_message_text(
            chat_id=update.effective_chat.id,
            message_id=msg.message_id,
            text=(
                f"✅ Direct request created for @{agent} (Issue #{issue_num})\n\n"
                f"Message: {message}\n\n"
                f"The auto-chaining system will invoke @{agent} on the next cycle (~60s)\n\n"
                f"🔗 {issue_url}"
            ),
        )
    except Exception as exc:
        deps.logger.error(f"Error in direct request: {exc}")
        await context.bot.edit_message_text(
            chat_id=update.effective_chat.id,
            message_id=msg.message_id,
            text=f"❌ Error: {exc}",
        )

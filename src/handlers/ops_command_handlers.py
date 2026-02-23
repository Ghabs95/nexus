"""Operational command handlers extracted from telegram_bot."""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Dict, List, Optional, Tuple

from telegram import Update
from telegram.ext import ContextTypes

from chat_agents_schema import get_project_chat_agent_types
from handlers.agent_definition_utils import extract_agent_identity
from handlers.agent_resolution_handler import resolve_agents_for_project


@dataclass
class OpsHandlerDeps:
    logger: Any
    allowed_user_ids: List[int]
    base_dir: str
    nexus_dir_name: str
    project_config: Dict[str, Dict[str, Any]]
    prompt_project_selection: Callable[[Update, ContextTypes.DEFAULT_TYPE, str], Awaitable[None]]
    ensure_project_issue: Callable[
        [Update, ContextTypes.DEFAULT_TYPE, str], Awaitable[Tuple[Optional[str], Optional[str], List[str]]]
    ]
    get_project_label: Callable[[str], str]
    get_stats_report: Callable[[int], str]
    format_error_for_user: Callable[[Exception, str], str]
    get_audit_history: Callable[[str, int], List[Dict[str, Any]]]
    get_github_repo: Callable[[str], str]
    get_direct_issue_plugin: Callable[[str], Any]
    orchestrator: Any
    ai_persona: str
    get_chat_history: Callable[[int], str]
    append_message: Callable[[int, str, str], None]
    create_chat: Callable[..., str]


def _normalize_agent_key(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", (value or "").strip().lower())


def _resolve_agent_type(
    agent_name: str,
    source_filename: str,
    agents_dir: str,
    nexus_dir_name: str,
    available_agent_types: Optional[List[str]] = None,
) -> Optional[str]:
    candidate_paths = [
        os.path.join(agents_dir, nexus_dir_name, "agents", source_filename),
        os.path.join(agents_dir, source_filename),
    ]

    for candidate_path in candidate_paths:
        if not os.path.isfile(candidate_path):
            continue

        if candidate_path.endswith((".yaml", ".yml")):
            try:
                _agent_name, agent_type = extract_agent_identity(candidate_path)
                if agent_type:
                    return agent_type
            except Exception:
                continue

    normalized = _normalize_agent_key(agent_name)

    normalized_types: List[str] = []
    if isinstance(available_agent_types, list):
        for item in available_agent_types:
            candidate = str(item or "").strip().lower()
            if candidate and candidate not in normalized_types:
                normalized_types.append(candidate)

    if normalized in normalized_types:
        return normalized

    source_stem = os.path.splitext(os.path.basename(source_filename or ""))[0]
    normalized_stem = _normalize_agent_key(source_stem)
    if normalized_stem in normalized_types:
        return normalized_stem

    return None


def _build_direct_chat_persona(base_persona: str, project: str, agent_name: str, agent_type: str) -> str:
    safe_base = base_persona or "You are a helpful AI assistant."
    context_block = (
        "\n\nDirect Conversation Context:\n"
        f"- Project: {project}\n"
        f"- Requested agent: @{agent_name}\n"
        f"- Routed agent_type: {agent_type}\n"
        "Behavior rules:\n"
        f"- Respond in the voice and decision style of `{agent_type}`.\n"
        "- This is a direct chat reply, not a workflow ticket.\n"
        "- Keep the answer concise, actionable, and business-oriented."
    )
    return f"{safe_base}{context_block}"


async def audit_handler(update: Update, context: ContextTypes.DEFAULT_TYPE, deps: OpsHandlerDeps) -> None:
    deps.logger.info(f"Audit trail requested by user: {update.effective_user.id}")
    if deps.allowed_user_ids and update.effective_user.id not in deps.allowed_user_ids:
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
    if deps.allowed_user_ids and update.effective_user.id not in deps.allowed_user_ids:
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
    if deps.allowed_user_ids and update.effective_user.id not in deps.allowed_user_ids:
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
        agents_map = resolve_agents_for_project(agents_dir, deps.nexus_dir_name)

        if not agents_map:
            await update.effective_message.reply_text(f"No agents configured for '{project}'")
            return

        agents_list = "\n".join([f"• @{agent}" for agent in sorted(agents_map.keys())])
        await update.effective_message.reply_text(
            f"🤖 **Agents for {project}:**\n\n{agents_list}\n\n"
            "Use `/direct <project> <@agent> <message>` to send a direct request.\n"
            "Use /chat for project-scoped conversations and strategy threads."
        )
    except Exception as exc:
        deps.logger.error(f"Error listing agents: {exc}")
        await update.effective_message.reply_text(f"❌ Error: {exc}")


async def direct_handler(update: Update, context: ContextTypes.DEFAULT_TYPE, deps: OpsHandlerDeps) -> None:
    deps.logger.info(f"Direct request by user: {update.effective_user.id}")
    if deps.allowed_user_ids and update.effective_user.id not in deps.allowed_user_ids:
        deps.logger.warning(f"Unauthorized access attempt by ID: {update.effective_user.id}")
        return

    if len(context.args) < 3:
        await update.effective_message.reply_text(
            "⚠️ Usage: /direct <project> <@agent> <message>\n\n"
            "Example: /direct case_italia @BackendLead Add caching to API endpoints\n"
            "Optional: add `--new-chat` for strategic agents to start a fresh chat thread"
        )
        return

    project = context.args[0].lower()
    agent = context.args[1].lstrip("@")
    message_tokens = [token for token in context.args[2:] if token != "--new-chat"]
    create_new_chat = "--new-chat" in context.args[2:]
    message = " ".join(message_tokens).strip()

    if not message:
        await update.effective_message.reply_text(
            "⚠️ Please include a message after the agent.\n\n"
            "Example: /direct wallible @Vision --new-chat Which strategy should we prioritize next quarter?"
        )
        return

    if project not in deps.project_config:
        await update.effective_message.reply_text(f"❌ Unknown project '{project}'")
        return

    agents_dir = os.path.join(deps.base_dir, deps.project_config[project]["agents_dir"])
    agents_map = resolve_agents_for_project(agents_dir, deps.nexus_dir_name)

    if agent not in agents_map:
        available = ", ".join([f"@{a}" for a in sorted(agents_map.keys())])
        await update.effective_message.reply_text(
            f"❌ Unknown agent '@{agent}' for {project}\n\n"
            f"Available: {available}"
        )
        return

    source_filename = agents_map.get(agent, "")
    project_cfg = deps.project_config.get(project) if isinstance(deps.project_config, dict) else {}
    project_chat_agent_types = get_project_chat_agent_types(project_cfg if isinstance(project_cfg, dict) else {})
    agent_type = _resolve_agent_type(
        agent,
        source_filename,
        agents_dir,
        deps.nexus_dir_name,
        available_agent_types=project_chat_agent_types,
    )

    if agent_type and agent_type in project_chat_agent_types:
        msg = await update.effective_message.reply_text(f"🤖 Asking @{agent} directly...")
        try:
            user_id = update.effective_user.id
            if create_new_chat:
                chat_title = f"Direct @{agent} ({project})"
                deps.create_chat(
                    user_id,
                    title=chat_title,
                    metadata={
                        "project_key": project,
                        "primary_agent_type": agent_type,
                    },
                )

            deps.append_message(user_id, "user", message)
            history = deps.get_chat_history(user_id)
            persona = _build_direct_chat_persona(deps.ai_persona, project, agent, agent_type)

            chat_result = deps.orchestrator.run_text_to_speech_analysis(
                text=message,
                task="advisor_chat",
                history=history,
                persona=persona,
            )

            reply_text = chat_result.get("text", "I couldn't generate a response right now.")
            deps.append_message(user_id, "assistant", reply_text)

            await context.bot.edit_message_text(
                chat_id=update.effective_chat.id,
                message_id=msg.message_id,
                text=(
                    f"🤖 *{agent} ({agent_type})*: \n\n{reply_text}\n\n"
                    f"🧵 Chat thread: {'new' if create_new_chat else 'current'}\n"
                    "💬 Use /chat to manage conversation threads and context."
                ),
                parse_mode="Markdown",
            )
            return
        except Exception as exc:
            deps.logger.error(f"Error in direct chat request: {exc}")
            await context.bot.edit_message_text(
                chat_id=update.effective_chat.id,
                message_id=msg.message_id,
                text=f"❌ Error in direct chat reply: {exc}",
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
                text="❌ Failed to create issue\n\nIf this is a discussion, use /chat instead.",
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
                f"🔗 {issue_url}\n\n"
                "💬 For conversational strategy Q&A, use /chat."
            ),
        )
    except Exception as exc:
        deps.logger.error(f"Error in direct request: {exc}")
        await context.bot.edit_message_text(
            chat_id=update.effective_chat.id,
            message_id=msg.message_id,
            text=f"❌ Error: {exc}",
        )

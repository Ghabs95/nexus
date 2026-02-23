"""Callback and picker handlers extracted from telegram_bot."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Dict

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import ContextTypes, ConversationHandler


@dataclass
class CallbackHandlerDeps:
    logger: Any
    github_repo: str
    prompt_issue_selection: Callable[..., Awaitable[None]]
    prompt_project_selection: Callable[[Update, ContextTypes.DEFAULT_TYPE, str], Awaitable[None]]
    dispatch_command: Callable[..., Awaitable[None]]
    get_project_label: Callable[[str], str]
    status_handler: Callable[[Update, ContextTypes.DEFAULT_TYPE], Awaitable[None]]
    active_handler: Callable[[Update, ContextTypes.DEFAULT_TYPE], Awaitable[None]]
    get_direct_issue_plugin: Callable[[str], Any]
    get_workflow_state_plugin: Callable[..., Any]
    workflow_state_plugin_kwargs: Dict[str, Any]
    action_handlers: Dict[str, Callable[[Update, ContextTypes.DEFAULT_TYPE], Awaitable[None]]]


async def project_picker_handler(update: Update, context: ContextTypes.DEFAULT_TYPE, deps: CallbackHandlerDeps):
    query = update.callback_query
    await query.answer()

    if not query.data or not query.data.startswith("pickcmd:"):
        return

    _, command, project_key = query.data.split(":", 2)
    context.user_data["pending_command"] = command
    context.user_data["pending_project"] = project_key

    if command == "agents":
        context.user_data.pop("pending_command", None)
        context.user_data.pop("pending_project", None)
        await deps.dispatch_command(update, context, command, project_key, "")
        return

    pending_issue = context.user_data.get("pending_issue")
    if pending_issue and command != "respond":
        context.user_data.pop("pending_issue", None)
        await deps.dispatch_command(update, context, command, project_key, pending_issue)
        return

    if pending_issue and command == "respond":
        await query.edit_message_text(
            f"Selected {deps.get_project_label(project_key)}. Now send the response message."
        )
        return

    await deps.prompt_issue_selection(update, context, command, project_key, edit_message=True)


async def issue_picker_handler(update: Update, context: ContextTypes.DEFAULT_TYPE, deps: CallbackHandlerDeps):
    query = update.callback_query
    await query.answer()

    if not query.data:
        return

    if query.data.startswith("pickissue_manual:"):
        _, command, project_key = query.data.split(":", 2)
        context.user_data["pending_command"] = command
        context.user_data["pending_project"] = project_key
        await query.edit_message_text(
            f"Selected {deps.get_project_label(project_key)}. Send the issue number."
        )
        return

    if query.data.startswith("pickissue_state:"):
        _, issue_state, command, project_key = query.data.split(":", 3)
        await deps.prompt_issue_selection(
            update,
            context,
            command,
            project_key,
            edit_message=True,
            issue_state=issue_state,
        )
        return

    if not query.data.startswith("pickissue:"):
        return

    _, command, project_key, issue_num = query.data.split(":", 3)
    await query.edit_message_reply_markup(reply_markup=None)
    await deps.dispatch_command(update, context, command, project_key, issue_num)


async def monitor_project_picker_handler(update: Update, context: ContextTypes.DEFAULT_TYPE, deps: CallbackHandlerDeps):
    query = update.callback_query
    await query.answer()

    if not query.data or not query.data.startswith("pickmonitor:"):
        return

    _, command, project_key = query.data.split(":", 2)
    context.args = [project_key]

    if command == "status":
        await deps.status_handler(update, context)
        return
    if command == "active":
        await deps.active_handler(update, context)
        return

    await query.edit_message_text("Unsupported monitoring command.")


async def close_flow_handler(update: Update, context: ContextTypes.DEFAULT_TYPE, deps: CallbackHandlerDeps):
    query = update.callback_query
    await query.answer()
    await query.edit_message_reply_markup(reply_markup=None)


async def flow_close_handler(update: Update, context: ContextTypes.DEFAULT_TYPE, deps: CallbackHandlerDeps):
    query = update.callback_query
    await query.answer()
    await query.edit_message_text("❌ Cancelled.")
    return ConversationHandler.END


async def menu_callback_handler(update: Update, context: ContextTypes.DEFAULT_TYPE, deps: CallbackHandlerDeps):
    query = update.callback_query
    await query.answer()

    if not query.data:
        return

    menu_key = query.data.split(":", 1)[1]

    if menu_key == "close":
        await query.edit_message_reply_markup(reply_markup=None)
        return

    if menu_key == "root":
        keyboard = [
            [InlineKeyboardButton("🗣️ Chat", callback_data="menu:chat")],
            [InlineKeyboardButton("✨ Task Creation", callback_data="menu:tasks")],
            [InlineKeyboardButton("📊 Monitoring", callback_data="menu:monitor")],
            [InlineKeyboardButton("🔁 Workflow Control", callback_data="menu:workflow")],
            [InlineKeyboardButton("🤝 Agents", callback_data="menu:agents")],
            [InlineKeyboardButton("🔧 Git Platform", callback_data="menu:github")],
            [InlineKeyboardButton("ℹ️ Help", callback_data="menu:help")],
            [InlineKeyboardButton("❌ Close", callback_data="menu:close")],
        ]
        await query.edit_message_text(
            "📍 **Nexus Menu**\nChoose a category:",
            reply_markup=InlineKeyboardMarkup(keyboard),
            parse_mode="Markdown",
        )
        return

    menu_texts = {
        "chat": (
            "🗣️ **Chat**\n"
            "- /chat — Open chat threads and context controls\n"
            "- /chatagents [project] — Show ordered chat agent types (first is primary)\n"
            "- Configure project, mode, and primary agent for conversational routing"
        ),
        "tasks": (
            "✨ **Task Creation**\n"
            "- /menu — Open command menu\n"
            "- /new — Start task creation\n"
            "- /cancel — Abort the current guided process\n\n"
            "Tip: send a voice note or text to auto-create a task."
        ),
        "monitor": (
            "📊 **Monitoring**\n"
            "- /status — View pending tasks in inbox\n"
            "- /active — View tasks currently being worked on\n"
            "- /myissues — View your tracked issues\n"
            "- /logs <project> <issue#> — View task logs\n"
            "- /logsfull <project> <issue#> — Full log lines (no truncation)\n"
            "- /tail <project> <issue#> [lines] [seconds] — Follow live logs\n"
            "- /tailstop — Stop current live tail session\n"
            "- /fuse <project> <issue#> — View retry fuse state\n"
            "- /audit <project> <issue#> — View workflow audit trail\n"
            "- /stats [days] — View system analytics (default: 30 days)\n"
            "- /comments <project> <issue#> — View issue comments\n"
            "- /track <project> <issue#> — Subscribe to updates\n"
            "- /untrack <project> <issue#> — Stop tracking"
        ),
        "workflow": (
            "🔁 **Workflow Control**\n"
            "- /reprocess <project> <issue#> — Re-run agent processing\n"
            "- /wfstate <project> <issue#> — Show workflow state + drift\n"
            "- /reconcile <project> <issue#> — Reconcile workflow/comment/local state\n"
            "- /continue <project> <issue#> — Resume a stuck agent\n"
            "- /forget <project> <issue#> — Purge local state for a stale/deleted issue\n"
            "- /kill <project> <issue#> — Stop a running agent\n"
            "- /pause <project> <issue#> — Pause auto-chaining\n"
            "- /resume <project> <issue#> — Resume auto-chaining\n"
            "- /stop <project> <issue#> — Stop workflow completely\n"
            "- /respond <project> <issue#> <text> — Respond to agent questions"
        ),
        "agents": (
            "🤝 **Agents**\n"
            "- /agents <project> — List agents for a project\n"
            "- /direct <project> <@agent> <message> — Send direct request\n"
            "- /direct <project> <@agent> --new-chat <message> — Strategic direct reply in a new chat"
        ),
        "github": (
            "🔧 **Git Platform**\n"
            "- /assign <project> <issue#> — Assign issue to yourself\n"
            "- /implement <project> <issue#> — Request Copilot implementation\n"
            "- /prepare <project> <issue#> — Add Copilot-friendly instructions"
        ),
        "help": "ℹ️ Use /help for the full command list.",
    }

    text = menu_texts.get(menu_key, "Unknown menu option.")
    await query.edit_message_text(
        text,
        reply_markup=InlineKeyboardMarkup(
            [
                [InlineKeyboardButton("⬅️ Back", callback_data="menu:root")],
                [InlineKeyboardButton("❌ Close", callback_data="menu:close")],
            ]
        ),
        parse_mode="Markdown",
    )


async def inline_keyboard_handler(update: Update, context: ContextTypes.DEFAULT_TYPE, deps: CallbackHandlerDeps):
    query = update.callback_query
    await query.answer()

    if not query.data:
        return

    parts = query.data.split("_", 1)
    if len(parts) < 2:
        return

    action = parts[0]
    issue_num = parts[1]

    deps.logger.info(f"Inline keyboard action: {action} for issue #{issue_num}")

    if action in deps.action_handlers:
        context.user_data["pending_command"] = action
        context.user_data["pending_issue"] = issue_num
        await deps.prompt_project_selection(update, context, action)
    elif action == "respond":
        await query.edit_message_text(
            f"✍️ To respond to issue #{issue_num}, use:\n\n"
            f"`/respond {issue_num} <your message>`\n\n"
            f"Example:\n"
            f"`/respond {issue_num} Approved, proceed with implementation`",
            parse_mode="Markdown",
        )
    elif action == "approve":
        context.args = [issue_num]
        await query.edit_message_text(f"✅ Approving implementation for issue #{issue_num}...")

        try:
            plugin = deps.get_direct_issue_plugin(deps.github_repo)
            if not plugin or not plugin.add_comment(
                issue_num,
                "✅ Implementation approved by @Ghabs. Please proceed.",
            ):
                await query.edit_message_text(f"❌ Error approving issue #{issue_num}")
                return
            await query.edit_message_text(
                f"✅ Implementation approved for issue #{issue_num}\n\n"
                f"Agent will continue automatically.",
                parse_mode="Markdown",
            )
        except Exception as exc:
            await query.edit_message_text(f"❌ Error approving: {exc}")
    elif action == "reject":
        context.args = [issue_num]
        await query.edit_message_text(f"❌ Rejecting implementation for issue #{issue_num}...")

        try:
            plugin = deps.get_direct_issue_plugin(deps.github_repo)
            if not plugin or not plugin.add_comment(
                issue_num,
                "❌ Implementation rejected by @Ghabs. Please revise.",
            ):
                await query.edit_message_text(f"❌ Error rejecting issue #{issue_num}")
                return
            await query.edit_message_text(
                f"❌ Implementation rejected for issue #{issue_num}\n\n"
                f"Agent has been notified.",
                parse_mode="Markdown",
            )
        except Exception as exc:
            await query.edit_message_text(f"❌ Error rejecting: {exc}")
    elif action == "wfapprove":
        parts2 = issue_num.split("_", 1)
        real_issue = parts2[0]
        step_num = parts2[1] if len(parts2) > 1 else "?"
        await query.edit_message_text(
            f"✅ Approving workflow step {step_num} for issue #{real_issue}..."
        )
        try:
            workflow_plugin = deps.get_workflow_state_plugin(
                **deps.workflow_state_plugin_kwargs,
                cache_key="workflow:state-engine",
            )
            approved_by = update.effective_user.username or str(update.effective_user.id)
            if not workflow_plugin or not await workflow_plugin.approve_step(real_issue, approved_by):
                await query.edit_message_text(
                    f"❌ No workflow found for issue #{real_issue}"
                )
                return
            await query.edit_message_text(
                f"✅ Step {step_num} approved for issue #{real_issue}\n\n"
                f"Workflow will continue automatically.",
                parse_mode="Markdown",
            )
        except Exception as exc:
            await query.edit_message_text(f"❌ Error approving workflow step: {exc}")
    elif action == "wfdeny":
        parts2 = issue_num.split("_", 1)
        real_issue = parts2[0]
        step_num = parts2[1] if len(parts2) > 1 else "?"
        await query.edit_message_text(
            f"❌ Denying workflow step {step_num} for issue #{real_issue}..."
        )
        try:
            workflow_plugin = deps.get_workflow_state_plugin(
                **deps.workflow_state_plugin_kwargs,
                cache_key="workflow:state-engine",
            )
            denied_by = update.effective_user.username or str(update.effective_user.id)
            if not workflow_plugin or not await workflow_plugin.deny_step(
                real_issue,
                denied_by,
                reason="Denied via Telegram",
            ):
                await query.edit_message_text(
                    f"❌ No workflow found for issue #{real_issue}"
                )
                return
            await query.edit_message_text(
                f"❌ Step {step_num} denied for issue #{real_issue}\n\n"
                f"Workflow has been stopped.",
                parse_mode="Markdown",
            )
        except Exception as exc:
            await query.edit_message_text(f"❌ Error denying workflow step: {exc}")

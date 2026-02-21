"""Issue-related command handlers extracted from telegram_bot."""

from __future__ import annotations

import os
import re
import subprocess
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Awaitable, Callable, Dict, List, Optional, Tuple

from telegram import Update
from telegram.ext import ContextTypes


@dataclass
class IssueHandlerDeps:
    logger: Any
    allowed_user_id: Optional[int]
    base_dir: str
    default_repo: str
    prompt_project_selection: Callable[[Update, ContextTypes.DEFAULT_TYPE, str], Awaitable[None]]
    ensure_project_issue: Callable[
        [Update, ContextTypes.DEFAULT_TYPE, str], Awaitable[Tuple[Optional[str], Optional[str], List[str]]]
    ]
    project_repo: Callable[[str], str]
    project_issue_url: Callable[[str, str], str]
    get_issue_details: Callable[[str, Optional[str]], Optional[Dict[str, Any]]]
    get_direct_issue_plugin: Callable[[str], Any]
    resolve_project_config_from_task: Callable[[str], Tuple[Optional[str], Optional[Dict[str, Any]]]]
    invoke_copilot_agent: Callable[..., Tuple[Optional[int], Optional[str]]]
    get_sop_tier: Callable[[str], Tuple[str, Any, Any]]
    find_task_file_by_issue: Callable[[str], Optional[str]]
    resolve_repo: Callable[[Optional[Dict[str, Any]], str], str]
    build_issue_url: Callable[[str, str, Optional[Dict[str, Any]]], str]
    user_manager: Any
    save_tracked_issues: Callable[[Dict[str, Any]], None]
    tracked_issues_ref: Dict[str, Any]
    default_issue_url: Callable[[str], str]
    get_project_label: Callable[[str], str]
    track_short_projects: List[str]


async def assign_handler(update: Update, context: ContextTypes.DEFAULT_TYPE, deps: IssueHandlerDeps) -> None:
    deps.logger.info(f"Assign triggered by user: {update.effective_user.id}")
    if deps.allowed_user_id and update.effective_user.id != deps.allowed_user_id:
        deps.logger.warning(f"Unauthorized access attempt by ID: {update.effective_user.id}")
        return

    if not context.args:
        await deps.prompt_project_selection(update, context, "assign")
        return

    project_key, issue_number, rest = await deps.ensure_project_issue(update, context, "assign")
    if not project_key:
        return

    repo = deps.project_repo(project_key)
    issue_url = deps.project_issue_url(project_key, issue_number)
    assignee = "@me"
    if rest:
        raw_assignee = rest[0]
        if raw_assignee.lower() == "copilot":
            assignee = os.getenv("GITHUB_COPILOT_USER", "copilot")
        else:
            assignee = raw_assignee

    msg = await update.effective_message.reply_text(f"🔄 Assigning issue #{issue_number}...")

    try:
        plugin = deps.get_direct_issue_plugin(repo)
        if not plugin or not plugin.add_assignee(issue_number, assignee):
            await context.bot.edit_message_text(
                chat_id=update.effective_chat.id,
                message_id=msg.message_id,
                text=f"❌ Failed to assign issue #{issue_number}",
            )
            return

        display_assignee = assignee
        if display_assignee == "@me":
            display_assignee = "you (@me)"
        await context.bot.edit_message_text(
            chat_id=update.effective_chat.id,
            message_id=msg.message_id,
            text=f"✅ Issue #{issue_number} assigned to {display_assignee}!\n\n{issue_url}",
            parse_mode="Markdown",
        )
    except Exception as exc:
        await context.bot.edit_message_text(
            chat_id=update.effective_chat.id,
            message_id=msg.message_id,
            text=f"❌ Failed to assign issue #{issue_number}\n\nError: {exc}",
        )


async def implement_handler(update: Update, context: ContextTypes.DEFAULT_TYPE, deps: IssueHandlerDeps) -> None:
    deps.logger.info(f"Implement requested by user: {update.effective_user.id}")
    if deps.allowed_user_id and update.effective_user.id != deps.allowed_user_id:
        deps.logger.warning(f"Unauthorized access attempt by ID: {update.effective_user.id}")
        return

    if not context.args:
        await deps.prompt_project_selection(update, context, "implement")
        return

    project_key, issue_number, _ = await deps.ensure_project_issue(update, context, "implement")
    if not project_key:
        return

    repo = deps.project_repo(project_key)
    issue_url = deps.project_issue_url(project_key, issue_number)

    msg = await update.message.reply_text(f"🔔 Requesting Copilot implementation for issue #{issue_number}...")

    try:
        plugin = deps.get_direct_issue_plugin(repo)
        if not plugin:
            await context.bot.edit_message_text(
                chat_id=update.effective_chat.id,
                message_id=msg.message_id,
                text="❌ Failed to initialize GitHub issue plugin",
            )
            return

        plugin.ensure_label(
            "agent:requested",
            "E6E6FA",
            "Requested Copilot implementation",
        )
        if not plugin.add_label(issue_number, "agent:requested"):
            await context.bot.edit_message_text(
                chat_id=update.effective_chat.id,
                message_id=msg.message_id,
                text=f"❌ Failed to request implementation for issue #{issue_number}.",
            )
            return

        comment = (
            "@ProjectLead — Copilot implementation has been requested via Telegram.\n\n"
            "Please review the issue and either click 'Code with agent mode' in the GitHub UI "
            "or add the label `agent:approved` to start implementation.\n\n"
            f"Issue: {issue_url}"
        )

        if not plugin.add_comment(issue_number, comment):
            await context.bot.edit_message_text(
                chat_id=update.effective_chat.id,
                message_id=msg.message_id,
                text=f"❌ Failed to request implementation for issue #{issue_number}.",
            )
            return

        await context.bot.edit_message_text(
            chat_id=update.effective_chat.id,
            message_id=msg.message_id,
            text=(
                f"✅ Requested implementation for issue #{issue_number}. "
                f"ProjectLead has been notified.\n\n{issue_url}"
            ),
            parse_mode="Markdown",
        )
    except Exception as exc:
        await context.bot.edit_message_text(
            chat_id=update.effective_chat.id,
            message_id=msg.message_id,
            text=f"❌ Failed to request implementation for issue #{issue_number}.\n\nError: {exc}",
        )


async def prepare_handler(update: Update, context: ContextTypes.DEFAULT_TYPE, deps: IssueHandlerDeps) -> None:
    deps.logger.info(f"Prepare requested by user: {update.effective_user.id}")
    if deps.allowed_user_id and update.effective_user.id != deps.allowed_user_id:
        deps.logger.warning(f"Unauthorized access attempt by ID: {update.effective_user.id}")
        return

    if not context.args:
        await deps.prompt_project_selection(update, context, "prepare")
        return

    project_key, issue_number, _ = await deps.ensure_project_issue(update, context, "prepare")
    if not project_key:
        return

    repo = deps.project_repo(project_key)
    issue_url = deps.project_issue_url(project_key, issue_number)

    msg = await update.message.reply_text(f"🔧 Preparing issue #{issue_number} for Copilot...")

    try:
        plugin = deps.get_direct_issue_plugin(repo)
        if not plugin:
            await context.bot.edit_message_text(
                chat_id=update.effective_chat.id,
                message_id=msg.message_id,
                text="❌ Failed to initialize GitHub issue plugin",
            )
            return

        data = plugin.get_issue(issue_number, ["body", "title"])
        if not data:
            await context.bot.edit_message_text(
                chat_id=update.effective_chat.id,
                message_id=msg.message_id,
                text=f"❌ Failed to prepare issue #{issue_number}.",
            )
            return

        body = data.get("body", "")

        branch_match = re.search(r"Target Branch:\s*`([^`]+)`", body)
        taskfile_match = re.search(r"Task File:\s*`([^`]+)`", body)
        branch_name = branch_match.group(1) if branch_match else "<create-branch>"
        task_file = taskfile_match.group(1) if taskfile_match else None

        copilot_block = """
## Copilot Instructions

- Follow existing repository style and tests.
- Create a branch: `{branch}` and open a PR against the appropriate base branch.
- Include unit tests or update existing tests when applicable.
- Keep changes minimal and focused; reference the task file if present.
""".format(branch=branch_name)

        if task_file:
            copilot_block += f"\n**Suggested files to modify:** `{task_file}`\n"

        copilot_block += "\n**Acceptance Criteria**\n- Add concise acceptance criteria here (one per line).\n"

        new_body = body + "\n\n---\n\n" + copilot_block

        if not plugin.update_issue_body(issue_number, new_body):
            await context.bot.edit_message_text(
                chat_id=update.effective_chat.id,
                message_id=msg.message_id,
                text=f"❌ Failed to prepare issue #{issue_number}.",
            )
            return

        await context.bot.edit_message_text(
            chat_id=update.effective_chat.id,
            message_id=msg.message_id,
            text=(
                f"✅ Prepared issue #{issue_number} for Copilot. You can now click "
                f"'Code with agent mode' in GitHub or ask ProjectLead to approve.\n\n{issue_url}"
            ),
            parse_mode="Markdown",
        )
    except Exception as exc:
        await context.bot.edit_message_text(
            chat_id=update.effective_chat.id,
            message_id=msg.message_id,
            text=f"❌ Failed to prepare issue #{issue_number}.\n\nError: {exc}",
        )


async def track_handler(update: Update, context: ContextTypes.DEFAULT_TYPE, deps: IssueHandlerDeps) -> None:
    deps.logger.info(f"Track requested by user: {update.effective_user.id}")
    if deps.allowed_user_id and update.effective_user.id != deps.allowed_user_id:
        deps.logger.warning(f"Unauthorized access attempt by ID: {update.effective_user.id}")
        return

    user = update.effective_user

    if not context.args:
        await update.effective_message.reply_text(
            "⚠️ Usage:\n"
            "/track <issue#> - Track issue globally\n"
            "/track <project> <issue#> - Track issue per-project\n\n"
            f"Projects: {', '.join(deps.track_short_projects)}\n\n"
            "Examples:\n"
            "  /track 123\n"
            "  /track casit 456"
        )
        return

    if len(context.args) >= 2:
        project = context.args[0].lower()
        issue_num = context.args[1].lstrip("#")

        if project not in deps.track_short_projects:
            await update.effective_message.reply_text(
                f"❌ Invalid project '{project}'.\n"
                f"Valid projects: {', '.join(deps.track_short_projects)}"
            )
            return

        if not issue_num.isdigit():
            await update.effective_message.reply_text("❌ Invalid issue number.")
            return

        deps.user_manager.track_issue(
            telegram_id=user.id,
            project=project,
            issue_number=issue_num,
            username=user.username,
            first_name=user.first_name,
        )

        await update.effective_message.reply_text(
            f"👁️ Now tracking {project.upper()} issue #{issue_num} for you\n\n"
            "Use /myissues to see all your tracked issues\n"
            f"Use /untrack {project} {issue_num} to stop tracking"
        )
    else:
        issue_num = context.args[0].lstrip("#")
        if not issue_num.isdigit():
            await update.effective_message.reply_text("❌ Invalid issue number.")
            return

        deps.tracked_issues_ref[issue_num] = {
            "added_at": datetime.now().isoformat(),
            "last_seen_state": None,
            "last_seen_labels": [],
        }
        deps.save_tracked_issues(deps.tracked_issues_ref)

        details = deps.get_issue_details(issue_num)
        if details:
            await update.effective_message.reply_text(
                f"👁️ Now tracking issue #{issue_num} (global)\n\n"
                f"Title: {details.get('title', 'N/A')}\n"
                f"Status: {details.get('state', 'N/A')}\n"
                f"Labels: {', '.join([l['name'] for l in details.get('labels', [])])}\n\n"
                f"🔗 {deps.default_issue_url(issue_num)}\n\n"
                "💡 Tip: Use /track <project> <issue#> for per-project tracking"
            )
        else:
            await update.effective_message.reply_text(
                "⚠️ Could not fetch issue details, but tracking started.\n\n"
                f"🔗 {deps.default_issue_url(issue_num)}"
            )


async def untrack_handler(update: Update, context: ContextTypes.DEFAULT_TYPE, deps: IssueHandlerDeps) -> None:
    deps.logger.info(f"Untrack requested by user: {update.effective_user.id}")
    if deps.allowed_user_id and update.effective_user.id != deps.allowed_user_id:
        deps.logger.warning(f"Unauthorized access attempt by ID: {update.effective_user.id}")
        return

    user = update.effective_user

    if not context.args:
        await deps.prompt_project_selection(update, context, "untrack")
        return

    project_key, issue_num, _ = await deps.ensure_project_issue(update, context, "untrack")
    if not project_key:
        return

    success = deps.user_manager.untrack_issue(
        telegram_id=user.id,
        project=project_key,
        issue_number=issue_num,
    )

    if success:
        await update.effective_message.reply_text(
            f"✅ Stopped tracking {deps.get_project_label(project_key)} issue #{issue_num}"
        )
    else:
        await update.effective_message.reply_text(
            f"❌ You weren't tracking {deps.get_project_label(project_key)} issue #{issue_num}"
        )


async def myissues_handler(update: Update, context: ContextTypes.DEFAULT_TYPE, deps: IssueHandlerDeps) -> None:
    deps.logger.info(f"My issues requested by user: {update.effective_user.id}")
    if deps.allowed_user_id and update.effective_user.id != deps.allowed_user_id:
        deps.logger.warning(f"Unauthorized access attempt by ID: {update.effective_user.id}")
        return

    user = update.effective_user
    tracked = deps.user_manager.get_user_tracked_issues(user.id)

    if not tracked:
        await update.effective_message.reply_text(
            "📋 You're not tracking any issues yet.\n\n"
            "Use /track <project> <issue#> to start tracking.\n\n"
            "Examples:\n"
            "  /track casit 123\n"
            "  /track wlbl 456"
        )
        return

    message = "📋 <b>Your Tracked Issues</b>\n\n"

    total_issues = 0
    for project, issues in sorted(tracked.items()):
        if issues:
            message += f"<b>{project.upper()}</b>\n"
            for issue_num in issues:
                total_issues += 1
                message += f"  • #{issue_num}\n"
            message += "\n"

    message += f"<b>Total:</b> {total_issues} issue(s)\n\n"
    message += "<i>Use /untrack &lt;project&gt; &lt;issue#&gt; to stop tracking</i>"

    await update.effective_message.reply_text(message, parse_mode="HTML")


async def comments_handler(update: Update, context: ContextTypes.DEFAULT_TYPE, deps: IssueHandlerDeps) -> None:
    deps.logger.info(f"Comments requested by user: {update.effective_user.id}")
    if deps.allowed_user_id and update.effective_user.id != deps.allowed_user_id:
        deps.logger.warning(f"Unauthorized access attempt by ID: {update.effective_user.id}")
        return

    if not context.args:
        await deps.prompt_project_selection(update, context, "comments")
        return

    project_key, issue_num, _ = await deps.ensure_project_issue(update, context, "comments")
    if not project_key:
        return

    repo = deps.project_repo(project_key)
    issue_url = deps.project_issue_url(project_key, issue_num)

    msg = await update.effective_message.reply_text(f"💬 Fetching comments for issue #{issue_num}...")

    try:
        plugin = deps.get_direct_issue_plugin(repo)
        if not plugin:
            await context.bot.edit_message_text(
                chat_id=update.effective_chat.id,
                message_id=msg.message_id,
                text=f"❌ Failed to fetch comments for issue #{issue_num}",
            )
            return

        data = plugin.get_issue(issue_num, ["comments", "title"])
        if not data:
            await context.bot.edit_message_text(
                chat_id=update.effective_chat.id,
                message_id=msg.message_id,
                text=f"❌ Failed to fetch comments for issue #{issue_num}",
            )
            return

        title = data.get("title", "Unknown")
        comments = data.get("comments", [])

        if not comments:
            await context.bot.edit_message_text(
                chat_id=update.effective_chat.id,
                message_id=msg.message_id,
                text=(
                    f"💬 **Issue #{issue_num}: {title}**\n\n"
                    "No comments yet.\n\n"
                    f"🔗 {issue_url}"
                ),
                parse_mode="Markdown",
            )
            return

        comments_text = f"💬 **Issue #{issue_num}: {title}**\n\n"
        comments_text += f"Total comments: {len(comments)}\n\n"

        recent_comments = comments[-5:]
        for comment in recent_comments:
            author_data = comment.get("author")
            if isinstance(author_data, dict):
                author = author_data.get("login", "unknown")
            else:
                author = author_data or "unknown"
            created = comment.get("created") or comment.get("createdAt", "")
            body = comment.get("body", "")

            try:
                dt = datetime.fromisoformat(created.replace("Z", "+00:00"))
                time_str = dt.strftime("%Y-%m-%d %H:%M")
            except Exception:
                time_str = created

            preview = body[:200] + "..." if len(body) > 200 else body
            comments_text += f"**{author}** ({time_str}):\n{preview}\n\n"

        if len(comments) > 5:
            comments_text += f"_...and {len(comments) - 5} more comments_\n\n"

        comments_text += f"🔗 {issue_url}"

        max_len = 3500
        if len(comments_text) <= max_len:
            await context.bot.edit_message_text(
                chat_id=update.effective_chat.id,
                message_id=msg.message_id,
                text=comments_text,
                parse_mode="Markdown",
                disable_web_page_preview=True,
            )
        else:
            chunks = [comments_text[i : i + max_len] for i in range(0, len(comments_text), max_len)]
            await context.bot.edit_message_text(
                chat_id=update.effective_chat.id,
                message_id=msg.message_id,
                text=chunks[0],
                parse_mode="Markdown",
                disable_web_page_preview=True,
            )
            for part in chunks[1:]:
                await context.bot.send_message(
                    chat_id=update.effective_chat.id,
                    text=part,
                    parse_mode="Markdown",
                    disable_web_page_preview=True,
                )

    except Exception as exc:
        await context.bot.edit_message_text(
            chat_id=update.effective_chat.id,
            message_id=msg.message_id,
            text=f"❌ Error: {exc}",
        )


async def respond_handler(update: Update, context: ContextTypes.DEFAULT_TYPE, deps: IssueHandlerDeps) -> None:
    deps.logger.info(f"Respond requested by user: {update.effective_user.id}")
    if deps.allowed_user_id and update.effective_user.id != deps.allowed_user_id:
        deps.logger.warning(f"Unauthorized access attempt by ID: {update.effective_user.id}")
        return

    if not context.args:
        await deps.prompt_project_selection(update, context, "respond")
        return

    project_key, issue_num, rest = await deps.ensure_project_issue(update, context, "respond")
    if not project_key:
        return
    if not rest:
        await update.effective_message.reply_text("⚠️ Please include a response message.")
        return

    response_text = " ".join(rest)

    msg = await update.effective_message.reply_text(f"📝 Posting response to issue #{issue_num}...")

    try:
        task_file = deps.find_task_file_by_issue(issue_num)
        details = None
        repo = None
        if not task_file:
            repo = deps.project_repo(project_key)
            details = deps.get_issue_details(issue_num, repo=repo)
            if details:
                body = details.get("body", "")
                match = re.search(r"Task File:\s*`([^`]+)`", body)
                task_file = match.group(1) if match else None

        if not task_file or not os.path.exists(task_file):
            await update.effective_message.reply_text(
                "⚠️ Posted comment but couldn't find task file to continue agent."
            )
            return

        project_name, config = deps.resolve_project_config_from_task(task_file)
        if not config or not config.get("agents_dir"):
            await update.effective_message.reply_text(
                "⚠️ Posted comment but no agents config for project."
            )
            return

        repo = deps.resolve_repo(config, deps.default_repo)

        plugin = deps.get_direct_issue_plugin(repo)
        if not plugin or not plugin.add_comment(issue_num, response_text):
            await context.bot.edit_message_text(
                chat_id=update.effective_chat.id,
                message_id=msg.message_id,
                text=f"❌ Failed to post response to issue #{issue_num}.",
            )
            return

        await context.bot.edit_message_text(
            chat_id=update.effective_chat.id,
            message_id=msg.message_id,
            text=f"✅ Response posted to issue #{issue_num}.\n\n🤖 Continuing agent...",
        )

        if not details:
            details = deps.get_issue_details(issue_num, repo=repo)
            if not details:
                await update.effective_message.reply_text(
                    "⚠️ Posted comment but couldn't fetch issue details to continue agent."
                )
                return

        with open(task_file, "r", encoding="utf-8") as handle:
            content = handle.read()

        type_match = re.search(r"\*\*Type:\*\*\s*(.+)", content)
        task_type = type_match.group(1).strip().lower() if type_match else "feature"

        tier_name, _, _ = deps.get_sop_tier(task_type)
        issue_url = deps.build_issue_url(repo, issue_num, config)

        agents_abs = os.path.join(deps.base_dir, config["agents_dir"])
        workspace_abs = os.path.join(deps.base_dir, config["workspace"])

        continuation_prompt = (
            f"@Ghabs has provided input:\n\n{response_text}\n\n"
            "Please proceed with the next step of the workflow."
        )

        log_subdir = project_name
        pid, tool_used = deps.invoke_copilot_agent(
            agents_dir=agents_abs,
            workspace_dir=workspace_abs,
            issue_url=issue_url,
            tier_name=tier_name,
            task_content=content,
            continuation=True,
            continuation_prompt=continuation_prompt,
            log_subdir=log_subdir,
            project_name=log_subdir,
        )

        if pid:
            await update.effective_message.reply_text(
                f"✅ Agent resumed for issue #{issue_num} (PID: {pid}, Tool: {tool_used})\n\n"
                f"Check /logs {issue_num} to monitor progress.\n\n"
                f"🔗 {issue_url}"
            )
        else:
            await update.effective_message.reply_text(
                f"⚠️ Response posted but failed to continue agent.\n"
                f"Use /continue {issue_num} to resume manually.\n\n"
                f"🔗 {issue_url}"
            )

    except subprocess.TimeoutExpired:
        await context.bot.edit_message_text(
            chat_id=update.effective_chat.id,
            message_id=msg.message_id,
            text=f"❌ Timeout posting comment to issue #{issue_num}",
        )
    except subprocess.CalledProcessError as exc:
        error = exc.stderr if exc.stderr else str(exc)
        await context.bot.edit_message_text(
            chat_id=update.effective_chat.id,
            message_id=msg.message_id,
            text=f"❌ Failed to post comment: {error}",
        )
    except Exception as exc:
        await context.bot.edit_message_text(
            chat_id=update.effective_chat.id,
            message_id=msg.message_id,
            text=f"❌ Error: {exc}",
        )

"""Workflow command handlers extracted from telegram_bot."""

from __future__ import annotations

import os
import re
import json
from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Dict, List, Optional, Tuple

from telegram import Update
from telegram.ext import ContextTypes

from config import NEXUS_CORE_STORAGE_DIR
from state_manager import StateManager
from runtime.agent_launcher import clear_launch_guard


@dataclass
class WorkflowHandlerDeps:
    logger: Any
    allowed_user_id: Optional[int]
    base_dir: str
    default_repo: str
    project_config: Dict[str, Dict[str, Any]]
    workflow_state_plugin_kwargs: Dict[str, Any]
    prompt_project_selection: Callable[[Update, ContextTypes.DEFAULT_TYPE, str], Awaitable[None]]
    ensure_project_issue: Callable[
        [Update, ContextTypes.DEFAULT_TYPE, str], Awaitable[Tuple[Optional[str], Optional[str], List[str]]]
    ]
    find_task_file_by_issue: Callable[[str], Optional[str]]
    project_repo: Callable[[str], str]
    get_issue_details: Callable[[str, Optional[str]], Optional[Dict[str, Any]]]
    resolve_project_config_from_task: Callable[[str], Tuple[Optional[str], Optional[Dict[str, Any]]]]
    invoke_copilot_agent: Callable[..., Tuple[Optional[int], Optional[str]]]
    get_sop_tier_from_issue: Callable[[str, Optional[str]], Optional[str]]
    get_sop_tier: Callable[[str], Tuple[str, Any, Any]]
    get_last_tier_for_issue: Callable[[str], Optional[str]]
    prepare_continue_context: Callable[..., Dict[str, Any]]
    kill_issue_agent: Callable[..., Dict[str, Any]]
    get_runtime_ops_plugin: Callable[..., Any]
    get_workflow_state_plugin: Callable[..., Any]
    scan_for_completions: Callable[[str], List[Any]]
    normalize_agent_reference: Callable[[Optional[str]], Optional[str]]
    get_expected_running_agent_from_workflow: Callable[[str], Optional[str]]
    reconcile_issue_from_signals: Callable[..., Awaitable[Dict[str, Any]]]
    get_direct_issue_plugin: Callable[[str], Any]
    extract_structured_completion_signals: Callable[[List[dict]], List[Dict[str, str]]]
    write_local_completion_from_signal: Callable[[str, str, Dict[str, str]], str]
    build_workflow_snapshot: Callable[..., Dict[str, Any]]
    read_latest_local_completion: Callable[[str], Optional[Dict[str, Any]]]
    workflow_pause_handler: Callable[[Update, ContextTypes.DEFAULT_TYPE], Awaitable[None]]
    workflow_resume_handler: Callable[[Update, ContextTypes.DEFAULT_TYPE], Awaitable[None]]
    workflow_stop_handler: Callable[[Update, ContextTypes.DEFAULT_TYPE], Awaitable[None]]


async def reprocess_handler(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    deps: WorkflowHandlerDeps,
) -> None:
    deps.logger.info(f"Reprocess requested by user: {update.effective_user.id}")
    if deps.allowed_user_id and update.effective_user.id != deps.allowed_user_id:
        deps.logger.warning(f"Unauthorized access attempt by ID: {update.effective_user.id}")
        return

    if not context.args:
        await deps.prompt_project_selection(update, context, "reprocess")
        return

    project_key, issue_num, _ = await deps.ensure_project_issue(update, context, "reprocess")
    if not project_key:
        return

    task_file = deps.find_task_file_by_issue(issue_num)
    details = None
    repo = None
    if not task_file:
        repo = deps.project_repo(project_key)
        details = deps.get_issue_details(issue_num, repo=repo)
        if not details:
            await update.effective_message.reply_text(f"❌ Could not load issue #{issue_num}.")
            return
        body = details.get("body", "")
        match = re.search(r"Task File:\s*`([^`]+)`", body)
        task_file = match.group(1) if match else None

    if not task_file:
        await update.effective_message.reply_text(f"❌ Task file not found for issue #{issue_num}.")
        return
    if not os.path.exists(task_file):
        await update.effective_message.reply_text(f"❌ Task file missing: {task_file}")
        return

    project_name, config = deps.resolve_project_config_from_task(task_file)
    if not config or not config.get("agents_dir"):
        name = project_name or "unknown"
        await update.effective_message.reply_text(f"❌ No agents config for project '{name}'.")
        return

    from integrations.git_platform_utils import build_issue_url, resolve_repo

    repo = resolve_repo(config, deps.default_repo)
    if not details:
        details = deps.get_issue_details(issue_num, repo=repo)
        if not details:
            await update.effective_message.reply_text(f"❌ Could not load issue #{issue_num}.")
            return

    if details.get("state") == "closed":
        await update.effective_message.reply_text(
            f"⚠️ Issue #{issue_num} is closed. Reprocess only applies to open issues."
        )
        return

    with open(task_file, "r", encoding="utf-8") as handle:
        content = handle.read()

    type_match = re.search(r"\*\*Type:\*\*\s*(.+)", content)
    task_type = type_match.group(1).strip().lower() if type_match else "feature"

    tracker_tier = deps.get_last_tier_for_issue(issue_num)
    label_tier = deps.get_sop_tier_from_issue(issue_num, project_name or project_key)
    tier_name = label_tier or tracker_tier
    if not tier_name:
        await update.effective_message.reply_text(
            f"⚠️ Cannot determine workflow tier for issue #{issue_num}.\n"
            f"Add a `workflow:` label (e.g. `workflow:full`) to the issue and retry."
        )
        return

    issue_url = build_issue_url(repo, issue_num, config)

    msg = await update.effective_message.reply_text(f"🔁 Reprocessing issue #{issue_num}...")

    agents_abs = os.path.join(deps.base_dir, config["agents_dir"])
    workspace_abs = os.path.join(deps.base_dir, config["workspace"])

    log_subdir = project_name or project_key
    pid, tool_used = deps.invoke_copilot_agent(
        agents_dir=agents_abs,
        workspace_dir=workspace_abs,
        issue_url=issue_url,
        tier_name=tier_name,
        task_content=content,
        log_subdir=log_subdir,
        project_name=log_subdir,
    )

    if pid:
        await context.bot.edit_message_text(
            chat_id=update.effective_chat.id,
            message_id=msg.message_id,
            text=(
                f"✅ Reprocess started for issue #{issue_num}. Agent PID: {pid} (Tool: {tool_used})\n\n"
                f"🔗 {issue_url}"
            ),
        )
    else:
        await context.bot.edit_message_text(
            chat_id=update.effective_chat.id,
            message_id=msg.message_id,
            text=f"❌ Failed to launch reprocess for issue #{issue_num}.",
        )


async def continue_handler(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    deps: WorkflowHandlerDeps,
) -> None:
    deps.logger.info(f"Continue requested by user: {update.effective_user.id}")
    if deps.allowed_user_id and update.effective_user.id != deps.allowed_user_id:
        deps.logger.warning(f"Unauthorized access attempt by ID: {update.effective_user.id}")
        return

    if not context.args:
        await deps.prompt_project_selection(update, context, "continue")
        return

    project_key, issue_num, rest = await deps.ensure_project_issue(update, context, "continue")
    if not project_key:
        return

    continue_ctx = deps.prepare_continue_context(
        issue_num=issue_num,
        project_key=project_key,
        rest_tokens=rest or [],
        base_dir=deps.base_dir,
        project_config=deps.project_config,
        default_repo=deps.default_repo,
        find_task_file_by_issue=deps.find_task_file_by_issue,
        get_issue_details=deps.get_issue_details,
        resolve_project_config_from_task=deps.resolve_project_config_from_task,
        get_runtime_ops_plugin=deps.get_runtime_ops_plugin,
        scan_for_completions=deps.scan_for_completions,
        normalize_agent_reference=deps.normalize_agent_reference,
        get_expected_running_agent_from_workflow=deps.get_expected_running_agent_from_workflow,
        get_sop_tier_from_issue=deps.get_sop_tier_from_issue,
        get_sop_tier=deps.get_sop_tier,
    )

    if continue_ctx["status"] in {"error", "already_running", "mismatch", "workflow_done_closed"}:
        await update.effective_message.reply_text(continue_ctx["message"], parse_mode="Markdown")
        return

    if continue_ctx["status"] == "workflow_done_open":
        msg = await update.effective_message.reply_text(
            f"✅ Workflow complete for issue #{issue_num} (last agent: `{continue_ctx['resumed_from']}`)\n"
            f"Issue is still open — running finalization now..."
        )
        try:
            from inbox_processor import _finalize_workflow

            _finalize_workflow(
                issue_num,
                continue_ctx["repo"],
                continue_ctx["resumed_from"],
                continue_ctx["project_name"],
            )
            await context.bot.edit_message_text(
                chat_id=update.effective_chat.id,
                message_id=msg.message_id,
                text=(
                    f"✅ Workflow complete for issue #{issue_num}\n"
                    f"Last agent: `{continue_ctx['resumed_from']}`\n"
                    "Issue finalized (closed + PR if applicable)."
                ),
            )
        except Exception as exc:
            deps.logger.error(f"Finalization failed for issue #{issue_num}: {exc}", exc_info=True)
            await context.bot.edit_message_text(
                chat_id=update.effective_chat.id,
                message_id=msg.message_id,
                text=f"⚠️ Finalization error for issue #{issue_num}: {exc}",
            )
        return

    if continue_ctx["status"] != "ready":
        await update.effective_message.reply_text(
            f"⚠️ Unexpected continue state: {continue_ctx['status']}"
        )
        return

    if continue_ctx.get("forced_agent_override"):
        workflow_plugin = deps.get_workflow_state_plugin(
            **deps.workflow_state_plugin_kwargs,
            cache_key="workflow:state-engine",
        )
        reset_ok = False
        if workflow_plugin:
            try:
                reset_ok = await workflow_plugin.reset_to_agent_for_issue(
                    issue_num,
                    continue_ctx["agent_type"],
                )
            except Exception as exc:
                deps.logger.error(
                    "Failed to reset workflow state for issue #%s to %s: %s",
                    issue_num,
                    continue_ctx["agent_type"],
                    exc,
                    exc_info=True,
                )
        if not reset_ok:
            await update.effective_message.reply_text(
                f"❌ Could not reset workflow to `{continue_ctx['agent_type']}` for issue #{issue_num}."
            )
            return

    resume_info = f" (after {continue_ctx['resumed_from']})" if continue_ctx["resumed_from"] else ""
    msg = await update.effective_message.reply_text(
        f"⏩ Continuing issue #{issue_num} with `{continue_ctx['agent_type']}`{resume_info}..."
    )

    pid, tool_used = deps.invoke_copilot_agent(
        agents_dir=continue_ctx["agents_abs"],
        workspace_dir=continue_ctx["workspace_abs"],
        issue_url=continue_ctx["issue_url"],
        tier_name=continue_ctx["tier_name"],
        task_content=continue_ctx["content"],
        continuation=True,
        continuation_prompt=continue_ctx["continuation_prompt"],
        log_subdir=continue_ctx["log_subdir"],
        agent_type=continue_ctx["agent_type"],
        project_name=continue_ctx["log_subdir"],
    )

    if pid:
        await context.bot.edit_message_text(
            chat_id=update.effective_chat.id,
            message_id=msg.message_id,
            text=(
                f"✅ Agent continued for issue #{issue_num}. PID: {pid} (Tool: {tool_used})\n\n"
                f"Prompt: {continue_ctx['continuation_prompt']}\n\n"
                "ℹ️ **Note:** The agent will first check if the workflow has already progressed.\n"
                "If another agent is already handling the next step, this agent will exit gracefully.\n"
                "Use `/continue` only when an agent is truly stuck mid-step.\n\n"
                f"🔗 {continue_ctx['issue_url']}"
            ),
        )
    else:
        await context.bot.edit_message_text(
            chat_id=update.effective_chat.id,
            message_id=msg.message_id,
            text=f"❌ Failed to continue agent for issue #{issue_num}.",
        )


async def kill_handler(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    deps: WorkflowHandlerDeps,
) -> None:
    deps.logger.info(f"Kill requested by user: {update.effective_user.id}")
    if deps.allowed_user_id and update.effective_user.id != deps.allowed_user_id:
        deps.logger.warning(f"Unauthorized access attempt by ID: {update.effective_user.id}")
        return

    if not context.args:
        await deps.prompt_project_selection(update, context, "kill")
        return

    project_key, issue_num, _ = await deps.ensure_project_issue(update, context, "kill")
    if not project_key:
        return

    kill_result = deps.kill_issue_agent(issue_num=issue_num, get_runtime_ops_plugin=deps.get_runtime_ops_plugin)
    if kill_result["status"] == "not_running":
        await update.effective_message.reply_text(kill_result["message"])
        return

    msg = await update.effective_message.reply_text(
        f"🔪 Killing agent for issue #{issue_num} (PID: {kill_result.get('pid', 'n/a')})..."
    )

    if kill_result["status"] == "killed":
        text = f"✅ Agent killed (PID: {kill_result['pid']}).\n\nUse /reprocess {issue_num} to restart."
    elif kill_result["status"] == "stopped":
        text = f"✅ Agent stopped (PID: {kill_result['pid']}).\n\nUse /reprocess {issue_num} to restart."
    else:
        text = f"❌ Error: {kill_result.get('message', 'Unknown kill error')}"

    await context.bot.edit_message_text(
        chat_id=update.effective_chat.id,
        message_id=msg.message_id,
        text=text,
    )


async def reconcile_handler(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    deps: WorkflowHandlerDeps,
) -> None:
    deps.logger.info(f"Reconcile requested by user: {update.effective_user.id}")
    if deps.allowed_user_id and update.effective_user.id != deps.allowed_user_id:
        deps.logger.warning(f"Unauthorized access attempt by ID: {update.effective_user.id}")
        return

    if not context.args:
        await deps.prompt_project_selection(update, context, "reconcile")
        return

    project_key, issue_num, _ = await deps.ensure_project_issue(update, context, "reconcile")
    if not project_key:
        return

    repo = deps.project_repo(project_key)

    msg = await update.effective_message.reply_text(
        f"🔄 Reconciling issue #{issue_num} from structured GitHub comments..."
    )

    result = await deps.reconcile_issue_from_signals(
        issue_num=issue_num,
        project_key=project_key,
        repo=repo,
        get_issue_plugin=deps.get_direct_issue_plugin,
        extract_structured_completion_signals=deps.extract_structured_completion_signals,
        workflow_state_plugin_kwargs=deps.workflow_state_plugin_kwargs,
        write_local_completion_from_signal=deps.write_local_completion_from_signal,
    )

    if not result.get("ok"):
        await context.bot.edit_message_text(
            chat_id=update.effective_chat.id,
            message_id=msg.message_id,
            text=f"⚠️ {result.get('error', 'Reconcile failed.')}",
        )
        return

    await context.bot.edit_message_text(
        chat_id=update.effective_chat.id,
        message_id=msg.message_id,
        text=(
            f"✅ Reconcile completed for issue #{issue_num}\n\n"
            f"Signals scanned: {result['signals_scanned']}\n"
            f"Signals applied to workflow: {result['signals_applied']}\n"
            f"Local completion updated: `{result['completion_file']}`\n"
            f"Current workflow: `{result['workflow_state']}` | "
            f"Step {result['workflow_step']} | Agent `{result['workflow_agent']}`"
        ),
        parse_mode="Markdown",
    )


async def wfstate_handler(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    deps: WorkflowHandlerDeps,
) -> None:
    deps.logger.info(f"Wfstate requested by user: {update.effective_user.id}")
    if deps.allowed_user_id and update.effective_user.id != deps.allowed_user_id:
        deps.logger.warning(f"Unauthorized access attempt by ID: {update.effective_user.id}")
        return

    if not context.args:
        await deps.prompt_project_selection(update, context, "wfstate")
        return

    project_key, issue_num, _ = await deps.ensure_project_issue(update, context, "wfstate")
    if not project_key:
        return

    repo = deps.project_repo(project_key)

    msg = await update.effective_message.reply_text(f"📊 Inspecting workflow state for issue #{issue_num}...")

    expected_running = deps.normalize_agent_reference(
        deps.get_expected_running_agent_from_workflow(issue_num) or ""
    )
    snapshot = deps.build_workflow_snapshot(
        issue_num=issue_num,
        repo=repo,
        get_issue_plugin=deps.get_direct_issue_plugin,
        expected_running_agent=expected_running,
        find_task_file_by_issue=deps.find_task_file_by_issue,
        read_latest_local_completion=deps.read_latest_local_completion,
        extract_structured_completion_signals=deps.extract_structured_completion_signals,
    )

    processor_signal = snapshot.get("processor_signal") or {}
    processor_type = processor_signal.get("type", "n/a")
    processor_severity = processor_signal.get("severity", "n/a")
    processor_at = processor_signal.get("timestamp", "n/a")
    processor_line = processor_signal.get("line", "n/a")

    recovery_hint = "none"
    if processor_type == "completion_mismatch":
        recovery_hint = "stale completion signal. Run /reconcile then /continue"
    elif processor_type in {"signal_drift", "retry_fuse", "pause_failed"}:
        recovery_hint = "workflow drift. Run /wfstate, then /reconcile and /continue"

    lines = [
        f"📊 Workflow Snapshot — Issue #{issue_num}",
        "",
        f"Repo: {snapshot['repo']}",
        f"Workflow ID: {snapshot['workflow_id'] or 'n/a'}",
        f"Workflow State: {snapshot['workflow_state']}",
        f"Current Step: {snapshot['current_step']} ({snapshot['current_step_name']})",
        f"Current Agent: {snapshot['current_agent']}",
        f"Expected RUNNING Agent: {snapshot['expected_running_agent'] or expected_running or 'n/a'}",
        "",
        f"Process: {'running' if snapshot['running'] else 'stopped'}",
        f"PID: {snapshot['pid'] or 'n/a'}",
        f"Task File: {snapshot['task_file'] or 'n/a'}",
        f"Workflow File: {snapshot['workflow_file'] or 'n/a'}",
        "",
        "Local Completion:",
        f"- from: {snapshot['local_from'] or 'n/a'}",
        f"- next: {snapshot['local_next'] or 'n/a'}",
        f"- status: {(snapshot['local'] or {}).get('status', 'n/a') if snapshot['local'] else 'n/a'}",
        f"- updated: {(snapshot['local'] or {}).get('mtime', 'n/a') if snapshot['local'] else 'n/a'}",
        f"- file: {(snapshot['local'] or {}).get('path', 'n/a') if snapshot['local'] else 'n/a'}",
        "",
        "Latest Structured Comment:",
        f"- from: {snapshot['comment_from'] or 'n/a'}",
        f"- next: {snapshot['comment_next'] or 'n/a'}",
        f"- comment_id: {(snapshot['latest_signal'] or {}).get('comment_id', 'n/a') if snapshot['latest_signal'] else 'n/a'}",
        f"- created: {(snapshot['latest_signal'] or {}).get('created', 'n/a') if snapshot['latest_signal'] else 'n/a'}",
        "",
        "Latest Processor Signal:",
        f"- type: {processor_type}",
        f"- severity: {processor_severity}",
        f"- at: {processor_at}",
        f"- detail: {processor_line}",
        "",
        f"Recovery Hint: {recovery_hint}",
        "",
        f"Drift Flags: {', '.join(snapshot['drift_flags']) if snapshot['drift_flags'] else 'none'}",
    ]

    if snapshot.get("workflow_pointer_mismatch"):
        lines.extend(
            [
                "",
                "⚠️ Workflow Pointer Mismatch:",
                f"- indexed step: {snapshot['indexed_step']} ({snapshot['indexed_step_name']}) / {snapshot['indexed_agent']}",
                f"- running step: {snapshot['running_step']} ({snapshot['running_step_name']}) / {snapshot['running_agent']}",
            ]
        )

    await context.bot.edit_message_text(
        chat_id=update.effective_chat.id,
        message_id=msg.message_id,
        text="\n".join(lines),
    )


async def pause_handler(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    deps: WorkflowHandlerDeps,
) -> None:
    if not context.args:
        await deps.prompt_project_selection(update, context, "pause")
        return

    project_key, issue_num, _ = await deps.ensure_project_issue(update, context, "pause")
    if not project_key:
        return

    context.args = [project_key, issue_num]
    await deps.workflow_pause_handler(update, context)


async def resume_handler(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    deps: WorkflowHandlerDeps,
) -> None:
    if not context.args:
        await deps.prompt_project_selection(update, context, "resume")
        return

    project_key, issue_num, _ = await deps.ensure_project_issue(update, context, "resume")
    if not project_key:
        return

    context.args = [project_key, issue_num]
    await deps.workflow_resume_handler(update, context)


async def stop_handler(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    deps: WorkflowHandlerDeps,
) -> None:
    if not context.args:
        await deps.prompt_project_selection(update, context, "stop")
        return

    project_key, issue_num, _ = await deps.ensure_project_issue(update, context, "stop")
    if not project_key:
        return

    context.args = [project_key, issue_num]
    await deps.workflow_stop_handler(update, context)


async def forget_handler(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    deps: WorkflowHandlerDeps,
) -> None:
    deps.logger.info(f"Forget requested by user: {update.effective_user.id}")
    if deps.allowed_user_id and update.effective_user.id != deps.allowed_user_id:
        deps.logger.warning(f"Unauthorized access attempt by ID: {update.effective_user.id}")
        return

    if not context.args:
        await deps.prompt_project_selection(update, context, "forget")
        return

    project_key, issue_num, _ = await deps.ensure_project_issue(update, context, "forget")
    if not project_key:
        return

    if project_key not in deps.project_config:
        await update.effective_message.reply_text("❌ Invalid project.")
        return

    workflow_id = StateManager.get_workflow_id_for_issue(str(issue_num))
    workflow_file_deleted = False
    if workflow_id:
        workflow_file = os.path.join(NEXUS_CORE_STORAGE_DIR, "workflows", f"{workflow_id}.json")
        if os.path.exists(workflow_file):
            try:
                os.remove(workflow_file)
                workflow_file_deleted = True
            except OSError as exc:
                deps.logger.warning(
                    "Failed to delete workflow file for issue #%s: %s",
                    issue_num,
                    exc,
                )

    runtime_ops = deps.get_runtime_ops_plugin(cache_key="runtime-ops:workflow")
    pid = runtime_ops.find_agent_pid_for_issue(issue_num) if runtime_ops else None
    killed = False
    if pid and runtime_ops:
        killed = bool(runtime_ops.kill_process(pid, force=True))

    launched = StateManager.load_launched_agents(recent_only=False)
    launched_removed = launched.pop(str(issue_num), None) is not None
    StateManager.save_launched_agents(launched)

    tracked = StateManager.load_tracked_issues()
    tracked_removed = tracked.pop(str(issue_num), None) is not None
    StateManager.save_tracked_issues(tracked)

    StateManager.remove_workflow_mapping(str(issue_num))
    StateManager.clear_pending_approval(str(issue_num))

    cleared_guards = clear_launch_guard(str(issue_num))

    try:
        completion_path = os.path.join(os.path.dirname(NEXUS_CORE_STORAGE_DIR), "completion_comments.json")
        with open(completion_path, "r", encoding="utf-8") as handle:
            completion_data = json.load(handle) or {}
        if isinstance(completion_data, dict):
            to_delete = [key for key in completion_data.keys() if key.startswith(f"{issue_num}:")]
            for key in to_delete:
                completion_data.pop(key, None)
            if to_delete:
                with open(completion_path, "w", encoding="utf-8") as handle:
                    json.dump(completion_data, handle)
    except Exception as exc:
        deps.logger.debug("completion_comments cleanup skipped for issue #%s: %s", issue_num, exc)

    await update.effective_message.reply_text(
        "🧹 **Issue state forgotten**\n\n"
        f"Issue: #{issue_num}\n"
        f"Project: {project_key}\n"
        f"Workflow mapping: cleared{' (file deleted)' if workflow_file_deleted else ''}\n"
        f"Tracker state: {'removed' if launched_removed else 'not present'}\n"
        f"Tracked issue: {'removed' if tracked_removed else 'not present'}\n"
        f"Running PID: {'killed' if killed else ('found but kill failed' if pid else 'none')}\n"
        f"Launch guards cleared: {cleared_guards}\n\n"
        "This issue will no longer auto-retry or emit orphan notifications unless relaunched manually."
    )

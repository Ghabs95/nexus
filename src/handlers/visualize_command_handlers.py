"""Visualize command handler — renders a Mermaid workflow diagram for an issue."""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from io import BytesIO
from typing import Any, Awaitable, Callable, Dict, List, Optional, Tuple

from telegram import Update
from telegram.ext import ContextTypes

from config import NEXUS_CORE_STORAGE_DIR
from state_manager import StateManager
from utils.log_utils import log_unauthorized_access
from services.mermaid_render_service import build_mermaid_diagram, render_mermaid_to_png


@dataclass
class VisualizeHandlerDeps:
    logger: Any
    allowed_user_ids: List[int]
    prompt_project_selection: Callable[[Update, ContextTypes.DEFAULT_TYPE, str], Awaitable[None]]
    ensure_project_issue: Callable[
        [Update, ContextTypes.DEFAULT_TYPE, str], Awaitable[Tuple[Optional[str], Optional[str], List[str]]]
    ]


async def visualize_handler(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    deps: VisualizeHandlerDeps,
) -> None:
    """Handle /visualize [project_key] [issue#] — send a Mermaid workflow diagram."""
    deps.logger.info("Visualize requested by user: %s", update.effective_user.id)
    if deps.allowed_user_ids and update.effective_user.id not in deps.allowed_user_ids:
        log_unauthorized_access(getattr(deps, "logger", None), update.effective_user.id)
        return

    if not context.args:
        await deps.prompt_project_selection(update, context, "visualize")
        return

    project_key, issue_num, _ = await deps.ensure_project_issue(update, context, "visualize")
    if not project_key:
        return

    msg = await update.effective_message.reply_text(
        f"🎨 Generating workflow diagram for issue #{issue_num}..."
    )

    # Load workflow steps from the workflow JSON file
    workflow_id = StateManager.get_workflow_id_for_issue(issue_num)
    steps: List[Dict[str, Any]] = []
    workflow_state = "unknown"

    if workflow_id:
        workflow_file = os.path.join(NEXUS_CORE_STORAGE_DIR, "workflows", f"{workflow_id}.json")
        if os.path.exists(workflow_file):
            try:
                with open(workflow_file, "r", encoding="utf-8") as fh:
                    payload = json.load(fh)
                steps = payload.get("steps", [])
                workflow_state = str(payload.get("state", "unknown"))
            except Exception as exc:
                deps.logger.warning("visualize: failed to read workflow file for #%s: %s", issue_num, exc)

    if not steps:
        await context.bot.edit_message_text(
            chat_id=update.effective_chat.id,
            message_id=msg.message_id,
            text=f"⚠️ No workflow steps found for issue #{issue_num}. Has a workflow been started?",
        )
        return

    diagram_text = build_mermaid_diagram(steps, issue_num)
    caption = f"📊 Workflow diagram — Issue #{issue_num} · state: {workflow_state}"

    png_bytes = await render_mermaid_to_png(diagram_text)

    if png_bytes:
        await context.bot.delete_message(
            chat_id=update.effective_chat.id,
            message_id=msg.message_id,
        )
        await update.effective_message.reply_photo(
            photo=BytesIO(png_bytes),
            caption=caption,
        )
    else:
        # Fallback: send raw Mermaid as a fenced code block
        fallback_text = f"{caption}\n\n```mermaid\n{diagram_text}\n```"
        await context.bot.edit_message_text(
            chat_id=update.effective_chat.id,
            message_id=msg.message_id,
            text=fallback_text,
            parse_mode="Markdown",
        )

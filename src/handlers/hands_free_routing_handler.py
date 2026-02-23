"""Hands-free message intent routing extracted from telegram_bot."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Dict, Optional

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import ContextTypes


@dataclass
class HandsFreeRoutingDeps:
    logger: Any
    orchestrator: Any
    ai_persona: str
    projects: Dict[str, str]
    extract_json_dict: Callable[[str], Dict[str, Any] | None]
    get_chat_history: Callable[[int], str]
    append_message: Callable[[int, str, str], None]
    get_chat: Callable[[int], Dict[str, Any]]
    process_inbox_task: Callable[[str, Any, str], Awaitable[Dict[str, Any]]]
    normalize_project_key: Callable[[str], Optional[str]]
    save_resolved_task: Callable[[dict, str, str], Awaitable[Dict[str, Any]]]
    task_confirmation_mode: str


def _normalize_allowed_agent_types(metadata: Dict[str, Any]) -> list[str]:
    allowed = metadata.get("allowed_agent_types")
    if not isinstance(allowed, list):
        return []
    cleaned: list[str] = []
    for item in allowed:
        if isinstance(item, str) and item.strip():
            cleaned.append(item.strip())
    return cleaned


def _detect_conversation_intent(text: str) -> str:
    candidate = (text or "").lower()
    if not candidate:
        return "general"

    gtm_terms = [
        "go to market",
        "gtm",
        "positioning",
        "campaign",
        "acquisition",
        "funnel",
        "brand",
        "launch plan",
        "channel strategy",
    ]
    business_terms = [
        "revenue",
        "pricing",
        "profit",
        "margin",
        "monetization",
        "kpi",
        "retention",
        "business model",
        "roi",
        "unit economics",
    ]
    strategy_terms = [
        "vision",
        "roadmap",
        "priorit",
        "north star",
        "objective",
        "strategy",
        "direction",
        "focus",
        "decision",
        "tradeoff",
    ]

    if any(term in candidate for term in gtm_terms):
        return "gtm"
    if any(term in candidate for term in business_terms):
        return "business"
    if any(term in candidate for term in strategy_terms):
        return "strategy"
    return "general"


def _select_conversation_agent_type(metadata: Dict[str, Any], text: str) -> tuple[str, str, str]:
    allowed_agent_types = _normalize_allowed_agent_types(metadata)
    primary_agent_type = str(metadata.get("primary_agent_type") or "").strip().lower()
    if not primary_agent_type:
        primary_agent_type = allowed_agent_types[0] if allowed_agent_types else "advisor"
    chat_mode = str(metadata.get("chat_mode", "strategy")).lower().strip() or "strategy"
    intent = _detect_conversation_intent(text)
    business_role = "advisor" if "advisor" in allowed_agent_types else "business"

    preferred_by_intent = {
        "gtm": "marketing",
        "business": business_role,
        "strategy": primary_agent_type,
        "general": primary_agent_type,
    }
    execution_overrides = {
        "gtm": "marketing",
        "business": business_role,
        "strategy": business_role,
        "general": primary_agent_type,
    }

    preferred = preferred_by_intent.get(intent, primary_agent_type)
    if chat_mode == "execution":
        preferred = execution_overrides.get(intent, preferred)

    if allowed_agent_types:
        if preferred in allowed_agent_types:
            return preferred, intent, f"intent={intent}, mode={chat_mode}, allowed_match"
        if primary_agent_type in allowed_agent_types:
            return primary_agent_type, intent, f"intent={intent}, mode={chat_mode}, primary_fallback"
        return allowed_agent_types[0], intent, f"intent={intent}, mode={chat_mode}, first_allowed_fallback"

    return preferred, intent, f"intent={intent}, mode={chat_mode}, unrestricted"


def _build_chat_persona(
    deps: HandsFreeRoutingDeps,
    user_id: int,
    routed_agent_type: str,
    detected_intent: str,
    routing_reason: str,
) -> str:
    base_persona = deps.ai_persona or "You are a helpful AI assistant."
    chat_data = deps.get_chat(user_id) or {}
    metadata = chat_data.get("metadata") or {}

    project_key = metadata.get("project_key")
    project_label = deps.projects.get(project_key, project_key or "Not set")
    chat_mode = str(metadata.get("chat_mode", "strategy"))
    primary_agent_type = str(metadata.get("primary_agent_type") or "").strip().lower()
    if not primary_agent_type:
        allowed = _normalize_allowed_agent_types(metadata)
        primary_agent_type = allowed[0] if allowed else "advisor"

    context_block = (
        "\n\nActive Chat Context:\n"
        f"- Project: {project_label} ({project_key or 'none'})\n"
        f"- Chat mode: {chat_mode}\n"
        f"- Primary agent_type: {primary_agent_type}\n"
        f"- Routed agent_type: {routed_agent_type}\n"
        f"- Detected intent: {detected_intent}\n"
        f"- Routing reason: {routing_reason}\n"
        "Behavior rules:\n"
        f"- Respond in the voice and decision style of `{routed_agent_type}`.\n"
        "- Keep recommendations scoped to the active project context.\n"
        "- If context is missing, ask a short clarification before making assumptions."
    )
    return f"{base_persona}{context_block}"


async def resolve_pending_project_selection(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    deps: HandsFreeRoutingDeps,
) -> bool:
    pending_project = context.user_data.get("pending_task_project_resolution")
    if not pending_project:
        return False

    selected = deps.normalize_project_key((update.message.text or "").strip())
    if not selected or selected not in deps.projects:
        options = ", ".join(sorted(deps.projects.keys()))
        await update.message.reply_text(f"Please reply with a valid project key: {options}")
        return True

    context.user_data.pop("pending_task_project_resolution", None)
    result = await deps.save_resolved_task(pending_project, selected, str(update.message.message_id))
    await update.message.reply_text(result["message"], parse_mode="Markdown")
    return True


async def route_hands_free_text(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    status_msg: Any,
    text: str,
    deps: HandsFreeRoutingDeps,
) -> None:
    deps.logger.info("Detecting intent for: %s...", text[:50])
    intent_result = deps.orchestrator.run_text_to_speech_analysis(text=text, task="detect_intent")

    if isinstance(intent_result, dict) and intent_result.get("text"):
        needs_reparse = intent_result.get("parse_error") or "intent" not in intent_result
        if needs_reparse:
            reparsed = deps.extract_json_dict(intent_result["text"])
            if reparsed:
                intent_result = reparsed

    intent = intent_result.get("intent", "task")

    if intent == "conversation":
        user_id = update.effective_user.id
        history = deps.get_chat_history(user_id)
        deps.append_message(user_id, "user", text)
        chat_data = deps.get_chat(user_id) or {}
        metadata = chat_data.get("metadata") if isinstance(chat_data, dict) else {}
        metadata = metadata if isinstance(metadata, dict) else {}
        routed_agent_type, detected_intent, routing_reason = _select_conversation_agent_type(metadata, text)

        deps.logger.info(
            "Conversation routing selected agent_type=%s (%s)",
            routed_agent_type,
            routing_reason,
        )
        persona = _build_chat_persona(
            deps,
            user_id,
            routed_agent_type,
            detected_intent,
            routing_reason,
        )

        await context.bot.edit_message_text(
            chat_id=update.effective_chat.id,
            message_id=status_msg.message_id,
            text="🤖 *Nexus:* Thinking...",
            parse_mode="Markdown",
        )

        chat_result = deps.orchestrator.run_text_to_speech_analysis(
            text=text,
            task="advisor_chat",
            history=history,
            persona=persona,
        )

        reply_text = chat_result.get("text", "I'm offline right now, how can I help later?")
        deps.append_message(user_id, "assistant", reply_text)

        await context.bot.edit_message_text(
            chat_id=update.effective_chat.id,
            message_id=status_msg.message_id,
            text=f"🤖 *Nexus ({routed_agent_type})*: \n\n{reply_text}",
            parse_mode="Markdown",
        )
        return

    confirmation_mode = str(deps.task_confirmation_mode or "smart").strip().lower()
    if confirmation_mode not in {"off", "always", "smart"}:
        confirmation_mode = "smart"

    confidence = intent_result.get("confidence") if isinstance(intent_result, dict) else None
    try:
        confidence_value = float(confidence) if confidence is not None else None
    except (TypeError, ValueError):
        confidence_value = None

    chat_data = deps.get_chat(update.effective_user.id) or {}
    metadata = chat_data.get("metadata") if isinstance(chat_data, dict) else {}
    metadata = metadata if isinstance(metadata, dict) else {}
    has_project_context = bool(metadata.get("project_key"))

    should_confirm = False
    if confirmation_mode == "always":
        should_confirm = True
    elif confirmation_mode == "smart":
        should_confirm = bool(
            update.message.voice
            or not has_project_context
            or (confidence_value is not None and confidence_value < 0.8)
        )

    if should_confirm:
        context.user_data["pending_task_confirmation"] = {
            "text": text,
            "message_id": str(update.message.message_id),
        }
        reason = "voice input" if update.message.voice else "auto-routing safety check"
        if not has_project_context:
            reason = "missing project context"
        elif confidence_value is not None and confidence_value < 0.8:
            reason = f"low intent confidence ({confidence_value:.2f})"

        preview = text if len(text) <= 300 else f"{text[:300]}..."
        keyboard = InlineKeyboardMarkup(
            [
                [InlineKeyboardButton("✅ Confirm", callback_data="taskconfirm:confirm")],
                [InlineKeyboardButton("✏️ Edit", callback_data="taskconfirm:edit")],
                [InlineKeyboardButton("❌ Cancel", callback_data="taskconfirm:cancel")],
            ]
        )
        await context.bot.edit_message_text(
            chat_id=update.effective_chat.id,
            message_id=status_msg.message_id,
            text=(
                "🛡️ *Confirm task creation*\n\n"
                f"Reason: {reason}\n"
                "I’m about to create a task from this request:\n\n"
                f"_{preview}_"
            ),
            reply_markup=keyboard,
            parse_mode="Markdown",
        )
        return

    result = await deps.process_inbox_task(text, deps.orchestrator, str(update.message.message_id))

    if not result["success"] and "pending_resolution" in result:
        context.user_data["pending_task_project_resolution"] = result["pending_resolution"]

    await context.bot.edit_message_text(
        chat_id=update.effective_chat.id,
        message_id=status_msg.message_id,
        text=result["message"],
    )

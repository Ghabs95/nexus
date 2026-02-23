"""Feature ideation chat/callback handlers extracted from telegram_bot."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Dict, List, Optional

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import ContextTypes


FEATURE_STATE_KEY = "feature_suggestions"


@dataclass
class FeatureIdeationHandlerDeps:
    logger: Any
    allowed_user_ids: List[int]
    projects: Dict[str, str]
    get_project_label: Callable[[str], str]
    orchestrator: Any


def is_feature_ideation_request(text: str) -> bool:
    candidate = (text or "").strip().lower()
    if not candidate:
        return False

    feature_terms = ["feature", "features", "implement", "add", "new"]
    if not any(term in candidate for term in feature_terms):
        return False

    triggers = [
        "new feature",
        "new features",
        "which feature",
        "which new",
        "what feature",
        "what new feature",
        "what can we add",
        "what should we add",
        "features can we add",
        "features should we add",
        "propose",
        "ideas",
        "roadmap",
    ]
    return any(trigger in candidate for trigger in triggers)


def detect_feature_project(text: str) -> Optional[str]:
    candidate = (text or "").strip().lower()
    if not candidate:
        return None

    aliases = {
        "nexus core": "nexus",
        "nexus-core": "nexus",
        "nexus": "nexus",
        "case italia": "case_italia",
        "wallible": "wallible",
        "biome": "biome",
        "casit": "case_italia",
        "wlbl": "wallible",
        "bm": "biome",
    }
    for alias, project_key in aliases.items():
        if alias in candidate:
            return project_key
    return None


def _requested_feature_count(text: str, default_count: int = 3, max_count: int = 5) -> int:
    candidate = (text or "").lower()
    if not candidate:
        return default_count

    if "top 5" in candidate or "max 5" in candidate or "five" in candidate:
        return max_count
    if "top 4" in candidate or "four" in candidate:
        return 4
    if "top 3" in candidate or "three" in candidate:
        return 3

    number_match = re.search(r"\b([1-9])\b", candidate)
    if number_match:
        requested = int(number_match.group(1))
        return max(1, min(max_count, requested))

    return default_count


def _extract_json_dict(raw_text: str) -> Optional[Dict[str, Any]]:
    if not raw_text:
        return None

    candidate = raw_text.strip()
    try:
        parsed = json.loads(candidate)
        if isinstance(parsed, dict):
            return parsed
    except Exception:
        pass

    fenced_match = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", candidate, flags=re.DOTALL)
    if fenced_match:
        try:
            parsed = json.loads(fenced_match.group(1))
            if isinstance(parsed, dict):
                return parsed
        except Exception:
            pass

    first_brace = candidate.find("{")
    last_brace = candidate.rfind("}")
    if first_brace >= 0 and last_brace > first_brace:
        snippet = candidate[first_brace : last_brace + 1]
        try:
            parsed = json.loads(snippet)
            if isinstance(parsed, dict):
                return parsed
        except Exception:
            pass
    return None


def _normalize_generated_features(items: Any, limit: int) -> List[Dict[str, Any]]:
    if not isinstance(items, list):
        return []

    normalized: List[Dict[str, Any]] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        title = str(item.get("title") or "").strip()
        summary = str(item.get("summary") or "").strip()
        why = str(item.get("why") or "").strip()
        steps = item.get("steps")
        if not title or not summary:
            continue
        if not isinstance(steps, list):
            steps = []
        cleaned_steps = [str(step).strip() for step in steps if isinstance(step, str) and step.strip()]
        normalized.append(
            {
                "title": title,
                "summary": summary,
                "why": why or "Aligns with roadmap, speed, and measurable impact.",
                "steps": cleaned_steps[:3],
            }
        )
        if len(normalized) >= limit:
            break
    return normalized


def _build_feature_suggestions(
    project_key: str,
    text: str,
    deps: FeatureIdeationHandlerDeps,
    preferred_agent_type: Optional[str],
    feature_count: int,
) -> List[Dict[str, Any]]:
    project_label = deps.get_project_label(project_key)
    routed_agent_type = str(preferred_agent_type or "advisor").strip().lower()
    persona = (
        "You are a senior product strategy assistant. "
        f"Respond in the perspective of agent_type `{routed_agent_type}` for project `{project_label}`.\n"
        "Return ONLY JSON with this schema:\n"
        "{\"items\":[{\"title\":\"...\",\"summary\":\"...\",\"why\":\"...\",\"steps\":[\"...\",\"...\",\"...\"]}]}\n"
        f"Generate exactly {feature_count} items. Keep each title concise and action-oriented."
    )

    def _extract_items_from_result(result: Dict[str, Any]) -> List[Dict[str, Any]]:
        if not isinstance(result, dict):
            return []

        if isinstance(result.get("items"), list):
            direct = _normalize_generated_features(result.get("items"), feature_count)
            if direct:
                return direct

        payload = _extract_json_dict(str(result.get("text", "")))
        return _normalize_generated_features((payload or {}).get("items"), feature_count)

    try:
        result = deps.orchestrator.run_text_to_speech_analysis(
            text=text,
            task="advisor_chat",
            persona=persona,
        )
        generated = _extract_items_from_result(result or {})
        if generated:
            return generated

        deps.logger.warning(
            "Dynamic feature ideation returned non-JSON/empty output (primary path), retrying with Copilot"
        )
    except Exception as exc:
        deps.logger.warning("Dynamic feature ideation failed on primary path: %s", exc)

    try:
        run_copilot = getattr(deps.orchestrator, "_run_copilot_analysis", None)
        if callable(run_copilot):
            copilot_result = run_copilot(text, task="advisor_chat", persona=persona)
            generated = _extract_items_from_result(copilot_result or {})
            if generated:
                return generated
            deps.logger.warning("Dynamic feature ideation Copilot retry returned non-JSON/empty output")
    except Exception as exc:
        deps.logger.warning("Dynamic feature ideation failed on Copilot retry: %s", exc)

    return []


def _feature_generation_retry_text(project_key: str, deps: FeatureIdeationHandlerDeps) -> str:
    return (
        f"⚠️ I couldn't generate feature proposals for *{deps.get_project_label(project_key)}* right now.\n\n"
        "Please try again."
    )


def _feature_list_text(
    project_key: str,
    features: List[Dict[str, Any]],
    deps: FeatureIdeationHandlerDeps,
    preferred_agent_type: Optional[str],
) -> str:
    agent_label = str(preferred_agent_type or "advisor")
    lines = [
        f"💡 *Feature proposals for {deps.get_project_label(project_key)}*",
        f"Perspective: `{agent_label}`",
        "",
        "Tap one option:",
    ]
    for index, item in enumerate(features, start=1):
        lines.append(f"{index}. *{item['title']}* — {item['summary']}")
    return "\n".join(lines)


def _feature_list_keyboard(features: List[Dict[str, Any]]) -> InlineKeyboardMarkup:
    keyboard = [
        [InlineKeyboardButton(item["title"], callback_data=f"feat:pick:{idx}")]
        for idx, item in enumerate(features)
    ]
    keyboard.append([InlineKeyboardButton("📁 Choose project", callback_data="feat:choose_project")])
    keyboard.append([InlineKeyboardButton("❌ Close", callback_data="flow:close")])
    return InlineKeyboardMarkup(keyboard)


def _feature_project_keyboard(deps: FeatureIdeationHandlerDeps) -> InlineKeyboardMarkup:
    keyboard = [
        [InlineKeyboardButton(deps.get_project_label(key), callback_data=f"feat:project:{key}")]
        for key in sorted(deps.projects.keys())
    ]
    keyboard.append([InlineKeyboardButton("❌ Close", callback_data="flow:close")])
    return InlineKeyboardMarkup(keyboard)


async def show_feature_project_picker(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    status_msg: Any,
    deps: FeatureIdeationHandlerDeps,
) -> None:
    context.user_data[FEATURE_STATE_KEY] = {
        "project": None,
        "items": [],
    }
    await context.bot.edit_message_text(
        chat_id=update.effective_chat.id,
        message_id=status_msg.message_id,
        text="📁 I couldn't detect the project. Select one to get 3 actionable feature proposals:",
        reply_markup=_feature_project_keyboard(deps),
    )


async def show_feature_suggestions(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    status_msg: Any,
    project_key: str,
    text: str,
    preferred_agent_type: Optional[str],
    feature_count: int,
    deps: FeatureIdeationHandlerDeps,
) -> None:
    features = _build_feature_suggestions(
        project_key=project_key,
        text=text,
        deps=deps,
        preferred_agent_type=preferred_agent_type,
        feature_count=feature_count,
    )
    context.user_data[FEATURE_STATE_KEY] = {
        "project": project_key,
        "items": features,
        "agent_type": preferred_agent_type,
        "feature_count": feature_count,
        "source_text": text,
    }

    if not features:
        await context.bot.edit_message_text(
            chat_id=update.effective_chat.id,
            message_id=status_msg.message_id,
            text=_feature_generation_retry_text(project_key, deps),
            reply_markup=InlineKeyboardMarkup(
                [
                    [InlineKeyboardButton("📁 Choose project", callback_data="feat:choose_project")],
                    [InlineKeyboardButton("❌ Close", callback_data="flow:close")],
                ]
            ),
            parse_mode="Markdown",
        )
        return

    await context.bot.edit_message_text(
        chat_id=update.effective_chat.id,
        message_id=status_msg.message_id,
        text=_feature_list_text(project_key, features, deps, preferred_agent_type),
        reply_markup=_feature_list_keyboard(features),
        parse_mode="Markdown",
    )


async def handle_feature_ideation_request(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    status_msg: Any,
    text: str,
    deps: FeatureIdeationHandlerDeps,
    preferred_project_key: Optional[str] = None,
    preferred_agent_type: Optional[str] = None,
) -> bool:
    if not is_feature_ideation_request(text):
        return False

    project_key = detect_feature_project(text)
    if not project_key and preferred_project_key in deps.projects:
        project_key = preferred_project_key
    if not project_key:
        await show_feature_project_picker(update, context, status_msg, deps)
        return True

    feature_count = _requested_feature_count(text)
    await show_feature_suggestions(
        update,
        context,
        status_msg,
        project_key,
        text,
        preferred_agent_type,
        feature_count,
        deps,
    )
    return True


async def feature_callback_handler(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    deps: FeatureIdeationHandlerDeps,
) -> None:
    query = update.callback_query
    if not query:
        return
    await query.answer()

    if deps.allowed_user_ids and update.effective_user.id not in deps.allowed_user_ids:
        deps.logger.warning(f"Unauthorized callback access attempt by ID: {update.effective_user.id}")
        return

    data = query.data or ""
    feature_state = context.user_data.get(FEATURE_STATE_KEY) or {}

    if data == "feat:choose_project":
        await query.edit_message_text(
            text="📁 Select a project to get 3 actionable feature proposals:",
            reply_markup=_feature_project_keyboard(deps),
        )
        return

    if data.startswith("feat:project:"):
        project_key = data.split(":", 2)[2]
        if project_key not in deps.projects:
            await query.edit_message_text("⚠️ Invalid project selection.")
            return
        preferred_agent_type = feature_state.get("agent_type")
        feature_count = int(feature_state.get("feature_count") or 3)
        source_text = str(feature_state.get("source_text") or "")
        features = _build_feature_suggestions(
            project_key=project_key,
            text=source_text,
            deps=deps,
            preferred_agent_type=preferred_agent_type,
            feature_count=max(1, min(5, feature_count)),
        )
        context.user_data[FEATURE_STATE_KEY] = {
            "project": project_key,
            "items": features,
            "agent_type": preferred_agent_type,
            "feature_count": feature_count,
            "source_text": source_text,
        }

        if not features:
            await query.edit_message_text(
                text=_feature_generation_retry_text(project_key, deps),
                reply_markup=InlineKeyboardMarkup(
                    [
                        [InlineKeyboardButton("📁 Choose project", callback_data="feat:choose_project")],
                        [InlineKeyboardButton("❌ Close", callback_data="flow:close")],
                    ]
                ),
                parse_mode="Markdown",
            )
            return

        await query.edit_message_text(
            text=_feature_list_text(project_key, features, deps, preferred_agent_type),
            reply_markup=_feature_list_keyboard(features),
            parse_mode="Markdown",
        )
        return

    if data.startswith("feat:pick:"):
        parts = data.split(":")
        if len(parts) != 3:
            await query.edit_message_text("⚠️ Invalid selection.")
            return

        project_key = feature_state.get("project")
        features = feature_state.get("items") or []
        if not project_key or not features:
            await query.edit_message_text(
                text="📁 Session expired. Select a project to get feature proposals:",
                reply_markup=_feature_project_keyboard(deps),
            )
            return

        try:
            selected_index = int(parts[2])
        except ValueError:
            await query.edit_message_text("⚠️ Invalid feature selection.")
            return

        if selected_index < 0 or selected_index >= len(features):
            await query.edit_message_text("⚠️ Invalid feature selection.")
            return

        selected = features[selected_index]
        detail_lines = [
            f"💡 *{selected['title']}*",
            "",
            selected["summary"],
            "",
            "*Why now*",
            selected["why"],
            "",
            "*Implementation outline*",
        ]
        for idx, step in enumerate(selected.get("steps", []), start=1):
            detail_lines.append(f"{idx}. {step}")

        keyboard = InlineKeyboardMarkup(
            [
                [InlineKeyboardButton("⬅️ Back to feature list", callback_data=f"feat:project:{project_key}")],
                [InlineKeyboardButton("📁 Choose project", callback_data="feat:choose_project")],
                [InlineKeyboardButton("❌ Close", callback_data="flow:close")],
            ]
        )
        await query.edit_message_text(
            text="\n".join(detail_lines),
            reply_markup=keyboard,
            parse_mode="Markdown",
        )
        return

    await query.edit_message_text("⚠️ Unknown feature action.")

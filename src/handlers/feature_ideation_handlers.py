"""Feature ideation chat/callback handlers extracted from telegram_bot."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Dict, List, Optional

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import ContextTypes
from chat_agents_schema import (
    get_default_project_chat_agent_type,
    get_project_chat_agent_config,
)
from handlers.common_routing import extract_json_dict
from handlers.agent_context_utils import (
    collect_context_candidate_files,
    extract_agent_prompt_metadata_from_yaml,
    extract_referenced_paths_from_markdown,
    load_agent_prompt_from_definition,
    load_role_context,
    normalize_paths,
    resolve_path,
    resolve_project_root,
)


FEATURE_STATE_KEY = "feature_suggestions"


@dataclass
class FeatureIdeationHandlerDeps:
    logger: Any
    allowed_user_ids: List[int]
    projects: Dict[str, str]
    get_project_label: Callable[[str], str]
    orchestrator: Any
    base_dir: str = ""
    project_config: Optional[Dict[str, Any]] = None


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


def detect_feature_project(text: str, projects: Optional[Dict[str, str]] = None) -> Optional[str]:
    candidate = (text or "").strip().lower()
    if not candidate:
        return None

    aliases: Dict[str, str] = {}
    try:
        from config import get_project_aliases

        aliases.update(get_project_aliases())
    except Exception:
        pass

    if isinstance(projects, dict):
        for key in projects.keys():
            normalized = str(key).strip().lower()
            if normalized:
                aliases.setdefault(normalized, normalized)

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
    parsed = extract_json_dict(raw_text)
    return parsed if parsed else None


def _extract_json_payload(raw_text: str) -> Any:
    if not raw_text:
        return None

    cleaned = str(raw_text).replace("```json", "").replace("```", "").strip()

    for candidate in (str(raw_text).strip(), cleaned):
        if not candidate:
            continue
        try:
            parsed = json.loads(candidate)
            if isinstance(parsed, (dict, list)):
                return parsed
        except Exception:
            pass

    text = str(raw_text)
    if "[" in text and "]" in text:
        start = text.find("[")
        end = text.rfind("]") + 1
        try:
            parsed = json.loads(text[start:end])
            if isinstance(parsed, list):
                return parsed
        except Exception:
            pass

    parsed_dict = _extract_json_dict(raw_text)
    if parsed_dict is not None:
        return parsed_dict

    return None


def _truncate_for_log(value: str, limit: int = 600) -> str:
    text = str(value or "").strip()
    if len(text) <= limit:
        return text
    return f"{text[:limit]}..."


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


def _project_config_for_key(project_key: str, deps: FeatureIdeationHandlerDeps) -> Dict[str, Any]:
    config = deps.project_config if isinstance(deps.project_config, dict) else {}
    project_cfg = config.get(project_key)
    if not isinstance(project_cfg, dict):
        return {}
    return project_cfg


def _default_chat_agent_type_for_project(project_key: str, deps: FeatureIdeationHandlerDeps) -> str:
    project_cfg = _project_config_for_key(project_key, deps)
    return get_default_project_chat_agent_type(project_cfg)


def _chat_agent_config_for_project(
    project_key: str,
    routed_agent_type: str,
    deps: FeatureIdeationHandlerDeps,
) -> Dict[str, Any]:
    project_cfg = _project_config_for_key(project_key, deps)
    return get_project_chat_agent_config(project_cfg, routed_agent_type)


def _resolve_project_root(project_key: str, deps: FeatureIdeationHandlerDeps) -> str:
    project_cfg = _project_config_for_key(project_key, deps)
    return resolve_project_root(str(getattr(deps, "base_dir", "") or ""), project_key, project_cfg)


def _resolve_path(project_root: str, raw_path: str) -> str:
    return resolve_path(project_root, raw_path)


def _normalize_paths(value: Any) -> List[str]:
    return normalize_paths(value)


def _extract_referenced_paths_from_agents(agents_text: str) -> List[str]:
    return extract_referenced_paths_from_markdown(agents_text)


def _collect_context_candidate_files(context_root: str, seed_files: Optional[List[str]] = None) -> List[str]:
    return collect_context_candidate_files(context_root, seed_files=seed_files)


def _extract_agent_prompt_metadata_from_yaml(path: str, max_chars: int = 3000) -> tuple[str, str]:
    return extract_agent_prompt_metadata_from_yaml(path, max_chars=max_chars)


def _load_agent_prompt_from_definition(
    project_key: str,
    routed_agent_type: str,
    deps: FeatureIdeationHandlerDeps,
) -> str:
    project_root = _resolve_project_root(project_key, deps)
    project_cfg = _project_config_for_key(project_key, deps)
    return load_agent_prompt_from_definition(
        base_dir=str(deps.base_dir or ""),
        project_root=project_root,
        project_cfg=project_cfg,
        routed_agent_type=routed_agent_type,
    )


def _load_role_context(
    project_key: str,
    routed_agent_type: str,
    deps: FeatureIdeationHandlerDeps,
    max_chars: int = 18000,
) -> str:
    """Load project context for prompts based on chat_agents config and AGENTS protocol."""
    agent_cfg = _chat_agent_config_for_project(project_key, routed_agent_type, deps)
    project_root = _resolve_project_root(project_key, deps)
    return load_role_context(project_root=project_root, agent_cfg=agent_cfg, max_chars=max_chars)


def _build_feature_persona(
    project_label: str,
    routed_agent_type: str,
    feature_count: int,
    context_block: str,
    agent_prompt: str,
) -> str:
    role = str(routed_agent_type or "").strip().lower()
    role_prompt = (
        f"Use this dedicated agent definition as your operating role and voice for `{role}`:\n"
        f"{agent_prompt}"
    )

    return (
        f"{role_prompt}\n"
        f"Project: {project_label}\n"
        "Return ONLY JSON with this schema:\n"
        "{\"items\":[{\"title\":\"...\",\"summary\":\"...\",\"why\":\"...\",\"steps\":[\"...\",\"...\",\"...\"]}]}\n"
        f"Generate exactly {feature_count} items. Keep titles concise and action-oriented."
        f"{context_block}"
    )


def _build_feature_suggestions(
    project_key: str,
    text: str,
    deps: FeatureIdeationHandlerDeps,
    preferred_agent_type: Optional[str],
    feature_count: int,
) -> List[Dict[str, Any]]:
    project_label = deps.get_project_label(project_key)
    routed_agent_type = str(preferred_agent_type or "").strip().lower()
    if not routed_agent_type:
        routed_agent_type = _default_chat_agent_type_for_project(project_key, deps)
    if not routed_agent_type:
        if getattr(deps, "logger", None):
            deps.logger.warning(
                "Feature ideation requires configured chat_agents for project '%s'",
                project_key,
            )
        return []

    agent_prompt = _load_agent_prompt_from_definition(project_key, routed_agent_type, deps)
    if not agent_prompt:
        if getattr(deps, "logger", None):
            deps.logger.warning(
                "Feature ideation requires agent prompt definition for agent_type '%s' in project '%s'",
                routed_agent_type,
                project_key,
            )
        return []

    context_block = _load_role_context(project_key, routed_agent_type, deps)
    persona = _build_feature_persona(
        project_label,
        routed_agent_type,
        feature_count,
        context_block,
        agent_prompt,
    )

    def _extract_items_from_result(result: Dict[str, Any]) -> List[Dict[str, Any]]:
        if not isinstance(result, dict):
            return []

        if isinstance(result.get("title"), str) and isinstance(result.get("summary"), str):
            single = _normalize_generated_features([result], feature_count)
            if single:
                return single

        if isinstance(result.get("items"), list):
            direct = _normalize_generated_features(result.get("items"), feature_count)
            if direct:
                return direct

        for list_key in ("features", "suggestions", "proposals"):
            if isinstance(result.get(list_key), list):
                direct = _normalize_generated_features(result.get(list_key), feature_count)
                if direct:
                    return direct

        for wrapped_key in ("response", "content", "message"):
            wrapped_value = result.get(wrapped_key)
            if not isinstance(wrapped_value, str) or not wrapped_value.strip():
                continue
            payload = _extract_json_payload(wrapped_value)
            if isinstance(payload, list):
                direct = _normalize_generated_features(payload, feature_count)
                if direct:
                    return direct
            if isinstance(payload, dict):
                direct = _normalize_generated_features(payload.get("items"), feature_count)
                if direct:
                    return direct

        payload = _extract_json_payload(str(result.get("text", "")))
        if isinstance(payload, list):
            return _normalize_generated_features(payload, feature_count)
        if isinstance(payload, dict):
            return _normalize_generated_features(payload.get("items"), feature_count)
        return []

    try:
        result = deps.orchestrator.run_text_to_speech_analysis(
            text=text,
            task="advisor_chat",
            persona=persona,
        )
        generated = _extract_items_from_result(result or {})
        if generated:
            return generated

        if getattr(deps, "logger", None):
            raw_text = ""
            if isinstance(result, dict):
                raw_text = str(result.get("text", ""))
            if not raw_text and isinstance(result, dict):
                deps.logger.warning(
                    "Primary feature ideation structured response keys: %s",
                    sorted(result.keys()),
                )
                deps.logger.warning(
                    "Primary feature ideation structured response payload (truncated): %s",
                    _truncate_for_log(json.dumps(result, ensure_ascii=False)),
                )
            deps.logger.warning(
                "Primary feature ideation raw response (truncated): %s",
                _truncate_for_log(raw_text),
            )
            deps.logger.warning(
                "Dynamic feature ideation returned non-JSON/empty output (primary path), retrying with Copilot"
            )
    except Exception as exc:
        if getattr(deps, "logger", None):
            deps.logger.warning("Dynamic feature ideation failed on primary path: %s", exc)

    try:
        run_copilot = getattr(deps.orchestrator, "_run_copilot_analysis", None)
        if callable(run_copilot):
            copilot_result = run_copilot(text, task="advisor_chat", persona=persona)
            generated = _extract_items_from_result(copilot_result or {})
            if generated:
                return generated
            if getattr(deps, "logger", None):
                deps.logger.warning("Dynamic feature ideation Copilot retry returned non-JSON/empty output")
    except Exception as exc:
        if getattr(deps, "logger", None):
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
    routed_agent_type = str(preferred_agent_type or "").strip().lower()
    if not routed_agent_type:
        routed_agent_type = _default_chat_agent_type_for_project(project_key, deps)
    agent_label = routed_agent_type or "unknown"
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

    project_key = detect_feature_project(text, deps.projects)
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

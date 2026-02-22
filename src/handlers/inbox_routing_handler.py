import os
import json
import logging
from typing import Dict, Any, Tuple
from config import BASE_DIR, PROJECT_CONFIG, get_inbox_dir

logger = logging.getLogger(__name__)

# Constants (mirrored from telegram_bot or config if not fully centralized)
PROJECTS = {
    "case_italia": "Case Italia",
    "wallible": "Wallible",
    "biome": "BIOME",
    "nexus": "Nexus"
}

TYPES = {
    "feature": "Feature",
    "feature-simple": "Feature (Simple)",
    "bug": "Bug",
    "hotfix": "Hotfix",
    "release": "Release",
    "chore": "Chore",
    "improvement": "Improvement",
    "improvement-simple": "Improvement (Simple)"
}

def _extract_json_dict(text: str) -> Dict[str, Any]:
    """Extract a JSON dictionary from a string that might contain markdown or other text."""
    try:
        # Strategy 1: Find everything between first { and last }
        if "{" in text and "}" in text:
            start = text.find("{")
            end = text.rfind("}") + 1
            json_str = text[start:end]
            return json.loads(json_str)

        # Strategy 2: Remove markdown formatting
        text_clean = text.replace("```json", "").replace("```", "").strip()
        return json.loads(text_clean)
        
    except json.JSONDecodeError as e:
        return {}

def _refine_task_description(content: str, project: str) -> str:
    """Prepend the project name if it's missing."""
    project_display = PROJECTS.get(project, project)
    if not content.lower().startswith(project.lower()) and not content.lower().startswith(project_display.lower()):
        return f"{project_display}: {content}"
    return content

async def process_inbox_task(text: str, orchestrator, message_id_or_unique_id: str) -> Dict[str, Any]:
    """
    Core logic for processing a task from natural language text.
    Classifies the task, creates the markdown file in the inbox, and returns the result.
    
    Returns a dict with:
    - success: bool
    - message: str (Error or success message to show the user)
    - project: str (Optional)
    - content: str (Optional)
    - pending_resolution: dict (Optional, if project needs manual selection)
    """
    logger.info("Running task classification...")
    result = orchestrator.run_text_to_speech_analysis(
        text=text,
        task="classify",
        projects=list(PROJECTS.keys()),
        types=list(TYPES.keys())
    )

    logger.info(f"Analysis result: {result}")

    # Parse Result
    try:
        if isinstance(result, dict) and result.get("text"):
            needs_reparse = result.get("parse_error") or "project" not in result
            if needs_reparse:
                reparsed = _extract_json_dict(result["text"])
                if reparsed:
                    result = reparsed

        project = result.get("project")
        if not project or project not in PROJECTS:
            task_type = result.get("type", "feature")
            if task_type not in TYPES:
                task_type = "feature"
            
            pending_resolution = {
                "raw_text": text or "",
                "content": result.get("text", text or ""),
                "task_type": task_type,
                "task_name": result.get("task_name", ""),
            }
            options = ", ".join(sorted(PROJECTS.keys()))
            error_msg = (
                f"❌ Could not classify project (received: '{project}').\n\n"
                f"Reply with a project key: {options}"
            )
            logger.error(f"Project classification failed: project={project}, valid={list(PROJECTS.keys())}")
            
            return {
                "success": False,
                "message": error_msg,
                "pending_resolution": pending_resolution
            }
        
        task_type = result.get("type", "feature")
        if task_type not in TYPES:
            logger.warning(f"Type '{task_type}' not in TYPES, defaulting to 'feature'")
            task_type = "feature"
        
        content = _refine_task_description(content, str(project))
        task_name = result.get("task_name", "")
        logger.info(f"Parsed: project={project}, type={task_type}, task_name={task_name}")
    except Exception as e:
        logger.error(f"JSON parsing error: {e}", exc_info=True)
        return {
            "success": False,
            "message": "⚠️ JSON Error"
        }

    # Save to File
    logger.info(f"Getting inbox dir for project: {project}")
    
    # Map project name to workspace (e.g., "nexus" → "ghabs")
    workspace = project
    if project in PROJECT_CONFIG:
        workspace = PROJECT_CONFIG[project].get("workspace", project)
        logger.info(f"Mapped project '{project}' → workspace '{workspace}'")
    else:
        logger.warning(f"Project '{project}' not in PROJECT_CONFIG, using as-is for workspace")
    
    target_dir = get_inbox_dir(os.path.join(BASE_DIR, workspace), project)
    logger.info(f"Target inbox dir: {target_dir}")
    os.makedirs(target_dir, exist_ok=True)
    
    filename = f"task_{message_id_or_unique_id}.md"
    filepath = os.path.join(target_dir, filename)

    logger.info(f"Writing to file: {filepath}")
    with open(filepath, "w") as f:
        f.write(
            f"# {TYPES.get(task_type, 'Task')}\n"
            f"**Project:** {PROJECTS.get(project, project)}\n"
            f"**Type:** {task_type}\n"
            f"**Task Name:** {task_name}\n"
            f"**Status:** Pending\n\n"
            f"{content}\n\n"
            f"---\n"
            f"**Raw Input:**\n{text}"
        )

    logger.info(f"✅ File saved: {filepath}")
    
    return {
        "success": True,
        "message": f"✅ Routed to `{project}`\n📝 *{content}*",
        "project": project,
        "content": content
    }

async def save_resolved_task(pending_project: dict, selected_project: str, message_id_or_unique_id: str) -> Dict[str, Any]:
    """Save a task that previously lacked a clear project after the user specifies one."""
    project = selected_project
    task_type = str(pending_project.get("task_type", "feature"))
    if task_type not in TYPES:
        task_type = "feature"
    text = str(pending_project.get("raw_text", "")).strip()
    content = str(pending_project.get("content", text)).strip()
    content = _refine_task_description(content, project)
    task_name = str(pending_project.get("task_name", "")).strip()

    workspace = str(project)
    if project in PROJECT_CONFIG:
        workspace = str(PROJECT_CONFIG[project].get("workspace", project))
    target_dir = get_inbox_dir(os.path.join(BASE_DIR, workspace), str(project))
    os.makedirs(target_dir, exist_ok=True)
    
    filename = f"task_{message_id_or_unique_id}.md"
    filepath = os.path.join(target_dir, filename)
    with open(filepath, "w") as f:
        f.write(
            f"# {TYPES.get(task_type, 'Task')}\n"
            f"**Project:** {PROJECTS.get(project, project)}\n"
            f"**Type:** {task_type}\n"
            f"**Task Name:** {task_name}\n"
            f"**Status:** Pending\n\n"
            f"{content}\n\n"
            f"---\n"
            f"**Raw Input:**\n{text}"
        )

    return {
        "success": True,
        "message": f"✅ Routed to `{project}`\n📝 *{content}*"
    }

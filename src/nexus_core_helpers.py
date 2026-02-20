"""
Nexus-Core Framework Integration Helpers.

This module provides integration between the original Nexus bot
and the nexus-core workflow framework.
"""
import logging
import os
from typing import Optional, Dict, List, Any

from config import (
    get_github_repo,
    _get_project_config,
    NEXUS_CORE_STORAGE_DIR,
    BASE_DIR,
)
from state_manager import StateManager
from audit_store import AuditStore
from plugin_runtime import get_workflow_state_plugin
from nexus.adapters.storage.file import FileStorage
from nexus.adapters.git.github import GitHubPlatform
from nexus.core.workflow import WorkflowEngine

logger = logging.getLogger(__name__)


def get_workflow_engine() -> WorkflowEngine:
    """Get initialized workflow engine instance."""
    storage = FileStorage(base_path=NEXUS_CORE_STORAGE_DIR)
    return WorkflowEngine(storage=storage)


def get_git_platform(repo: str = None) -> GitHubPlatform:
    """Get initialized GitHub platform adapter."""
    return GitHubPlatform(repo=repo or get_github_repo("nexus"))


def get_workflow_definition_path(project_name: str) -> Optional[str]:
    """Get workflow definition path for a project with fallback logic.
    
    Priority:
    1. Project-specific override in PROJECT_CONFIG
    2. Global workflow_definition_path in PROJECT_CONFIG
    3. None (caller must abort)
    
    Args:
        project_name: Project name (e.g., 'nexus', 'casit-agents')
        
    Returns:
        Absolute path to workflow YAML file, or None if not configured
    """
    config = _get_project_config()
    
    # Check project-specific override
    if project_name in config:
        project_config = config[project_name]
        if isinstance(project_config, dict) and "workflow_definition_path" in project_config:
            path = project_config["workflow_definition_path"]
            # Resolve relative paths to absolute
            if path and not os.path.isabs(path):
                path = os.path.join(BASE_DIR, path)
            return path
    
    # Check global workflow_definition_path
    if "workflow_definition_path" in config:
        path = config["workflow_definition_path"]
        # Resolve relative paths to absolute  
        if path and not os.path.isabs(path):
            path = os.path.join(BASE_DIR, path)
        return path
    
    # No workflow definition found
    return None


_WORKFLOW_STATE_PLUGIN_BASE_KWARGS = {
    "storage_dir": NEXUS_CORE_STORAGE_DIR,
    "issue_to_workflow_id": StateManager.get_workflow_id_for_issue,
    "issue_to_workflow_map_setter": StateManager.map_issue_to_workflow,
    "workflow_definition_path_resolver": get_workflow_definition_path,
    "github_repo": get_github_repo("nexus"),
    "set_pending_approval": StateManager.set_pending_approval,
    "clear_pending_approval": StateManager.clear_pending_approval,
    "audit_log": AuditStore.audit_log,
}
_WORKFLOW_STATE_PLUGIN_CACHE_KEY = "workflow:state-engine"


async def create_workflow_for_issue(
    issue_number: str,
    issue_title: str,
    project_name: str,
    tier_name: str,
    task_type: str,
    description: str = ""
) -> Optional[str]:
    """
    Create a nexus-core workflow for a GitHub issue.
    
    Args:
        issue_number: GitHub issue number
        issue_title: Issue title (slug)
        project_name: Project name (e.g., 'casit-agents')
        tier_name: Workflow tier (tier-1-simple, tier-2-standard, etc.)
        task_type: Task type (feature, bug, hotfix, etc.)
        description: Task description
    
    Returns:
        workflow_id if successful, None otherwise
    """
    workflow_plugin = get_workflow_state_plugin(
        **_WORKFLOW_STATE_PLUGIN_BASE_KWARGS,
        cache_key=_WORKFLOW_STATE_PLUGIN_CACHE_KEY,
    )

    workflow_id = await workflow_plugin.create_workflow_for_issue(
        issue_number=issue_number,
        issue_title=issue_title,
        project_name=project_name,
        tier_name=tier_name,
        task_type=task_type,
        description=description,
    )

    if workflow_id:
        return workflow_id

    workflow_definition_path = get_workflow_definition_path(project_name)
    if not workflow_definition_path:
        msg = (
            f"No workflow_definition_path configured for project '{project_name}'. "
            "Cannot create workflow without a YAML definition."
        )
        logger.error(msg)
        from notifications import send_telegram_alert
        send_telegram_alert(f"❌ {msg}")
    elif not os.path.exists(workflow_definition_path):
        msg = (
            f"Workflow definition not found at: {workflow_definition_path} "
            f"(project: {project_name})"
        )
        logger.error(msg)
        from notifications import send_telegram_alert
        send_telegram_alert(f"❌ {msg}")
    return None


async def start_workflow(workflow_id: str, issue_number: str = None) -> bool:
    """
    Start a workflow.
    
    Args:
        workflow_id: Workflow ID
        issue_number: Optional issue number for GitHub comment
    
    Returns:
        True if successful
    """
    workflow_plugin = get_workflow_state_plugin(
        **_WORKFLOW_STATE_PLUGIN_BASE_KWARGS,
        cache_key=_WORKFLOW_STATE_PLUGIN_CACHE_KEY,
    )

    success = await workflow_plugin.start_workflow(workflow_id)
    if success and issue_number:
        logger.info(f"Started workflow {workflow_id} for issue #{issue_number}")
    return success


async def pause_workflow(issue_number: str, reason: str = "User requested") -> bool:
    """
    Pause a workflow by issue number.
    
    Args:
        issue_number: GitHub issue number
        reason: Reason for pausing
    
    Returns:
        True if successful
    """
    workflow_plugin = get_workflow_state_plugin(
        **_WORKFLOW_STATE_PLUGIN_BASE_KWARGS,
        cache_key=_WORKFLOW_STATE_PLUGIN_CACHE_KEY,
    )
    return await workflow_plugin.pause_workflow(issue_number, reason=reason)


async def resume_workflow(issue_number: str) -> bool:
    """
    Resume a paused workflow by issue number.
    
    Args:
        issue_number: GitHub issue number
    
    Returns:
        True if successful
    """
    workflow_plugin = get_workflow_state_plugin(
        **_WORKFLOW_STATE_PLUGIN_BASE_KWARGS,
        cache_key=_WORKFLOW_STATE_PLUGIN_CACHE_KEY,
    )
    return await workflow_plugin.resume_workflow(issue_number)


async def get_workflow_status(issue_number: str) -> Optional[Dict]:
    """
    Get workflow status for an issue.
    
    Args:
        issue_number: GitHub issue number
    
    Returns:
        Dict with workflow status or None
    """
    workflow_plugin = get_workflow_state_plugin(
        **_WORKFLOW_STATE_PLUGIN_BASE_KWARGS,
        cache_key=_WORKFLOW_STATE_PLUGIN_CACHE_KEY,
    )
    return await workflow_plugin.get_workflow_status(issue_number)


async def handle_approval_gate(
    workflow_id: str,
    issue_number: str,
    step_num: int,
    step_name: str,
    agent_name: str,
    approvers: List[str],
    approval_timeout: int,
    project: str = "nexus",
) -> None:
    """
    Called after complete_step when the next step has approval_required=True.
    Persists the pending approval and sends a Telegram notification.
    
    Args:
        workflow_id: The workflow ID (for reference)
        issue_number: GitHub issue number
        step_num: Step number awaiting approval
        step_name: Step name awaiting approval
        agent_name: Agent that will run the step when approved
        approvers: List of required approvers
        approval_timeout: Timeout in seconds
        project: Project name
    """
    def _notify_approval_required(**kwargs):
        from notifications import notify_approval_required

        notify_approval_required(**kwargs)

    workflow_plugin = get_workflow_state_plugin(
        **_WORKFLOW_STATE_PLUGIN_BASE_KWARGS,
        notify_approval_required=_notify_approval_required,
        cache_key=_WORKFLOW_STATE_PLUGIN_CACHE_KEY,
    )

    await workflow_plugin.request_approval_gate(
        workflow_id=workflow_id,
        issue_number=issue_number,
        step_num=step_num,
        step_name=step_name,
        agent_name=agent_name,
        approvers=approvers,
        approval_timeout=approval_timeout,
        project=project,
    )

    logger.info(
        f"Approval gate triggered for issue #{issue_number} "
        f"step {step_num} ({step_name})."
    )


async def complete_step_for_issue(
    issue_number: str,
    completed_agent_type: str,
    outputs: Dict[str, Any],
):
    """Mark the current running step for *issue_number* as complete.

    Delegates to ``WorkflowStateEnginePlugin.complete_step_for_issue()``.
    The engine evaluates router steps automatically, handling conditional
    branches and review/develop loops.

    Args:
        issue_number: GitHub issue number.
        completed_agent_type: The ``agent_type`` that just finished.
        outputs: Structured outputs from the completion summary (use
            ``CompletionSummary.to_dict()`` or pass a raw dict).

    Returns:
        Updated :class:`~nexus.core.models.Workflow` (inspect ``.state`` and
        ``.active_agent_type`` to determine what to do next), or ``None``
        when no workflow is mapped to the issue.
    """
    workflow_plugin = get_workflow_state_plugin(
        **_WORKFLOW_STATE_PLUGIN_BASE_KWARGS,
        cache_key=_WORKFLOW_STATE_PLUGIN_CACHE_KEY,
    )
    return await workflow_plugin.complete_step_for_issue(
        issue_number=str(issue_number),
        completed_agent_type=completed_agent_type,
        outputs=outputs,
    )

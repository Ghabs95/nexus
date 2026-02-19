"""
Nexus-Core Framework Integration Helpers.

This module provides integration between the original Nexus bot
and the nexus-core workflow framework.
"""
import asyncio
import logging
import os
from typing import Optional, Dict, List

from config import (
    get_github_repo,
    _get_project_config,
    NEXUS_CORE_STORAGE_DIR,
    USE_NEXUS_CORE,
    WORKFLOW_CHAIN,
    BASE_DIR,
)
from state_manager import StateManager
from nexus.adapters.storage.file import FileStorage
from nexus.adapters.git.github import GitHubPlatform
from nexus.core.workflow import WorkflowDefinition, WorkflowEngine
from nexus.core.models import Workflow, WorkflowStep, Agent

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
    3. None (use WORKFLOW_CHAIN fallback)
    
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
    if not USE_NEXUS_CORE:
        logger.info("nexus-core disabled, skipping workflow creation")
        return None
    
    try:
        engine = get_workflow_engine()
        
        # Map tier to workflow chain
        workflow_type = _tier_to_workflow_type(tier_name)
        workflow_id = f"{project_name}-{issue_number}-{tier_name}"
        workflow_name = f"{project_name}/{issue_title}"
        workflow_description = description or f"Workflow for issue #{issue_number}"
        
        # Get workflow definition path (with fallback logic)
        workflow_definition_path = get_workflow_definition_path(project_name)

        metadata = {
            "issue_number": issue_number,
            "project": project_name,
            "tier": tier_name,
            "task_type": task_type,
            "github_issue_url": f"https://github.com/{get_github_repo('nexus')}/issues/{issue_number}",
            "workflow_type": workflow_type,
            "workflow_definition_path": workflow_definition_path,
        }

        if workflow_definition_path and os.path.exists(workflow_definition_path):
            workflow = WorkflowDefinition.from_yaml(
                workflow_definition_path,
                workflow_id=workflow_id,
                name_override=workflow_name,
                description_override=workflow_description,
                metadata=metadata,
            )
        else:
            if workflow_definition_path:
                logger.warning(
                    f"Workflow path not found: {workflow_definition_path}. "
                    "Falling back to WORKFLOW_CHAIN."
                )

            chain = WORKFLOW_CHAIN.get(workflow_type, WORKFLOW_CHAIN["shortened"])

            # Create workflow steps from chain
            steps = []
            for step_num, (agent_name, step_name) in enumerate(chain, start=1):
                agent = Agent(
                    name=f"{agent_name}Agent",
                    display_name=agent_name,
                    description=f"Step {step_num}: {step_name}",
                    timeout=3600,
                    max_retries=2,
                )

                step = WorkflowStep(
                    step_num=step_num,
                    name=step_name.lower().replace(" ", "_"),
                    agent=agent,
                    prompt_template=f"{step_name}: {{description}}",
                )
                steps.append(step)

            # Create workflow object
            workflow = Workflow(
                id=workflow_id,
                name=workflow_name,
                version="1.0",
                description=workflow_description,
                steps=steps,
                metadata=metadata,
            )
        
        # Persist workflow
        await engine.create_workflow(workflow)
        
        # Map issue to workflow
        StateManager.map_issue_to_workflow(issue_number, workflow_id)
        
        logger.info(f"Created nexus-core workflow {workflow_id} for issue #{issue_number}")
        return workflow_id
        
    except Exception as e:
        logger.error(f"Failed to create nexus-core workflow for issue #{issue_number}: {e}")
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
    if not USE_NEXUS_CORE:
        return False
    
    try:
        engine = get_workflow_engine()
        workflow = await engine.start_workflow(workflow_id)
        
        logger.info(f"Started workflow {workflow_id} (state: {workflow.state.value})")
        
        # Optionally add GitHub comment
        if issue_number:
            git_platform = get_git_platform()
            current_step = workflow.steps[workflow.current_step]
            # Note: This would actually call GitHub API in production
            logger.info(f"Would add comment to issue #{issue_number}: Workflow started at step {current_step.name}")
        
        return True
        
    except Exception as e:
        logger.error(f"Failed to start workflow {workflow_id}: {e}")
        return False


async def pause_workflow(issue_number: str, reason: str = "User requested") -> bool:
    """
    Pause a workflow by issue number.
    
    Args:
        issue_number: GitHub issue number
        reason: Reason for pausing
    
    Returns:
        True if successful
    """
    if not USE_NEXUS_CORE:
        return False
    
    workflow_id = StateManager.get_workflow_id_for_issue(issue_number)
    if not workflow_id:
        logger.warning(f"No workflow found for issue #{issue_number}")
        return False
    
    try:
        engine = get_workflow_engine()
        await engine.pause_workflow(workflow_id)
        
        logger.info(f"Paused workflow {workflow_id} for issue #{issue_number}: {reason}")
        return True
        
    except Exception as e:
        logger.error(f"Failed to pause workflow for issue #{issue_number}: {e}")
        return False


async def resume_workflow(issue_number: str) -> bool:
    """
    Resume a paused workflow by issue number.
    
    Args:
        issue_number: GitHub issue number
    
    Returns:
        True if successful
    """
    if not USE_NEXUS_CORE:
        return False
    
    workflow_id = StateManager.get_workflow_id_for_issue(issue_number)
    if not workflow_id:
        logger.warning(f"No workflow found for issue #{issue_number}")
        return False
    
    try:
        engine = get_workflow_engine()
        await engine.resume_workflow(workflow_id)
        
        logger.info(f"Resumed workflow {workflow_id} for issue #{issue_number}")
        return True
        
    except Exception as e:
        logger.error(f"Failed to resume workflow for issue #{issue_number}: {e}")
        return False


async def get_workflow_status(issue_number: str) -> Optional[Dict]:
    """
    Get workflow status for an issue.
    
    Args:
        issue_number: GitHub issue number
    
    Returns:
        Dict with workflow status or None
    """
    if not USE_NEXUS_CORE:
        return None
    
    workflow_id = StateManager.get_workflow_id_for_issue(issue_number)
    if not workflow_id:
        return None
    
    try:
        engine = get_workflow_engine()
        workflow = await engine.get_workflow(workflow_id)
        
        if not workflow:
            return None
        
        current_step = workflow.steps[workflow.current_step]
        
        return {
            "workflow_id": workflow.id,
            "name": workflow.name,
            "state": workflow.state.value,
            "current_step": workflow.current_step + 1,
            "total_steps": len(workflow.steps),
            "current_step_name": current_step.name,
            "current_agent": current_step.agent.display_name,
            "created_at": workflow.created_at.isoformat() if workflow.created_at else None,
            "updated_at": workflow.updated_at.isoformat() if workflow.updated_at else None,
            "metadata": workflow.metadata
        }
        
    except Exception as e:
        logger.error(f"Failed to get workflow status for issue #{issue_number}: {e}")
        return None


def _tier_to_workflow_type(tier_name: str) -> str:
    """Map tier name to workflow type."""
    tier_mapping = {
        "tier-1-simple": "fast-track",
        "tier-2-standard": "shortened",
        "tier-3-complex": "full",
        "tier-4-critical": "full"
    }
    return tier_mapping.get(tier_name, "shortened")


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
    StateManager.set_pending_approval(
        issue_num=issue_number,
        step_num=step_num,
        step_name=step_name,
        approvers=approvers,
        approval_timeout=approval_timeout,
    )
    StateManager.audit_log(
        int(issue_number),
        "APPROVAL_REQUESTED",
        f"step {step_num} ({step_name}), approvers={approvers}",
    )

    from notifications import notify_approval_required
    notify_approval_required(
        issue_number=issue_number,
        step_num=step_num,
        step_name=step_name,
        agent=agent_name,
        approvers=approvers,
        project=project,
    )

    logger.info(
        f"Approval gate triggered for issue #{issue_number} "
        f"step {step_num} ({step_name}). Notified approvers: {approvers}"
    )


def handle_approval_gate_sync(*args, **kwargs) -> None:
    """Synchronous wrapper for handle_approval_gate."""
    asyncio.run(handle_approval_gate(*args, **kwargs))


# Sync wrappers for use in non-async code
def create_workflow_for_issue_sync(*args, **kwargs) -> Optional[str]:
    """Synchronous wrapper for create_workflow_for_issue."""
    return asyncio.run(create_workflow_for_issue(*args, **kwargs))


def start_workflow_sync(*args, **kwargs) -> bool:
    """Synchronous wrapper for start_workflow."""
    return asyncio.run(start_workflow(*args, **kwargs))


def pause_workflow_sync(*args, **kwargs) -> bool:
    """Synchronous wrapper for pause_workflow."""
    return asyncio.run(pause_workflow(*args, **kwargs))


def resume_workflow_sync(*args, **kwargs) -> bool:
    """Synchronous wrapper for resume_workflow."""
    return asyncio.run(resume_workflow(*args, **kwargs))


def get_workflow_status_sync(*args, **kwargs) -> Optional[Dict]:
    """Synchronous wrapper for get_workflow_status."""
    return asyncio.run(get_workflow_status(*args, **kwargs))

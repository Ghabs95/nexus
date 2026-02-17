"""
Integration example: Using nexus-core framework in the original Nexus bot.

This shows how to migrate from the current StateManager to nexus-core's WorkflowEngine.
"""
import asyncio
import os
import sys
from datetime import datetime

# Add src to path for imports
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(__file__)), 'src'))

from config import DATA_DIR, GITHUB_AGENTS_REPO
from nexus.adapters.storage.file import FileStorage
from nexus.adapters.git.github import GitHubPlatform
from nexus.core.workflow import WorkflowEngine
from nexus.core.models import Workflow, WorkflowStep, Agent, WorkflowState


async def create_issue_workflow(
    issue_number: str,
    issue_title: str,
    project_name: str,
    tier_name: str,
    task_type: str
) -> str:
    """
    Create a new workflow for a GitHub issue using nexus-core.
    
    This replaces the manual workflow state tracking in StateManager.
    
    Args:
        issue_number: GitHub issue number
        issue_title: Issue title
        project_name: Project name (e.g., 'casit-agents')
        tier_name: Workflow tier (e.g., 'tier-1-simple', 'tier-2-standard')
        task_type: Task type (e.g., 'feature', 'bug', 'hotfix')
    
    Returns:
        workflow_id: Unique workflow ID for tracking
    """
    
    # Initialize storage
    storage = FileStorage(base_path=os.path.join(DATA_DIR, "nexus-core-workflows"))
    
    # Initialize workflow engine
    engine = WorkflowEngine(storage=storage)
    
    # Define agents
    copilot_agent = Agent(
        name="CopilotAgent",
        display_name="GitHub Copilot Agent",
        description="AI-powered coding agent",
        timeout=3600,
        max_retries=2
    )
    
    # Define workflow steps based on tier
    steps = []
    
    if tier_name == "tier-1-simple":
        steps = [
            WorkflowStep(
                step_num=1,
                name="implement",
                agent=copilot_agent,
                prompt_template="Implement the feature directly: {description}"
            ),
            WorkflowStep(
                step_num=2,
                name="test",
                agent=copilot_agent,
                prompt_template="Write and run tests for: {implement.output}"
            ),
            WorkflowStep(
                step_num=3,
                name="pr",
                agent=copilot_agent,
                prompt_template="Create PR for review: {test.output}"
            )
        ]
    elif tier_name == "tier-2-standard":
        steps = [
            WorkflowStep(
                step_num=1,
                name="triage",
                agent=copilot_agent,
                prompt_template="Analyze requirements and design solution: {description}"
            ),
            WorkflowStep(
                step_num=2,
                name="implement",
                agent=copilot_agent,
                prompt_template="Implement the designed solution: {triage.output}"
            ),
            WorkflowStep(
                step_num=3,
                name="test",
                agent=copilot_agent,
                prompt_template="Write comprehensive tests: {implement.output}"
            ),
            WorkflowStep(
                step_num=4,
                name="pr",
                agent=copilot_agent,
                prompt_template="Create PR for review: {test.output}"
            )
        ]
    else:  # tier-3-complex or tier-4-critical
        steps = [
            WorkflowStep(
                step_num=1,
                name="triage",
                agent=copilot_agent,
                prompt_template="Analyze task and identify requirements: {description}"
            ),
            WorkflowStep(
                step_num=2,
                name="design",
                agent=copilot_agent,
                prompt_template="Create detailed design document: {triage.output}"
            ),
            WorkflowStep(
                step_num=3,
                name="implement",
                agent=copilot_agent,
                prompt_template="Implement according to design: {design.output}"
            ),
            WorkflowStep(
                step_num=4,
                name="test",
                agent=copilot_agent,
                prompt_template="Write comprehensive tests: {implement.output}"
            ),
            WorkflowStep(
                step_num=5,
                name="review",
                agent=copilot_agent,
                prompt_template="Internal code review: {test.output}"
            ),
            WorkflowStep(
                step_num=6,
                name="pr",
                agent=copilot_agent,
                prompt_template="Create PR for final review: {review.output}"
            )
        ]
    
    # Create workflow object
    workflow_id = f"{project_name}-{issue_number}-{tier_name}"
    workflow = Workflow(
        id=workflow_id,
        name=f"{project_name}/{issue_title}",
        version="1.0",
        description=f"Workflow for GitHub issue #{issue_number}",
        steps=steps,
        metadata={
            "issue_number": issue_number,
            "project": project_name,
            "tier": tier_name,
            "task_type": task_type,
            "github_issue_url": f"https://github.com/{GITHUB_AGENTS_REPO}/issues/{issue_number}"
        }
    )
    
    # Create workflow in storage
    await engine.create_workflow(workflow)
    
    print(f"✅ Created workflow {workflow_id} for issue #{issue_number}")
    print(f"   Steps: {len(steps)}")
    print(f"   Tier: {tier_name}")
    
    return workflow_id


async def start_workflow_for_issue(workflow_id: str, issue_number: str):
    """
    Start a workflow and link it to a GitHub issue.
    
    This replaces calling invoke_copilot_agent directly.
    """
    storage = FileStorage(base_path=os.path.join(DATA_DIR, "nexus-core-workflows"))
    git_platform = GitHubPlatform(repo=GITHUB_AGENTS_REPO)
    
    engine = WorkflowEngine(storage=storage)
    
    # Start the workflow
    workflow = await engine.start_workflow(workflow_id)
    
    current_step = workflow.steps[workflow.current_step]
    
    print(f"✅ Started workflow {workflow_id}")
    print(f"   Current step: {current_step.name} (#{current_step.step_num})")
    print(f"   Agent: {current_step.agent.display_name}")
    
    # Note: In production, you would add comment to GitHub issue using git_platform
    print(f"   Would add comment to issue #{issue_number}")
    
    return current_step


async def check_workflow_status(workflow_id: str):
    """Check the current status of a workflow."""
    storage = FileStorage(base_path=os.path.join(DATA_DIR, "nexus-core-workflows"))
    
    workflow = await storage.load_workflow(workflow_id)
    if not workflow:
        print(f"❌ Workflow {workflow_id} not found")
        return
    
    print(f"\n📊 Workflow Status: {workflow.name}")
    print(f"   ID: {workflow.id}")
    print(f"   Status: {workflow.state.value}")
    print(f"   Current Step: {workflow.current_step + 1}/{len(workflow.steps)}")
    
    current_step = workflow.steps[workflow.current_step]
    print(f"\n   Current: {current_step.name}")
    print(f"   Agent: {current_step.agent.display_name}")
    
    if workflow.created_at:
        print(f"   Created: {workflow.created_at}")
    if workflow.updated_at:
        print(f"   Updated: {workflow.updated_at}")
    
    print(f"\n   Metadata:")
    for key, value in (workflow.metadata or {}).items():
        print(f"     {key}: {value}")


async def pause_workflow_for_issue(workflow_id: str, issue_number: str, reason: str):
    """
    Pause a workflow when user sends /pause command.
    
    This replaces StateManager.set_workflow_state(issue_num, WorkflowState.PAUSED).
    """
    storage = FileStorage(base_path=os.path.join(DATA_DIR, "nexus-core-workflows"))
    git_platform = GitHubPlatform(repo=GITHUB_AGENTS_REPO)
    
    engine = WorkflowEngine(storage=storage)
    
    workflow = await engine.pause_workflow(workflow_id)
    
    print(f"⏸️  Paused workflow {workflow_id}")
    print(f"   Reason: {reason}")
    
    # Note: In production, you would add comment to GitHub issue
    print(f"   Would add comment to issue #{issue_number}")


async def resume_workflow_for_issue(workflow_id: str, issue_number: str):
    """
    Resume a paused workflow when user sends /resume command.
    
    This replaces StateManager.set_workflow_state(issue_num, WorkflowState.ACTIVE).
    """
    storage = FileStorage(base_path=os.path.join(DATA_DIR, "nexus-core-workflows"))
    git_platform = GitHubPlatform(repo=GITHUB_AGENTS_REPO)
    
    engine = WorkflowEngine(storage=storage)
    
    workflow = await engine.resume_workflow(workflow_id)
    
    print(f"▶️  Resumed workflow {workflow_id}")
    
    # Note: In production, you would add comment to GitHub issue
    print(f"   Would add comment to issue #{issue_number}")


async def demo():
    """
    Demonstrate the integration.
    
    This simulates what would happen when inbox_processor processes a new task file.
    """
    print("=" * 60)
    print("Nexus-Core Integration Demo")
    print("=" * 60)
    
    # Simulate creating a workflow for a new GitHub issue
    workflow_id = await create_issue_workflow(
        issue_number="123",
        issue_title="feat/add-user-authentication",
        project_name="casit-agents",
        tier_name="tier-2-standard",
        task_type="feature"
    )
    
    print("\n" + "=" * 60)
    
    # Check initial status
    await check_workflow_status(workflow_id)
    
    print("\n" + "=" * 60)
    
    # Start the workflow
    await start_workflow_for_issue(workflow_id, "123")
    
    print("\n" + "=" * 60)
    
    # Simulate user pausing the workflow
    await pause_workflow_for_issue(workflow_id, "123", reason="User requested pause for review")
    
    print("\n" + "=" * 60)
    
    # Check status while paused
    await check_workflow_status(workflow_id)
    
    print("\n" + "=" * 60)
    
    # Resume the workflow
    await resume_workflow_for_issue(workflow_id, "123")
    
    print("\n" + "=" * 60)
    
    # Final status check
    await check_workflow_status(workflow_id)
    
    print("\n" + "=" * 60)
    print("Demo Complete!")
    print("=" * 60)


if __name__ == "__main__":
    asyncio.run(demo())

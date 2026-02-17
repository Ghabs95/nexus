"""Workflow orchestration - handles agent sequencing and workflow management."""
import logging
from typing import Optional, List, Tuple
from models import Workflow, WorkflowStep, TierName, Agent, Issue
from config import WORKFLOW_CHAIN, FINAL_AGENTS, PROJECT_CONFIG

logger = logging.getLogger(__name__)


class WorkflowOrchestrator:
    """Manages workflow definition and agent sequencing."""

    @staticmethod
    def get_workflow(tier: TierName) -> Workflow:
        """Get a workflow definition by tier."""
        workflow = Workflow(tier=tier)
        
        # Convert enum to string value if needed
        tier_key = tier.value if isinstance(tier, TierName) else str(tier).lower()
        
        if tier_key not in WORKFLOW_CHAIN:
            logger.error(f"Unknown workflow tier: {tier_key}")
            return workflow

        steps = WORKFLOW_CHAIN[tier_key]
        final_agent = FINAL_AGENTS.get(tier_key)

        for step_num, (agent_name, description) in enumerate(steps, 1):
            is_final = (agent_name == final_agent)
            step = WorkflowStep(
                step_num=step_num,
                agent_name=agent_name,
                description=description,
                is_final=is_final
            )
            workflow.steps.append(step)

        return workflow

    @staticmethod
    def get_next_agent(issue: Issue, current_step: int) -> Optional[WorkflowStep]:
        """Get the next agent in the workflow after current_step."""
        if not issue.tier:
            logger.warning(f"Issue #{issue.number} has no workflow tier")
            return None

        workflow = WorkflowOrchestrator.get_workflow(issue.tier)
        next_step = workflow.get_next_agent(current_step)

        if next_step:
            logger.info(
                f"Issue #{issue.number}: Next step is Step {next_step.step_num} "
                f"({next_step.agent_name}) - {next_step.description}"
            )
        else:
            logger.info(f"Issue #{issue.number}: Workflow complete (no next agent)")

        return next_step

    @staticmethod
    def is_final_agent(tier: TierName, agent_name: str) -> bool:
        """Check if this agent is the final step in the workflow."""
        if not isinstance(tier, TierName):
            try:
                tier = TierName[tier.upper().replace("-", "_")]
            except (KeyError, AttributeError):
                return False

        final_agent = FINAL_AGENTS.get(tier.value)
        return agent_name == final_agent

    @staticmethod
    def is_workflow_complete(issue: Issue, current_step: int) -> bool:
        """Check if workflow is complete after this step."""
        if not issue.tier:
            return False

        workflow = WorkflowOrchestrator.get_workflow(issue.tier)
        return workflow.is_complete(current_step)

    @staticmethod
    def get_workflow_progress(issue: Issue, completed_steps: int) -> Tuple[int, float]:
        """Get total steps and progress percentage for a workflow."""
        if not issue.tier:
            return (0, 0.0)

        workflow = WorkflowOrchestrator.get_workflow(issue.tier)
        total = len(workflow)
        percentage = (completed_steps / total * 100) if total > 0 else 0.0

        return (total, percentage)

    @staticmethod
    def get_all_agents_in_workflow(tier: TierName) -> List[str]:
        """Get all unique agent names in a workflow tier."""
        workflow = WorkflowOrchestrator.get_workflow(tier)
        return [step.agent_name for step in workflow.steps]

    @staticmethod
    def format_workflow_steps(tier: TierName) -> str:
        """Format workflow steps for display."""
        workflow = WorkflowOrchestrator.get_workflow(tier)
        lines = [f"**{tier.value.upper()} Workflow ({len(workflow)} steps):**\n"]

        for step in workflow.steps:
            final_marker = " ✅" if step.is_final else ""
            lines.append(f"  Step {step.step_num}: @{step.agent_name} - {step.description}{final_marker}")

        return "\n".join(lines)

    @staticmethod
    def extract_next_agent_from_log(log_text: str) -> Optional[str]:
        """
        Extract the next agent name from agent log output.
        
        Agents write "Ready for @AgentName" to indicate the next step.
        """
        import re

        # Pattern: "Ready for @AgentName" or "Next agent: AgentName"
        patterns = [
            r"Ready for @(\w+)",
            r"Next agent:\s*@?(\w+)",
            r"Invoke:\s*@?(\w+)",
            r"@(\w+)\s+is next",
        ]

        for pattern in patterns:
            match = re.search(pattern, log_text, re.IGNORECASE)
            if match:
                agent_name = match.group(1)
                logger.info(f"Extracted next agent from log: {agent_name}")
                return agent_name

        return None

    @staticmethod
    def validate_project_exists(project: str) -> bool:
        """Check if project is configured."""
        return project.lower() in PROJECT_CONFIG


class CompletionDetector:
    """Detects when workflow steps are completed."""

    @staticmethod
    def extract_step_number(text: str) -> Optional[int]:
        """Extract step number from completion text (e.g., "Step 3 Complete")."""
        import re
        match = re.search(r"Step\s+(\d+)\s+Compl", text, re.IGNORECASE)
        return int(match.group(1)) if match else None

    @staticmethod
    def is_completion_marker(text: str) -> bool:
        """Check if text contains a completion marker."""
        markers = [
            "Step",  # "Step X Complete"
            "Completed",
            "Ready for @",
            "Next agent:",
        ]
        return any(marker.lower() in text.lower() for marker in markers)

    @staticmethod
    def parse_github_comment(comment_body: str) -> Optional[Tuple[str, int]]:
        """
        Parse GitHub comment to extract agent name and step number.
        
        Returns: (agent_name, step_number) or None
        """
        import re

        # Look for "Ready for @AgentName"
        match = re.search(r"Ready for @(\w+)", comment_body)
        if match:
            agent_name = match.group(1)
            # Try to find step number
            step_match = re.search(r"Step\s+(\d+)", comment_body)
            step_num = int(step_match.group(1)) if step_match else 0
            return (agent_name, step_num)

        return None

    @staticmethod
    def extract_completion_info(text: str) -> dict:
        """Extract all completion-related info from text."""
        import re
        import json

        info = {
            "agent": None,
            "step": None,
            "complete": False,
            "next_agent": None,
        }

        # Extract agent name from "by @AgentName" or "Ready for @AgentName"
        agent_match = re.search(r"(?:by|for)\s+@(\w+)", text)
        if agent_match:
            info["agent"] = agent_match.group(1)

        # Extract step number "Step X Complete" or "Step X:"
        step_match = re.search(r"Step\s+(\d+)", text)
        if step_match:
            info["step"] = int(step_match.group(1))

        # Check for completion
        if re.search(r"complete|finished|done|ready", text, re.IGNORECASE):
            info["complete"] = True

        # Extract next agent
        next_match = re.search(r"Ready for @(\w+)", text)
        if next_match:
            info["next_agent"] = next_match.group(1)

        return info

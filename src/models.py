"""Data models for Nexus bot and processor."""
from dataclasses import dataclass, field
from typing import List, Optional, Dict, Any
from enum import Enum
from datetime import datetime


class WorkflowState(Enum):
    """Workflow state enumeration."""
    ACTIVE = "active"
    PAUSED = "paused"
    STOPPED = "stopped"


class IssueState(Enum):
    """GitHub issue state."""
    OPEN = "open"
    CLOSED = "closed"


class TierName(Enum):
    """Workflow tier classification."""
    FULL = "full"
    SHORTENED = "shortened"
    FAST_TRACK = "fast-track"


@dataclass
class Agent:
    """Agent metadata."""
    name: str
    display_name: str
    filename: str
    tier: str  # "full", "shortened", or "fast-track"
    is_final: bool = False  # True if this agent closes the issue

    def __str__(self) -> str:
        return f"@{self.name}"

    def __hash__(self) -> int:
        return hash(self.name)


@dataclass
class WorkflowStep:
    """Single step in a workflow."""
    step_num: int
    agent_name: str
    description: str
    is_final: bool = False

    def __str__(self) -> str:
        return f"Step {self.step_num}: {self.agent_name} - {self.description}"


@dataclass
class Workflow:
    """Complete workflow definition."""
    tier: TierName
    steps: List[WorkflowStep] = field(default_factory=list)

    def get_next_agent(self, current_step: int) -> Optional[WorkflowStep]:
        """Get the next step after current_step."""
        for step in self.steps:
            if step.step_num == current_step + 1:
                return step
        return None

    def get_step(self, step_num: int) -> Optional[WorkflowStep]:
        """Get a specific step by number."""
        for step in self.steps:
            if step.step_num == step_num:
                return step
        return None

    def is_complete(self, current_step: int) -> bool:
        """Check if workflow is complete after this step."""
        return self.get_next_agent(current_step) is None

    def __len__(self) -> int:
        return len(self.steps)


@dataclass
class Issue:
    """GitHub issue with workflow metadata."""
    number: int
    title: str
    state: IssueState
    project: str  # e.g., "case_italia", "wallible"
    tier: Optional[TierName] = None
    labels: List[str] = field(default_factory=list)
    body: str = ""
    updated_at: Optional[datetime] = None
    workflow_state: WorkflowState = WorkflowState.ACTIVE
    current_step: int = 0
    current_agent: Optional[Agent] = None
    comments: List[Dict[str, Any]] = field(default_factory=list)

    def __str__(self) -> str:
        return f"Issue #{self.number}: {self.title}"

    def __hash__(self) -> int:
        return hash(self.number)

    @property
    def url(self) -> str:
        """Get GitHub issue URL."""
        from config import GITHUB_AGENTS_REPO
        return f"https://github.com/{GITHUB_AGENTS_REPO}/issues/{self.number}"

    def is_workflow_issue(self) -> bool:
        """Check if this issue has a workflow label."""
        return any(label.startswith("workflow:") for label in self.labels)

    def get_workflow_tier(self) -> Optional[TierName]:
        """Extract workflow tier from labels."""
        for label in self.labels:
            if label == "workflow:full":
                return TierName.FULL
            elif label == "workflow:shortened":
                return TierName.SHORTENED
            elif label == "workflow:fast-track":
                return TierName.FAST_TRACK
        return None


@dataclass
class CompletionMarker:
    """Represents an agent completion event."""
    issue_num: int
    agent_name: str
    step_num: int
    timestamp: datetime
    source: str  # "log_file" or "github_comment"
    marker_text: str  # The actual marker found (for debugging)

    def __str__(self) -> str:
        return f"Issue #{self.issue_num}: {self.agent_name} Step {self.step_num} (@{self.timestamp})"


@dataclass
class WorkflowExecution:
    """Tracks execution state of a workflow."""
    issue_num: int
    workflow: Workflow
    started_at: datetime
    completed_at: Optional[datetime] = None
    failed_at: Optional[datetime] = None
    failure_reason: Optional[str] = None
    completed_steps: List[WorkflowStep] = field(default_factory=list)
    failed_step: Optional[WorkflowStep] = None

    def add_completion(self, step: WorkflowStep) -> None:
        """Record a step completion."""
        if step not in self.completed_steps:
            self.completed_steps.append(step)

    def mark_failed(self, step: WorkflowStep, reason: str) -> None:
        """Mark workflow as failed at a step."""
        self.failed_at = datetime.now()
        self.failed_step = step
        self.failure_reason = reason

    def mark_completed(self) -> None:
        """Mark workflow as fully completed."""
        self.completed_at = datetime.now()

    def progress_percentage(self) -> float:
        """Calculate workflow progress as percentage."""
        if not self.workflow:
            return 0.0
        return (len(self.completed_steps) / len(self.workflow)) * 100

    def is_active(self) -> bool:
        """Check if workflow is still running."""
        return self.completed_at is None and self.failed_at is None

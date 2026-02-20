# Nexus Abstraction Plan

**Goal**: Transform Nexus from a personal Telegram bot into a reusable framework for AI workflow orchestration.

---

## Architecture: Before & After

### Current (Coupled)
```
Telegram Bot → Inbox Files → GitHub Issues → Copilot CLI → GitHub Comments
     ↓              ↓              ↓                ↓              ↓
  Hardcoded    File System    GH CLI Only    CLI-specific    GH-specific
```

### Target (Abstract)
```
Input Adapter → Workflow Engine → Agent Orchestrator → Output Adapter
      ↓                ↓                  ↓                    ↓
  (Telegram,      (State Machine)    (AI Provider         (GitHub,
   Slack,         (Step Manager)      Manager)             GitLab,
   Webhook)       (Audit Log)        (Retry Logic)         Slack)
```

---

## Core Abstractions

### 1. Input Adapters (Interface: `InputSource`)

**Purpose**: Decouple task ingestion from specific messenger platforms

```python
class InputSource(ABC):
    """Abstract input source for tasks/commands."""
    
    @abstractmethod
    async def receive_task(self) -> Task:
        """Receive a task from this input source."""
        pass
    
    @abstractmethod
    async def send_response(self, task_id: str, response: Response) -> None:
        """Send a response back to the user."""
        pass
    
    @abstractmethod
    async def request_input(self, task_id: str, prompt: str) -> str:
        """Request additional input from user."""
        pass
```

**Implementations**:
- `TelegramInputSource` (existing)
- `SlackInputSource` (new)
- `WebhookInputSource` (new)
- `CLIInputSource` (for testing)

---

### 2. Storage Adapters (Interface: `StorageBackend`)

**Purpose**: Make state persistence pluggable

```python
class StorageBackend(ABC):
    """Abstract storage for workflow state, agents, audit logs."""
    
    @abstractmethod
    async def save_workflow_state(self, workflow_id: str, state: dict) -> None:
        pass
    
    @abstractmethod
    async def load_workflow_state(self, workflow_id: str) -> Optional[dict]:
        pass
    
    @abstractmethod
    async def append_audit_log(self, workflow_id: str, event: AuditEvent) -> None:
        pass
    
    @abstractmethod
    async def save_launched_agent(self, agent_id: str, metadata: dict) -> None:
        pass
```

**Implementations**:
- `FileStorageBackend` (existing)
- `PostgreSQLStorageBackend` (new)
- `RedisStorageBackend` (new)
- `S3StorageBackend` (new)

---

### 3. Git Platform Adapters (Interface: `GitPlatform`)

**Purpose**: Support GitHub, GitLab, Bitbucket

```python
class GitPlatform(ABC):
    """Abstract git platform operations."""
    
    @abstractmethod
    async def create_issue(self, repo: str, title: str, body: str, labels: List[str]) -> Issue:
        pass
    
    @abstractmethod
    async def add_comment(self, issue_id: str, comment: str) -> None:
        pass
    
    @abstractmethod
    async def get_issue(self, issue_id: str) -> Issue:
        pass
    
    @abstractmethod
    async def search_linked_prs(self, issue_id: str) -> List[PullRequest]:
        pass
    
    @abstractmethod
    async def close_issue(self, issue_id: str, comment: str) -> None:
        pass
```

**Implementations**:
- `GitHubPlatform` (existing)
- `GitLabPlatform` (new)
- `BitbucketPlatform` (new)

---

### 4. AI Provider Adapters (Interface: `AIProvider`)

**Purpose**: Make AI tool selection pluggable

```python
class AIProvider(ABC):
    """Abstract AI provider for agent execution."""
    
    @abstractmethod
    async def execute_agent(
        self,
        agent_name: str,
        context: dict,
        workspace: Path,
        timeout: int
    ) -> AgentResult:
        pass
    
    @abstractmethod
    async def check_availability(self) -> bool:
        pass
    
    @abstractmethod
    def get_rate_limit_status(self) -> RateLimitStatus:
        pass
```

**Implementations**:
- `CopilotCLIProvider` (existing)
- `GeminiCLIProvider` (existing)
- `OpenAIProvider` (new)
- `AnthropicProvider` (new)
- `LocalModelProvider` (new - Ollama, LM Studio)

---

### 5. Notification Adapters (Interface: `NotificationChannel`)

**Purpose**: Send updates to different platforms

```python
class NotificationChannel(ABC):
    """Abstract notification channel."""
    
    @abstractmethod
    async def send_message(self, user_id: str, message: str, buttons: Optional[List[Button]] = None) -> None:
        pass
    
    @abstractmethod
    async def send_alert(self, message: str, severity: Severity) -> None:
        pass
    
    @abstractmethod
    async def update_message(self, message_id: str, new_text: str) -> None:
        pass
```

**Implementations**:
- `TelegramNotifier` (existing)
- `SlackNotifier` (new)
- `DiscordNotifier` (new)
- `EmailNotifier` (new)
- `WebhookNotifier` (new)

---

## Core Framework Structure

### New Package Layout

```
nexus-core/
├── pyproject.toml
├── README.md
├── examples/
│   ├── basic_workflow.py
│   ├── github_ci_automation.py
│   └── support_ticket_router.py
├── nexus/
│   ├── __init__.py
│   ├── core/
│   │   ├── workflow.py          # WorkflowEngine, WorkflowDefinition
│   │   ├── orchestrator.py      # AgentOrchestrator (from ai_orchestrator)
│   │   ├── state.py             # StateManager (abstracted)
│   │   ├── monitor.py           # AgentMonitor (from agent_monitor)
│   │   └── models.py            # Core models (cleaned)
│   ├── adapters/
│   │   ├── input/
│   │   │   ├── base.py          # InputSource interface
│   │   │   ├── telegram.py
│   │   │   ├── slack.py
│   │   │   └── webhook.py
│   │   ├── storage/
│   │   │   ├── base.py          # StorageBackend interface
│   │   │   ├── file.py
│   │   │   ├── postgres.py
│   │   │   └── redis.py
│   │   ├── git/
│   │   │   ├── base.py          # GitPlatform interface
│   │   │   ├── github.py
│   │   │   └── gitlab.py
│   │   ├── ai/
│   │   │   ├── base.py          # AIProvider interface
│   │   │   ├── copilot_cli.py
│   │   │   ├── gemini_cli.py
│   │   │   ├── openai_api.py
│   │   │   └── anthropic_api.py
│   │   └── notifications/
│   │       ├── base.py          # NotificationChannel interface
│   │       ├── telegram.py
│   │       ├── slack.py
│   │       └── email.py
│   ├── utils/
│   │   ├── retry.py             # Retry logic (from error_handling)
│   │   ├── rate_limiter.py      # Rate limiting
│   │   └── validators.py
│   └── config/
│       ├── loader.py            # Config loading from YAML/JSON
│       └── schema.py            # Config validation
└── tests/
    ├── unit/
    ├── integration/
    └── fixtures/
```

---

## Migration Strategy

### Phase 1: Extract Core (Week 1-2)

**Goal**: Create `nexus-core` package with no breaking changes to existing Nexus

1. Create new directory structure
2. Extract these modules as-is:
   - `models.py` → `nexus/core/models.py`
   - `agent_monitor.py` → `nexus/core/monitor.py`
   - `ai_orchestrator.py` → `nexus/core/orchestrator.py`
   - `rate_limiter.py` → `nexus/utils/rate_limiter.py`
   - `error_handling.py` → `nexus/utils/retry.py`

3. Create base interfaces:
   - `nexus/adapters/storage/base.py`
   - `nexus/adapters/git/base.py`
   - `nexus/adapters/ai/base.py`
   - `nexus/adapters/notifications/base.py`

4. Implement file-based & GitHub adapters (migrate existing code)

**Validation**: Existing Nexus still works with imports from `nexus-core`

### Phase 2: Workflow Engine (Week 3-4)

**Goal**: Generic workflow orchestration engine

1. Create `WorkflowEngine` class:
   - Load workflow definitions from YAML/JSON
   - Execute steps sequentially
   - Handle state transitions
   - Persist state via `StorageBackend`

2. Create `WorkflowDefinition` format:
```yaml
name: "Feature Development"
version: "1.0"
steps:
  - name: "triage"
    agent: "ProjectLead"
    inputs: ["issue_description", "labels"]
    outputs: ["tier", "priority"]
    timeout: 300
    retry: 3
  - name: "design"
    agent: "Architect"
    condition: "tier == 'full'"
    inputs: ["triage.tier", "issue_description"]
    outputs: ["architecture_doc"]
    timeout: 600
    retry: 2
  # ... more steps
```

3. Migrate `WORKFLOW_CHAIN` config to YAML files

**Validation**: Can run existing workflows with new engine

### Phase 3: Multi-Adapter Support (Week 5-6)

**Goal**: Support Slack, GitLab, OpenAI, PostgreSQL

1. Implement new adapters:
   - `SlackInputSource` + `SlackNotifier`
   - `GitLabPlatform`
   - `OpenAIProvider`
   - `PostgreSQLStorageBackend`

2. Create adapter registry & factory:
```python
registry = AdapterRegistry()
registry.register_storage("postgres", PostgreSQLStorageBackend)
registry.register_git("gitlab", GitLabPlatform)

# Usage
storage = registry.create_storage("postgres", connection_string=db_url)
git = registry.create_git("gitlab", token=gl_token)
```

3. Configuration-driven adapter selection:
```yaml
adapters:
  storage:
    type: postgres
    connection_string: ${DATABASE_URL}
  git:
    type: github
    token: ${GITHUB_TOKEN}
  notifications:
    - type: slack
      webhook: ${SLACK_WEBHOOK}
    - type: email
      smtp_host: ${SMTP_HOST}
```

**Validation**: Can run workflows with different adapter combos

### Phase 4: Documentation & Examples (Week 7-8)

1. API documentation (Sphinx)
2. Tutorial: "Build Your First Workflow"
3. Example workflows:
   - CI/CD automation
   - Support ticket routing
   - Code review agent
   - Documentation generator

4. Migration guide: Nexus → nexus-core

**Validation**: External developer can build workflow in <1 hour

---

## Configuration Format

### Current (Python)
```python
PROJECT_CONFIG = {
    "case_italia": {
        "agents_dir": "ghabs/agents/casit-agents",
        "workspace": "case_italia",
        "git_repo": "Ghabs95/agents",
    }
}
```

### Target (YAML)
```yaml
# nexus.yaml
version: "1.0"
name: "My Workflow System"

adapters:
  storage:
    type: file
    base_path: ./data
  
  git:
    type: github
    repo: Ghabs95/agents
    token: ${GITHUB_TOKEN}
  
  notifications:
    - type: telegram
      token: ${TELEGRAM_TOKEN}
      chat_id: ${TELEGRAM_CHAT_ID}
  
  ai_providers:
    - type: copilot_cli
      path: copilot
      preference: code_generation
    - type: gemini_cli
      path: gemini
      preference: analysis

projects:
  - id: case_italia
    name: "Case Italia"
    workspace: ./workspaces/case_italia
    agents_dir: ./agents/casit-agents
    default_workflow: feature_development
  
  - id: wallible
    name: "Wallible"
    workspace: ./workspaces/wallible
    agents_dir: ./agents/wlbl-agents
    default_workflow: feature_development

workflows:
  - name: feature_development
    file: ./workflows/feature_dev.yaml
  - name: bug_fix
    file: ./workflows/bug_fix.yaml
  - name: hotfix
    file: ./workflows/hotfix.yaml

monitoring:
  health_check_port: 8080
  metrics_enabled: true
  log_level: INFO
```

---

## Breaking Changes & Migration Path

### For Existing Nexus Users (You)

**Option A: Continue with Nexus Classic**
- Keep current codebase in `nexus/` directory
- Import core components from `nexus-core` package
- Gradually migrate to new config format

**Option B: Migrate to Nexus Core**
1. Install: `pip install nexus-core`
2. Create `nexus.yaml` config
3. Run migration script: `nexus migrate-config`
4. Update systemd services to use new CLI

### For New Users

1. `pip install nexus-core`
2. Generate config: `nexus init`
3. Define workflow: `nexus workflow create my_workflow.yaml`
4. Run: `nexus start`

---

## Success Criteria

### Technical
- [ ] All core components have abstract interfaces
- [ ] File & Postgres storage adapters work
- [ ] GitHub & GitLab git adapters work
- [ ] Copilot, Gemini, OpenAI providers work
- [ ] Telegram & Slack notifiers work
- [ ] 90%+ test coverage
- [ ] Example workflows run successfully

### Documentation
- [ ] API reference complete
- [ ] Tutorial published
- [ ] 3 example workflows documented
- [ ] Migration guide written

### Community
- [ ] GitHub repo public
- [ ] 10 external contributors
- [ ] 100 GitHub stars
- [ ] 5 production deployments

---

## Timeline

| Week | Milestone | Deliverable |
|------|-----------|-------------|
| 1-2 | Extract Core | `nexus-core` package, base interfaces |
| 3-4 | Workflow Engine | YAML-based workflows, WorkflowEngine class |
| 5-6 | Multi-Adapter | Slack, GitLab, OpenAI, Postgres adapters |
| 7-8 | Documentation | Docs site, tutorials, examples |
| 9-10 | Beta Testing | 5 beta users, bug fixes |
| 11-12 | Launch | Public release, blog post, HN launch |

---

## Next: Implementation

Ready to start? I'll create:
1. Base package structure
2. Core interfaces
3. First adapter implementations (File, GitHub, Telegram)
4. Example workflow

Let's build this! 🚀

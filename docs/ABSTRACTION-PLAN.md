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

### Package Layout

```
nexus-arc/
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
│   │   ├── state.py             # HostStateManager (abstracted)
│   │   ├── monitor.py           # MonitorEngine (from agent_monitor)
│   │   ├── router.py            # WorkflowRouter (tier detection)
│   │   ├── execution.py         # ExecutionEngine (agent launching)
│   │   ├── analytics.py         # MetricsEngine (performance data)
│   │   ├── events.py            # EventBus, NexusEvent (Phase 3)
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
│   │   │   ├── redis.py
│   │   │   └── structured_log.py # JSON logs for Grafana Loki
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
│   │   ├── analytics/
│   │   │   └── loki.py          # LogQL adapter for Grafana Loki
│   │   └── notifications/
│   │       ├── base.py          # NotificationChannel interface
│   │       ├── telegram.py
│   │       ├── slack.py
│   │       └── email.py
│   ├── plugins/
│   │   └── base.py              # PluginRegistry, PluginSpec, PluginKind
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

### Phase 1: Extract Core ✅ COMPLETED

**Goal**: Create `nexus-arc` package with no breaking changes to existing Nexus

1. ✅ Created new directory structure
2. ✅ Extracted these modules:
   - `models.py` → `nexus/core/models.py`
   - `agent_monitor.py` → `nexus/core/monitor.py`
   - `ai_orchestrator.py` → `nexus/core/orchestrator.py`
   - `rate_limiter.py` → `nexus/utils/rate_limiter.py`
   - `error_handling.py` → `nexus/utils/retry.py`
3. ✅ Created base interfaces:
   - `nexus/adapters/storage/base.py`
   - `nexus/adapters/git/base.py`
   - `nexus/adapters/ai/base.py`
   - `nexus/adapters/notifications/base.py`
4. ✅ Implemented file-based & GitHub adapters (migrated existing code)

---

### Phase 2: Logic & Intelligence ✅ COMPLETED

**Goal**: Move orchestration intelligence and observability from the host into `nexus-arc`

#### Targeted Extractions

| Feature | Previous Location (`nexus`) | New Path (`nexus-arc`) | Status |
| :--- | :--- | :--- | :--- |
| Audit Logic | `src/audit_store.py` | `nexus/core/storage/audit.py` | ✅ Done |
| Analytics Engines | `src/analytics.py` | `nexus/core/analytics.py` | ✅ Done |
| Timeout/Retry Logic | `src/runtime/agent_monitor.py` | `nexus/core/monitor.py` | ✅ Done |
| Tier Selection Logic | `src/runtime/agent_monitor.py` | `nexus/core/routing.py` | ✅ Done |
| CLI Agent Launcher | `src/runtime/agent_launcher.py` | `nexus/core/execution.py` | ✅ Done |
| Workflow State | `src/state_manager.py` | `nexus/core/workflow_state.py` | ✅ Done |

#### Key Accomplishments

**1. Centralized Observability (Audit & Analytics)**
- Extracted audit logging logic to `nexus.core.storage.audit`. The host application now delegates all auditing to the core framework.
- Added `list_all_audit_events` to the `FileStorage` adapter for efficient cross-event analysis.
- Created `StructuredLogAuditBackend` decorator to natively emit structured JSON logs suitable for Promtail/Fluentbit scraping into Grafana Loki.
- Created `nexus.core.analytics.MetricsEngine` for file-based computation, alongside `LokiAnalyticsAdapter` for direct LogQL querying of Grafana Loki.
- Both engines support built-in Markdown report generation.

**2. Standardized Reliability & Monitoring**
- Created `nexus.core.monitor.MonitorEngine` for process health checks and timeout detection.
- Refactored the host's `AgentMonitor` to delegate its core detection logic to the framework.
- The `WorkflowEngine` now natively handles step-level retries with configurable backoff strategies (linear, exponential, constant).

**3. Intelligence & Routing**
- Created `nexus.core.router.WorkflowRouter` to centralize tier detection logic (Full vs. Shortened vs. Fast-track) based on issue labels and content.
- Created `nexus.core.execution.ExecutionEngine` to handle agent resolution, instruction generation (Copilot Instructions), and workspace skill synchronization.

**4. State Management Refactoring**
- Extracted workflow state (mapping + approvals) into `WorkflowStateStore` protocol in `nexus-arc` with `FileWorkflowStateStore` and `PostgresWorkflowStateStore` implementations.
- Renamed `StateManager` → `HostStateManager` in the host app, scoped to host-only concerns (launched agents, tracked issues, SocketIO).
- Created `workflow_state_factory.py` with broadcasting decorator for real-time SocketIO updates.

#### Refactored Files in `nexus`
- `src/audit_store.py` → delegates to `nexus.core.storage.audit`
- `src/analytics.py` → delegates to `nexus.core.analytics`
- `src/runtime/agent_monitor.py` → delegates to `nexus.core.monitor` and `nexus.core.router`
- `src/runtime/agent_launcher.py` → uses `nexus.core.execution` and `nexus.core.monitor`
- `src/state_manager.py` → `HostStateManager` (host-only concerns)
- `src/integrations/workflow_state_factory.py` → provides `WorkflowStateStore` via factory

---

### Phase 3: Event Bus & Plugin System 🔜 NEXT

**Goal**: Add reactive event-driven architecture and enhanced plugin lifecycle

#### Part A: Event Bus (`nexus.core.events`)

**Problem**: No internal event system. When a workflow step completes, the engine calls a hardcoded `on_step_transition` callback. No way for other modules (audit, analytics, monitoring, notifications) to subscribe independently.

**Core Abstractions**:

```
nexus/core/events.py
├── NexusEvent          — base dataclass for all events
├── EventBus            — singleton pub/sub dispatcher
├── EventHandler        — Protocol for handler callables
└── event types:
    ├── WorkflowStarted
    ├── WorkflowCompleted
    ├── WorkflowFailed
    ├── StepStarted
    ├── StepCompleted
    ├── StepFailed
    ├── AgentLaunched
    ├── AgentTimeout
    ├── AgentRetry
    └── AuditLogged
```

**`NexusEvent` Base**:

```python
@dataclass
class NexusEvent:
    event_type: str
    timestamp: datetime
    workflow_id: str | None = None
    data: dict[str, Any] = field(default_factory=dict)
```

**`EventBus` API**:

```python
class EventBus:
    def subscribe(self, event_type: str, handler: EventHandler) -> str:
        """Subscribe a handler. Returns subscription ID."""
    
    def unsubscribe(self, subscription_id: str) -> None:
        """Remove a subscription."""
    
    async def emit(self, event: NexusEvent) -> None:
        """Emit event to all matching subscribers."""
    
    def subscribe_pattern(self, pattern: str, handler: EventHandler) -> str:
        """Subscribe using glob pattern (e.g., 'workflow.*')."""
```

**Integration Points**:

| Component | Currently | After Phase 3 |
|---|---|---|
| `WorkflowEngine.complete_step` | Calls `on_step_transition` callback | Emits `StepCompleted` event |
| `WorkflowEngine.start_workflow` | Direct state mutation | Emits `WorkflowStarted` event |
| `AuditStore.log` | Direct storage write | Also emits `AuditLogged` event |
| `StructuredLogAuditBackend` | Listens to storage calls | Subscribes to `audit.*` events |
| `MonitorEngine` | Called explicitly | Subscribes to `agent.*` events |

#### Part B: Plugin System Enhancement

**Problem**: Current plugin system (`PluginRegistry`, `PluginSpec`, `PluginKind`) is solid for factory-based instantiation but lacks lifecycle hooks, event handler plugins, dependency resolution, and health monitoring.

**New `PluginKind`: `EVENT_HANDLER`**:

```python
class PluginKind(Enum):
    # ... existing kinds ...
    EVENT_HANDLER = "event_handler"
```

**`PluginLifecycle` Protocol**:

```python
@runtime_checkable
class PluginLifecycle(Protocol):
    async def on_load(self, registry: PluginRegistry) -> None:
        """Called after the plugin is registered."""
    
    async def on_unload(self) -> None:
        """Called before the plugin is removed."""
    
    async def health_check(self) -> PluginHealthStatus:
        """Return current plugin health."""
```

**`PluginHealthStatus`**:

```python
@dataclass
class PluginHealthStatus:
    healthy: bool
    name: str
    details: str = ""
    last_check: datetime = field(default_factory=lambda: datetime.now(UTC))
```

**Registry Extensions**:

```python
class PluginRegistry:
    # ... existing methods ...
    
    async def health_check_all(self) -> list[PluginHealthStatus]:
        """Run health checks on all plugins that support it."""
    
    def get_event_handlers(self) -> list[PluginSpec]:
        """List all registered event handler plugins."""
```

#### Implementation Steps

- [ ] **Step 1: Event Bus Core** — Create `nexus/core/events.py` with `NexusEvent`, `EventBus`, typed event classes. Add unit tests for subscribe/emit/unsubscribe/pattern matching.
- [ ] **Step 2: Wire EventBus into WorkflowEngine** — Add `event_bus` parameter to `WorkflowEngine.__init__`. Emit events from `start_workflow`, `complete_step`, `cancel_workflow`, `pause_workflow`. Maintain backward compat with `on_step_transition`.
- [ ] **Step 3: Plugin Lifecycle** — Add `PluginLifecycle` protocol. Add `PluginHealthStatus` dataclass. Add `EVENT_HANDLER` to `PluginKind` enum. Extend `PluginRegistry` with `health_check_all()`.
- [ ] **Step 4: Built-in Event Subscribers** — Refactor `StructuredLogAuditBackend` to subscribe to events. Create `TelegramNotificationSubscriber` as event handler plugin example. Wire `MonitorEngine` as event subscriber.
- [ ] **Step 5: Host Integration** — Initialize `EventBus` in host startup. Update `nexus_core_helpers.get_workflow_engine()` to pass EventBus. Migrate callbacks to event subscriptions.

#### Architecture

```
┌─────────────────────────────────────────────────────┐
│                    nexus (host)                      │
│  ┌───────────┐  ┌─────────────┐  ┌───────────────┐ │
│  │  Bot/CLI  │  │  Scheduler  │  │  Webhooks     │ │
│  └─────┬─────┘  └──────┬──────┘  └───────┬───────┘ │
│        │               │                 │          │
│        └───────────────┬┘                 │          │
│                        ▼                  │          │
│  ┌─────────────────────────────────────────────────┐│
│  │              WorkflowEngine                     ││
│  │         (emits events on state changes)         ││
│  └──────────────────┬──────────────────────────────┘│
│                     ▼                               │
│  ┌─────────────────────────────────────────────────┐│
│  │                 EventBus                        ││
│  │    subscribe() / emit() / unsubscribe()         ││
│  └──┬──────┬──────┬──────┬──────┬─────────────────┘│
│     │      │      │      │      │                   │
│     ▼      ▼      ▼      ▼      ▼                   │
│  ┌────┐ ┌────┐ ┌────┐ ┌────┐ ┌──────────┐          │
│  │Loki│ │Tele│ │Mon │ │Aud │ │3rd Party │          │
│  │Log │ │gram│ │itor│ │ it │ │ Plugins  │          │
│  └────┘ └────┘ └────┘ └────┘ └──────────┘          │
│                                                     │
│  ┌─────────────────────────────────────────────────┐│
│  │              PluginRegistry                     ││
│  │   register() / create() / health_check_all()    ││
│  │   load_entrypoint_plugins() / get_event_handlers││
│  └─────────────────────────────────────────────────┘│
└─────────────────────────────────────────────────────┘
```

#### Backward Compatibility
- The `on_step_transition` and `on_workflow_complete` callbacks remain functional — invoked alongside EventBus emissions. Zero breakage for existing host code.
- The `EventBus` is optional for `WorkflowEngine`. If not provided, the engine works exactly as before.
- Plugin lifecycle hooks are optional. Existing plugins continue to work unchanged.

---

### Phase 4: Multi-Adapter Support (Future)

**Goal**: Support Slack, GitLab, OpenAI, PostgreSQL

1. Implement new adapters:
   - `SlackInputSource` + `SlackNotifier`
   - `GitLabPlatform`
   - `OpenAIProvider`

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

---

### Phase 5: Documentation & Examples (Future)

1. API documentation (Sphinx)
2. Tutorial: "Build Your First Workflow"
3. Example workflows:
   - CI/CD automation
   - Support ticket routing
   - Code review agent
   - Documentation generator
4. Migration guide: Nexus → nexus-arc

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

## Success Criteria

### Technical
- [x] All core components have abstract interfaces
- [x] File storage adapter works
- [x] GitHub git adapter works
- [x] Copilot & Gemini providers work
- [x] Telegram notifier works
- [x] Audit & analytics extracted to core
- [x] Monitor & routing extracted to core
- [x] Workflow state management decoupled
- [ ] Postgres storage adapter works
- [ ] GitLab git adapter works
- [ ] OpenAI provider works
- [ ] Slack notifier works
- [ ] Event Bus implemented
- [ ] Plugin lifecycle hooks implemented
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

| Phase | Status | Milestone | Deliverable |
|-------|--------|-----------|-------------|
| 1 | ✅ Done | Extract Core | `nexus-arc` package, base interfaces, adapters |
| 2 | ✅ Done | Logic & Intelligence | Audit, analytics, monitoring, routing, state mgmt |
| 3 | 🔜 Next | Event Bus & Plugins | EventBus, plugin lifecycle, health checks |
| 4 | ⏳ Future | Multi-Adapter | Slack, GitLab, OpenAI, Postgres adapters |
| 5 | ⏳ Future | Documentation | Docs site, tutorials, examples |
| — | ⏳ Future | Beta & Launch | Beta users, bug fixes, public release |

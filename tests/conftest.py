"""Pytest configuration and shared fixtures."""

import pytest
import os
import sys
from pathlib import Path
from unittest.mock import patch, MagicMock

# Add src directory to Python path for imports
src_path = Path(__file__).parent.parent / "src"
sys.path.insert(0, str(src_path))


@pytest.fixture(autouse=True)
def mock_env_vars(monkeypatch, tmp_path):
    """Auto-use fixture to set required environment variables for all tests."""
    monkeypatch.setenv("TELEGRAM_TOKEN", "test_token_123")
    monkeypatch.setenv("AI_API_KEY", "test_api_key_123")
    monkeypatch.setenv("AI_MODEL", "gemini-test")
    monkeypatch.setenv("ALLOWED_USER", "12345")
    monkeypatch.setenv("BASE_DIR", "/tmp/test_nexus")
    
    # Create minimal project config for tests with multiple projects
    project_config_file = tmp_path / "project_config.yaml"
    project_config_file.write_text("""
    nexus:
        agents_dir: ghabs/nexus-core/examples/agents
        workspace: ghabs/nexus
        github_repo: test/repo
    test-project:
        agents_dir: ghabs/test-agents
        workspace: ghabs/test-project
        github_repo: test/project
    my-project:
        agents_dir: ghabs/my-agents
        workspace: ghabs/my-project
        github_repo: test/my-project
    """)
    
    monkeypatch.setenv("PROJECT_CONFIG_PATH", str(project_config_file))


@pytest.fixture(autouse=True)
def mock_audit_log():
    """Auto-use fixture to mock StateManager.audit_log to prevent writing to audit.log during tests."""
    with patch('state_manager.StateManager.audit_log'):
        yield


@pytest.fixture
def temp_data_dir(tmp_path, monkeypatch):
    """Create a temporary data directory for tests."""
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    monkeypatch.setenv("DATA_DIR", str(data_dir))
    return data_dir


@pytest.fixture
def sample_audit_log(tmp_path):
    """Create a sample audit.log file for testing."""
    log_file = tmp_path / "audit.log"
    content = """2026-02-16T10:00:00 | Issue #1 | WORKFLOW_STARTED | Started full tier workflow
2026-02-16T10:01:00 | Issue #1 | AGENT_LAUNCHED | Launched @ProjectLead agent (PID: 12345)
2026-02-16T10:15:00 | Issue #1 | AGENT_LAUNCHED | Launched @Copilot agent (PID: 12346)
2026-02-16T10:30:00 | Issue #1 | WORKFLOW_COMPLETED | Workflow finished successfully
2026-02-16T11:00:00 | Issue #2 | WORKFLOW_STARTED | Started shortened tier workflow
2026-02-16T11:01:00 | Issue #2 | AGENT_LAUNCHED | Launched @ProjectLead agent (PID: 12347)
2026-02-16T11:16:00 | Issue #2 | AGENT_TIMEOUT_KILL | @ProjectLead timed out, killed process
2026-02-16T11:17:00 | Issue #2 | AGENT_RETRY | Retrying @ProjectLead (attempt 1/2)
2026-02-16T11:18:00 | Issue #2 | AGENT_LAUNCHED | Launched @ProjectLead agent (PID: 12348)
2026-02-16T11:35:00 | Issue #2 | WORKFLOW_COMPLETED | Workflow finished successfully
"""
    log_file.write_text(content)
    return log_file


@pytest.fixture
def sample_workflow_chain():
    """Sample workflow chain configuration for testing."""
    return {
        "full": [
            ("ProjectLead", "Vision & Scope"),
            ("Architect", "Technical Design"),
            ("Copilot", "Implementation")
        ],
        "shortened": [
            ("ProjectLead", "Triage"),
            ("Copilot", "Fix")
        ],
        "fast-track": [
            ("Copilot", "Quick Fix")
        ]
    }

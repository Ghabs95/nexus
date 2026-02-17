# Contributing to Nexus

Thank you for your interest in contributing to Nexus! This guide will help you get started.

## Development Setup

### Prerequisites
- Python 3.8+
- Git
- GitHub CLI (`gh`)
- Telegram account
- Basic understanding of async Python

### Setup Development Environment

```bash
# Fork and clone the repository
git clone https://github.com/YourUsername/nexus.git
cd nexus

# Create virtual environment
python3 -m venv venv
source venv/bin/activate  # On Windows: venv\Scripts\activate

# Install dependencies
pip install -r requirements.txt

# Install development dependencies
pip install pytest pytest-mock pytest-asyncio pytest-cov black flake8

# Copy and configure environment variables
cp vars.secret.example vars.secret
# Edit vars.secret with your credentials
```

### Running Tests

```bash
# Run all tests
pytest -v

# Run specific test file
pytest tests/test_notifications.py -v

# Run with coverage report
pytest --cov=src --cov-report=html
# Open htmlcov/index.html in browser

# Run specific test
pytest tests/test_rate_limiter.py::TestRateLimiter::test_basic_limiting -v
```

### Running Locally

```bash
# Terminal 1: Start the bot
source vars.secret
python src/telegram_bot.py

# Terminal 2: Start the processor
source vars.secret
python src/inbox_processor.py

# Terminal 3: Start health check (optional)
source vars.secret
python src/health_check.py
```

## Code Style

### Python Style Guide

We follow PEP 8 with some modifications:

```python
# Use 4 spaces for indentation
# Maximum line length: 100 characters
# Use double quotes for strings
# Type hints for function signatures

def my_function(arg1: str, arg2: int = 10) -> bool:
    """
    Brief description of function.
    
    Args:
        arg1: Description of arg1
        arg2: Description of arg2 (default: 10)
    
    Returns:
        True if successful, False otherwise
    """
    # Implementation
    return True
```

### Format Your Code

```bash
# Auto-format with black (optional but recommended)
black src/ tests/

# Check style with flake8
flake8 src/ tests/ --max-line-length=100
```

### Commit Message Format

```
<type>(<scope>): <subject>

<body>

<footer>
```

**Types:**
- `feat`: New feature
- `fix`: Bug fix
- `docs`: Documentation only
- `style`: Code style changes (formatting, no logic change)
- `refactor`: Code refactoring
- `test`: Adding or updating tests
- `chore`: Maintenance tasks

**Examples:**
```
feat(notifications): add PR review inline keyboard buttons

Added automatic PR detection when workflow completes with approve/reject
buttons for quick review.

Closes #123
```

```
fix(rate-limiter): correct sliding window calculation

Fixed off-by-one error in timestamp comparison that allowed one extra
request beyond the limit.
```

## Project Structure

```
nexus/
├── src/                      # Main application code
│   ├── telegram_bot.py       # Telegram interface (2300+ lines)
│   ├── inbox_processor.py    # Workflow orchestration (1200+ lines)
│   ├── state_manager.py      # State persistence (190 lines)
│   ├── agent_monitor.py      # Timeout & retry logic (370 lines)
│   ├── notifications.py      # Notification system (320 lines)
│   ├── rate_limiter.py       # Rate limiting (330 lines)
│   ├── user_manager.py       # User tracking (360 lines)
│   ├── health_check.py       # Health endpoint (290 lines)
│   ├── config.py             # Configuration (150 lines)
│   ├── models.py             # Data models (180 lines)
│   └── error_handling.py     # Error utilities (180 lines)
├── tests/                    # Test suite (115 tests)
│   ├── conftest.py           # Shared fixtures
│   ├── test_*.py             # Test modules
├── logs/                     # Runtime logs
├── data/                     # Persistent state
├── docs/                     # Additional documentation
├── README.md                 # User guide
├── ARCHITECTURE.md           # System design
├── DEPLOYMENT.md             # Deployment guide
├── CONTRIBUTING.md           # This file
└── requirements.txt          # Python dependencies
```

## Making Changes

### 1. Create a Branch

```bash
# Always branch from main
git checkout main
git pull origin main

# Create feature branch
git checkout -b feat/my-feature

# Or bug fix branch
git checkout -b fix/bug-description
```

### 2. Make Your Changes

#### Adding a New Command

```python
# In src/telegram_bot.py

# 1. Define the handler
@rate_limited("user_global")
async def my_command_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /mycommand command."""
    # Your logic here
    await update.effective_message.reply_text("Response")

# 2. Register in main()
application.add_handler(CommandHandler("mycommand", my_command_handler))

# 3. Add to help text
HELP_TEXT = """
...
/mycommand - Brief description
"""

# 4. Update bot commands (optional)
commands = [
    BotCommand("mycommand", "Brief description"),
]
```

#### Adding a New Notification Type

```python
# In src/notifications.py

def notify_my_event(issue_number: str, extra_data: str) -> bool:
    """
    Send notification for custom event.
    
    Args:
        issue_number: GitHub issue number
        extra_data: Additional context
    
    Returns:
        True if sent successfully
    """
    message = (
        f"🔔 **My Event**\n\n"
        f"Issue: #{issue_number}\n"
        f"Data: {extra_data}"
    )
    
    keyboard = (
        InlineKeyboard()
        .add_button("Action 1", callback_data=f"action1_{issue_number}")
        .add_button("Action 2", callback_data=f"action2_{issue_number}")
    )
    
    return send_notification(message, keyboard=keyboard)
```

#### Adding a New Test

```python
# In tests/test_my_module.py

import pytest
from my_module import my_function

class TestMyFunction:
    """Tests for my_function."""
    
    def test_basic_case(self):
        """Test basic functionality."""
        result = my_function("input")
        assert result == "expected"
    
    def test_edge_case(self):
        """Test edge case handling."""
        with pytest.raises(ValueError):
            my_function(None)
    
    @pytest.mark.asyncio
    async def test_async_function(self):
        """Test async functionality."""
        result = await my_async_function()
        assert result is True
```

### 3. Test Your Changes

```bash
# Run affected tests
pytest tests/test_my_module.py -v

# Run all tests
pytest -v

# Check coverage
pytest --cov=src --cov-report=term-missing

# Aim for:
# - All tests passing
# - New code covered by tests
# - No decrease in overall coverage
```

### 4. Commit Your Changes

```bash
# Stage changes
git add src/my_file.py tests/test_my_file.py

# Commit with descriptive message
git commit -m "feat(module): add new feature

Detailed description of what changed and why.

Closes #123"

# Push to your fork
git push origin feat/my-feature
```

### 5. Create Pull Request

1. Go to GitHub repository
2. Click "New Pull Request"
3. Select your branch
4. Fill in PR template:
   - **Description**: What does this PR do?
   - **Motivation**: Why is this change needed?
   - **Testing**: How was it tested?
   - **Screenshots**: If UI changes
   - **Checklist**: Complete all items

## Testing Guidelines

### Writing Good Tests

**DO:**
- Test one thing per test function
- Use descriptive test names
- Test edge cases and error conditions
- Mock external dependencies (GitHub API, Telegram API)
- Use fixtures for common setup
- Test both success and failure paths

**DON'T:**
- Don't test implementation details
- Don't make tests dependent on each order
- Don't use time.sleep() (use freezegun or mock time)
- Don't hit real APIs in unit tests
- Don't commit failing tests

### Test Fixtures

```python
# In tests/conftest.py

import pytest
from unittest.mock import MagicMock

@pytest.fixture
def mock_telegram_update():
    """Create a mock Telegram update object."""
    update = MagicMock()
    update.effective_message.chat_id = 12345
    update.effective_user.id = 67890
    return update

@pytest.fixture
def mock_context():
    """Create a mock context object."""
    context = MagicMock()
    context.args = []
    return context
```

### Mocking External Services

```python
# Mock Telegram API
@patch('telegram_bot.telegram.Bot.send_message')
async def test_my_handler(mock_send, mock_update, mock_context):
    await my_handler(mock_update, mock_context)
    mock_send.assert_called_once()

# Mock GitHub CLI
@patch('subprocess.run')
def test_github_command(mock_run):
    mock_run.return_value.stdout = '{"number": 123}'
    result = run_gh_command(['issue', 'view', '123'])
    assert result['number'] == 123

# Mock requests
@patch('requests.post')
def test_notification(mock_post):
    mock_post.return_value.status_code = 200
    send_notification("Test message")
    assert mock_post.called
```

## Documentation

### Code Comments

```python
# Use docstrings for functions/classes
def calculate_score(data: dict) -> float:
    """
    Calculate the quality score from analysis data.
    
    Args:
        data: Dictionary containing metrics
            - coverage: float, code coverage percentage
            - complexity: int, cyclomatic complexity
    
    Returns:
        Score between 0.0 and 1.0
    
    Raises:
        ValueError: If data is missing required fields
    """
    pass

# Use inline comments sparingly, only for complex logic
# Good: Explain WHY, not WHAT
result = value * 0.8  # Apply 20% discount for early bird

# Bad: Stating the obvious
result = value * 0.8  # Multiply value by 0.8
```

### README Updates

When adding features, update:
- Feature list in [README.md](README.md#features)
- Command reference in [README.md](README.md#commands)
- Configuration section if adding env vars
- Examples if adding workflow changes

### Architecture Documentation

For significant changes, update [ARCHITECTURE.md](ARCHITECTURE.md):
- Data flow diagrams
- Component descriptions
- API contracts
- State management

## Common Development Tasks

### Adding a Rate Limit

```python
# In src/rate_limiter.py
RATE_LIMITS = {
    "my_feature": {"limit": 10, "window": 60},  # 10 per minute
}

# In src/telegram_bot.py
@rate_limited("my_feature")
async def my_handler(update, context):
    # Handler code
    pass
```

### Adding an Environment Variable

```python
# In src/config.py
MY_NEW_CONFIG = os.getenv("MY_NEW_CONFIG", "default_value")

# In vars.secret
MY_NEW_CONFIG=production_value

# In README.md and DEPLOYMENT.md
# Document the new variable with description
```

### Adding a New Agent to Workflow

```python
# In src/config.py
WORKFLOW_CHAIN = {
    "full": [
        ("ProjectLead", "Triage and route"),
        ("MyNewAgent", "My agent's step"),  # Add here
        ("NextAgent", "Following step"),
        # ...
    ]
}

FINAL_AGENTS = {
    "full": "FinalAgentName",  # Update if this agent is last
}
```

### Debugging Tips

```python
# Add logging
import logging
logger = logging.getLogger(__name__)
logger.info(f"Debug info: {variable}")

# Use breakpoint() for interactive debugging
def my_function():
    breakpoint()  # Execution pauses here
    # Continue with 'c', step with 'n', inspect with 'p variable'

# Use print for quick checks (remove before commit)
print(f"DEBUG: {variable}")
```

## Pull Request Process

1. **Fork the repository**
2. **Create your feature branch** (`git checkout -b feat/my-feature`)
3. **Make your changes** with tests
4. **Run the full test suite** (`pytest -v`)
5. **Commit your changes** with descriptive messages
6. **Push to your fork** (`git push origin feat/my-feature`)
7. **Create Pull Request** with description
8. **Address review comments** if any
9. **Wait for approval** and merge

## Code Review Checklist

Reviewers will check:
- [ ] Code follows style guide
- [ ] Tests added for new functionality
- [ ] All tests passing
- [ ] Documentation updated
- [ ] No breaking changes (or documented)
- [ ] Commit messages are clear
- [ ] No secrets in code
- [ ] Performance impact considered
- [ ] Error handling appropriate

## Getting Help

- **Documentation**: Check [README.md](README.md), [ARCHITECTURE.md](ARCHITECTURE.md)
- **Issues**: Search existing issues or create new one
- **Code**: Read existing implementations for examples
- **Tests**: Look at test files for usage patterns

## License

By contributing, you agree that your contributions will be licensed under the same license as the project.

## Questions?

If you have questions about contributing:
1. Check this guide thoroughly
2. Search existing issues
3. Create a new issue with "question" label
4. Be specific about what you need help with

Thank you for contributing to Nexus! 🚀

#!/usr/bin/env python3
"""
GitHub Webhook Server - Receives and processes GitHub webhook events

This service replaces the polling-based GitHub comment checking with 
real-time webhook event processing for faster response times.

Event handlers:
- issues.opened: Convert GitHub issue to markdown task in .nexus/inbox/ for triage
- issue_comment.created: Detect workflow completion and chain to next agent
- pull_request.opened/synchronized: Notify about new PRs
- pull_request_review.submitted: Notify about PR reviews
"""

import json
import logging
import os
import sys
from flask import Flask, request, jsonify

# Add parent directory to path for imports
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from config import (
    BASE_DIR, 
    PROJECT_CONFIG,
    WEBHOOK_PORT,
    WEBHOOK_SECRET,
    LOGS_DIR
)
from agent_launcher import launch_next_agent
from plugin_runtime import get_github_webhook_policy_plugin
from notifications import (
    send_notification,
    notify_workflow_completed,
    send_telegram_alert
)

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler(os.path.join(LOGS_DIR, 'webhook.log')),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger(__name__)

app = Flask(__name__)

# Track processed events to avoid duplicates
processed_events = set()


def _get_webhook_policy():
    """Get framework webhook policy plugin (best effort)."""
    try:
        return get_github_webhook_policy_plugin(cache_key="github-webhook-policy:webhook")
    except Exception as exc:
        logger.warning("Webhook policy plugin unavailable, using inline parsing: %s", exc)
        return None


def _repo_to_project_key(repo_name: str) -> str:
    """Best-effort mapping from repository full_name to configured project key."""
    policy = _get_webhook_policy()
    if policy and hasattr(policy, "resolve_project_key"):
        return policy.resolve_project_key(repo_name, PROJECT_CONFIG, default_project="nexus")

    for project_key, project_cfg in PROJECT_CONFIG.items():
        if isinstance(project_cfg, dict) and project_cfg.get("github_repo") == repo_name:
            return project_key
    return "nexus"


def _effective_merge_policy(repo_name: str) -> str:
    """Resolve effective merge policy for a repo.

    Returns one of: always, workflow-based, never.
    """
    policy = _get_webhook_policy()
    if policy and hasattr(policy, "resolve_merge_policy"):
        return policy.resolve_merge_policy(repo_name, PROJECT_CONFIG, default_policy="always")

    project_key = _repo_to_project_key(repo_name)
    project_cfg = PROJECT_CONFIG.get(project_key, {})
    if isinstance(project_cfg, dict) and project_cfg.get("require_human_merge_approval"):
        return str(project_cfg.get("require_human_merge_approval"))
    return str(PROJECT_CONFIG.get("require_human_merge_approval", "always"))


def _notify_lifecycle(message: str) -> bool:
    """Send lifecycle notification via abstract notifier, fallback to Telegram alert."""
    if send_notification(message):
        return True
    return send_telegram_alert(message)


def verify_signature(payload_body, signature_header):
    """Verify GitHub webhook signature."""
    policy = _get_webhook_policy()
    if policy and hasattr(policy, "verify_signature"):
        verified = bool(policy.verify_signature(payload_body, signature_header, WEBHOOK_SECRET))
        if not WEBHOOK_SECRET and verified:
            logger.warning("⚠️ WEBHOOK_SECRET not configured - accepting all requests (INSECURE!)")
        if not verified:
            logger.error("❌ Signature verification failed")
        return verified

    if not WEBHOOK_SECRET:
        logger.warning("⚠️ WEBHOOK_SECRET not configured - accepting all requests (INSECURE!)")
        return True
    
    if not signature_header:
        logger.error("❌ No X-Hub-Signature-256 header")
        return False
    
    # GitHub sends signature as "sha256=<hash>"
    hash_algorithm, github_signature = signature_header.split('=')
    if hash_algorithm != 'sha256':
        logger.error(f"❌ Unsupported hash algorithm: {hash_algorithm}")
        return False
    
    # Calculate expected signature
    mac = hmac.new(
        WEBHOOK_SECRET.encode('utf-8'),
        msg=payload_body,
        digestmod=hashlib.sha256
    )
    expected_signature = mac.hexdigest()
    
    # Constant-time comparison to prevent timing attacks
    if not hmac.compare_digest(expected_signature, github_signature):
        logger.error("❌ Signature verification failed")
        return False
    
    return True


def handle_issue_opened(payload):
    """
    Handle issues.opened events.
    
    Converts GitHub issue to a markdown task file in .nexus/inbox/
    for the inbox processor to route to the appropriate agent based on type.
    
    Agent types (abstract roles):
    - triage: Initial issue analysis and classification
    - escalation: High-priority/urgent issues (escalate to senior agent)
    - debug: Bug analysis and root cause
    
    The actual agent implementing each type is defined in the workflow YAML.
    """
    policy = _get_webhook_policy()
    if policy and hasattr(policy, "parse_issue_event"):
        event = policy.parse_issue_event(payload)
        action = event.get("action")
        issue_number = event.get("number", "")
        issue_title = event.get("title", "")
        issue_body = event.get("body", "")
        issue_author = event.get("author", "")
        issue_url = event.get("url", "")
        issue_labels = event.get("labels", [])
        repo_name = event.get("repo", "unknown")
        closed_by = event.get("closed_by", "unknown")
    else:
        action = payload.get("action")
        issue = payload.get("issue", {})
        repository = payload.get("repository", {})
        issue_number = str(issue.get("number", ""))
        issue_title = issue.get("title", "")
        issue_body = issue.get("body", "")
        issue_author = issue.get("user", {}).get("login", "")
        issue_url = issue.get("html_url", "")
        issue_labels = [l.get("name") for l in issue.get("labels", [])]
        repo_name = repository.get("full_name", "unknown")
        closed_by = payload.get("sender", {}).get("login", "unknown")
    
    logger.info(f"📋 New issue: #{issue_number} - {issue_title} by {issue_author}")
    
    # Handle issue close notifications
    if action == "closed":
        if policy and hasattr(policy, "build_issue_closed_message"):
            message = policy.build_issue_closed_message(event if 'event' in locals() else {
                "number": issue_number,
                "title": issue_title,
                "repo": repo_name,
                "closed_by": closed_by,
                "url": issue_url,
            })
        else:
            message = (
                "🔒 **Issue Closed**\n\n"
                f"Issue: #{issue_number}\n"
                f"Title: {issue_title}\n"
                f"Repository: {repo_name}\n"
                f"Closed by: @{closed_by}\n\n"
                f"🔗 {issue_url}"
            )
        _notify_lifecycle(message)
        return {"status": "issue_closed_notified", "issue": issue_number}

    # Only process open actions for task creation
    if action != "opened":
        return {"status": "ignored", "reason": f"action is {action}, not opened"}
    
    # Skip issues created by Nexus itself (inbox processor → create_github_issue).
    # These already have an agent launched via the standard task processing path.
    # Detect via workflow labels that create_github_issue() always applies.
    workflow_labels = [l for l in issue_labels if l.startswith("workflow:")]
    if workflow_labels:
        logger.info(f"⏭️ Skipping self-created issue #{issue_number} (has workflow label: {workflow_labels})")
        return {"status": "ignored", "reason": "self-created issue (has workflow label)"}
    
    # Also skip if an active task file already exists for this issue
    try:
        import os
        from config import PROJECT_CONFIG, BASE_DIR
        from config import get_tasks_active_dir
        for _key, _cfg in PROJECT_CONFIG.items():
            if isinstance(_cfg, dict) and _cfg.get("github_repo") == repo_name:
                _ws = os.path.join(BASE_DIR, _cfg.get("workspace", ""))
                _active = get_tasks_active_dir(_ws)
                _task = os.path.join(_active, f"issue_{issue_number}.md")
                if os.path.exists(_task):
                    logger.info(f"⏭️ Skipping issue #{issue_number} — active task file already exists: {_task}")
                    return {"status": "ignored", "reason": "task file already exists"}
                break
    except Exception as e:
        logger.warning(f"Could not check for existing task file: {e}")
    
    # Determine which agent type to route to
    try:
        from config import PROJECT_CONFIG, get_inbox_dir
        
        triage_config = PROJECT_CONFIG.get("github_issue_triage", {})
        agent_type = triage_config.get("default_agent_type", "triage")
        
        # Check for label-based override
        label_based = triage_config.get("label_based", {})
        for label in issue_labels:
            if label in label_based:
                agent_type = label_based[label]
                logger.info(f"  Label '{label}' → routing to agent_type: {agent_type}")
                break
        
        # Check for repo-specific override
        per_repo = triage_config.get("per_repo", {})
        if repo_name in per_repo:
            agent_type = per_repo[repo_name]
            logger.info(f"  Repository '{repo_name}' → routing to agent_type: {agent_type}")
        
    except Exception as e:
        logger.warning(f"⚠️ Could not load triage config, using default: {e}")
        from config import PROJECT_CONFIG
        triage_config = PROJECT_CONFIG.get("github_issue_triage", {})
        agent_type = triage_config.get("default_agent_type", "triage")
    
    # Create markdown task file for inbox processor
    try:
        from pathlib import Path
        from config import get_inbox_dir, PROJECT_CONFIG, BASE_DIR
        import os
        
        # Determine project from repository name
        project_workspace = None
        for project_key, project_cfg in PROJECT_CONFIG.items():
            if isinstance(project_cfg, dict) and project_cfg.get("github_repo") == repo_name:
                project_workspace = project_cfg.get("workspace")
                logger.info(f"📌 Mapped repository '{repo_name}' → project '{project_key}' (workspace: {project_workspace})")
                break
        
        if not project_workspace:
            logger.warning(f"⚠️ No project mapping for repository '{repo_name}', using default 'ghabs'")
            project_workspace = "ghabs"
        
        # Get inbox directory for the project's workspace
        workspace_abs = os.path.join(BASE_DIR, project_workspace)
        inbox_dir = get_inbox_dir(workspace_abs)
        Path(inbox_dir).mkdir(parents=True, exist_ok=True)
        
        # Create task filename (issue number based)
        task_file = Path(inbox_dir) / f"issue_{issue_number}.md"
        
        # Create markdown content with agent type and source metadata
        # The inbox processor will route this to the appropriate agent based on type
        # SOURCE=webhook tells inbox processor to skip GitHub issue creation (already exists)
        task_content = f"""# Issue #{issue_number}: {issue_title}

**From:** @{issue_author}  
**URL:** {issue_url}  
**Repository:** {repo_name}  
**Agent Type:** {agent_type}
**Source:** webhook
**Issue Number:** {issue_number}

## Description

{issue_body or "_(No description provided)_"}

## Labels

{', '.join([f"`{l}`" for l in issue_labels]) if issue_labels else "_None_"}

## Status: Ready for {agent_type} agent

This issue will be routed to the {agent_type} agent as defined in the workflow.
The actual agent assignment depends on the current project's workflow configuration.
"""
        
        # Write to file
        task_file.write_text(task_content)
        logger.info(f"✅ Created task file: {task_file} (agent_type: {agent_type})")

        if policy and hasattr(policy, "build_issue_created_message"):
            message = policy.build_issue_created_message(
                event if 'event' in locals() else {
                    "number": issue_number,
                    "title": issue_title,
                    "repo": repo_name,
                    "author": issue_author,
                    "url": issue_url,
                },
                agent_type,
            )
        else:
            message = (
                "📥 **Issue Created**\n\n"
                f"Issue: #{issue_number}\n"
                f"Title: {issue_title}\n"
                f"Repository: {repo_name}\n"
                f"Author: @{issue_author}\n"
                f"Routed to: `{agent_type}`\n\n"
                f"🔗 {issue_url}"
            )
        _notify_lifecycle(message)
        
        return {
            "status": "task_created",
            "issue": issue_number,
            "task_file": str(task_file),
            "title": issue_title,
            "agent_type": agent_type,
            "repository": repo_name
        }
        
    except Exception as e:
        logger.error(f"❌ Error creating task file for issue #{issue_number}: {e}", exc_info=True)
        send_telegram_alert(f"Issue processing error for #{issue_number}: {str(e)}")
        return {
            "status": "error",
            "issue": issue_number,
            "error": str(e)
        }


def handle_issue_comment(payload):
    """
    Handle issue_comment events.
    
    Detects workflow completion markers in comments and chains to next agent.
    """
    action = payload.get("action")
    comment = payload.get("comment", {})
    issue = payload.get("issue", {})
    
    comment_id = comment.get("id")
    comment_body = comment.get("body", "")
    issue_number = str(issue.get("number", ""))
    comment_author = comment.get("user", {}).get("login", "")
    
    logger.info(f"📝 Issue comment: #{issue_number} by {comment_author} (action: {action})")
    
    # Only process created comments
    if action != "created":
        return {"status": "ignored", "reason": f"action is {action}, not created"}
    
    # Ignore non-copilot comments
    if comment_author != "copilot":
        return {"status": "ignored", "reason": "not from copilot"}
    
    # Check if already processed
    event_key = f"comment_{comment_id}"
    if event_key in processed_events:
        logger.info(f"⏭️ Already processed comment {comment_id}")
        return {"status": "duplicate"}
    
    # Detect workflow completion
    completion_markers = [
        r"workflow\s+complete",
        r"ready\s+for\s+review",
        r"ready\s+to\s+merge",
        r"implementation\s+complete",
        r"all\s+steps\s+completed"
    ]
    
    import re
    is_completion = any(re.search(pattern, comment_body, re.IGNORECASE) 
                       for pattern in completion_markers)
    
    # Look for next agent mention
    next_agent_match = re.search(r'@(\w+)', comment_body)
    next_agent = next_agent_match.group(1) if next_agent_match else None
    
    if is_completion and not next_agent:
        # Workflow completed - check for PR and notify
        logger.info(f"✅ Workflow completion detected for issue #{issue_number}")
        
        # Determine project from issue labels or body
        project = determine_project(issue)
        
        # Check for linked PR and notify
        from inbox_processor import check_and_notify_pr
        check_and_notify_pr(issue_number, project)
        
        # Mark as processed
        processed_events.add(event_key)
        return {"status": "workflow_completed", "issue": issue_number}
    
    elif next_agent:
        # Chain to next agent
        logger.info(f"🔗 Chaining to @{next_agent} for issue #{issue_number}")
        
        try:
            success = launch_next_agent(
                issue_number=issue_number,
                next_agent=next_agent,
                trigger_source="github_webhook"
            )
            
            if success:
                processed_events.add(event_key)
                return {
                    "status": "agent_launched",
                    "issue": issue_number,
                    "next_agent": next_agent
                }
            else:
                return {
                    "status": "launch_failed",
                    "issue": issue_number,
                    "next_agent": next_agent
                }
        except Exception as e:
            logger.error(f"❌ Failed to launch next agent: {e}")
            return {"status": "error", "message": str(e)}
    
    return {"status": "no_action"}


def handle_pull_request(payload):
    """Handle pull_request events (opened, synchronized, etc.)."""
    policy = _get_webhook_policy()
    if policy and hasattr(policy, "parse_pull_request_event"):
        event = policy.parse_pull_request_event(payload)
        action = event.get("action")
        pr_number = event.get("number")
        pr_title = event.get("title", "")
        pr_author = event.get("author", "")
        pr_url = event.get("url", "")
        repo_name = event.get("repo", "unknown")
        merged = bool(event.get("merged"))
        merged_by = event.get("merged_by", "unknown")
    else:
        action = payload.get("action")
        pr = payload.get("pull_request", {})
        repository = payload.get("repository", {})
        pr_number = pr.get("number")
        pr_title = pr.get("title", "")
        pr_author = pr.get("user", {}).get("login", "")
        pr_url = pr.get("html_url", "")
        repo_name = repository.get("full_name", "unknown")
        merged = bool(pr.get("merged"))
        merged_by = pr.get("merged_by", {}).get("login", "unknown")
    
    logger.info(f"🔀 Pull request #{pr_number}: {action} by {pr_author}")

    if action == "opened":
        if policy and hasattr(policy, "build_pr_created_message"):
            message = policy.build_pr_created_message(event if 'event' in locals() else {
                "number": pr_number,
                "title": pr_title,
                "repo": repo_name,
                "author": pr_author,
                "url": pr_url,
            })
        else:
            message = (
                "🔀 **PR Created**\n\n"
                f"PR: #{pr_number}\n"
                f"Title: {pr_title}\n"
                f"Repository: {repo_name}\n"
                f"Author: @{pr_author}\n\n"
                f"🔗 {pr_url}"
            )
        _notify_lifecycle(message)
        return {
            "status": "pr_opened_notified",
            "pr": pr_number,
            "action": action,
        }

    if action == "closed" and merged:
        merge_policy = _effective_merge_policy(repo_name)

        # Notify merge only when manual merge approval is not enforced.
        should_notify = (
            policy.should_notify_pr_merged(merge_policy)
            if policy and hasattr(policy, "should_notify_pr_merged")
            else (merge_policy != "always")
        )
        if should_notify:
            if policy and hasattr(policy, "build_pr_merged_message"):
                message = policy.build_pr_merged_message(
                    event if 'event' in locals() else {
                        "number": pr_number,
                        "title": pr_title,
                        "repo": repo_name,
                        "merged_by": merged_by,
                        "url": pr_url,
                    },
                    merge_policy,
                )
            else:
                message = (
                    "✅ **PR Merged**\n\n"
                    f"PR: #{pr_number}\n"
                    f"Title: {pr_title}\n"
                    f"Repository: {repo_name}\n"
                    f"Merged by: @{merged_by}\n"
                    f"Policy: `{merge_policy}`\n\n"
                    f"🔗 {pr_url}"
                )
            _notify_lifecycle(message)
            return {
                "status": "pr_merged_notified",
                "pr": pr_number,
                "action": action,
                "merge_policy": merge_policy,
            }

        logger.info(
            "Skipping PR merged notification for #%s due to manual merge policy '%s'",
            pr_number,
            merge_policy,
        )
        return {
            "status": "pr_merged_skipped_manual_review",
            "pr": pr_number,
            "action": action,
            "merge_policy": merge_policy,
        }
    
    # For now, just log - can add PR notifications later
    return {
        "status": "logged",
        "pr": pr_number,
        "action": action
    }


def handle_pull_request_review(payload):
    """Handle pull_request_review events."""
    action = payload.get("action")
    review = payload.get("review", {})
    pr = payload.get("pull_request", {})
    
    pr_number = pr.get("number")
    review_state = review.get("state")
    reviewer = review.get("user", {}).get("login", "")
    
    logger.info(f"👀 PR review #{pr_number}: {review_state} by {reviewer}")
    
    # For now, just log - can add review notifications later
    return {
        "status": "logged",
        "pr": pr_number,
        "state": review_state
    }


def determine_project(issue):
    """
    Determine project from issue labels or body.
    
    Returns: project key (casit, wlbl, bm) or None
    """
    # Check labels first
    labels = issue.get("labels", [])
    for label in labels:
        label_name = label.get("name", "").lower()
        if "casit" in label_name or "caseitalia" in label_name:
            return "casit"
        elif "wlbl" in label_name or "wallible" in label_name:
            return "wlbl"
        elif "bm" in label_name or "biome" in label_name:
            return "bm"
    
    # Check issue body for project mentions
    body = issue.get("body", "").lower()
    if "caseitalia" in body or "case-italia" in body:
        return "casit"
    elif "wallible" in body or "wlbl" in body:
        return "wlbl"
    elif "biome" in body or "biomejs" in body:
        return "bm"
    
    # Default to casit if can't determine
    logger.warning(f"⚠️ Could not determine project for issue #{issue.get('number')}, defaulting to casit")
    return "casit"


@app.route('/health', methods=['GET'])
def health_check():
    """Health check endpoint."""
    return jsonify({
        "status": "healthy",
        "service": "nexus-webhook",
        "version": "1.0.0"
    }), 200


@app.route('/webhook', methods=['POST'])
def webhook():
    """Main webhook endpoint for GitHub events."""
    
    # Verify signature
    signature = request.headers.get('X-Hub-Signature-256')
    if not verify_signature(request.data, signature):
        logger.error("❌ Webhook signature verification failed")
        return jsonify({"error": "Invalid signature"}), 403
    
    # Parse event type
    event_type = request.headers.get('X-GitHub-Event')
    if not event_type:
        logger.error("❌ No X-GitHub-Event header")
        return jsonify({"error": "No event type"}), 400
    
    payload = request.json
    delivery_id = request.headers.get('X-GitHub-Delivery')
    
    logger.info(f"📨 Webhook received: {event_type} (delivery: {delivery_id})")
    
    # Route to appropriate handler
    try:
        if event_type == "issues":
            result = handle_issue_opened(payload)
        elif event_type == "issue_comment":
            result = handle_issue_comment(payload)
        elif event_type == "pull_request":
            result = handle_pull_request(payload)
        elif event_type == "pull_request_review":
            result = handle_pull_request_review(payload)
        elif event_type == "ping":
            logger.info("🏓 Ping received")
            result = {"status": "pong"}
        else:
            logger.info(f"⏭️ Unhandled event type: {event_type}")
            result = {"status": "unhandled", "event_type": event_type}
        
        return jsonify(result), 200
    
    except Exception as e:
        logger.error(f"❌ Error processing webhook: {e}", exc_info=True)
        send_telegram_alert(f"Webhook Error: {str(e)}")
        return jsonify({"error": str(e)}), 500


@app.route('/', methods=['GET'])
def index():
    """Root endpoint - basic info."""
    return jsonify({
        "service": "Nexus GitHub Webhook Server",
        "version": "1.0.0",
        "endpoints": {
            "/webhook": "POST - GitHub webhook events",
            "/health": "GET - Health check"
        }
    }), 200


def main():
    """Start the webhook server."""
    port = WEBHOOK_PORT
    logger.info(f"🚀 Starting webhook server on port {port}")
    logger.info(f"📍 Webhook URL: http://localhost:{port}/webhook")
    
    if not WEBHOOK_SECRET:
        logger.warning("⚠️ WEBHOOK_SECRET not configured - signature verification disabled!")
    
    # Run Flask app
    app.run(
        host='0.0.0.0',
        port=port,
        debug=False,
        threaded=True
    )


if __name__ == "__main__":
    main()

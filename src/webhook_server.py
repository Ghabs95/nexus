#!/usr/bin/env python3
"""
GitHub Webhook Server - Receives and processes GitHub webhook events

This service replaces the polling-based GitHub comment checking with 
real-time webhook event processing for faster response times.

Event handlers:
- issue_comment.created: Detect workflow completion and chain to next agent
- pull_request.opened/synchronized: Notify about new PRs
- pull_request_review.submitted: Notify about PR reviews
"""

import hashlib
import hmac
import json
import logging
import os
import sys
from flask import Flask, request, jsonify

# Add parent directory to path for imports
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from config import (
    BASE_DIR, 
    GITHUB_AGENTS_REPO, 
    WEBHOOK_PORT,
    WEBHOOK_SECRET
)
from agent_launcher import launch_next_agent
from notifications import (
    notify_workflow_completed,
    send_telegram_alert
)

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler('/home/ubuntu/git/ghabs/nexus/logs/webhook.log'),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger(__name__)

app = Flask(__name__)

# Track processed events to avoid duplicates
processed_events = set()


def verify_signature(payload_body, signature_header):
    """Verify GitHub webhook signature."""
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
    action = payload.get("action")
    pr = payload.get("pull_request", {})
    
    pr_number = pr.get("number")
    pr_title = pr.get("title", "")
    pr_author = pr.get("user", {}).get("login", "")
    
    logger.info(f"🔀 Pull request #{pr_number}: {action} by {pr_author}")
    
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
        if event_type == "issue_comment":
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

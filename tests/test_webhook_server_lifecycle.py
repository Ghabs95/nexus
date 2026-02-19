"""Tests for webhook lifecycle notifications."""

from unittest.mock import patch


def _issue_payload(action: str) -> dict:
    return {
        "action": action,
        "issue": {
            "number": 41,
            "title": "Example issue",
            "body": "Body",
            "html_url": "https://github.com/acme/repo/issues/41",
            "user": {"login": "alice"},
            "labels": [],
        },
        "repository": {"full_name": "Ghabs95/nexus-core"},
        "sender": {"login": "bob"},
    }


def _pr_payload(action: str, merged: bool = False) -> dict:
    return {
        "action": action,
        "pull_request": {
            "number": 10,
            "title": "Fix issue",
            "html_url": "https://github.com/acme/repo/pull/10",
            "user": {"login": "dev"},
            "merged": merged,
            "merged_by": {"login": "maintainer"},
        },
        "repository": {"full_name": "Ghabs95/nexus-core"},
    }


@patch("webhook_server._notify_lifecycle", return_value=True)
def test_issue_closed_sends_notification(mock_notify):
    from webhook_server import handle_issue_opened

    result = handle_issue_opened(_issue_payload("closed"))

    assert result["status"] == "issue_closed_notified"
    mock_notify.assert_called_once()


@patch("webhook_server._notify_lifecycle", return_value=True)
def test_pr_opened_sends_notification(mock_notify):
    from webhook_server import handle_pull_request

    result = handle_pull_request(_pr_payload("opened"))

    assert result["status"] == "pr_opened_notified"
    mock_notify.assert_called_once()


@patch("webhook_server._effective_merge_policy", return_value="always")
@patch("webhook_server._notify_lifecycle", return_value=True)
def test_pr_merged_skips_when_manual_review_policy(mock_notify, mock_policy):
    from webhook_server import handle_pull_request

    result = handle_pull_request(_pr_payload("closed", merged=True))

    assert result["status"] == "pr_merged_skipped_manual_review"
    mock_policy.assert_called_once()
    mock_notify.assert_not_called()


@patch("webhook_server._effective_merge_policy", return_value="never")
@patch("webhook_server._notify_lifecycle", return_value=True)
def test_pr_merged_notifies_when_policy_allows(mock_notify, mock_policy):
    from webhook_server import handle_pull_request

    result = handle_pull_request(_pr_payload("closed", merged=True))

    assert result["status"] == "pr_merged_notified"
    mock_policy.assert_called_once()
    mock_notify.assert_called_once()

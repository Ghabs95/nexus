from report_scheduler import ReportScheduler


class _DummyBot:
    async def send_message(self, **_kwargs):
        return None


def test_tracked_issues_status_normalizes_legacy_entries(monkeypatch):
    scheduler = ReportScheduler(bot=_DummyBot(), chat_id=1)

    monkeypatch.setattr(
        scheduler.state_manager,
        "load_tracked_issues",
        lambda: {
            "1": {"added_at": "2026-02-18T02:15:53.142355", "last_seen_state": None, "last_seen_labels": []},
            "2": {"status": "closed", "project": "wallible"},
        },
    )

    status = scheduler._get_tracked_issues_status()

    assert status["total_issues"] == 2
    assert status["status_counts"]["active"] == 1
    assert status["status_counts"]["closed"] == 1

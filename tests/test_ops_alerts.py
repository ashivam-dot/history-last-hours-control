from datetime import datetime, timezone
from unittest.mock import patch

from control import ops_alerts


UTC = timezone.utc


def test_first_slot_starts_after_activation_and_waits_for_schedule_grace():
    watch = ops_alerts.WATCHES[0]
    activated = datetime(2026, 10, 4, 11, 30, tzinfo=UTC)
    assert ops_alerts.last_due(datetime(2026, 10, 4, 17, 59, tzinfo=UTC), activated, watch) is None
    assert ops_alerts.last_due(datetime(2026, 10, 4, 18, 0, tzinfo=UTC), activated, watch) == datetime(
        2026, 10, 4, 16, 30, tzinfo=UTC)


def test_manual_run_does_not_hide_missing_scheduled_slot():
    slot = datetime(2026, 10, 4, 16, 30, tzinfo=UTC)
    manual = {"event": "workflow_dispatch", "head_branch": "main",
              "created_at": "2026-10-04T17:00:00Z"}
    delayed_schedule = {**manual, "event": "schedule",
                        "created_at": "2026-10-04T17:20:00Z"}
    assert not ops_alerts.scheduled_run_present([manual], slot)
    assert ops_alerts.scheduled_run_present([manual, delayed_schedule], slot)


def test_old_failure_event_uses_latest_completed_run_before_alerting():
    watch = ops_alerts.WATCHES[0]
    old = {"id": 12, "name": watch.name, "head_branch": "main", "status": "completed",
           "created_at": "2026-10-04T16:30:00Z", "conclusion": "failure",
           "html_url": "https://github.com/ashivam-dot/history-last-hours-control/actions/runs/12"}
    recovered = {**old, "id": 13, "created_at": "2026-10-04T17:00:00Z",
                 "conclusion": "success"}
    event = {"repository": {"full_name": ops_alerts.REPOSITORY}, "workflow_run": old}
    with patch.object(ops_alerts, "runs_for", return_value=[old, recovered]), \
         patch.object(ops_alerts, "sync_issue", return_value="resolved") as sync:
        assert ops_alerts.from_workflow_run(event, "token") == "resolved"
    sync.assert_called_once_with("token", watch, "failed", None)


def test_disabled_automation_clears_missing_alerts_without_querying_runs():
    with patch.object(ops_alerts, "sync_issue", return_value="clear") as sync, \
         patch.object(ops_alerts, "runs_for") as runs:
        result = ops_alerts.check_missed(datetime.now(UTC), "token", False, "")
    assert result == {"control": "clear", "delivery": "clear"}
    assert sync.call_count == 2
    runs.assert_not_called()

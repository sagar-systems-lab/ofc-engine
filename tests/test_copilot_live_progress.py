from datetime import datetime, timedelta, timezone

from ofc_engine.copilot_hosted import HostedCopilot


def test_pending_session_progress_uses_original_server_admission_time():
    created = datetime.now(timezone.utc) - timedelta(seconds=70)
    waiting = HostedCopilot._pending_status("cp-run-" + "a" * 32, created)
    assert waiting["status"] == "PROCESSING"
    assert waiting["admitted_at_utc"] == created.isoformat()
    assert "stage" not in waiting
    assert "provider_calls" not in waiting


def test_stale_admission_remains_fail_closed_without_retry():
    created = datetime.now(timezone.utc) - timedelta(minutes=4)
    stale = HostedCopilot._pending_status("cp-run-" + "b" * 32, created)
    assert stale["status"] == "BLOCKED"
    assert stale["reason_code"] == "WORKFLOW_INTERRUPTED"

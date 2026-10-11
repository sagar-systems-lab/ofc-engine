"""Durable fixture replay and HTTP trust-boundary tests."""
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from http.server import ThreadingHTTPServer
import json
import sqlite3
from threading import Thread
from urllib.error import HTTPError
from urllib.request import Request, urlopen

import pytest

from ofc_engine.run_history import CorruptRun, RunConflict, RunHistory


def snapshot(scenario="repair", *, revision=1):
    events = [
        {"sequence": 0, "state": "BASELINE_FROZEN", "revision": 0},
        {"sequence": 1, "state": "EVALUATED", "revision": 0},
        {"sequence": 2, "state": "INTEGRITY_ONLY", "revision": revision},
    ]
    return {
        "scenario": scenario, "demo_mode": "SYNTHETIC", "source_mode": "SYNTHETIC",
        "release_authorized": False, "external_action_executed": False,
        "provider_authenticated": False, "current_revision": revision,
        "last_event_sequence": 2, "timeline": events,
    }


def test_creation_restart_and_full_event_replay(tmp_path):
    db = tmp_path / "runs.sqlite3"
    history = RunHistory(db)
    first = history.create(scenario="repair", request_id="request-123", snapshot=snapshot())
    assert first.created and len(first.run_id) == 32
    reopened = RunHistory(db)
    assert reopened.get(first.run_id).snapshot == snapshot()
    events = reopened.events(first.run_id, after_sequence=-1)
    assert [event["sequence"] for event in events["events"]] == [0, 1, 2]
    assert [event["sequence"] for event in reopened.events(first.run_id, after_sequence=0)["events"]] == [1, 2]
    assert reopened.events(first.run_id, after_sequence=2)["events"] == []
    assert events["release_authorized"] is False


def test_same_request_id_replays_idempotently_after_restart(tmp_path):
    path = tmp_path / "runs.sqlite3"
    first = RunHistory(path).create(scenario="repair", request_id="request-123", snapshot=snapshot())
    second = RunHistory(path).create(scenario="repair", request_id="request-123", snapshot=snapshot())
    assert first.run_id == second.run_id and not second.created
    with sqlite3.connect(path) as db:
        assert db.execute("select count(*) from runs").fetchone()[0] == 1


def test_payload_conflicts_are_not_silent_retries(tmp_path):
    history = RunHistory(tmp_path / "runs.sqlite3")
    history.create(scenario="repair", request_id="request-123", snapshot=snapshot())
    with pytest.raises(RunConflict):
        history.create(scenario="outage", request_id="request-123", snapshot=snapshot("outage"))
    changed = snapshot()
    changed["some_new_field"] = "unapproved"
    replay = history.create(scenario="repair", request_id="request-123", snapshot=changed)
    assert replay.snapshot == snapshot() and not replay.created


def test_concurrent_creation_is_single_write(tmp_path):
    path = tmp_path / "runs.sqlite3"
    RunHistory(path)
    def create(_):
        return RunHistory(path).create(scenario="repair", request_id="request-123", snapshot=snapshot()).run_id
    with ThreadPoolExecutor(max_workers=8) as pool:
        ids = list(pool.map(create, range(16)))
    assert len(set(ids)) == 1
    with sqlite3.connect(path) as db:
        assert db.execute("select count(*) from runs").fetchone()[0] == 1


@pytest.mark.parametrize("mode", ["VERIFIED_LIVE", "RECORDED_REDACTED"])
def test_no_false_provider_status_or_release(tmp_path, mode):
    run = snapshot()
    run["source_mode"] = mode
    with pytest.raises(ValueError):
        RunHistory(tmp_path / "runs.sqlite3").create(scenario="repair", request_id="request-123", snapshot=run)
    run = snapshot()
    run["release_authorized"] = True
    with pytest.raises(ValueError):
        RunHistory(tmp_path / "runs.sqlite3").create(scenario="repair", request_id="request-123", snapshot=run)


@pytest.mark.parametrize("field,value", [
    ("last_event_sequence", 5), ("timeline", []),
    ("timeline", [{"sequence": 5, "state": "PASS", "revision": 0}]),
    ("current_revision", -1), ("provider_authenticated", True),
])
def test_invalid_snapshot_rejected(tmp_path, field, value):
    run = snapshot()
    run[field] = value
    with pytest.raises(ValueError):
        RunHistory(tmp_path / "runs.sqlite3").create(scenario="repair", request_id="request-123", snapshot=run)


def test_corrupt_snapshot_detected_before_replay(tmp_path):
    path = tmp_path / "runs.sqlite3"
    hist = RunHistory(path)
    record = hist.create(scenario="repair", request_id="request-123", snapshot=snapshot())
    with sqlite3.connect(path) as db:
        db.execute("update runs set snapshot_json='{}' where run_id=?", (record.run_id,))
    with pytest.raises(CorruptRun):
        hist.get(record.run_id)
    with pytest.raises(CorruptRun):
        hist.events(record.run_id, after_sequence=0)
    with pytest.raises(CorruptRun):
        hist.create(scenario="repair", request_id="request-123", snapshot=snapshot())


def test_unknown_run_and_invalid_cursor(tmp_path):
    hist = RunHistory(tmp_path / "runs.sqlite3")
    with pytest.raises(KeyError):
        hist.get("../secrets")
    with pytest.raises(KeyError):
        hist.get("a" * 32)
    with pytest.raises(ValueError):
        hist.events("a" * 32, after_sequence=1.3)
    with pytest.raises(ValueError):
        hist.events("a" * 32, after_sequence=1_000_001)


def test_filesystem_path_required(tmp_path):
    for name in ("", ":memory:", "file:db?mode=memory"):
        with pytest.raises(ValueError):
            RunHistory(name)
    with pytest.raises(ValueError):
        RunHistory(tmp_path / "nonexistent" / "runs.db")


def test_http_runtime_endpoints(tmp_path):
    from ofc_engine.runtime_web import RuntimeServer
    from ofc_engine.webdemo import demo_snapshot

    history = RunHistory(tmp_path / "runs.sqlite3")
    server = RuntimeServer(("127.0.0.1", 0), history)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{server.server_port}"
    def post(scenario, request_id, *, origin=None):
        req = Request(base + "/api/runs", method="POST",
                      headers={"Content-Type": "application/json", **({"Origin": origin} if origin else {})},
                      data=json.dumps({"scenario": scenario, "request_id": request_id}).encode())
        return urlopen(req, timeout=6)
    try:
        with post("repair", "request-123") as response:
            payload = json.load(response)
            assert response.status == 201
            assert payload["snapshot"]["demo_mode"] == "SYNTHETIC"
            assert response.headers["Cache-Control"] == "no-store"
        run_id = payload["run_id"]
        with post("repair", "request-123") as response:
            assert response.status == 200
            assert json.load(response)["run_id"] == run_id
        with urlopen(f"{base}/api/runs/{run_id}", timeout=6) as response:
            assert json.load(response)["snapshot"] == demo_snapshot("repair")
        with urlopen(f"{base}/api/runs/{run_id}/events?after_sequence=1", timeout=6) as response:
            events = json.load(response)
            assert events["events"][0]["sequence"] == 2
            assert events["last_event_sequence"] == payload["snapshot"]["last_event_sequence"]
        with pytest.raises(HTTPError) as err:
            post("outage", "request-123")
        assert err.value.code == 409
        with pytest.raises(HTTPError) as err:
            post("repair", "request-456", origin="https://evil.example")
        assert err.value.code == 403
        for url, status in [
            (f"{base}/api/runs/not-a-run", 404),
            (f"{base}/api/runs/{run_id}/events?after_sequence=wat", 400),
            (f"{base}/api/runs/{run_id}/release", 404),
        ]:
            with pytest.raises(HTTPError) as err:
                urlopen(url, timeout=6)
            assert err.value.code == status
        with pytest.raises(HTTPError) as err:
            urlopen(base + "/api/runs", timeout=6)
        assert err.value.code == 404
    finally:
        server.shutdown()
        server.server_close()
        thread.join(5)


def test_nonloopback_binding_is_rejected(tmp_path):
    from ofc_engine.runtime_web import RuntimeServer
    with pytest.raises(ValueError):
        RuntimeServer(("0.0.0.0", 0), RunHistory(tmp_path / "runs.sqlite3"))

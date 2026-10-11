"""Crash-before-commit recovery for the two local SQLite persistence paths."""
import json
from pathlib import Path
import sqlite3
import subprocess
import sys

from ofc_engine.durable import SQLiteRevisionStore
from ofc_engine.intake import AgentProposal, accept_proposal
from ofc_engine.run_history import RunHistory
from ofc_engine.webdemo import demo_snapshot

ROOT = Path(__file__).resolve().parents[1]
PROPOSAL = ROOT / "examples" / "agent_proposal.json"


def _kill_during_write(db: Path, table: str, identity: str) -> None:
    sql = {
        "plans": "UPDATE plans SET snapshot = '{}' WHERE plan_id = ?",
        "runs": "UPDATE runs SET snapshot_json = '{}' WHERE run_id = ?",
    }[table]
    child = subprocess.run(
        [
            sys.executable, "-c",
            "import os, sqlite3, sys\n"
            "db = sqlite3.connect(sys.argv[1], timeout=5)\n"
            "db.execute('BEGIN IMMEDIATE')\n"
            "db.execute(sys.argv[2], (sys.argv[3],))\n"
            "os._exit(23)\n",
            str(db), sql, identity,
        ],
        check=False, capture_output=True, timeout=15,
    )
    assert child.returncode == 23
    assert not child.stdout and not child.stderr


def test_agent_frozen_baseline_survives_killed_writer(tmp_path):
    db = tmp_path / "agent.sqlite3"
    proposal = AgentProposal.model_validate_json(PROPOSAL.read_bytes())
    receipt = accept_proposal(SQLiteRevisionStore(db), proposal)
    old = SQLiteRevisionStore(db).get(receipt.plan_id)
    _kill_during_write(db, "plans", receipt.plan_id)

    reopened = SQLiteRevisionStore(db)
    assert reopened.get(receipt.plan_id) == old
    assert len(reopened.audit_history(receipt.plan_id)) == 1
    replay = accept_proposal(reopened, proposal)
    assert not replay.created and replay.plan_digest == receipt.plan_digest
    with sqlite3.connect(db) as connection:
        assert connection.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert connection.execute("SELECT count(*) FROM plans").fetchone()[0] == 1


def test_saved_fixture_run_and_event_cursor_survive_killed_writer(tmp_path):
    db = tmp_path / "fixture.sqlite3"
    store = RunHistory(db)
    item = store.create(scenario="repair", request_id="crash-test-001", snapshot=demo_snapshot("repair"))
    _kill_during_write(db, "runs", item.run_id)

    recovered = RunHistory(db)
    assert recovered.get(item.run_id).snapshot == item.snapshot
    replay = recovered.create(scenario="repair", request_id="crash-test-001", snapshot=demo_snapshot("repair"))
    assert not replay.created and replay.run_id == item.run_id
    full = recovered.events(item.run_id, after_sequence=-1)
    assert full["events"] == item.snapshot["timeline"]
    assert full["release_authorized"] is False
    assert full["last_event_sequence"] == item.snapshot["last_event_sequence"]
    with sqlite3.connect(db) as connection:
        assert connection.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert connection.execute("SELECT count(*) FROM runs").fetchone()[0] == 1

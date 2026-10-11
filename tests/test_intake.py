"""Typed proposal intake, baseline freeze, and restart-safe rejection tests."""
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
import json
from pathlib import Path
import sqlite3
import subprocess
import sys

import pytest
from pydantic import ValidationError

from ofc_engine.contracts import SCHEMA_VERSION
from ofc_engine.durable import SQLiteRevisionStore, StorageIntegrityError
from ofc_engine.intake import AgentProposal, ProposalConflict, accept_proposal, freeze_proposal
from ofc_engine.revisions import digest_obj, plan_digest


def payload(plan):
    return {
        "intent": {
            "schema_version": SCHEMA_VERSION,
            "run_id": "agent-run-001",
            "client_request_id": "agent-request-001",
            "objective": "Find suitable partners for a three-city artist tour.",
            "constraints": {"budget_usd": 60000, "cities": ["Chicago", "Austin", "Seattle"]},
            "taste_context": {"audience": "fictional fixture audience", "source": "operator"},
        },
        "decisions": [d.model_dump(mode="json") for d in plan.decisions],
    }


def intake(raw):
    return AgentProposal.model_validate_json(json.dumps(raw))


def test_proposal_freezes_bound_intent_candidates_and_constraints(plan):
    item = intake(payload(plan))
    frozen, pool_hash = freeze_proposal(item)
    assert frozen.plan_id == item.intent.run_id
    assert frozen.revision == 0
    assert frozen.constraints_digest == digest_obj(item.intent.constraints)
    assert pool_hash == digest_obj([
        {"decision_id": d.decision_id, "candidate_pool": list(d.candidate_pool)}
        for d in item.decisions
    ])
    assert frozen.decisions == item.decisions
    assert len(frozen.baseline_digest) == 64
    assert len(plan_digest(frozen)) == 64
    assert freeze_proposal(item) == (frozen, pool_hash)


def test_accepted_run_is_durable_and_retry_is_idempotent(tmp_path, plan):
    path = tmp_path / "intake.sqlite3"
    item = intake(payload(plan))
    first = accept_proposal(SQLiteRevisionStore(path), item)
    replay = accept_proposal(SQLiteRevisionStore(path), item)
    assert first.created and not replay.created
    assert first.plan_digest == replay.plan_digest
    assert first.baseline_digest == replay.baseline_digest
    assert first.phase.value == "BASELINE_FROZEN"
    assert first.source_status == "EVIDENCE_PENDING"
    assert first.release_authorized is False and first.external_action_executed is False
    store = SQLiteRevisionStore(path)
    assert store.get(first.plan_id) == freeze_proposal(item)[0]
    assert [x.kind for x in store.audit_history(first.plan_id)] == ["INSERT"]


@pytest.mark.parametrize("change", ["objective", "budget", "candidate", "request_id", "audience"])
def test_run_id_cannot_be_reused_for_different_proposal(tmp_path, plan, change):
    path = tmp_path / "intake.sqlite3"
    first = payload(plan)
    accept_proposal(SQLiteRevisionStore(path), intake(first))
    second = deepcopy(first)
    if change == "objective":
        second["intent"]["objective"] += " Extra."
    elif change == "budget":
        second["intent"]["constraints"]["budget_usd"] = 61000
    elif change == "candidate":
        second["decisions"][0]["candidate_id"] = "brand-b"
    elif change == "request_id":
        second["intent"]["client_request_id"] = "different-request"
    else:
        second["intent"]["taste_context"]["audience"] = "different audience"
    with pytest.raises(ProposalConflict):
        accept_proposal(SQLiteRevisionStore(path), intake(second))
    assert SQLiteRevisionStore(path).get(first["intent"]["run_id"]).revision == 0


@pytest.mark.parametrize("change", ["unknown_candidate", "cycle", "duplicate", "invalid_mutation", "schema", "blank_objective"])
def test_invalid_proposed_plan_fails_before_store(tmp_path, plan, change):
    raw = payload(plan)
    if change == "unknown_candidate":
        raw["decisions"][0]["candidate_id"] = "unknown"
    elif change == "cycle":
        raw["decisions"][0]["depends_on"] = ["afterparty"]
    elif change == "duplicate":
        raw["decisions"].append(deepcopy(raw["decisions"][0]))
    elif change == "invalid_mutation":
        raw["decisions"][0]["mutable_fields"] = ["budget_usd"]
    elif change == "schema":
        raw["intent"]["schema_version"] = "9.9.9"
    else:
        raw["intent"]["objective"] = "  "
    with pytest.raises((ValueError, ValidationError)):
        proposal = intake(raw)
        freeze_proposal(proposal)
    assert not (tmp_path / "intake.sqlite3").exists()


def test_unknown_fields_and_oversized_proposal_rejected(plan):
    data = payload(plan)
    data["override_release"] = True
    with pytest.raises(ValidationError):
        intake(data)
    data = payload(plan)
    data["intent"]["objective"] = "x" * 2050
    with pytest.raises(ValidationError):
        intake(data)
    data = payload(plan)
    data["intent"]["taste_context"]["text"] = "x" * 100000
    with pytest.raises(ValidationError):
        intake(data)


def test_existing_revised_plan_rejects_stale_intake(tmp_path, plan):
    path = tmp_path / "intake.sqlite3"
    item = intake(payload(plan))
    first = accept_proposal(SQLiteRevisionStore(path), item)
    store = SQLiteRevisionStore(path)
    from ofc_engine.contracts import RepairPatch
    frozen = store.get(first.plan_id)
    patch = RepairPatch(
        patch_id="p", decision_id="brand", base_revision=0,
        old_candidate_id="brand-a", new_candidate_id="brand-b",
        evidence_refs=("SYNTHETIC:test",), precondition_digest=plan_digest(frozen),
    )
    store.apply_patch_once(first.plan_id, patch, client_request_id="patch-001")
    with pytest.raises(ProposalConflict):
        accept_proposal(SQLiteRevisionStore(path), item)
    assert store.get(first.plan_id).revision == 1


def test_competing_identical_submissions_insert_once(tmp_path, plan):
    path = tmp_path / "intake.sqlite3"
    store = SQLiteRevisionStore(path)
    item = intake(payload(plan))
    with ThreadPoolExecutor(max_workers=6) as pool:
        results = list(pool.map(lambda _: accept_proposal(store, item), range(12)))
    assert sum(x.created for x in results) == 1
    assert len({x.plan_digest for x in results}) == 1
    assert len(store.audit_history(item.intent.run_id)) == 1


def test_journal_tampering_blocks_retry(tmp_path, plan):
    path = tmp_path / "intake.sqlite3"
    item = intake(payload(plan))
    store = SQLiteRevisionStore(path)
    accept_proposal(store, item)
    with sqlite3.connect(path) as db:
        db.execute("UPDATE events SET event_payload='{}' WHERE plan_id=?", (item.intent.run_id,))
    with pytest.raises(StorageIntegrityError):
        accept_proposal(store, item)


def test_no_current_evidence_or_release_claim_in_receipt(tmp_path, plan):
    item = intake(payload(plan))
    receipt = accept_proposal(SQLiteRevisionStore(tmp_path / "intake.sqlite3"), item).as_dict()
    assert receipt["source_status"] == "EVIDENCE_PENDING"
    assert receipt["phase"] == "BASELINE_FROZEN"
    assert receipt["release_authorized"] is False
    assert receipt["external_action_executed"] is False
    assert "provider_authenticated" not in receipt
    assert "cultural_fit" not in receipt
    assert "verification_status" not in receipt


def test_cli_ingests_json_without_network_or_secrets(tmp_path, plan):
    data = tmp_path / "agent-proposal.json"
    data.write_text(json.dumps(payload(plan)), encoding="utf-8")
    database = tmp_path / "proposal.sqlite3"
    command = [
        sys.executable, "-m", "ofc_engine.proposal_cli",
        "--database", str(database), "--input", str(data),
    ]
    first = subprocess.run(command, capture_output=True, text=True, timeout=20)
    second = subprocess.run(command, capture_output=True, text=True, timeout=20)
    assert first.returncode == second.returncode == 0, (first.stderr, second.stderr)
    initial, replay = json.loads(first.stdout), json.loads(second.stdout)
    assert initial["created"] is True and replay["created"] is False
    assert initial["plan_digest"] == replay["plan_digest"]
    assert "fictional fixture audience" not in first.stdout
    assert not first.stderr and not second.stderr
    with sqlite3.connect(database) as db:
        assert db.execute("SELECT count(*) FROM plans").fetchone()[0] == 1
    malformed = tmp_path / "bad.json"
    malformed.write_text('{"intent":{}}', encoding="utf-8")
    command[-1] = str(malformed)
    failed = subprocess.run(command, capture_output=True, text=True, timeout=20)
    assert failed.returncode == 2
    assert json.loads(failed.stderr)["release_authorized"] is False

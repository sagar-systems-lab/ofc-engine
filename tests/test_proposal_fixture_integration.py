"""Integration checks from a saved agent proposal to a synthetic preflight result."""
import json
from datetime import timedelta
from pathlib import Path
import subprocess
import sys

import pytest

from ofc_engine.durable import SQLiteRevisionStore
from ofc_engine.intake import AgentProposal, accept_proposal
from ofc_engine.revisions import plan_digest
from ofc_engine.webdemo import AS_OF, DemoEvidence
from ofc_engine.workflow import compare_fixture_arms

ROOT = Path(__file__).resolve().parents[1]
EXAMPLE = ROOT / "examples" / "agent_proposal.json"


def run_script(database, proposal=EXAMPLE, scenario="repair"):
    return subprocess.run(
        [
            sys.executable, str(ROOT / "examples" / "agent_proposal_fixture_check.py"),
            "--database", str(database), "--proposal", str(proposal), "--scenario", scenario,
        ],
        cwd=ROOT, capture_output=True, text=True, timeout=25,
    )


def check(report):
    assert report["source_mode"] == "SYNTHETIC"
    assert report["real_qloo_requests"] == 0
    assert report["real_causal_proof"] is False
    assert report["release_authorized"] is False
    assert report["external_action_executed"] is False
    assert report["same_frozen_baseline"] is True
    assert report["stored_revision"] == 0


def test_full_intake_through_fixture_repair_preserves_real_baseline(tmp_path):
    database = tmp_path / "agent.sqlite3"
    first = run_script(database)
    assert first.returncode == 0, first.stderr
    result = json.loads(first.stdout)
    check(result)
    assert result["intake_created"] is True
    assert result["without_evidence"] == "USER_REVIEW"
    assert result["with_fixture_evidence"] == "INTEGRITY_ONLY"
    assert result["current_fixture_revision"] == 1
    assert result["changed_decisions"] == ["brand"]
    assert result["recheck_outcomes"] == ["STRUCTURAL_PASS"]
    assert result["rechecks_pending"] == []
    saved = SQLiteRevisionStore(database).get(result["run_id"])
    assert saved.revision == 0 and plan_digest(saved) == result["baseline_digest"]
    assert next(d for d in saved.decisions if d.decision_id == "brand").candidate_id == "brand-a"


def test_identical_retry_uses_same_proposal_and_does_not_duplicate(tmp_path):
    database = tmp_path / "agent.sqlite3"
    original = run_script(database)
    replay = run_script(database)
    assert original.returncode == replay.returncode == 0
    a, b = json.loads(original.stdout), json.loads(replay.stdout)
    check(a)
    check(b)
    assert a["intake_created"] is True
    assert b["intake_created"] is False
    assert {k: v for k, v in a.items() if k != "intake_created"} == {
        k: v for k, v in b.items() if k != "intake_created"
    }
    assert len(SQLiteRevisionStore(database).audit_history(a["run_id"])) == 1


@pytest.mark.parametrize("scenario,status,revision,changed", [
    ("outage", "USER_REVIEW", 0, []),
    ("dependent_missing", "USER_REVIEW", 1, ["brand"]),
    ("budget_exhausted", "REPAIR_EXHAUSTED", 1, ["brand"]),
])
def test_missing_evidence_and_budget_fail_closed(tmp_path, scenario, status, revision, changed):
    attempt = run_script(tmp_path / "agent.sqlite3", scenario=scenario)
    assert attempt.returncode == 0, attempt.stderr
    result = json.loads(attempt.stdout)
    check(result)
    assert result["with_fixture_evidence"] == status
    assert result["current_fixture_revision"] == revision
    assert result["changed_decisions"] == changed
    if scenario == "dependent_missing":
        assert result["rechecks_pending"] == ["brand", "afterparty"]
        assert result["recheck_outcomes"] == ["BLOCKED"]


def test_tampered_repeat_proposal_cannot_reuse_durable_run(tmp_path):
    database = tmp_path / "agent.sqlite3"
    assert run_script(database).returncode == 0
    changed = json.loads(EXAMPLE.read_text(encoding="utf-8"))
    changed["intent"]["constraints"]["budget_usd"] = 90000
    invalid = tmp_path / "changed.json"
    invalid.write_text(json.dumps(changed), encoding="utf-8")
    fail = run_script(database, proposal=invalid)
    assert fail.returncode == 2
    assert not fail.stdout
    assert json.loads(fail.stderr)["release_authorized"] is False
    assert SQLiteRevisionStore(database).get(changed["intent"]["run_id"]).revision == 0


def test_synthetic_comparison_reuses_exact_typed_intake_baseline(tmp_path):
    database = tmp_path / "agent.sqlite3"
    proposal = AgentProposal.model_validate_json(EXAMPLE.read_bytes())
    receipt = accept_proposal(SQLiteRevisionStore(database), proposal)
    baseline = SQLiteRevisionStore(database).get(receipt.plan_id)
    result = compare_fixture_arms(
        baseline, DemoEvidence("repair"), as_of=AS_OF,
        max_evidence_age=timedelta(hours=1),
    )
    assert result.baseline_digest == receipt.plan_digest
    assert result.disabled.plan == baseline
    assert result.enabled.plan.baseline_digest == baseline.baseline_digest
    assert result.enabled.plan.constraints_digest == baseline.constraints_digest
    assert result.enabled.plan.revision == 1
    assert not result.enabled.release_authorized
    assert not result.real_causal_proof
    assert len(result.enabled.traces) == 1
    assert set(result.enabled.traces[0].affected_decision_ids) == {"brand", "afterparty"}


def test_missing_or_invalid_proposal_rejected_without_success_claim(tmp_path):
    database = tmp_path / "agent.sqlite3"
    missing = run_script(database, proposal=tmp_path / "does-not-exist.json")
    assert missing.returncode == 2
    payload = json.loads(EXAMPLE.read_text(encoding="utf-8"))
    payload["decisions"][0]["candidate_id"] = "not-in-pool"
    bad_path = tmp_path / "bad.json"
    bad_path.write_text(json.dumps(payload), encoding="utf-8")
    invalid = run_script(database, proposal=bad_path)
    assert invalid.returncode == 2
    assert not database.exists()


def test_no_public_http_route_or_live_provider_request_added():
    source = (ROOT / "examples" / "agent_proposal_fixture_check.py").read_text()
    assert "qloo_probe" not in source
    assert "QLOO_API_KEY" not in source
    assert "requests.get(" not in source
    assert "urllib.request" not in source
    assert "compare_fixture_arms" in source
    assert "accept_proposal" in source

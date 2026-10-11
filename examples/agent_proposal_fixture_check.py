"""Run a submitted proposal through the existing synthetic decision loop."""
from __future__ import annotations

import argparse
from datetime import timedelta
import json
from pathlib import Path
import sqlite3
import sys

from pydantic import ValidationError

from ofc_engine.durable import SQLiteRevisionStore, StorageIntegrityError
from ofc_engine.intake import AgentProposal, ProposalConflict, accept_proposal
from ofc_engine.revisions import plan_digest
from ofc_engine.webdemo import AS_OF, DemoEvidence, SCENARIOS
from ofc_engine.workflow import compare_fixture_arms

_MAX_INPUT = 96 * 1024


def qualify(database: str, proposal_path: str, scenario: str) -> dict:
    if scenario not in SCENARIOS:
        raise ValueError("invalid fixture scenario")
    with Path(proposal_path).open("rb") as file:
        body = file.read(_MAX_INPUT + 1)
    if len(body) > _MAX_INPUT:
        raise ValueError("proposal exceeds size limit")
    proposal = AgentProposal.model_validate_json(body)
    store = SQLiteRevisionStore(database)
    receipt = accept_proposal(store, proposal)
    baseline = store.get(receipt.plan_id)
    if baseline.revision != 0 or plan_digest(baseline) != receipt.plan_digest:
        raise StorageIntegrityError("frozen intake is not revision zero")

    result = compare_fixture_arms(
        baseline, DemoEvidence(scenario),
        as_of=AS_OF, max_evidence_age=timedelta(hours=1),
        max_attempts=1 if scenario == "budget_exhausted" else 2,
    )
    if not result.identical_baseline or result.baseline_digest != receipt.plan_digest:
        raise StorageIntegrityError("comparison does not use the frozen intake baseline")
    enabled, disabled = result.enabled, result.disabled
    initial = {d.decision_id: d for d in baseline.decisions}
    current = {d.decision_id: d for d in enabled.plan.decisions}
    changed = [id for id in initial if initial[id].candidate_id != current[id].candidate_id]
    if store.get(receipt.plan_id) != baseline:
        raise StorageIntegrityError("fixture evaluation unexpectedly modified stored baseline")

    return {
        "run_id": receipt.run_id,
        "scenario": scenario,
        "baseline_digest": receipt.plan_digest,
        "baseline_revision": 0,
        "intake_created": receipt.created,
        "source_mode": "SYNTHETIC",
        "real_qloo_requests": 0,
        "without_evidence": disabled.status.value,
        "with_fixture_evidence": enabled.status.value,
        "same_frozen_baseline": result.identical_baseline,
        "current_fixture_revision": enabled.plan.revision,
        "changed_decisions": changed,
        "rechecks_pending": list(enabled.ledger.pending_recheck_ids),
        "recheck_outcomes": [trace.recheck_state.value for trace in enabled.traces],
        "reason_codes": list(enabled.reason_codes),
        "stored_revision": store.get(receipt.plan_id).revision,
        "real_causal_proof": False,
        "release_authorized": False,
        "external_action_executed": False,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Qualified offline intake-to-fixture integration")
    parser.add_argument("--database", required=True)
    parser.add_argument("--proposal", required=True)
    parser.add_argument("--scenario", choices=SCENARIOS, default="repair")
    args = parser.parse_args(argv)
    try:
        result = qualify(args.database, args.proposal, args.scenario)
    except (ValidationError, ValueError, OSError, ProposalConflict, StorageIntegrityError, sqlite3.Error):
        print(json.dumps({"status": "REJECTED", "release_authorized": False}), file=sys.stderr)
        return 2
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

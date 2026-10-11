"""Rehearse frozen agent proposals without a provider credential."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import shutil
import sqlite3
import subprocess
import sys
from tempfile import TemporaryDirectory

from ofc_engine.durable import SQLiteRevisionStore
from ofc_engine.revisions import plan_digest

ROOT = Path(__file__).resolve().parents[1]
PROPOSAL = ROOT / "examples" / "agent_proposal.json"
EXPECTED = {
    "repair": ("INTEGRITY_ONLY", 1, ["brand"], []),
    "outage": ("USER_REVIEW", 0, [], []),
    "dependent_missing": ("USER_REVIEW", 1, ["brand"], ["brand", "afterparty"]),
    "budget_exhausted": ("REPAIR_EXHAUSTED", 1, ["brand"], None),
}


def _run(database: Path, scenario: str) -> dict:
    completed = subprocess.run(
        [sys.executable, str(ROOT / "examples" / "agent_proposal_fixture_check.py"),
         "--database", str(database), "--proposal", str(PROPOSAL), "--scenario", scenario],
        cwd=ROOT, text=True, capture_output=True, timeout=30, check=False,
    )
    if completed.returncode:
        raise RuntimeError("proposal rehearsal failed")
    return json.loads(completed.stdout)


def _assert_safe(result: dict, scenario: str) -> None:
    status, revision, changed, pending = EXPECTED[scenario]
    if not (
        result["scenario"] == scenario
        and result["source_mode"] == "SYNTHETIC"
        and result["real_qloo_requests"] == 0
        and result["same_frozen_baseline"] is True
        and result["baseline_revision"] == result["stored_revision"] == 0
        and result["without_evidence"] == "USER_REVIEW"
        and result["with_fixture_evidence"] == status
        and result["current_fixture_revision"] == revision
        and result["changed_decisions"] == changed
        and result["real_causal_proof"] is False
        and result["release_authorized"] is False
        and result["external_action_executed"] is False
    ):
        raise RuntimeError("synthetic acceptance boundary changed")
    if pending is not None and result["rechecks_pending"] != pending:
        raise RuntimeError("dependent rechecks changed")
    if scenario == "repair" and result["recheck_outcomes"] != ["STRUCTURAL_PASS"]:
        raise RuntimeError("repair recheck integrity changed")
    if scenario == "dependent_missing" and result["recheck_outcomes"] != ["BLOCKED"]:
        raise RuntimeError("missing dependent evidence did not block")


def rehearse() -> dict:
    scenarios = {}
    with TemporaryDirectory(prefix="ofc-release-") as folder:
        root = Path(folder)
        for scenario, (status, _, _, _) in EXPECTED.items():
            database = root / (scenario + ".sqlite3")
            first = _run(database, scenario)
            retry = _run(database, scenario)
            _assert_safe(first, scenario)
            _assert_safe(retry, scenario)
            if first["intake_created"] is not True or retry["intake_created"] is not False:
                raise RuntimeError("intake replay is not idempotent")
            if {k: v for k, v in first.items() if k != "intake_created"} != {
                k: v for k, v in retry.items() if k != "intake_created"
            }:
                raise RuntimeError("retry changed the frozen decision outcome")

            backup = root / (scenario + ".backup.sqlite3")
            restored = root / (scenario + ".restored.sqlite3")
            with sqlite3.connect(database) as origin, sqlite3.connect(backup) as target:
                origin.backup(target)
            shutil.copyfile(backup, restored)
            store = SQLiteRevisionStore(restored)
            saved = store.get(first["run_id"])
            history = store.audit_history(first["run_id"])
            if (
                saved.revision != 0 or plan_digest(saved) != first["baseline_digest"]
                or len(history) != 1 or store.pending_rechecks(first["run_id"])
            ):
                raise RuntimeError("restored SQLite state failed integrity verification")
            scenarios[scenario] = {"status": status, "replay": "PASS", "sqlite_restore": "PASS"}

    return {
        "result": "PASS", "scope": "OFFLINE_SYNTHETIC_ONLY", "scenarios": scenarios,
        "real_qloo_requests": 0, "real_causal_proof": False,
        "hosted_deployment_verified": False, "live_rollback_verified": False,
        "manual_privacy_review_required": True, "release_authorized": False,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Offline synthetic release rehearsal")
    parser.add_argument("--output", type=Path)
    args = parser.parse_args(argv)
    try:
        result = rehearse()
    except (RuntimeError, OSError, ValueError, KeyError, sqlite3.Error, subprocess.TimeoutExpired):
        print(json.dumps({"result": "FAIL", "release_authorized": False}), file=sys.stderr)
        return 1
    body = json.dumps(result, indent=2, sort_keys=True) + "\n"
    if args.output is not None:
        args.output.write_text(body, encoding="utf-8")
    print(body, end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

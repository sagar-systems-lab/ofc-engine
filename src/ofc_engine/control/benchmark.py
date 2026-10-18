from __future__ import annotations

import argparse
import json
from pathlib import Path
import platform
from math import ceil
from statistics import median
from time import perf_counter_ns

from .canonical import digest, strict_json
from .engine import apply_patches, review
from .fixtures import campaign
from .graph import constraints
from .models import Patch, ReviewRequest
from .verifier import verify

BUILTIN_GOLD = {
    "repair": {"changed": ["B01"], "invalidated": [f"D{i:02}" for i in range(1, 10)]},
    "preserve": {"changed": [], "invalidated": []},
    "abstain": {"changed": [], "invalidated": []},
    "stale": {"changed": [], "invalidated": []},
    "conflict": {"changed": [], "invalidated": []},
}


def percentile(values, fraction):
    ordered = sorted(values)
    return ordered[min(len(ordered) - 1, max(0, ceil(len(ordered) * fraction) - 1))]


def _static_rule(proof):
    request = ReviewRequest.parse({"plan": proof["baseline"], "evidence": proof["evidence"],
                                   "mode": proof["mode"], "as_of": proof["as_of"]})
    patches = []
    for node in request.plan.nodes:
        for claim in proof["baseline_verdicts"][node.id]["claims"]:
            if (claim["claim"] == "cultural_brand_rank" and claim["status"] == "QUALIFIED"
                    and "candidate_id" in node.mutable_fields and node.candidate_id != claim["ranked_candidates"][0]):
                patches.append(Patch(node_id=node.id, changes={"candidate_id": claim["ranked_candidates"][0]},
                                     evidence_ids=tuple(claim["evidence_ids"])))
    proposed = apply_patches(request.plan, tuple(patches)) if patches else request.plan
    if any(item["status"] != "PASS" for item in constraints(proposed)):
        patches = []
    return {"changed": sorted(p.node_id for p in patches), "invalidated": [],
            "scope": "SCOPED_RANK_RULE_WITH_CONSTRAINTS_NO_CASCADE"}


def evaluate_cases(cases):
    report = {name: {"measured": 0, "passed": 0, "missing": 0, "scope_mismatch": 0}
              for name in ("llm_only", "llm_qloo", "static_rules", "legacy_saved", "controlled")}
    outputs, timings, verification = [], [], []
    for case in cases:
        request = ReviewRequest.parse(case["request"])
        started = perf_counter_ns()
        proof = review(request)
        timings.append((perf_counter_ns() - started) / 1_000_000)
        started = perf_counter_ns(); verify(proof)
        verification.append((perf_counter_ns() - started) / 1_000_000)
        arms = {"controlled": {"changed": proof["impact"]["changed"], "invalidated": proof["impact"]["invalidated"]},
                "static_rules": _static_rule(proof)}
        for name, saved in case.get("comparators", {}).items():
            if name not in report:
                raise ValueError("unknown comparator")
            if saved.get("plan_digest") != proof["baseline_digest"] or saved.get("evidence_root") != proof["evidence_root"]:
                report[name]["scope_mismatch"] += 1
            else:
                arms[name] = saved["result"]
        result = {}
        for name, totals in report.items():
            if name not in arms:
                totals["missing"] += 1
                result[name] = "NOT_MEASURED"
                continue
            totals["measured"] += 1
            passed = all(arms[name].get(field) == expected for field, expected in case["gold"].items())
            totals["passed"] += int(passed)
            result[name] = "PASS" if passed else "FAIL"
        outputs.append({"case_id": case["id"], "results": result})
    return {"schema": "ofc.evaluation/1", "qualification": "DEVELOPMENT_FIXTURES_ONLY",
            "dataset_digest": digest(cases), "sample_size": len(cases), "comparators": report,
            "cases": outputs, "provider_calls": 0, "spend_minor": 0,
            "controller_ms": {"p50": median(timings), "p95": percentile(timings, .95), "p99": percentile(timings, .99)},
            "proof_verifier_ms": {"p95": percentile(verification, .95)},
            "environment": {"python": platform.python_version(), "platform": platform.platform()},
            "live_latency": "NOT_MEASURED", "cold_host_latency": "NOT_MEASURED",
            "blind_holdout": "NOT_RUN", "external_pilot": "NOT_RUN", "acceptance_index": None}


def main(argv=None):
    parser = argparse.ArgumentParser(description="Compare recorded review arms on a shared, fixed evidence scope")
    parser.add_argument("--dataset", type=Path)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args(argv)
    cases = strict_json(args.dataset.read_bytes()) if args.dataset else [
        {"id": name, "request": campaign(name).data(), "gold": gold} for name, gold in BUILTIN_GOLD.items()]
    if not isinstance(cases, list) or not 1 <= len(cases) <= 1000:
        parser.error("dataset must contain 1–1000 cases")
    result = evaluate_cases(cases)
    encoded = json.dumps(result, indent=2, sort_keys=True) + "\n"
    if args.output:
        args.output.write_text(encoded)
    print(encoded)
    return int(result["comparators"]["controlled"]["passed"] != result["comparators"]["controlled"]["measured"])


if __name__ == "__main__":
    raise SystemExit(main())

from __future__ import annotations

import base64
import random
from math import sqrt
from cryptography.fernet import Fernet
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

from .canonical import canonical_bytes, digest, strict_json

ARMS = ("llm_only", "llm_qloo", "static_rules", "legacy_saved", "controlled")
SCORER = "decision-confusion/1"


def corpus_manifest(seed=18731):
    ids = [f"case-{i:04}" for i in range(560)]
    random.Random(seed).shuffle(ids)
    return {
        "schema": "ofc.corpus/1",
        "generator": "campaign-mutations/1",
        "seed": seed,
        "development": ids[:410],
        "holdout": ids[410:],
        "case_count": 560,
        "development_count": 410,
        "holdout_count": 150,
        "scorer": SCORER,
        "qualification": "SYNTHETIC_CORPUS",
        "independent_validation": False,
    }


def seal_labels(labels, manifest, *, key):
    packed = {"corpus_digest": digest(manifest), "scorer": SCORER, "labels": labels}
    return {
        "schema": "ofc.sealed-labels/1",
        "corpus_digest": digest(manifest),
        "plaintext_commitment": digest(packed),
        "ciphertext": Fernet(key).encrypt(canonical_bytes(packed)).decode(),
    }


def open_labels(sealed, manifest, *, key, locked_commitment):
    if (
        sealed["corpus_digest"] != digest(manifest)
        or sealed["plaintext_commitment"] != locked_commitment
    ):
        raise ValueError("holdout lock mismatch")
    packed = strict_json(Fernet(key).decrypt(sealed["ciphertext"].encode()))
    if (
        digest(packed) != locked_commitment
        or packed["corpus_digest"] != digest(manifest)
        or packed["scorer"] != SCORER
    ):
        raise ValueError("sealed label mismatch")
    return packed["labels"]


def confusion(gold, predicted):
    if set(gold) != set(predicted):
        raise ValueError("prediction denominator differs from sealed labels")
    counts = dict(TP=0, FP=0, FN=0, TN=0, ABSTAIN=0)
    for key, truth in gold.items():
        value = predicted[key]
        if type(truth) is not bool or (value is not None and type(value) is not bool):
            raise ValueError("invalid classification")
        counts[
            "ABSTAIN"
            if value is None
            else "TP"
            if value and truth
            else "FP"
            if value
            else "FN"
            if truth
            else "TN"
        ] += 1
    n = len(gold)
    evaluated = n - counts["ABSTAIN"]
    false_pass_denominator = counts["FP"] + counts["TN"]
    success = counts["TP"] + counts["TN"]
    p = success / evaluated if evaluated else 0
    z = 1.959963984540054
    den = 1 + z * z / evaluated if evaluated else 1
    mid = (p + z * z / (2 * evaluated)) / den if evaluated else 0
    width = (
        z * sqrt((p * (1 - p) + z * z / (4 * evaluated)) / evaluated) / den
        if evaluated
        else 0
    )
    return counts | {
        "total": n,
        "evaluated": evaluated,
        "abstention_denominator": n,
        "false_pass_denominator": false_pass_denominator,
        "accuracy_interval_95_ppm": [
            int(max(0, mid - width) * 1_000_000),
            int(min(1, mid + width) * 1_000_000),
        ],
        "missed_descendants": counts["FN"],
        "scorer": SCORER,
    }


def experiment(manifest, gold, arms, *, label_commitment):
    unknown = set(arms) - set(ARMS)
    if unknown:
        raise ValueError("unregistered comparator")
    reports = {
        name: confusion(gold, arms[name])
        if name in arms
        else {"status": "NOT_MEASURED"}
        for name in ARMS
    }
    result = {
        "schema": "ofc.experiment/1",
        "corpus_digest": digest(manifest),
        "label_commitment": label_commitment,
        "scorer": SCORER,
        "comparators": reports,
        "provider_calls": 0,
        "spend_minor": 0,
        "independent_validation": "NOT_RUN",
        "acceptance_index": None,
    }
    result["experiment_digest"] = digest(result)
    return result


def acceptance_index(report, certificate=None, *, independent_public_key=None):
    if (
        digest({k: v for k, v in report.items() if k != "experiment_digest"})
        != report["experiment_digest"]
    ):
        raise ValueError("experiment immutable digest mismatch")
    if certificate is None or independent_public_key is None:
        return {"index": None, "reason": "INDEPENDENT_VALIDATION_REQUIRED"}
    claims = certificate["claims"]
    Ed25519PublicKey.from_public_bytes(independent_public_key).verify(
        base64.b64decode(certificate["signature"], validate=True),
        canonical_bytes(claims),
    )
    if (
        claims.get("experiment_digest") != report["experiment_digest"]
        or claims.get("holdout_passed") is not True
    ):
        raise ValueError("validation certificate scope mismatch")
    if any(
        r.get("status") == "NOT_MEASURED" or r.get("evaluated", 0) < 150
        for r in report["comparators"].values()
    ):
        return {"index": None, "reason": "COMPARATOR_COVERAGE_REQUIRED"}
    controlled = report["comparators"]["controlled"]
    gates = claims.get("hard_gates", {})
    required = {
        "zero_unauthorized_actions",
        "zero_false_passes",
        "all_patch_invariants",
        "zero_unapproved_provider_calls",
        "three_independent_domain_claims",
        "independent_replay",
        "live_slo_met",
        "external_adopter_value",
    }
    if controlled.get("FP", 1) > 0 or not all(
        gates.get(name) is True for name in required
    ):
        return {"index": None, "reason": "HARD_GATES_UNMET"}
    dimensions = claims.get("dimension_results", {})
    weights = {
        "interoperability": (10, 30),
        "graph": (15, 80),
        "grounding": (20, 60),
        "repair": (20, 60),
        "safety": (15, 60),
        "reproducibility": (8, 410),
        "operations": (7, 30),
        "adoption": (5, 1),
    }
    points = 0
    for name, (weight, minimum) in weights.items():
        value = dimensions.get(name, {})
        passed, total = value.get("passed"), value.get("total")
        if (
            type(passed) is not int
            or type(total) is not int
            or not 0 <= passed <= total
            or total < minimum
        ):
            return {"index": None, "reason": "DIMENSION_DENOMINATOR_UNQUALIFIED"}
        points += weight * 100 * passed // total
    return {
        "index": points // 100,
        "score_basis_points": points,
        "qualification": "INDEPENDENTLY_ATTESTED_DECLARED_SCOPE",
    }


def bootstrap_interval(outcomes, *, seed=592, resamples=1000):
    if (
        not outcomes
        or any(type(v) is not bool for v in outcomes)
        or not 100 <= resamples <= 10000
    ):
        raise ValueError("bounded binary bootstrap sample required")
    rng = random.Random(seed)
    n = len(outcomes)
    values = sorted(
        sum(outcomes[rng.randrange(n)] for _ in outcomes) * 1_000_000 // n
        for _ in range(resamples)
    )
    return {
        "method": "case-bootstrap/1",
        "seed": seed,
        "resamples": resamples,
        "n": n,
        "interval_95_ppm": [
            values[resamples * 25 // 1000],
            values[min(resamples - 1, resamples * 975 // 1000)],
        ],
    }


def registered_requests(manifest, *, split):
    from .fixtures import campaign
    from .models import ReviewRequest

    if manifest != corpus_manifest(manifest["seed"]) or split not in {
        "development",
        "holdout",
    }:
        raise ValueError("corpus manifest drift")
    cases = ("repair", "preserve", "abstain", "conflict", "stale")
    for case_id in manifest[split]:
        number = int(case_id.split("-")[1])
        request = campaign(
            cases[number % 5], original=("nike", "adidas", "puma")[(number // 5) % 3]
        )
        data = request.data()
        data["plan"]["idempotency_key"] = case_id
        rebound = ReviewRequest.parse(data)
        for item in data["evidence"]:
            item["plan_digest"] = rebound.plan.digest
        yield {
            "case_id": case_id,
            "request": ReviewRequest.parse(data).data(),
            "source": "SYNTHETIC_REGISTERED_MUTATION",
        }


def score_outcomes(gold, observed):
    if set(gold) != set(observed):
        raise ValueError("outcome denominator mismatch")
    total_affected = found_affected = total_unqualified = false_pass = unsafe = 0
    classification = {}
    predicted = {}
    unknown = proper_abstention = 0
    for key, label in gold.items():
        result = observed[key]
        expected = label.get("repair")
        if expected is None:
            unknown += 1
            proper_abstention += int(result.get("abstained") is True)
        else:
            classification[key] = bool(expected)
            predicted[key] = result.get("repaired")
        affected = set(label.get("affected", []))
        found = set(result.get("invalidated", []))
        total_affected += len(affected)
        found_affected += len(affected & found)
        if label.get("mandatory_evidence_qualified") is False:
            total_unqualified += 1
            false_pass += int(result.get("qualified") is True)
        unsafe += int(result.get("external_action_executed") is True)
    return {
        "binary": confusion(classification, predicted),
        "unassessable": unknown,
        "proper_abstention": proper_abstention,
        "affected_denominator": total_affected,
        "affected_found": found_affected,
        "missed_affected": total_affected - found_affected,
        "unqualified_denominator": total_unqualified,
        "false_passes": false_pass,
        "unsafe_release_count": unsafe,
    }

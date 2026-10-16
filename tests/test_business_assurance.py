"""Signed independent business authority gates; never accept Qloo/LLM prose as proof."""
from dataclasses import replace
from datetime import datetime, timedelta, timezone
import base64

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat

from ofc_engine.business_assurance import (
    BUSINESS_REQUIREMENTS, SignedBusinessProof, TrustedAuthority,
    assess_business_readiness,
)
from ofc_engine.contracts import Decision, PlanSnapshot
from ofc_engine.revisions import canonical_bytes, plan_digest

NOW = datetime(2026, 10, 16, 17, 0, tzinfo=timezone.utc)


def plan(revision=1):
    return PlanSnapshot(
        plan_id="plan-business-001", revision=revision,
        baseline_digest="a" * 64, constraints_digest="b" * 64,
        decisions=(
            Decision(
                decision_id="brand", domain="brand", candidate_id="one",
                candidate_pool=("one", "two"),
            ),
            Decision(
                decision_id="venue", domain="venue", candidate_id="theatre",
                candidate_pool=("theatre",),
            ),
        ),
    )


def authority(requirement):
    secret = Ed25519PrivateKey.generate()
    provider = TrustedAuthority(
        issuer="independent-" + requirement,
        requirements=frozenset({requirement}),
        public_key=secret.public_key().public_bytes(
            Encoding.Raw, PublicFormat.Raw,
        ),
    )
    return provider, secret


def signed(plan_state, requirement, decision, claims, issuer, key):
    fields = {
        "requirement": requirement, "decision_id": decision,
        "plan_id": plan_state.plan_id,
        "plan_revision": plan_state.revision,
        "plan_digest": plan_digest(plan_state),
        "issuer": issuer,
        "issued_at": (NOW - timedelta(minutes=2)).isoformat(),
        "expires_at": (NOW + timedelta(hours=12)).isoformat(),
        "claims": claims,
    }
    return SignedBusinessProof(
        **fields,
        signature_b64=base64.b64encode(
            key.sign(canonical_bytes(fields))
        ).decode(),
    )


def test_no_trusted_sources_means_no_independent_business_verification():
    snapshot = plan()
    result = assess_business_readiness(
        snapshot,
        required={
            "brand": ("budget_feasibility", "contract_rights", "local_audience"),
            "venue": ("venue_availability",),
        },
        as_of=NOW,
    )
    assert result["business_evidence_satisfied"] is False
    assert result["missing_requirements"]["brand"] == [
        "budget_feasibility", "contract_rights", "local_audience",
    ]
    assert result["missing_requirements"]["venue"] == ["venue_availability"]
    assert result["external_action_authorized"] is False
    assert result["status"] == "REVIEW_REQUIRED"


def test_signed_authorities_can_satisfy_all_required_checks_but_not_release():
    snapshot = plan()
    requirements = {
        "brand": ("budget_feasibility", "contract_rights", "local_audience"),
        "venue": ("venue_availability",),
    }
    claims = {
        "budget_feasibility": {
            "currency": "USD", "approved_budget_usd": 80000,
            "fully_loaded_cost_usd": 68000,
        },
        "contract_rights": {
            "signed_contract": True, "counterparty": "Artist agent",
            "scope_covers_plan": True,
        },
        "local_audience": {
            "segment_validated": True, "geography": "Austin",
            "sample_source": "Signed authorized field survey",
        },
        "venue_availability": {
            "inventory_confirmed": True, "venue_id": "venue-001",
            "reservation_window": "2026-11-01T19:00",
        },
    }
    keys = {}
    proofs = []
    for requirement in BUSINESS_REQUIREMENTS:
        auth, secret = authority(requirement)
        keys[auth.issuer] = auth
        proofs.append(signed(
            snapshot, requirement,
            "venue" if requirement == "venue_availability" else "brand",
            claims[requirement], auth.issuer, secret,
        ))
    report = assess_business_readiness(
        snapshot, required=requirements,
        authorities=keys, proofs=tuple(proofs), as_of=NOW,
    )
    assert report["status"] == "BUSINESS_EVIDENCE_COMPLETE"
    assert report["business_evidence_satisfied"] is True
    assert report["external_action_authorized"] is False
    assert report["external_action_executed"] is False

    invalid = replace(
        proofs[0], claims={**proofs[0].claims, "fully_loaded_cost_usd": 90000},
    )
    changed = assess_business_readiness(
        snapshot, required=requirements, authorities=keys,
        proofs=(invalid, *proofs[1:]), as_of=NOW,
    )
    assert changed["status"] == "REVIEW_REQUIRED"
    assert changed["invalid_evidence_count"] == 1
    assert changed["missing_requirements"]["brand"] == ["budget_feasibility"]

    wrong_revision = assess_business_readiness(
        plan(2), required=requirements, authorities=keys,
        proofs=tuple(proofs), as_of=NOW,
    )
    assert wrong_revision["status"] == "REVIEW_REQUIRED"
    assert wrong_revision["invalid_evidence_count"] == 4
    assert wrong_revision["external_action_authorized"] is False


def test_untrusted_key_and_expired_evidence_fail_closed():
    snapshot = plan()
    trusted, secret = authority("contract_rights")
    other, other_secret = authority("contract_rights")
    claim = signed(snapshot, "contract_rights", "brand", {
        "signed_contract": True,
        "counterparty": "Rights owner",
        "scope_covers_plan": True,
    }, trusted.issuer, other_secret)
    result = assess_business_readiness(
        snapshot, required={"brand": ("contract_rights",)},
        proofs=(claim,), authorities={trusted.issuer: trusted}, as_of=NOW,
    )
    assert result["invalid_evidence_count"] == 1
    valid = signed(snapshot, "contract_rights", "brand", {
        "signed_contract": True, "counterparty": "Rights owner",
        "scope_covers_plan": True,
    }, trusted.issuer, secret)
    stale = assess_business_readiness(
        snapshot, required={"brand": ("contract_rights",)},
        proofs=(valid,), authorities={trusted.issuer: trusted},
        as_of=NOW + timedelta(days=2),
    )
    assert stale["status"] == "REVIEW_REQUIRED"
    assert stale["external_action_authorized"] is False


def test_partial_policy_or_empty_requirement_never_marks_full_plan_complete():
    incomplete = assess_business_readiness(
        plan(), required={"brand": ()}, as_of=NOW,
    )
    assert incomplete["status"] == "REVIEW_REQUIRED"
    assert incomplete["missing_requirements"]["brand"] == [
        "BUSINESS_REQUIREMENT_POLICY_MISSING"
    ]
    assert incomplete["missing_requirements"]["venue"] == [
        "UNQUALIFIED_DECISION_DOMAIN"
    ]
    completely_absent = assess_business_readiness(
        plan(), required={}, as_of=NOW,
    )
    assert completely_absent["business_evidence_satisfied"] is False
    assert completely_absent["external_action_authorized"] is False

"""Offline guards for the opt-in hosted qualification script."""
import pytest

from scripts.qualify_copilot_hosted import (
    QualificationFailure, validate_execute_gate,
)


def caps(*, product="CHECK_REQUIRED", qloo="CHECK_REQUIRED",
         llm="CHECK_REQUIRED", qloo_remaining=9, model_remaining=6):
    return {
        "ofc": "ACTIVE", "storage": "ACTIVE",
        "product": product, "qloo": qloo, "llm": llm,
        "capacity": {
            "qloo_requests_remaining": qloo_remaining,
            "llm_requests_remaining": model_remaining,
        },
    }


def test_stale_previous_checks_allow_only_explicit_qualification_gate():
    validate_execute_gate(caps())
    validate_execute_gate(caps(product="CHECK_REQUIRED", qloo="ACTIVE", llm="CONNECTED"))
    validate_execute_gate(caps(product="ACTIVE", qloo="ACTIVE", llm="ACTIVE"))


@pytest.mark.parametrize(("overrides", "reason"), [
    ({"product": "DEGRADED"}, "PRODUCT_NOT_READY_FOR_QUALIFICATION"),
    ({"product": "NOT_CONFIGURED"}, "PRODUCT_NOT_READY_FOR_QUALIFICATION"),
    ({"qloo": "UNAVAILABLE"}, "QLOO_NEVER_QUALIFIED_OR_UNAVAILABLE"),
    ({"llm": "UNAVAILABLE"}, "MODEL_NEVER_QUALIFIED_OR_UNAVAILABLE"),
    ({"qloo_remaining": 5}, "INSUFFICIENT_LIVE_E2E_PROVIDER_BUDGET"),
    ({"model_remaining": 2}, "INSUFFICIENT_LIVE_E2E_PROVIDER_BUDGET"),
    ({"qloo_remaining": True}, "INSUFFICIENT_LIVE_E2E_PROVIDER_BUDGET"),
])
def test_live_gate_fails_closed_without_spending(overrides, reason):
    with pytest.raises(QualificationFailure, match=reason):
        validate_execute_gate(caps(**overrides))

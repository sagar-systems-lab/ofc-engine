"""Offline regression tests for bounded narration and saved-evidence recovery."""
from hashlib import sha256
import json

import pytest

from ofc_engine import copilot_hosted
from ofc_engine.copilot_compare import CopilotPair
from ofc_engine.copilot_hosted import HostedCopilot
from ofc_engine.copilot_llm import CopilotLLM, JSONReply, LLMUnavailable


def sample_pair(*, extra=""):
    prompt = "Review Nike and Adidas across New York, Los Angeles and Austin"
    shared = {"observations": [{"ranked_candidate_ids": ["Adidas", "Nike"]}]}
    pair = CopilotPair(
        run_id="cp-run-offline", prompt_digest=sha256(prompt.encode()).hexdigest(),
        model_id="gemini-3.1-flash-lite", baseline_digest="a" * 64,
        reviewed_digest="b" * 64, qloo_evidence_digest="c" * 64,
        off={"qloo": shared, "decisions": [{"decision_id": "brand", "candidate_id": "Nike"}],
             "fixture_extension": extra},
        on={"qloo": shared, "decisions": [{"decision_id": "brand", "candidate_id": "Adidas",
                                           "ofc_verdict": "PASS", "ofc_reason": "RANK_CHANGED"}]},
        verification_status="REVIEW_REQUIRED",
    )
    return pair, prompt


def test_preflight_rejects_oversized_narration_without_provider_calls():
    class NoTransport:
        calls = 0

        def post(self, *args, **kwargs):
            self.calls += 1
            raise AssertionError("should not contact model")

    fake = NoTransport()
    model = CopilotLLM(provider="gemini", api_key="offline", transport=fake)
    pair, prompt = sample_pair(extra="x" * 22000)
    with pytest.raises(LLMUnavailable, match="MODEL_NARRATION_CONTEXT_TOO_LARGE"):
        model.preflight_narration(pair, prompt=prompt, mode="off")
    assert fake.calls == 0


def test_fallback_preserves_off_original_and_on_review_without_claiming_city_validation():
    pair, _ = sample_pair()
    cities = ("New York", "Los Angeles", "Austin")
    off = HostedCopilot._evidence_only_answer(pair.off, mode="off", cities=cities)
    on = HostedCopilot._evidence_only_answer(pair.on, mode="on", cities=cities)
    assert "Original choice (brand): Nike" in off
    assert "OFC choice" not in off
    assert "OFC choice (brand): Adidas" in on
    assert "Verdict: PASS" in on
    assert "not independently verified" in off and "not independently verified" in on
    assert "No booking" in on
    assert len(off) <= 4096 and len(on) <= 4096


def test_gemini_narration_marks_output_truncation_without_retry():
    class TruncatingTransport:
        def __init__(self):
            self.calls = []

        def post(self, url, headers, data, *, timeout, max_bytes):
            self.calls.append(json.loads(data))
            envelope = {"candidates": [{"finishReason": "MAX_TOKENS"}]}
            return JSONReply(status=200, body=json.dumps(envelope).encode())

    fake = TruncatingTransport()
    model = CopilotLLM(provider="gemini", api_key="offline", transport=fake)
    pair, prompt = sample_pair()
    with pytest.raises(LLMUnavailable, match="MODEL_OUTPUT_TRUNCATED"):
        model.narrate_arm(pair, prompt=prompt, mode="off")
    assert len(fake.calls) == 1
    config = fake.calls[0]["generationConfig"]
    assert config["thinkingConfig"]["thinkingLevel"] == "minimal"
    assert config["maxOutputTokens"] == 2048


def test_two_arm_failure_is_single_attempt_per_arm(monkeypatch):
    calls = {"off": 0, "on": 0, "reserve": 0}

    def reserve(*args, **kwargs):
        calls["reserve"] += 1

    class Model:
        def narrate_arm(self, pair, *, prompt, mode):
            calls[mode] += 1
            if mode == "on":
                raise LLMUnavailable("MODEL_NETWORK_UNAVAILABLE")
            return "original"

    monkeypatch.setattr(copilot_hosted, "reserve_model_call", reserve)
    hosted = object.__new__(HostedCopilot)
    hosted.store = object()
    hosted.llm_daily_limit = 9
    result = hosted._narrate_arms(
        model=Model(), pair="saved", prompt="test", run_id="offline",
    )
    assert result == ("original", None)
    assert calls == {"off": 1, "on": 1, "reserve": 2}


def test_fallback_single_saved_rank_not_misreported_as_inconsistent():
    observation = {
        "decision_id": "brand_selection",
        "ranked_candidate_ids": ["Puma", "Adidas"],
    }
    shared = {"observations": [observation]}
    original = {
        "qloo": shared,
        "decisions": [{
            "decision_id": "brand_selection", "candidate_id": "Puma",
            "qloo_observation": observation,
        }],
    }
    reviewed = {
        "qloo": shared,
        "decisions": [{
            "decision_id": "brand_selection", "candidate_id": "Puma",
            "qloo_observation": observation, "ofc_verdict": "PASS",
            "ofc_reason": "QLOO_RANK_RECHECK_MATCHED",
            "rank_recheck_completed": True,
        }],
    }
    off = HostedCopilot._evidence_only_answer(original, mode="off", cities=())
    on = HostedCopilot._evidence_only_answer(reviewed, mode="on", cities=())
    assert "Puma > Adidas" in off and "Puma > Adidas" in on
    assert "Independent Qloo rank recheck matched" in on
    assert "Independent Qloo rank recheck matched" not in off
    assert "consistent ordering" not in off + on
    assert "Original choice (brand_selection): Puma" in off
    assert "OFC choice (brand_selection): Puma" in on
    assert "No booking, partnership or other external action" in on


def test_fallback_never_combines_distinct_decision_rankings():
    first = {"decision_id": "brand", "ranked_candidate_ids": ["Nike", "Adidas"]}
    second = {"decision_id": "venue", "ranked_candidate_ids": ["A", "B"]}
    output = HostedCopilot._evidence_only_answer({
        "qloo": {"observations": [first, second]},
        "decisions": [
            {"decision_id": "brand", "candidate_id": "Nike",
             "qloo_observation": first},
            {"decision_id": "venue", "candidate_id": "A",
             "qloo_observation": second},
        ],
    }, mode="off", cities=())
    assert "Recorded Qloo cultural ranking (brand): Nike > Adidas" in output
    assert "Recorded Qloo cultural ranking (venue): A > B" in output
    assert "consistent ordering" not in output

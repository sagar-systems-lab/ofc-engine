"""Deterministic no-network concurrency checks for independent model explanations."""
from threading import Barrier, BrokenBarrierError, Lock

import pytest

from ofc_engine import copilot_hosted
from ofc_engine.copilot_hosted import HostedCopilot
from ofc_engine.copilot_hosted_quota import CopilotQuotaReached
from ofc_engine.copilot_llm import LLMUnavailable


def runtime():
    result = object.__new__(HostedCopilot)
    result.store = object()
    result.llm_daily_limit = 9
    return result


def test_independent_arms_overlap_only_after_two_quota_reservations(monkeypatch):
    reserved = []
    started = []
    barrier = Barrier(2)
    lock = Lock()

    def reserve(store, *, daily_limit, run_id):
        assert run_id == "test-run" and daily_limit == 9
        reserved.append(1)

    class FakeModel:
        def narrate_arm(self, pair, *, prompt, mode):
            assert pair == "sealed" and prompt == "artist brand"
            assert len(reserved) == 2
            with lock:
                started.append(mode)
            try:
                barrier.wait(timeout=3)
            except BrokenBarrierError:
                pytest.fail("narration calls were serialized")
            return "answer-" + mode

    monkeypatch.setattr(copilot_hosted, "reserve_model_call", reserve)
    result = runtime()._narrate_arms(
        model=FakeModel(), pair="sealed", prompt="artist brand", run_id="test-run",
    )
    assert result == ("answer-off", "answer-on")
    assert sorted(started) == ["off", "on"]
    assert len(reserved) == 2


def test_denied_second_reservation_never_contacts_model(monkeypatch):
    calls = []

    def reserve(store, *, daily_limit, run_id):
        calls.append("reserve")
        if len(calls) == 2:
            raise CopilotQuotaReached("LLM_DAILY_QUOTA_EXHAUSTED")

    class FakeModel:
        def narrate_arm(self, *args, **kwargs):
            calls.append("provider_call")
            raise AssertionError("should never start inference")

    monkeypatch.setattr(copilot_hosted, "reserve_model_call", reserve)
    with pytest.raises(CopilotQuotaReached, match="LLM_DAILY_QUOTA_EXHAUSTED"):
        runtime()._narrate_arms(
            model=FakeModel(), pair="sealed", prompt="brand", run_id="test-run",
        )
    assert calls == ["reserve", "reserve"]


def test_failure_does_not_trigger_provider_retry(monkeypatch):
    counts = {"reserve": 0, "off": 0, "on": 0}
    barrier = Barrier(2)

    def reserve(*args, **kwargs):
        counts["reserve"] += 1

    class FakeModel:
        def narrate_arm(self, pair, *, prompt, mode):
            counts[mode] += 1
            barrier.wait(timeout=3)
            if mode == "on":
                raise LLMUnavailable("MODEL_NARRATION_INVALID")
            return "original"

    monkeypatch.setattr(copilot_hosted, "reserve_model_call", reserve)
    with pytest.raises(LLMUnavailable, match="MODEL_NARRATION_INVALID"):
        runtime()._narrate_arms(
            model=FakeModel(), pair="sealed", prompt="brand", run_id="test-run",
        )
    assert counts == {"reserve": 2, "off": 1, "on": 1}

"""Offline fault-injection guards for explicitly confirmed intake recovery."""
import hashlib
import json
from contextlib import contextmanager

import pytest

from ofc_engine.copilot_hosted import HostedCopilot, SessionDenied
from ofc_engine.revisions import digest_obj


def valid():
    return {
        "artist": "Billie Eilish",
        "brands": ["Nike", "Adidas"],
        "selected": "Nike",
    }


def test_typed_manual_plan_requires_explicit_unambiguous_names():
    draft = HostedCopilot._confirmed_intake_plan(valid())
    assert draft.artist_name == draft.taste_seed_name == "Billie Eilish"
    assert draft.taste_seed_category == "artist"
    assert draft.choices[0].candidate_names == ("Nike", "Adidas")
    assert draft.choices[0].selected_name == "Nike"
    assert draft.choices[0].domain == "brand"
    assert draft.budget_usd is None and draft.cities == ()
    assert "No cultural or commercial claims were verified" in draft.baseline_answer


@pytest.mark.parametrize("mutate", [
    lambda x: x.update(artist=""),
    lambda x: x.update(artist="A" * 161),
    lambda x: x.update(brands=["Nike", "Nike"]),
    lambda x: x.update(brands=["Nike"]),
    lambda x: x.update(selected="Puma"),
    lambda x: x.update(brand_id="forged"),
    lambda x: x.update(artist="A\\x00B"),
    lambda x: x.update(brands=["Nike", 123]),
])
def test_manual_plan_fails_closed_on_invalid_identity(mutate):
    value = valid()
    mutate(value)
    with pytest.raises(ValueError):
        HostedCopilot._confirmed_intake_plan(value)


def test_manual_parent_requires_same_session_prompt_and_network_failure():
    run_id = "cp-run-" + "1" * 32
    prompt = "Which brand fits Billie Eilish: Nike or Adidas? I prefer Nike."

    def check(*, reason="MODEL_NETWORK_UNAVAILABLE", status="BLOCKED",
              prompt_digest=None, attempts=0, session="cp-" + "a" * 32,
              parent=run_id):
        record = {
            "run_id": run_id, "status": status, "reason_code": reason,
            "qloo_requests_this_run": attempts,
        }
        row = (status, json.dumps(record), digest_obj(record), None,
               prompt_digest or hashlib.sha256(prompt.encode()).hexdigest())

        class DB:
            def execute(self, query, params):
                assert "where run_id=%s and session_id=%s" in query
                return self

            def fetchone(self):
                if parent != run_id or session != "cp-" + "a" * 32:
                    return None
                return row

        class Store:
            @contextmanager
            def _connect(self):
                yield DB()

        service = object.__new__(HostedCopilot)
        service.store = Store()
        service._validate_confirmed_parent(session, parent, prompt)

    check()
    check(reason="MODEL_TIMEOUT")
    for kwargs in [
        {"reason": "PROVIDER_HTTP_429"},
        {"status": "COMPLETE"},
        {"prompt_digest": "f" * 64},
        {"attempts": 1},
        {"session": "cp-" + "b" * 32},
        {"parent": "cp-run-" + "2" * 32},
    ]:
        with pytest.raises(SessionDenied):
            check(**kwargs)

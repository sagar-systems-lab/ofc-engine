from ofc_engine.copilot_activity import activity_projection


class FakeCursor:
    def __init__(self, states):
        self.states = states

    def fetchall(self):
        return [(state,) for state in self.states]


class FakeConnection:
    def __init__(self, *, search=(), rank=(), catalog=(), discovery=()):
        self.states = {
            "copilot_search": search,
            "dynamic_qloo_attempts": rank,
            "copilot_domain_observations": catalog,
            "copilot_discoveries": discovery,
        }

    def execute(self, sql, params):
        assert params == ("cp-run-test",)
        matches = [value for name, value in self.states.items() if name in sql]
        assert len(matches) == 1
        assert "result" not in sql and "ciphertext" not in sql
        return FakeCursor(matches[0])


def project(record, **rows):
    return activity_projection(FakeConnection(**rows), "cp-run-test", record)


def test_completed_search_does_not_imply_rank_or_review():
    output = project(
        {"status": "BLOCKED", "reason_code": "COPILOT_EVIDENCE_UNAVAILABLE"},
        search=("COMPLETE", "COMPLETE", "COMPLETE"), rank=("RESERVED",),
    )
    assert output["agent_plan"] == "NOT_OBSERVED"
    assert output["qloo_evidence"] == "RANK_PENDING_OR_INTERRUPTED"
    assert output["ofc_review"] == "NOT_OBSERVED"
    assert output["results"] == "BLOCKED"
    assert output["requests"] == {
        "search_complete": 3,
        "search_reserved": 0,
        "rank_complete": 0,
        "rank_reserved": 1,
    }
    assert set(output) == {
        "source", "agent_plan", "qloo_evidence", "ofc_review", "results", "requests",
    }


def test_complete_saved_pair_with_rank_and_receipt():
    output = project(
        {
            "status": "REVIEW_REQUIRED",
            "plan": {"choices": [{"decision_id": "brand"}]},
            "value_receipt": {"status": "REVIEW_REQUIRED"},
        },
        search=("COMPLETE", "COMPLETE"), rank=("COMPLETE",),
    )
    assert output["agent_plan"] == "OBSERVED"
    assert output["qloo_evidence"] == "RANK_OBSERVED"
    assert output["ofc_review"] == "OBSERVED"
    assert output["results"] == "REVIEW_REQUIRED"


def test_processing_and_partial_do_not_report_complete():
    output = project(
        {"status": "PROCESSING"},
        search=("RESERVED",),
    )
    assert output["agent_plan"] == "ADMITTED"
    assert output["qloo_evidence"] == "SEARCH_PENDING_OR_INTERRUPTED"
    assert output["results"] == "PROCESSING"
    incomplete = project(
        {"status": "PROCESSING"},
        rank=("COMPLETE", "RESERVED"), catalog=("COMPLETE",),
    )
    assert incomplete["qloo_evidence"] == "PARTIAL"
    assert incomplete["requests"]["rank_complete"] == 2
    assert incomplete["requests"]["rank_reserved"] == 1


def test_unsupported_scope_keeps_unobserved_stages():
    output = project(
        {"status": "CAPABILITY_UNAVAILABLE", "plan": {"choices": []}},
    )
    assert output["agent_plan"] == "OBSERVED"
    assert output["qloo_evidence"] == "NOT_OBSERVED"
    assert output["ofc_review"] == "NOT_OBSERVED"
    assert output["results"] == "CAPABILITY_UNAVAILABLE"

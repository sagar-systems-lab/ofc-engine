import json

import pytest

from ofc_engine.control.intake import GraphDraft
from ofc_engine.copilot_llm import CopilotLLM, LLMUnavailable
from test_copilot_llm import FakeTransport, draft


def graph_output(size=30):
    value = draft()
    root = value["choices"][0]
    value["choices"] = [root] + [{"decision_id": f"operation_{i}", "domain": "operations",
                                 "selected_name": "Declared task", "candidate_names": ["Declared task"],
                                 "mutable": False, "depends_on": ["brand"] if i < 10 else []} for i in range(1, size)]
    return value


@pytest.mark.parametrize("provider", ["gemini", "groq"])
def test_large_natural_language_draft_is_explicit_profile_and_uses_fake_transport(provider):
    fake = FakeTransport(graph_output())
    model = CopilotLLM(provider=provider, api_key="unit-test-only", transport=fake, intake_profile="graph")
    result = model.extract("Review my declared campaign decisions and dependencies")
    assert isinstance(result, GraphDraft) and len(result.choices) == 30
    assert len(result.choices[1].candidate_names) == 1
    body = fake.calls[0][2]
    schema = body["generationConfig"]["responseJsonSchema"] if provider == "gemini" else body["response_format"]["json_schema"]["schema"]
    assert schema["properties"]["choices"]["maxItems"] == 64
    assert len(fake.calls) == 1


def test_standard_profile_does_not_silently_expand_or_mutate_shared_schema():
    model = CopilotLLM(provider="gemini", api_key="unit-test-only", transport=FakeTransport(graph_output()))
    with pytest.raises(LLMUnavailable): model.extract("A large proposed plan")
    fake = FakeTransport()
    CopilotLLM(provider="gemini", api_key="unit-test-only", transport=fake).extract("An ordinary proposal")
    assert fake.calls[0][2]["generationConfig"]["responseJsonSchema"]["properties"]["choices"]["maxItems"] == 6


def test_extracted_cycle_is_rejected_before_entity_lookup():
    value = graph_output()
    value["choices"][0]["depends_on"] = ["operation_1"]
    with pytest.raises(ValueError): GraphDraft.model_validate_json(json.dumps(value))

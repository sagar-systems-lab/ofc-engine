"""Gemini/Groq extraction contract with no external model calls."""
import json
import pytest

from ofc_engine.copilot_llm import (
    CopilotDraft, CopilotLLM, HTTPSJSON, JSONReply, LLMUnavailable,
)


def draft():
    return {
        "objective": "Select a brand partner",
        "artist_name": "Example Artist",
        "budget_usd": 60000,
        "cities": ["Chicago", "Austin", "Seattle"],
        "choices": [{
            "decision_id": "brand",
            "domain": "brand",
            "selected_name": "Brand A",
            "candidate_names": ["Brand A", "Brand B"],
            "mutable": True,
            "depends_on": [],
        }],
        "baseline_answer": "I would provisionally propose Brand A.",
        "clarification_question": None,
    }


class FakeTransport:
    def __init__(self, output=None, *, status=200, invalid=None):
        self.output = output or draft()
        self.status = status
        self.invalid = invalid
        self.calls = []

    def post(self, url, headers, data, *, timeout, max_bytes):
        request = json.loads(data)
        self.calls.append((url, headers, request, timeout, max_bytes))
        if self.invalid == "bad-json":
            return JSONReply(200, b"{")
        text = json.dumps(self.output)
        if self.invalid == "malformed-draft":
            text = "{}"
        if "generativelanguage.googleapis.com" in url:
            reply = {
                "candidates": [{
                    "finishReason": "MAX_TOKENS" if self.invalid == "truncated" else "STOP",
                    "content": {"parts": [{"text": text}]},
                }],
            }
        else:
            reply = {
                "choices": [{
                    "finish_reason": "length" if self.invalid == "truncated" else "stop",
                    "message": {"content": text},
                }],
            }
        return JSONReply(self.status, json.dumps(reply).encode())


@pytest.mark.parametrize("provider", ["gemini", "groq"])
def test_real_provider_request_shape_but_fake_transport(provider):
    fake = FakeTransport()
    client = CopilotLLM(
        provider=provider, api_key="test-key-not-public", transport=fake,
    )
    output = client.extract("Plan a music tour for Chicago Austin Seattle")
    assert isinstance(output, CopilotDraft)
    assert output.budget_usd == 60000
    assert output.choices[0].selected_name == "Brand A"
    assert output.choices[0].candidate_names == ("Brand A", "Brand B")
    assert output.clarification_question is None
    assert len(fake.calls) == 1
    url, headers, body, timeout, max_bytes = fake.calls[0]
    assert timeout == 15
    assert max_bytes <= 96 * 1024
    assert "test-key-not-public" not in json.dumps(body)
    if provider == "gemini":
        assert "generativelanguage.googleapis.com" in url
        assert client.model == "gemini-3.1-flash-lite"
        assert body["generationConfig"]["responseMimeType"] == "application/json"
        assert headers["x-goog-api-key"] == "test-key-not-public"
    else:
        assert url == "https://api.groq.com/openai/v1/chat/completions"
        assert client.model == "openai/gpt-oss-20b"
        assert body["response_format"]["json_schema"]["strict"] is True
        assert headers["Authorization"] == "Bearer test-key-not-public"


@pytest.mark.parametrize("provider", ["gemini", "groq"])
@pytest.mark.parametrize("failure", ["bad-json", "malformed-draft", "truncated"])
def test_provider_failure_never_invents_plan_or_exposes_raw_data(provider, failure):
    client = CopilotLLM(
        provider=provider, api_key="private-token-here",
        transport=FakeTransport(invalid=failure),
    )
    with pytest.raises(LLMUnavailable) as exc:
        client.extract("Pick a brand partner")
    assert str(exc.value) in ("INVALID_MODEL_JSON", "MODEL_DRAFT_INVALID")
    assert "private-token-here" not in str(exc.value)


def test_invalid_candidate_and_dependency_rejected():
    data = draft()
    data["choices"][0]["selected_name"] = "Unknown Brand"
    with pytest.raises(ValueError):
        CopilotDraft.model_validate(data)
    data = draft()
    data["choices"][0]["depends_on"] = ["not-a-decision"]
    with pytest.raises(ValueError):
        CopilotDraft.model_validate(data)


def test_missing_required_choices_requires_clarification():
    data = draft()
    data["choices"] = []
    with pytest.raises(ValueError):
        CopilotDraft.model_validate(data)
    data["clarification_question"] = "Which brands should I compare?"
    assert CopilotDraft.model_validate(data).choices == ()


def test_budget_and_input_guards_are_fail_closed():
    fake = FakeTransport()
    for provider in ("gemini", "groq"):
        client = CopilotLLM(provider=provider, api_key="local", transport=fake)
        for value in ("", "  ", "x" * 4001, "abc\x00def"):
            with pytest.raises(ValueError):
                client.extract(value)
    assert fake.calls == []
    for bad in ("", "\n", "missing\nsecret"):
        with pytest.raises(ValueError):
            CopilotLLM(provider="gemini", api_key=bad)


def test_http_adapter_enforces_fixed_hosts_before_network():
    transport = HTTPSJSON()
    with pytest.raises(LLMUnavailable, match="DISALLOWED_MODEL_ENDPOINT"):
        transport.post(
            "https://attacker.invalid/steal", {},
            b"{}", timeout=0.1, max_bytes=100,
        )


def test_provider_429_does_not_autoretry():
    fake = FakeTransport(status=429)
    client = CopilotLLM(provider="gemini", api_key="ci", transport=fake)
    with pytest.raises(LLMUnavailable, match="PROVIDER_HTTP_429"):
        client.extract("Choose a brand")
    assert len(fake.calls) == 1

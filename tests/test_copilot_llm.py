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
        "taste_seed_name": None,
        "taste_seed_category": None,
        "discovery_category": None,
        "taste_location": None,
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
        assert body["generationConfig"]["thinkingConfig"] == {"thinkingLevel": "minimal"}
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
    assert str(exc.value) == {
        "bad-json": "INVALID_MODEL_JSON",
        "malformed-draft": "MODEL_DRAFT_SCHEMA_INVALID",
        "truncated": "MODEL_OUTPUT_TRUNCATED",
    }[failure]
    assert "private-token-here" not in str(exc.value)


def test_invalid_candidate_and_dependency_rejected():
    data = draft()
    data["choices"][0]["selected_name"] = "Unknown Brand"
    with pytest.raises(ValueError):
        CopilotDraft.model_validate_json(json.dumps(data))
    data = draft()
    data["choices"][0]["depends_on"] = ["not-a-decision"]
    with pytest.raises(ValueError):
        CopilotDraft.model_validate_json(json.dumps(data))


def test_missing_required_choices_requires_clarification():
    data = draft()
    data["choices"] = []
    with pytest.raises(ValueError):
        CopilotDraft.model_validate_json(json.dumps(data))
    data["clarification_question"] = "Which brands should I compare?"
    assert CopilotDraft.model_validate_json(json.dumps(data)).choices == ()


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


@pytest.mark.parametrize(("reply", "expected"), [
    ({"candidates": []}, "MODEL_RESPONSE_MISSING"),
    ({"candidates": [{"finishReason": "MAX_TOKENS"}]}, "MODEL_OUTPUT_TRUNCATED"),
    ({"candidates": [{"finishReason": "SAFETY"}]}, "MODEL_FINISH_REJECTED"),
    ({"candidates": [{"finishReason": "STOP", "content": {"parts": []}}]},
     "MODEL_RESPONSE_MISSING"),
    ({"candidates": [{"finishReason": "STOP", "content": {
        "parts": [{"text": "not valid json"}]}}]}, "MODEL_DRAFT_JSON_INVALID"),
    ({"candidates": [{"finishReason": "STOP", "content": {
        "parts": [{"text": "{}"}]}}]}, "MODEL_DRAFT_SCHEMA_INVALID"),
])
def test_gemini_failure_subtype_is_safe_and_single_call(reply, expected):
    class Stub:
        def __init__(self):
            self.calls = 0

        def post(self, url, headers, data, *, timeout, max_bytes):
            self.calls += 1
            return JSONReply(200, json.dumps(reply).encode())

    transport = Stub()
    with pytest.raises(LLMUnavailable, match=expected):
        CopilotLLM(provider="gemini", api_key="fake", transport=transport).extract(
            "Compare Brand A and Brand B for Example Artist"
        )
    assert transport.calls == 1


@pytest.mark.parametrize(("body", "error"), [
    ({"candidates": [{"finishReason": "STOP", "content": {
        "parts": [{"text": '{"ready":true}'}]}}]}, None),
    ({"candidates": [{"finishReason": "STOP", "content": {
        "parts": [{"text": '{"ready":false}'}]}}]}, "MODEL_PROBE_INVALID"),
    ({"candidates": [{"finishReason": "STOP", "content": {
        "parts": [{"text": '{}'}]}}]}, "MODEL_PROBE_INVALID"),
    ({"candidates": [{"finishReason": "STOP", "content": {
        "parts": [{"text": 'not-json'}]}}]}, "MODEL_DRAFT_JSON_INVALID"),
    ({"candidates": [{"finishReason": "MAX_TOKENS"}]}, "MODEL_OUTPUT_TRUNCATED"),
    ({"candidates": [{"finishReason": "SAFETY"}]}, "MODEL_FINISH_REJECTED"),
    ({"candidates": []}, "MODEL_RESPONSE_MISSING"),
])
def test_gemini_minimal_json_connectivity_probe_one_call(body, error):
    class Stub:
        def __init__(self):
            self.calls = []

        def post(self, url, headers, data, *, timeout, max_bytes):
            self.calls.append((url, headers, json.loads(data)))
            return JSONReply(200, json.dumps(body).encode())

    fake = Stub()
    model = CopilotLLM(provider="gemini", api_key="fake-key", transport=fake)
    if error is None:
        assert model.probe_connectivity() is None
    else:
        with pytest.raises(LLMUnavailable, match=error):
            model.probe_connectivity()
    assert len(fake.calls) == 1
    url, headers, payload = fake.calls[0]
    assert url.endswith("gemini-3.1-flash-lite:generateContent")
    assert headers["x-goog-api-key"] == "fake-key"
    assert payload["generationConfig"]["thinkingConfig"] == {"thinkingLevel": "minimal"}
    assert payload["generationConfig"]["maxOutputTokens"] == 128
    assert payload["generationConfig"]["responseJsonSchema"]["required"] == ["ready"]
    assert "choices" not in json.dumps(payload)


def test_minimal_probe_never_accepts_provider_http_rejection():
    class Reject:
        calls = 0

        def post(self, *args, **kwargs):
            self.calls += 1
            return JSONReply(403, b'{"private":"must not appear"}')

    fake = Reject()
    with pytest.raises(LLMUnavailable, match="PROVIDER_HTTP_403"):
        CopilotLLM(provider="gemini", api_key="hidden", transport=fake).probe_connectivity()
    assert fake.calls == 1


@pytest.mark.parametrize("alias", [
    "brand partner", "brand partnership", "brand_sponsorship",
    "brand sponsor", "sponsorship-brand", "artist brand sponsorship",
])
def test_explicit_brand_aliases_preserve_candidate_scope(alias):
    data = draft()
    data["choices"][0]["domain"] = alias
    normalized = CopilotDraft.model_validate_json(json.dumps(data))
    assert normalized.choices[0].domain == "brand"
    assert normalized.choices[0].candidate_names == ("Brand A", "Brand B")


@pytest.mark.parametrize("domain", [
    "contract", "venue", "payment", "sponsorship", "restaurant",
])
def test_unqualified_domains_are_not_generalized_to_brand(domain):
    data = draft()
    data["choices"][0]["domain"] = domain
    assert CopilotDraft.model_validate_json(json.dumps(data)).choices[0].domain == domain


@pytest.mark.parametrize("category", ["musician", "singer", "musical_artist"])
def test_explicit_named_artist_seed_can_normalize_music_labels(category):
    data = draft()
    data["taste_seed_category"] = category
    data["taste_seed_name"] = data["artist_name"]
    assert CopilotDraft.model_validate_json(json.dumps(data)).taste_seed_category == "artist"


def test_seed_different_from_artist_not_reinterpreted():
    data = draft()
    data["taste_seed_name"] = "Other Artist"
    data["taste_seed_category"] = "singer"
    assert CopilotDraft.model_validate_json(json.dumps(data)).taste_seed_category == "singer"


def test_explicit_artist_brand_scope_changes_only_model_taxonomy():
    from ofc_engine.copilot_hosted import HostedCopilot

    data = draft()
    data["artist_name"] = "Taylor Swift"
    data["taste_seed_name"] = "Taylor Swift"
    data["taste_seed_category"] = "celebrity"
    data["choices"][0].update(
        {"decision_id": "sponsor", "domain": "sponsorship",
         "selected_name": "Nike", "candidate_names": ["Nike", "Nike Training Club"]}
    )
    parsed = CopilotDraft.model_validate_json(json.dumps(data))
    prompt = (
        "Artist: Taylor Swift. Original brand choice: Nike. "
        "Candidate brands: Nike and Nike Training Club. "
        "This is strictly a hypothetical plan."
    )
    corrected = HostedCopilot._explicit_brand_scope(parsed, prompt)
    assert corrected.choices[0].domain == "brand"
    assert corrected.taste_seed_category == "artist"
    assert corrected.artist_name == parsed.artist_name
    assert corrected.choices[0].candidate_names == parsed.choices[0].candidate_names
    assert corrected.choices[0].selected_name == "Nike"
    assert parsed.choices[0].domain == "sponsorship"


def test_artist_brand_scope_never_relabels_contracts_or_ambiguous_requests():
    from ofc_engine.copilot_hosted import HostedCopilot

    data = draft()
    data["artist_name"] = "Taylor Swift"
    data["choices"][0]["domain"] = "contract"
    parsed = CopilotDraft.model_validate_json(json.dumps(data))
    explicit = "Artist: Taylor Swift. Candidate brands: Nike and Nike Training Club. Original brand choice: Nike."
    assert HostedCopilot._explicit_brand_scope(parsed, explicit).choices[0].domain == "contract"
    data["choices"][0]["domain"] = "sponsorship"
    ambiguous = CopilotDraft.model_validate_json(json.dumps(data))
    assert HostedCopilot._explicit_brand_scope(
        ambiguous, "A sponsorship for Taylor Swift"
    ).choices[0].domain == "sponsorship"
    data["taste_seed_name"] = "Another Artist"
    mismatch = CopilotDraft.model_validate_json(json.dumps(data))
    assert HostedCopilot._explicit_brand_scope(mismatch, explicit).choices[0].domain == "sponsorship"


def test_copilot_narration_uses_human_names_without_mutating_evidence():
    from dataclasses import replace
    from hashlib import sha256
    from types import SimpleNamespace

    from ofc_engine.copilot_compare import CopilotPair
    from ofc_engine.copilot_hosted import HostedCopilot

    artist_id = "00000000-0000-4000-8000-000000000001"
    nike_id = "00000000-0000-4000-8000-000000000002"
    adidas_id = "00000000-0000-4000-8000-000000000003"
    ranked = [adidas_id, nike_id]
    shared = {"provider": "QLOO", "evidence_digest": "f" * 64,
              "observations": [{"ranked_candidate_ids": ranked.copy()}]}
    observation = {"ranked_candidate_ids": ranked.copy()}
    off = {
        "ofc_enabled": False, "qloo": shared,
        "decisions": [{"candidate_id": nike_id,
                       "candidate_pool": [nike_id, adidas_id],
                       "qloo_observation": observation.copy()}],
    }
    on = {
        "ofc_enabled": True, "qloo": shared,
        "decisions": [{"candidate_id": adidas_id,
                       "candidate_pool": [nike_id, adidas_id],
                       "qloo_observation": observation.copy(),
                       "ofc_verdict": "PASS"}],
    }
    prompt = "Compare Nike and Adidas for a hypothetical artist partnership"
    pair = CopilotPair(
        run_id="test-run", prompt_digest=sha256(prompt.encode()).hexdigest(),
        model_id="gemini-3.1-flash-lite",
        baseline_digest="a" * 64, reviewed_digest="b" * 64,
        qloo_evidence_digest="f" * 64,
        off=off, on=on, verification_status="REVIEW_REQUIRED",
    )
    draft_obj = SimpleNamespace(choices=[SimpleNamespace(
        domain="brand", decision_id="brand", candidate_names=("Nike", "Adidas"),
    )])
    brands = {
        "Nike": SimpleNamespace(entity_id=nike_id),
        "Adidas": SimpleNamespace(entity_id=adidas_id),
    }
    artist = SimpleNamespace(entity_id=artist_id, name="Example Artist")
    projected = HostedCopilot._public_pair(pair, draft_obj, brands, artist)
    assert pair.off["decisions"][0]["candidate_id"] == nike_id
    assert pair.on["decisions"][0]["candidate_id"] == adidas_id
    assert pair.off["qloo"]["observations"][0]["ranked_candidate_ids"] == ranked
    assert projected["off"]["decisions"][0]["candidate_id"] == "Nike"
    assert projected["on"]["decisions"][0]["candidate_id"] == "Adidas"
    assert projected["off"]["qloo"] == projected["on"]["qloo"]
    narrated_pair = replace(pair, off=projected["off"], on=projected["on"])
    fake = FakeTransport(output={"answer": "Adidas has a higher observed ranking."})
    client = CopilotLLM(provider="gemini", api_key="offline-only", transport=fake)
    assert client.narrate_arm(narrated_pair, prompt=prompt, mode="off")
    assert client.narrate_arm(narrated_pair, prompt=prompt, mode="on")
    for _, _, request, _, _ in fake.calls:
        text = request["contents"][0]["parts"][0]["text"]
        assert nike_id not in text and adidas_id not in text
        assert "Nike" in text and "Adidas" in text
    assert pair.off["qloo"]["observations"][0]["ranked_candidate_ids"] == ranked


def test_qloo_budget_prioritizes_brands_in_three_city_agent_plan():
    from ofc_engine.copilot_hosted import HostedCopilot

    data = draft()
    data["objective"] = "Evaluate a three-city artist tour with a brand and three venues"
    for city in ("Chicago", "Austin", "Seattle"):
        data["choices"].append({
            "decision_id": "venue_" + city.lower(),
            "domain": "place",
            "selected_name": city + " Venue A",
            "candidate_names": [city + " Venue A", city + " Venue B"],
            "mutable": True,
            "depends_on": ["brand"],
        })
    typed = CopilotDraft.model_validate_json(json.dumps(data))
    catalog = {choice.decision_id: "place" for choice in typed.choices[1:]}
    core, full = HostedCopilot._qloo_budget_projection(
        typed, catalog, "artist",
    )
    assert len(typed.cities) == 3
    assert len(typed.choices) == 4
    assert core == 5
    assert full == 14
    assert core <= 12 < full
    planned, skipped, required = HostedCopilot._catalog_budget_scope(
        typed, catalog, "artist", 12,
    )
    assert planned == {}
    assert skipped == catalog
    assert required == 5
    planned, skipped, required = HostedCopilot._catalog_budget_scope(
        typed, catalog, "artist", 14,
    )
    assert planned == catalog
    assert skipped == {}
    assert required == 14
    planned, skipped, required = HostedCopilot._catalog_budget_scope(
        typed, catalog, "artist", 4,
    )
    assert planned == catalog and skipped == {} and required == 14


def test_qloo_budget_counts_distinct_brand_lookups_and_rechecks():
    from ofc_engine.copilot_hosted import HostedCopilot

    data = draft()
    data["choices"].append({
        "decision_id": "second_brand",
        "domain": "brand",
        "selected_name": "Brand B",
        "candidate_names": ["Brand A", "Brand B"],
        "mutable": True,
        "depends_on": [],
    })
    typed = CopilotDraft.model_validate_json(json.dumps(data))
    assert HostedCopilot._qloo_budget_projection(typed, {}, "artist") == (7, 7)
    assert HostedCopilot._qloo_budget_projection(typed, {}, "brand") == (1, 3)

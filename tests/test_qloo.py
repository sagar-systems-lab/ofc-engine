"""Provider adapter boundary tested without Qloo credentials or network calls."""
import json
import os
from pathlib import Path
import urllib.error

import pytest

from ofc_engine.qloo import (
    HOST, MAX_BYTES, HTTPSGet, ProbeSpec, QlooProbeClient, QlooProbeError,
    TransportReply, brand_affinity_probe, search_probe,
)
from ofc_engine import qloo_probe


class FakeTransport:
    def __init__(self, *, status=200, body=b'{"results":[],"query":{"warnings":[]}}', fail=None):
        self.calls = []
        self.status, self.body, self.fail = status, body, fail

    def get(self, url, headers, *, timeout, max_bytes):
        self.calls.append((url, dict(headers), timeout, max_bytes))
        if self.fail:
            raise self.fail
        return TransportReply(self.status, self.body)


def client(transport):
    return QlooProbeClient(api_key="test-only-never-live", transport=transport)


def test_search_is_bounded_and_encoded():
    spec = search_probe("  A & B  ", entity_type="urn:entity:artist")
    assert spec.path == "/search"
    assert "A+%26+B" in spec.url
    assert "types=urn%3Aentity%3Aartist" in spec.url
    assert spec.url.startswith(HOST + "/")
    assert spec == search_probe("A & B", entity_type="urn:entity:artist")
    assert spec.fingerprint == search_probe("A & B", entity_type="urn:entity:artist").fingerprint


@pytest.mark.parametrize("entity_type", ["urn:entity:locality", "person", "", "urn:entity:neon_car"])
def test_search_type_restriction(entity_type):
    with pytest.raises(ValueError):
        search_probe("artist", entity_type=entity_type)


@pytest.mark.parametrize("take", [0, 21, -1, True, 1.5])
def test_search_take_bounded(take):
    with pytest.raises(ValueError):
        search_probe("Artist", entity_type="urn:entity:artist", take=take)


def test_brand_fixed_pool_params():
    q = brand_affinity_probe("ARTIST", ("A", "B", "C"))
    p = dict(q.params)
    assert p["filter.type"] == "urn:entity:brand"
    assert p["signal.interests.entities"] == "ARTIST"
    assert p["filter.results.entities"] == "A,B,C"
    assert p["take"] == "3"
    assert p["feature.explainability"] == "true"
    assert "X-Api-Key" not in q.url


def test_geographic_probe_modes_are_distinct():
    a = brand_affinity_probe("artist", ("a", "b"), locality="Chicago", locality_mode="filter")
    b = brand_affinity_probe("artist", ("a", "b"), locality="Chicago", locality_mode="signal")
    assert a.fingerprint != b.fingerprint
    assert "filter.location.query" in dict(a.params)
    assert dict(a.params)["filter.location.radius"] == "0"
    assert "signal.location.query" in dict(b.params)
    assert "filter.location.query" not in dict(b.params)
    assert "filter.location.radius" not in dict(b.params)


@pytest.mark.parametrize("ids", [(), ("one",), ("a", "a"), ("a", "b", "c", "d", "e", "f"), ("a,b", "b")])
def test_fixed_pool_invalid_ids_rejected(ids):
    with pytest.raises(ValueError):
        brand_affinity_probe("artist", ids)


def test_probe_invalid_options_rejected():
    with pytest.raises(ValueError):
        brand_affinity_probe("artist", ("a", "b"), locality="LA")
    with pytest.raises(ValueError):
        brand_affinity_probe("artist", ("a", "b"), locality_mode="signal")
    with pytest.raises(ValueError):
        brand_affinity_probe("artist", ("a", "b"), locality="LA", locality_mode="unsupported")
    with pytest.raises(ValueError):
        brand_affinity_probe("", ("a", "b"))
    with pytest.raises(ValueError):
        ProbeSpec("https://evil.invalid/", (("x", "y"),))
    with pytest.raises(ValueError):
        ProbeSpec("/search", (("query", "one"), ("query", "two")))


def test_get_transport_never_leaks_secret():
    fake = FakeTransport()
    result = client(fake).probe(search_probe("Artist", entity_type="urn:entity:artist"))
    url, headers, timeout, limit = fake.calls[0]
    assert headers["X-Api-Key"] == "test-only-never-live"
    assert headers["Accept"] == "application/json"
    assert "test-only-never-live" not in url
    assert result.top_level_fields == ("query", "results")
    assert result.authenticated_by_adapter is False
    assert result.normalized_for_policy is False
    assert not hasattr(result, "verdict")
    assert "test-only-never-live" not in repr(result)
    assert "test-only-never-live" not in json.dumps(result.safe_summary())
    assert limit == MAX_BYTES and timeout == 8


@pytest.mark.parametrize("status", [301, 400, 401, 403, 429, 500, 503])
def test_non_200_never_fakes_success(status):
    with pytest.raises(QlooProbeError, match=f"HTTP_{status}"):
        client(FakeTransport(status=status)).probe(search_probe("A", entity_type="urn:entity:brand"))


@pytest.mark.parametrize("payload", [b"garbage", b"[]", b"null", b'"a"', b"", b"\xff"])
def test_bad_json_shape_rejected(payload):
    with pytest.raises(QlooProbeError):
        client(FakeTransport(body=payload)).probe(search_probe("A", entity_type="urn:entity:brand"))


def test_network_errors_redacted_and_no_retry():
    transport = FakeTransport(fail=TimeoutError("secret-looking timeout value"))
    with pytest.raises(QlooProbeError) as err:
        client(transport).probe(search_probe("A", entity_type="urn:entity:brand"))
    assert err.value.code == "TRANSPORT_ERROR"
    assert len(transport.calls) == 1
    assert "secret-looking" not in str(err.value)


def test_oversized_payload_fails_closed():
    with pytest.raises(QlooProbeError, match="RESPONSE_TOO_LARGE"):
        client(FakeTransport(body=b"x" * (MAX_BYTES + 1))).probe(search_probe("A", entity_type="urn:entity:brand"))


def test_from_environment_does_not_default_to_mock_key(monkeypatch):
    monkeypatch.delenv("QLOO_API_KEY", raising=False)
    with pytest.raises(QlooProbeError, match="API_KEY_MISSING"):
        QlooProbeClient.from_environment()


def test_invalid_timeout_or_key():
    with pytest.raises(QlooProbeError):
        QlooProbeClient(api_key="abc\r\nHeader: value")
    for timeout in (0, 21, True):
        with pytest.raises(ValueError):
            QlooProbeClient(api_key="sample", timeout=timeout)


def test_default_http_rejects_unpinned_hosts():
    with pytest.raises(QlooProbeError, match="DISALLOWED_HOST"):
        HTTPSGet().get("https://fake.invalid/?a=1", {"X-Api-Key": "test"}, timeout=1, max_bytes=100)


def test_cli_defaults_to_zero_network(monkeypatch, capsys):
    monkeypatch.delenv("QLOO_API_KEY", raising=False)
    assert qloo_probe.main(["--search", "Example", "--type", "artist"]) == 0
    result = json.loads(capsys.readouterr().out)
    assert result["real_qloo_requests"] == 0 and result["mode"] == "DRY_RUN_NO_NETWORK"
    assert "query_fingerprint" in result


def test_cli_needs_explicit_live_and_key(monkeypatch, capsys):
    monkeypatch.delenv("QLOO_API_KEY", raising=False)
    assert qloo_probe.main(["--live", "--search", "Example"]) == 2
    assert json.loads(capsys.readouterr().err)["code"] == "API_KEY_MISSING"


def test_cli_will_not_capture_without_live():
    with pytest.raises(SystemExit):
        qloo_probe.main(["--capture", "--search", "Some artist"])


def test_capture_private_file_permissions(monkeypatch, tmp_path):
    monkeypatch.setattr(qloo_probe.Path, "home", classmethod(lambda cls: tmp_path))
    out = qloo_probe.private_capture({"entities": [{"id": "abc"}]}, {"source": "test"})
    assert out.stat().st_mode & 0o777 == 0o600
    assert out.parent.stat().st_mode & 0o777 == 0o700
    assert "private-qloo-probes" in str(out)
    assert json.loads(out.read_text())["payload"]["entities"][0]["id"] == "abc"


def test_no_unintended_http_calls_on_cli_dryrun(monkeypatch, capsys):
    def fail(*args, **kwargs):
        raise AssertionError("network should never be accessed")
    monkeypatch.setattr(QlooProbeClient, "probe", fail)
    assert qloo_probe.main(["--artist-id", "ART", "--brands", "A,B", "--locality", "Delhi", "--locality-mode", "signal"]) == 0
    assert json.loads(capsys.readouterr().out)["real_qloo_requests"] == 0


def test_payload_injection_must_not_set_qualified_status():
    malicious = {"VERIFIED": True, "source_mode": "VERIFIED_LIVE", "verdict": "PASS", "release_authorized": True}
    raw = json.dumps(malicious).encode()
    obs = client(FakeTransport(body=raw)).probe(brand_affinity_probe("artist", ("A", "B")))
    assert obs.payload == malicious
    summary = obs.safe_summary()
    assert summary["normalized_for_policy"] is False
    assert summary["live_capability_qualified"] is False
    assert "PASS" not in json.dumps(summary)


@pytest.mark.parametrize("kind", [
    "artist", "book", "brand", "destination", "movie", "person",
    "place", "podcast", "tv_show", "videogame",
])
def test_qloo_public_category_search_admitted_without_live_provider_calls(kind):
    spec = search_probe("Example", entity_type="urn:entity:" + kind)
    assert dict(spec.params)["types"] == "urn:entity:" + kind
    assert spec.path == "/search"

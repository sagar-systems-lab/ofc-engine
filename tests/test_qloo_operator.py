import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from ofc_engine.qloo import QlooProbeClient, QlooProbeError, TransportReply, brand_affinity_probe
from ofc_engine.qloo_operator import main
from ofc_engine.qloo_probe import private_capture


ARTIST = "4BBEF799-A0C4-4110-AB01-39216993C312"
BRANDS = (
    "C70CE2B8-0AD1-4150-BF18-5A6347F2E860",
    "43A36E73-9BC7-4B09-A116-78E51554C3E3",
    "E26BC58D-18D2-405B-BC7B-7C556ADE3829",
)
SPEC = brand_affinity_probe(ARTIST, BRANDS)
ACK = "EXACTLY_ONE_QLOO_REQUEST"


class FakeTransport:
    def __init__(self, *, fail=False, malformed=False):
        self.calls = 0
        self.fail = fail
        self.malformed = malformed

    def get(self, url, headers, *, timeout, max_bytes):
        self.calls += 1
        assert url.startswith("https://hackathon.api.qloo.com/v2/insights?")
        assert headers["X-Api-Key"] == "test-private-key"
        if self.fail:
            raise QlooProbeError("NETWORK_ERROR")
        if self.malformed:
            payload = {"success": True, "results": {"entities": []}}
        else:
            payload = {
                "success": True, "query": {"explainability": {}},
                "results": {"entities": [
                    {"entity_id": BRANDS[1], "type": "urn:entity",
                     "query": {"affinity": 0.92, "explainability": {"signal.interests.entities": {}}}},
                    {"entity_id": BRANDS[0], "type": "urn:entity",
                     "query": {"affinity": 0.61, "explainability": {"signal.interests.entities": {}}}},
                    {"entity_id": BRANDS[2], "type": "urn:entity",
                     "query": {"affinity": 0.23, "explainability": {"signal.interests.entities": {}}}},
                ]},
            }
        return TransportReply(200, json.dumps(payload).encode())


@pytest.fixture
def isolated_home(monkeypatch, tmp_path):
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    return tmp_path


def args(*extra):
    return [
        "--artist-id", ARTIST, "--brands", ",".join(BRANDS),
        "--selected", BRANDS[0], *extra,
    ]


def set_fake(monkeypatch, transport):
    monkeypatch.setattr(
        QlooProbeClient, "from_environment",
        classmethod(lambda cls: cls(api_key="test-private-key", transport=transport)),
    )


def test_default_is_offline_without_key(isolated_home, monkeypatch, capsys):
    def forbidden(*a, **kw):
        raise AssertionError("no transport without explicit permission")

    monkeypatch.setattr(QlooProbeClient, "from_environment", forbidden)
    assert main(args()) == 0
    result = json.loads(capsys.readouterr().out)
    assert result["status"] == "DRY_RUN_NO_NETWORK"
    assert result["real_qloo_requests"] == 0
    assert not (isolated_home / ".local/share/ofc-engine/private-qloo-attempts").exists()


def test_live_without_exact_ack_is_blocked(isolated_home, monkeypatch, capsys):
    transport = FakeTransport()
    set_fake(monkeypatch, transport)
    assert main(args("--live")) == 2
    assert json.loads(capsys.readouterr().out)["code"] == "EXPLICIT_OPERATOR_APPROVAL_REQUIRED"
    assert transport.calls == 0
    assert main(args("--live", "--authorize", "wrong")) == 2
    assert transport.calls == 0


def test_explicit_one_shot_and_cache_reuse_consumes_no_second_call(isolated_home, monkeypatch, capsys):
    transport = FakeTransport()
    set_fake(monkeypatch, transport)
    assert main(args("--live", "--authorize", ACK)) == 0
    result = json.loads(capsys.readouterr().out)
    assert result["status"] == "ONE_LIVE_RESPONSE_CAPTURED"
    assert result["real_qloo_requests"] == 1
    assert result["request_attempts_this_run"] == 1
    assert result["provider_authenticated"] is False
    assert result["release_authorized"] is False
    assert result["external_action_executed"] is False
    assert result["comparison"]["evidence_on"]["current_revision"] == 1
    assert result["comparison"]["evidence_on"]["selected_candidates"]["brand"] == BRANDS[1]
    assert result["comparison"]["evidence_off"]["current_revision"] == 0
    assert result["comparison"]["real_causal_proof"] is False
    assert "test-private-key" not in json.dumps(result)
    assert '"affinity":' not in json.dumps(result)
    assert transport.calls == 1
    private_path = Path(result["private_capture_file"])
    assert private_path.exists()
    assert private_path.stat().st_mode & 0o777 == 0o600
    assert (isolated_home / ".local/share/ofc-engine/private-qloo-attempts" /
            f"{SPEC.fingerprint}.json").exists()

    assert main(args("--live", "--authorize", ACK)) == 0
    second = json.loads(capsys.readouterr().out)
    assert second["status"] == "RECORDED_REPLAY_QUALIFIED"
    assert second["real_qloo_requests"] == 0
    assert second["mode"] == "LOCAL_PRIVATE_CACHE"
    assert second["provider_authenticated"] is False
    assert transport.calls == 1


def test_failed_attempt_is_reserved_and_will_not_retry(isolated_home, monkeypatch, capsys):
    transport = FakeTransport(fail=True)
    set_fake(monkeypatch, transport)
    assert main(args("--live", "--authorize", ACK)) == 3
    failed = json.loads(capsys.readouterr().out)
    assert failed["status"] == "REQUEST_OUTCOME_UNKNOWN"
    assert failed["retry_allowed"] is False
    assert failed["request_attempts_this_run"] == 1
    assert transport.calls == 1

    assert main(args("--live", "--authorize", ACK)) == 2
    blocked = json.loads(capsys.readouterr().out)
    assert blocked["code"] == "REQUEST_ALREADY_RESERVED"
    assert blocked["real_qloo_requests"] == 0
    assert transport.calls == 1


def test_invalid_live_schema_is_cached_and_not_retried(isolated_home, monkeypatch, capsys):
    transport = FakeTransport(malformed=True)
    set_fake(monkeypatch, transport)
    assert main(args("--live", "--authorize", ACK)) == 5
    output = json.loads(capsys.readouterr().out)
    assert output["status"] == "RESPONSE_NOT_QUALIFIED"
    assert output["real_qloo_requests"] == 1
    assert Path(output["private_capture_file"]).exists()
    assert transport.calls == 1
    assert main(args("--live", "--authorize", ACK)) == 2
    assert json.loads(capsys.readouterr().out)["status"] == "BLOCKED"
    assert transport.calls == 1


def test_stale_capture_never_triggers_refresh(isolated_home, monkeypatch, capsys):
    from ofc_engine.qloo_operator import _replay
    old = (datetime.now(timezone.utc) - timedelta(days=5)).isoformat()
    private_capture({
        "success": True, "results": {"entities": [
            {"entity_id": BRANDS[1], "type": "urn:entity", "query": {"affinity": 0.92}},
            {"entity_id": BRANDS[0], "type": "urn:entity", "query": {"affinity": 0.61}},
            {"entity_id": BRANDS[2], "type": "urn:entity", "query": {"affinity": 0.23}},
        ]},
    }, {
        "endpoint": SPEC.path, "http_status": 200, "real_qloo_requests": 1,
        "mode": "LIVE_RESPONSE_UNQUALIFIED", "utc_collected_at": old,
        "query_fingerprint": SPEC.fingerprint,
    })
    def forbidden(*a, **kw):
        raise AssertionError("stale must not auto-refresh")
    monkeypatch.setattr(QlooProbeClient, "from_environment", forbidden)
    assert main(args("--live", "--authorize", ACK)) == 0
    result = json.loads(capsys.readouterr().out)
    assert result["status"] == "CACHED_EVIDENCE_STALE"
    assert result["real_qloo_requests"] == 0


def test_untrusted_directory_symlink_blocks_before_request(isolated_home, monkeypatch, capsys):
    root = isolated_home / ".local/share/ofc-engine"
    root.mkdir(parents=True)
    external = isolated_home / "external"
    external.mkdir()
    (root / "private-qloo-attempts").symlink_to(external, target_is_directory=True)
    transport = FakeTransport()
    set_fake(monkeypatch, transport)
    assert main(args("--live", "--authorize", ACK)) == 2
    assert json.loads(capsys.readouterr().out)["code"] == "UNSAFE_ATTEMPT_DIRECTORY"
    assert transport.calls == 0


def test_unknown_brand_and_wrong_approval_fail_before_request(isolated_home, capsys):
    invalid = args("--live", "--authorize", ACK)
    invalid[invalid.index("--selected") + 1] = "not-in-pool"
    assert main(invalid) == 2
    assert json.loads(capsys.readouterr().out)["code"] == "SELECTED_OUTSIDE_POOL"
    assert main(args("--authorize", ACK)) == 2
    assert json.loads(capsys.readouterr().out)["code"] == "UNAUTHORIZED_REQUEST"

import json
from pathlib import Path

import pytest

from ofc_engine.qloo import search_probe
from ofc_engine.qloo_probe import find_private_cached_probe, main


def _capture(home: Path, fingerprint: str, *, valid: bool = True) -> Path:
    directory = home / ".local/share/ofc-engine/private-qloo-probes"
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / "sample.json"
    path.write_text(json.dumps({
        "summary": {
            "endpoint": "/search",
            "query_fingerprint": fingerprint,
            "http_status": 200 if valid else 401,
            "real_qloo_requests": 1,
            "mode": "LIVE_RESPONSE_UNQUALIFIED",
        },
        "payload": {"results": []},
    }))
    return path


def test_exact_private_cache_hit(monkeypatch, tmp_path):
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    spec = search_probe("Taylor Swift", entity_type="urn:entity:artist")
    path = _capture(tmp_path, spec.fingerprint)
    result = find_private_cached_probe(spec.fingerprint)
    assert result is not None
    assert result[0] == path
    assert result[1]["query_fingerprint"] == spec.fingerprint


def test_mismatched_and_failed_captures_not_reused(monkeypatch, tmp_path):
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    spec = search_probe("Taylor Swift", entity_type="urn:entity:artist")
    _capture(tmp_path, "other")
    assert find_private_cached_probe(spec.fingerprint) is None
    _capture(tmp_path, spec.fingerprint, valid=False)
    assert find_private_cached_probe(spec.fingerprint) is None


def test_live_command_uses_cache_without_client_or_key(monkeypatch, tmp_path, capsys):
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    spec = search_probe("Taylor Swift", entity_type="urn:entity:artist")
    _capture(tmp_path, spec.fingerprint)
    from ofc_engine import qloo_probe
    monkeypatch.setattr(qloo_probe.QlooProbeClient, "from_environment",
                        lambda: (_ for _ in ()).throw(AssertionError("unwanted live call")))
    assert main(["--live", "--capture", "--search", "Taylor Swift", "--type", "artist"]) == 0
    result = json.loads(capsys.readouterr().out)
    assert result["mode"] == "PRIVATE_CAPTURE_REPLAY"
    assert result["real_qloo_requests"] == 0
    assert result["authenticated_by_adapter"] is False


def test_refresh_requires_explicit_live_capture():
    with pytest.raises(SystemExit, match="--refresh requires --live --capture"):
        main(["--refresh", "--search", "Taylor Swift", "--type", "artist"])


def test_cache_symlink_rejected(monkeypatch, tmp_path):
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    target = tmp_path / "other"
    target.mkdir()
    link = tmp_path / ".local/share/ofc-engine/private-qloo-probes"
    link.parent.mkdir(parents=True)
    link.symlink_to(target, target_is_directory=True)
    assert find_private_cached_probe("a" * 64) is None

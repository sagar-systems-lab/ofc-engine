"""Hosted qualification checks, without network traffic or external credentials."""
from contextlib import contextmanager
from dataclasses import replace
from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path
import sys
from threading import Thread

import pytest

from ofc_engine.hosted_preview import HostedFixtureServer

ROOT = Path(__file__).resolve().parents[1]


def _load_smoke():
    spec = spec_from_file_location('smoke_hosted', ROOT / 'scripts' / 'smoke_hosted.py')
    assert spec is not None and spec.loader is not None
    module = module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


@contextmanager
def preview():
    server = HostedFixtureServer(('127.0.0.1', 0), build_sha='abc123d')
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield 'http://127.0.0.1:' + str(server.server_port)
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def test_full_local_preview_qualification():
    probe = _load_smoke()
    with preview() as url:
        result = probe.qualify(url, expected_sha='abc123d')
    assert result['result'] == 'PASS'
    assert len(result['scenarios']) == 4
    assert not result['release_authorized']
    assert result['real_qloo_requests'] == 0


@pytest.mark.parametrize('bad', [
    'http://example.onrender.com', 'https://example.invalid',
    'https://user:pass@example.onrender.com', 'http://127.0.0.1/other',
    'https://example.onrender.com/?secret=a', 'https://example.onrender.com#fragment',
    'file:///etc/passwd', 'https://render.com', 'https://example.onrender.com.evil.invalid',
])
def test_invalid_remote_origin_fails_closed(bad):
    probe = _load_smoke()
    with pytest.raises(ValueError):
        probe.normalize_url(bad)


def test_expected_build_identity_mismatch_fails():
    probe = _load_smoke()
    with preview() as url:
        with pytest.raises(probe.SmokeFailure, match='build identity'):
            probe.qualify(url, expected_sha='ff120aa')


def test_bounded_wake_wait_and_hash_validation():
    probe = _load_smoke()
    with pytest.raises(ValueError):
        probe.qualify('https://preview.onrender.com', wait_seconds=-1)
    with pytest.raises(ValueError):
        probe.qualify('https://preview.onrender.com', wait_seconds=181)
    with pytest.raises(ValueError):
        probe.qualify('https://preview.onrender.com', expected_sha='unverified')


def test_refuses_false_release_and_stale_baseline(monkeypatch):
    probe = _load_smoke()
    actual = probe.PreviewProbe.get

    def wrong(self, path):
        response = actual(self, path)
        if path == '/api/demo?scenario=repair':
            import json
            payload = json.loads(response.content)
            payload['release_authorized'] = True
            return replace(response, content=json.dumps(payload).encode())
        return response

    monkeypatch.setattr(probe.PreviewProbe, 'get', wrong)
    with preview() as url:
        with pytest.raises(probe.SmokeFailure, match='unsafe'):
            probe.qualify(url)


def test_blueprint_free_plan_and_manual_deploy():
    blueprint = (ROOT / 'render.yaml').read_text()
    assert 'type: web' in blueprint and 'runtime: docker' in blueprint
    assert 'plan: free' in blueprint
    assert 'healthCheckPath: /readyz' in blueprint
    assert 'autoDeployTrigger: "off"' in blueprint
    assert 'databases:' not in blueprint
    assert 'sync: false' not in blueprint
    assert 'envVars:' not in blueprint
    workflow = (ROOT / '.github/workflows/hosted-qualification.yml').read_text()
    assert 'workflow_dispatch:' in workflow
    assert 'python scripts/smoke_hosted.py' in workflow
    assert 'python scripts/browser_smoke.py' in workflow
    assert 'playwright install --with-deps chromium' in workflow

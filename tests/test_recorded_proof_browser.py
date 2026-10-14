"""Read-only proof import surface stays local and rejects unqualified manifest states."""
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from pathlib import Path
from threading import Thread
from urllib.error import HTTPError
from urllib.request import Request, urlopen

import pytest

from ofc_engine.hosted_preview import HostedFixtureServer
from ofc_engine.webdemo import _ASSETS

ROOT = Path(__file__).resolve().parents[1] / "src/ofc_engine/web"


@contextmanager
def running_preview():
    server = HostedFixtureServer(("127.0.0.1", 0))
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def test_recorded_inspector_and_asset_routes_are_public_static_only():
    for route, file in (
        ("/recorded", "recorded.html"),
        ("/recorded.html", "recorded.html"),
        ("/recorded.js", "recorded.js"),
        ("/recorded.css", "recorded.css"),
    ):
        assert _ASSETS[route][0] == file
        assert (ROOT / file).is_file()
    with running_preview() as base:
        for path in ("/recorded", "/recorded.js", "/recorded.css"):
            with urlopen(base + path, timeout=5) as response:
                assert response.status == 200
                assert response.headers["Cache-Control"] == "no-store"
                assert response.headers["Content-Security-Policy"].startswith("default-src 'none'")
                assert response.read()
        for route in ("/api/recorded", "/api/proof", "/api/recorded/commit"):
            with pytest.raises(HTTPError) as exc:
                urlopen(base + route, timeout=5)
            assert exc.value.code == 404
        for route in ("/recorded", "/api/recorded"):
            with pytest.raises(HTTPError) as exc:
                urlopen(Request(base + route, data=b"{}", method="POST"), timeout=5)
            assert exc.value.code == 405


def test_private_file_only_flow_without_network_or_persistent_storage():
    html = (ROOT / "recorded.html").read_text()
    js = (ROOT / "recorded.js").read_text()
    studio = (ROOT / "studio.html").read_text()
    for marker in (
        'id="proof-file"', 'id="proof-result"', 'id="drop-area"',
        'id="rank-list"', 'id="manifest-digest"', 'id="import-status"',
        'type="file"', 'RECORDED EVIDENCE', "LOCAL FILE / NO UPLOAD",
        "No server uploads",
    ):
        if marker == 'RECORDED EVIDENCE':
            assert "RECORDED EVIDENCE" in html
        else:
            assert marker in html
    assert 'href="/recorded"' in studio
    for marker in (
        "crypto.subtle.digest", "SHA-256", "file.text()", "proof-result",
        "manifest checksum mismatch", "recorded-only", "release_authorized !== false",
        "provider_authenticated !== false", "pending_recheck_cleared !== false",
    ):
        assert marker.lower() in js.lower()
    for forbidden in (
        "XMLHttpRequest", "sendBeacon", "WebSocket(",
        "localStorage", "sessionStorage", "innerHTML", "outerHTML",
        "eval(", "new Function(", "setTimeout(", "setInterval(", "indexedDB",
    ):
        assert forbidden not in js
    assert "onerror=" not in html and "onclick=" not in html
    assert "<iframe" not in html and "https://" not in html
    assert "url(http" not in (ROOT / "recorded.css").read_text()
    assert 'src="/recorded.js"' in html
    assert 'fetch("/api/local-proof", {cache: "no-store", credentials: "same-origin"})' in js
    assert 'dataset.ofcLocalProof === "true"' in js

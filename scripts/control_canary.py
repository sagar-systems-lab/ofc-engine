"""Rehearse feature rollback against isolated loopback servers."""

import json
from threading import Thread
from urllib.error import HTTPError
from urllib.request import urlopen

from ofc_engine.hosted_preview import HostedFixtureServer


def run():
    server = HostedFixtureServer(("127.0.0.1", 0), control_enabled=True)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    origin = "http://127.0.0.1:" + str(server.server_port)
    try:
        with urlopen(origin + "/plans", timeout=3) as response:
            assert response.status == 200
        with urlopen(origin + "/api/demo?scenario=repair", timeout=3) as response:
            before = json.load(response)
        server.control_enabled = False
        try:
            urlopen(origin + "/plans", timeout=3)
            raise AssertionError("disabled graph route remained active")
        except HTTPError as exc:
            assert exc.code == 404
        with urlopen(origin + "/api/demo?scenario=repair", timeout=3) as response:
            after = json.load(response)
        assert before == after
        return {
            "result": "PASS",
            "scope": "ISOLATED_LOOPBACK",
            "provider_calls": 0,
            "spend_minor": 0,
            "production_changed": False,
        }
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=3)


if __name__ == "__main__":
    print(json.dumps(run(), sort_keys=True))

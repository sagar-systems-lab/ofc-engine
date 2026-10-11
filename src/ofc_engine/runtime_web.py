"""Loopback-only, persisted synthetic run and event-replay HTTP endpoints."""
from __future__ import annotations

import argparse
from http.server import ThreadingHTTPServer
import json
from urllib.parse import parse_qs, urlsplit

from .run_history import CorruptRun, RunConflict, RunHistory
from .webdemo import DemoHandler, SCENARIOS, demo_snapshot

_MAX_REQUEST = 2048


class RuntimeHandler(DemoHandler):
    def _origin_allowed(self) -> bool:
        host = self.headers.get("Host", "")
        port = self.server.server_port
        if host not in (f"127.0.0.1:{port}", f"localhost:{port}"):
            return False
        origin = self.headers.get("Origin")
        if origin is not None and origin not in (f"http://127.0.0.1:{port}", f"http://localhost:{port}"):
            return False
        if self.headers.get("Sec-Fetch-Site") == "cross-site":
            return False
        return True

    def _json(self, status: int, value: dict) -> None:
        self._send(status, json.dumps(value, separators=(",", ":"), allow_nan=False).encode(), "application/json")

    def do_GET(self) -> None:
        if not self._origin_allowed():
            self._json(403, {"error": "ORIGIN_REJECTED"})
            return
        parts = urlsplit(self.path)
        segments = parts.path.strip("/").split("/")
        if len(segments) >= 3 and segments[:2] == ["api", "runs"]:
            run_id = segments[2]
            try:
                if len(segments) == 3 and not parts.query:
                    saved = self.server.history.get(run_id)
                    self._json(200, {"run_id": saved.run_id, "snapshot": saved.snapshot, "source_mode": "SYNTHETIC"})
                    return
                if len(segments) == 4 and segments[3] == "events":
                    query = parse_qs(parts.query, keep_blank_values=True)
                    if set(query) != {"after_sequence"} or len(query["after_sequence"]) != 1:
                        raise ValueError("missing cursor")
                    raw = query["after_sequence"][0]
                    if not raw or raw.strip() != raw or not (raw == "-1" or raw.isascii() and raw.isdecimal()):
                        raise ValueError("malformed cursor")
                    self._json(200, self.server.history.events(run_id, after_sequence=int(raw)))
                    return
            except KeyError:
                self._json(404, {"error": "RUN_NOT_FOUND"})
                return
            except ValueError:
                self._json(400, {"error": "INVALID_CURSOR"})
                return
            except CorruptRun:
                self._json(503, {"error": "RUN_INTEGRITY_FAILURE"})
                return
        super().do_GET()

    def do_POST(self) -> None:
        if not self._origin_allowed():
            self._json(403, {"error": "ORIGIN_REJECTED"})
            return
        if self.path != "/api/runs":
            self._json(405, {"error": "READ_ONLY"})
            return
        content_type = self.headers.get("Content-Type", "").lower()
        if content_type != "application/json":
            self._json(415, {"error": "EXPECTED_JSON"})
            return
        raw_length = self.headers.get("Content-Length", "")
        if not raw_length.isascii() or not raw_length.isdecimal():
            self._json(411, {"error": "CONTENT_LENGTH_REQUIRED"})
            return
        length = int(raw_length)
        if length > _MAX_REQUEST:
            self._json(413, {"error": "REQUEST_TOO_LARGE"})
            return
        try:
            payload = json.loads(self.rfile.read(length))
            if not isinstance(payload, dict) or set(payload) != {"scenario", "request_id"}:
                raise ValueError("invalid payload")
            scenario = payload["scenario"]
            if not isinstance(scenario, str) or scenario not in SCENARIOS:
                raise ValueError("invalid scenario")
            snapshot = demo_snapshot(scenario)
            saved = self.server.history.create(scenario=scenario, request_id=payload["request_id"], snapshot=snapshot)
        except RunConflict:
            self._json(409, {"error": "IDEMPOTENCY_CONFLICT"})
            return
        except (ValueError, TypeError, UnicodeDecodeError):
            self._json(400, {"error": "INVALID_REQUEST"})
            return
        except CorruptRun:
            self._json(503, {"error": "RUN_INTEGRITY_FAILURE"})
            return
        self._json(201 if saved.created else 200, {
            "run_id": saved.run_id, "snapshot": saved.snapshot, "source_mode": "SYNTHETIC",
        })


class RuntimeServer(ThreadingHTTPServer):
    def __init__(self, address: tuple[str, int], history: RunHistory):
        if address[0] != "127.0.0.1":
            raise ValueError("runtime server is loopback-only")
        self.history = history
        super().__init__(address, RuntimeHandler)


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="Local synthetic proof console with saved run history")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--database", required=True, help="SQLite file on a persistent local volume")
    args = parser.parse_args(argv)
    history = RunHistory(args.database)
    with RuntimeServer(("127.0.0.1", args.port), history) as server:
        print(f"Synthetic saved-run demo: http://127.0.0.1:{server.server_port}", flush=True)
        server.serve_forever()


if __name__ == "__main__":
    main()

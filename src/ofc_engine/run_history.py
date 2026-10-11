"""Immutable synthetic run snapshots with transactional request deduplication."""
from __future__ import annotations

from contextlib import closing
from dataclasses import dataclass
from hashlib import sha256
import json
from pathlib import Path
import re
import sqlite3
from typing import Any
from uuid import uuid4

_REQUEST_KEY = re.compile(r"^[a-zA-Z0-9_-]{8,128}$")
_RUN_ID = re.compile(r"^[0-9a-f]{32}$")
_SCENARIOS = frozenset({"repair", "outage", "dependent_missing", "budget_exhausted"})


class RunConflict(ValueError):
    pass


class CorruptRun(RuntimeError):
    pass


@dataclass(frozen=True)
class SavedRun:
    run_id: str
    snapshot: dict[str, Any]
    created: bool


def _canonical(snapshot: dict[str, Any]) -> bytes:
    return json.dumps(snapshot, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")


def _validate(snapshot: dict[str, Any], scenario: str) -> None:
    if not isinstance(snapshot, dict):
        raise ValueError("invalid run snapshot")
    if (
        snapshot.get("scenario") != scenario
        or snapshot.get("demo_mode") != "SYNTHETIC"
        or snapshot.get("source_mode") != "SYNTHETIC"
        or snapshot.get("release_authorized") is not False
        or snapshot.get("external_action_executed") is not False
        or snapshot.get("provider_authenticated") is not False
    ):
        raise ValueError("non-synthetic or authorized run rejected")
    events = snapshot.get("timeline")
    revision = snapshot.get("current_revision")
    if not isinstance(revision, int) or isinstance(revision, bool) or revision < 0:
        raise ValueError("invalid revision")
    if not isinstance(events, list) or not 1 <= len(events) <= 128:
        raise ValueError("invalid event timeline")
    if snapshot.get("last_event_sequence") != len(events) - 1:
        raise ValueError("event sequence mismatch")
    last_revision = 0
    for sequence, event in enumerate(events):
        if not isinstance(event, dict) or event.get("sequence") != sequence:
            raise ValueError("event sequence gap")
        if not isinstance(event.get("state"), str) or not event["state"]:
            raise ValueError("invalid event state")
        r = event.get("revision")
        if not isinstance(r, int) or isinstance(r, bool) or r < last_revision or r > revision:
            raise ValueError("event revision regression")
        last_revision = r
    if last_revision != revision:
        raise ValueError("terminal revision mismatch")
    if len(_canonical(snapshot)) > 512_000:
        raise ValueError("snapshot too large")


class RunHistory:
    """Stores completed, unauthoritative fixture runs on a durable local volume."""

    def __init__(self, filename: str | Path) -> None:
        path = Path(filename).expanduser()
        if str(filename) in ("", ":memory:") or str(filename).startswith("file:"):
            raise ValueError("filesystem database path required")
        if not path.parent.is_dir():
            raise ValueError("database directory does not exist")
        self._path = path
        with closing(self._connect()) as db, db:
            db.execute("PRAGMA journal_mode=WAL")
            db.execute("""CREATE TABLE IF NOT EXISTS runs (
                run_id TEXT PRIMARY KEY,
                request_id TEXT NOT NULL UNIQUE,
                scenario TEXT NOT NULL,
                snapshot_json TEXT NOT NULL,
                snapshot_sha256 TEXT NOT NULL
            )""")

    def _connect(self) -> sqlite3.Connection:
        db = sqlite3.connect(self._path, timeout=10)
        try:
            db.execute("PRAGMA busy_timeout=10000")
            db.execute("PRAGMA synchronous=FULL")
            return db
        except BaseException:
            db.close()
            raise

    @staticmethod
    def _decode(scenario: str, encoded: str, digest: str) -> dict[str, Any]:
        if sha256(encoded.encode("utf-8")).hexdigest() != digest:
            raise CorruptRun("saved run digest mismatch")
        try:
            snapshot = json.loads(encoded)
            _validate(snapshot, scenario)
        except (ValueError, TypeError) as exc:
            raise CorruptRun("invalid saved run") from exc
        return snapshot

    def create(self, *, scenario: str, request_id: str, snapshot: dict[str, Any]) -> SavedRun:
        if scenario not in _SCENARIOS or not isinstance(request_id, str) or not _REQUEST_KEY.fullmatch(request_id):
            raise ValueError("invalid scenario or request ID")
        _validate(snapshot, scenario)
        encoded = _canonical(snapshot).decode("utf-8")
        digest = sha256(encoded.encode("utf-8")).hexdigest()
        with closing(self._connect()) as db, db:
            db.execute("BEGIN IMMEDIATE")
            prior = db.execute(
                "SELECT run_id,scenario,snapshot_json,snapshot_sha256 FROM runs WHERE request_id=?",
                (request_id,),
            ).fetchone()
            if prior is not None:
                run_id, old_scenario, old_json, old_digest = prior
                if old_scenario != scenario:
                    raise RunConflict("request ID already used with different scenario")
                prior_snapshot = self._decode(old_scenario, old_json, old_digest)
                return SavedRun(run_id, prior_snapshot, False)
            run_id = uuid4().hex
            db.execute(
                "INSERT INTO runs(run_id,request_id,scenario,snapshot_json,snapshot_sha256) VALUES(?,?,?,?,?)",
                (run_id, request_id, scenario, encoded, digest),
            )
            return SavedRun(run_id, snapshot, True)

    def get(self, run_id: str) -> SavedRun:
        if not isinstance(run_id, str) or not _RUN_ID.fullmatch(run_id):
            raise KeyError("unknown run")
        with closing(self._connect()) as db, db:
            row = db.execute(
                "SELECT scenario,snapshot_json,snapshot_sha256 FROM runs WHERE run_id=?", (run_id,)
            ).fetchone()
        if row is None:
            raise KeyError("unknown run")
        return SavedRun(run_id, self._decode(*row), False)

    def events(self, run_id: str, *, after_sequence: int) -> dict[str, Any]:
        if type(after_sequence) is not int or not -1 <= after_sequence <= 1_000_000:
            raise ValueError("invalid event cursor")
        run = self.get(run_id)
        timeline = run.snapshot["timeline"]
        if after_sequence > len(timeline) - 1:
            raise ValueError("event cursor ahead of run")
        return {
            "run_id": run_id,
            "after_sequence": after_sequence,
            "events": timeline[after_sequence + 1 :],
            "last_event_sequence": len(timeline) - 1,
            "source_mode": "SYNTHETIC",
            "release_authorized": False,
        }

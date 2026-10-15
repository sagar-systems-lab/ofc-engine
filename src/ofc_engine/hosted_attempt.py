"""Atomic Qloo request reservation on one operator-managed persistent volume."""
from __future__ import annotations

from contextlib import contextmanager
import json
import os
from pathlib import Path
import sqlite3
from stat import S_ISDIR, S_ISREG


class HostedAttemptLedger:
    def __init__(self, path: Path, *, scope: str) -> None:
        if not scope or len(scope) != 64 or any(c not in "0123456789abcdef" for c in scope):
            raise ValueError("invalid reservation scope")
        if path.is_symlink() or path.parent.is_symlink():
            raise ValueError("unsafe ledger path")
        parent = path.parent.lstat()
        if (
            not S_ISDIR(parent.st_mode) or parent.st_uid != os.getuid()
            or parent.st_mode & 0o077
        ):
            raise ValueError("ledger directory must be private and owned")
        if not path.exists():
            flags = os.O_CREAT | os.O_EXCL | os.O_WRONLY
            if hasattr(os, "O_NOFOLLOW"):
                flags |= os.O_NOFOLLOW
            try:
                os.close(os.open(path, flags, 0o600))
            except FileExistsError:
                pass
        file_stat = path.lstat()
        if (
            not S_ISREG(file_stat.st_mode) or file_stat.st_uid != os.getuid()
            or file_stat.st_mode & 0o077 or file_stat.st_nlink != 1
        ):
            raise ValueError("unsafe ledger file")
        self.path = path
        self.scope = scope
        with self._connect() as db:
            db.execute("""CREATE TABLE IF NOT EXISTS provider_attempt (
                id INTEGER PRIMARY KEY CHECK(id=1),
                scope TEXT NOT NULL,
                state TEXT NOT NULL CHECK(state IN ('RESERVED','COMPLETE')),
                result TEXT
            )""")
            row = db.execute("SELECT scope FROM provider_attempt WHERE id=1").fetchone()
            if row is not None and row[0] != scope:
                raise ValueError("ledger belongs to another frozen Qloo scope")

    @contextmanager
    def _connect(self):
        db = sqlite3.connect(str(self.path), timeout=5, isolation_level=None)
        try:
            yield db
        finally:
            db.close()

    def _row(self, db: sqlite3.Connection):
        row = db.execute(
            "SELECT scope, state, result FROM provider_attempt WHERE id=1"
        ).fetchone()
        if row is not None and row[0] != self.scope:
            raise ValueError("ledger scope mismatch")
        return row

    def get(self) -> tuple[str, dict | None]:
        with self._connect() as db:
            row = self._row(db)
        if row is None:
            return "READY", None
        if row[1] == "RESERVED":
            return "SPENT", None
        try:
            result = json.loads(row[2])
        except (ValueError, TypeError):
            raise ValueError("invalid stored Qloo result") from None
        if not isinstance(result, dict) or result.get("status") not in (
            "LIVE_RANK_OBSERVED", "BLOCKED"
        ) or result.get("release_authorized") is not False:
            raise ValueError("invalid stored Qloo result")
        return result["status"], result

    def reserve(self) -> bool:
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            try:
                if self._row(db) is not None:
                    db.execute("COMMIT")
                    return False
                db.execute(
                    "INSERT INTO provider_attempt(id,scope,state,result) VALUES(1,?,'RESERVED',NULL)",
                    (self.scope,),
                )
                db.execute("COMMIT")
            except BaseException:
                db.execute("ROLLBACK")
                raise
        return True

    def complete(self, result: dict) -> None:
        if result.get("status") not in ("LIVE_RANK_OBSERVED", "BLOCKED"):
            raise ValueError("invalid terminal result")
        payload = json.dumps(result, sort_keys=True, separators=(",", ":"), allow_nan=False)
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            try:
                row = self._row(db)
                if row is None or row[1] != "RESERVED":
                    raise ValueError("reservation not pending")
                db.execute(
                    "UPDATE provider_attempt SET state='COMPLETE',result=? WHERE id=1",
                    (payload,),
                )
                db.execute("COMMIT")
            except BaseException:
                db.execute("ROLLBACK")
                raise

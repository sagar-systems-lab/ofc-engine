"""Exercise migrations, encrypted backup/restore and key rotation on temporary local data."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sqlite3
from tempfile import TemporaryDirectory

from cryptography.fernet import Fernet
from ofc_engine.control.fixtures import cascading_campaign
from ofc_engine.control.runtime import ControlRuntime
from ofc_engine.control.storage import ControlStore
from ofc_engine.control.verifier import verify


def rehearse():
    with TemporaryDirectory(prefix="ofc-rehearsal-") as directory:
        path = Path(directory) / "original.db"
        backup = Path(directory) / "restore.db"
        key = Fernet.generate_key()
        store = ControlStore(path, encryption_key=key)
        request = cascading_campaign()
        rid = store.admit(request)["review_id"]
        ControlRuntime(store).run_once()
        original = store.proof(rid, "sandbox", "campaign-agent")
        # SQLite's backup API produces a consistent snapshot, including the audit anchors.
        with sqlite3.connect(path) as source, sqlite3.connect(backup) as target:
            source.backup(target)
        restored = ControlStore(backup, encryption_key=key)
        result = restored.proof(rid, "sandbox", "campaign-agent")
        assert result == original and verify(result)["valid"]
        rotated = Fernet.generate_key()
        restored.rotate_encryption(rotated)
        assert (
            ControlStore(backup, encryption_key=rotated).proof(
                rid, "sandbox", "campaign-agent"
            )
            == original
        )
        restored.migrate()
        restored.migrate()
        return {
            "schema": "ofc.restore-rehearsal/1",
            "result": "PASS",
            "revisions": result["revision"],
            "proof_digest": result["proof_digest"],
            "provider_calls": 0,
            "spend_minor": 0,
            "scope": "ISOLATED_SQLITE",
            "postgres_restore": "NOT_RUN",
            "production_changed": False,
        }


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    result = rehearse()
    raw = json.dumps(result, sort_keys=True, indent=2) + "\n"
    if args.output:
        args.output.write_text(raw)
    else:
        print(raw, end="")


if __name__ == "__main__":
    main()

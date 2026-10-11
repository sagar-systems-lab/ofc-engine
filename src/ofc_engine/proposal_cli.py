"""Local CLI for idempotent, evidence-pending agent proposal intake."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import sqlite3
import sys

from pydantic import ValidationError

from .durable import SQLiteRevisionStore, StorageIntegrityError
from .intake import AgentProposal, ProposalConflict, accept_proposal

_MAX_INPUT = 96 * 1024


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Freeze an agent proposal without fetching evidence or releasing actions")
    parser.add_argument("--database", required=True, help="Existing directory with a persistent local SQLite database")
    parser.add_argument("--input", required=True, help="UTF-8 JSON proposal file, or - for stdin")
    args = parser.parse_args(argv)

    try:
        if args.input == "-":
            raw = sys.stdin.buffer.read(_MAX_INPUT + 1)
        else:
            with Path(args.input).open("rb") as stream:
                raw = stream.read(_MAX_INPUT + 1)
        if len(raw) > _MAX_INPUT:
            raise ValueError("input too large")
        proposal = AgentProposal.model_validate_json(raw)
        receipt = accept_proposal(SQLiteRevisionStore(args.database), proposal)
    except (ValidationError, ValueError, FileNotFoundError, ProposalConflict, StorageIntegrityError, sqlite3.Error):
        print(json.dumps({"status": "REJECTED", "release_authorized": False}), file=sys.stderr)
        return 2

    print(json.dumps(receipt.as_dict(), sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

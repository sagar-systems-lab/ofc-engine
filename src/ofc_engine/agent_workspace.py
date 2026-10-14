"""Launch the frozen OFC studio with saved local Qloo evidence."""
from __future__ import annotations

import argparse
from dataclasses import dataclass
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import secrets
from stat import S_ISDIR, S_ISREG

from .agent_evidence_session import OperatorEvidenceSession
from .connected_local import ConnectedLocalServer
from .durable import SQLiteRevisionStore
from .local_proof_server import load_manifest
from .qloo_operator_workspace import _save, build_workspace


@dataclass
class ConnectedWorkspace:
    server: ConnectedLocalServer
    token_file: Path | None
    proof_file: Path
    run_id: str
    rank_status: str
    historical: bool

    def close(self) -> None:
        self.server.server_close()
        if self.token_file is not None:
            self.token_file.unlink(missing_ok=True)


def _create_session_token() -> tuple[str, Path]:
    folder = Path.home() / ".local/share/ofc-engine/private-agent-sessions"
    if folder.is_symlink():
        raise ValueError("unsafe token directory")
    folder.mkdir(parents=True, exist_ok=True, mode=0o700)
    info = folder.lstat()
    if (
        not S_ISDIR(info.st_mode) or info.st_uid != os.getuid()
        or info.st_mode & 0o077
    ):
        raise ValueError("unsafe token directory")
    token = secrets.token_urlsafe(36)
    path = folder / (secrets.token_hex(16) + ".token")
    flags = os.O_CREAT | os.O_EXCL | os.O_WRONLY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    fd = os.open(path, flags, 0o600)
    try:
        with os.fdopen(fd, "w", encoding="ascii") as out:
            out.write(token + "\n")
    except BaseException:
        path.unlink(missing_ok=True)
        raise
    return token, path


def prepare_workspace(
    *,
    database: Path,
    proposal: Path,
    original_capture: Path,
    later_capture: Path,
    artist_id: str,
    brand_ids: tuple[str, ...],
    decision_id: str = "brand",
    age_hours: int = 24,
    historical: bool = False,
    port: int = 0,
    auth_token: str | None = None,
) -> ConnectedWorkspace:
    if not 0 <= port <= 65535:
        raise ValueError("invalid port")
    db_info = database.lstat()
    if not S_ISREG(db_info.st_mode) or not db_info.st_size:
        raise ValueError("unsafe database")
    session = OperatorEvidenceSession.from_capture_files(
        proposal_path=proposal,
        original_capture=original_capture,
        later_capture=later_capture,
        decision_id=decision_id, artist_id=artist_id, brand_ids=brand_ids,
        age_hours=age_hours, historical=historical,
    )
    store = SQLiteRevisionStore(database)
    review = session.review(store, session.proposal.intent.run_id)
    report = build_workspace(
        store, session.proposal, session.original[decision_id].capture,
        artist_id=artist_id, brand_ids=brand_ids, decision_id=decision_id,
        now=datetime.now(timezone.utc), historical=historical,
        recheck_capture=session.later[decision_id].capture,
    )
    if (
        review["revision"] != report["plan"]["revision"]
        or review["plan_digest"] != report["plan"]["stored_digest"]
        or not review["pending_recheck_ids"]
        or report["recheck"]["pending_recheck_cleared"]
        or review["release_authorized"]
        or report["qualifications"]["release_authorized"]
    ):
        raise ValueError("agent and proof revision mismatch")
    manifest_path, _ = _save(report)
    manifest = load_manifest(manifest_path)

    token_file = None
    token = auth_token
    if token is None:
        token, token_file = _create_session_token()
    try:
        server = ConnectedLocalServer(
            ("127.0.0.1", port), database=str(database),
            token=token, session=session, manifest=manifest,
        )
    except BaseException:
        if token_file is not None:
            token_file.unlink(missing_ok=True)
        raise
    return ConnectedWorkspace(
        server, token_file, manifest_path, session.proposal.intent.run_id,
        review["status"], historical,
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Frozen OFC product UI and private agent evidence on one local origin"
    )
    parser.add_argument("--database", required=True, type=Path)
    parser.add_argument("--proposal", required=True, type=Path)
    parser.add_argument("--capture", required=True, type=Path)
    parser.add_argument("--recheck-capture", required=True, type=Path)
    parser.add_argument("--artist-id", required=True)
    parser.add_argument("--brands", required=True)
    parser.add_argument("--decision-id", default="brand")
    parser.add_argument("--age-hours", type=int, default=24)
    parser.add_argument("--historical", action="store_true")
    parser.add_argument("--port", type=int, default=18080)
    args = parser.parse_args(argv)
    try:
        workspace = prepare_workspace(
            database=args.database, proposal=args.proposal,
            original_capture=args.capture, later_capture=args.recheck_capture,
            artist_id=args.artist_id,
            brand_ids=tuple(args.brands.split(",")),
            decision_id=args.decision_id, age_hours=args.age_hours,
            historical=args.historical, port=args.port,
            auth_token=os.environ.get("OFC_AGENT_TOKEN") or None,
        )
    except (OSError, ValueError, TypeError, KeyError):
        print(json.dumps({
            "status": "BLOCKED", "release_authorized": False,
            "real_qloo_requests": 0,
        }, sort_keys=True))
        return 2

    try:
        print(json.dumps({
            "status": "LOCAL_PRODUCT_READY",
            "run_id": workspace.run_id,
            "recorded_rank_status": workspace.rank_status,
            "historical_replay": workspace.historical,
            "product_url": f"http://127.0.0.1:{workspace.server.server_port}/",
            "proof_route": "/recorded",
            "agent_review_route": f"/api/proposals/{workspace.run_id}/review",
            "token_file": str(workspace.token_file) if workspace.token_file else None,
            "source_mode": "RECORDED_REDACTED",
            "real_qloo_requests": 0,
            "release_authorized": False,
            "external_action_executed": False,
        }, sort_keys=True), flush=True)
        workspace.server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        workspace.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

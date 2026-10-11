"""Read-only release preflight over tracked files and full Git history."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import re
import subprocess

_FORBIDDEN_PATHS = (
    re.compile(r"(^|/)\.env(?:$|\.(?!example$))", re.I),
    re.compile(r"(^|/)(?:AGENTS(?:\(\d+\))?\.md|OFC_ENGINE_.*\.(?:md|docx|pdf|zip)|NOW_REPOSITORY_PUBLIC_SURFACE_DISCIPLINE.*\.md)$", re.I),
    re.compile(r"\.(?:sqlite3(?:-wal|-shm)?|db(?:-wal|-shm)?|pem|key|secret|zip|p12|pfx)$", re.I),
    re.compile(r"(^|/)(?:private-qloo-probes|artifacts/private|secrets)/", re.I),
)
_SECRET_MARKERS = (
    ("private_key_block", re.compile(rb"-----BEGIN (?:RSA |EC |OPENSSH |DSA )?PRIVATE KEY-----")),
    ("aws_access_key", re.compile(rb"\b(?:AKIA|ASIA)[A-Z0-9]{16}\b")),
    ("github_access_token", re.compile(rb"\b(?:ghp_|gho_|ghu_|ghs_|ghr_)[A-Za-z0-9]{36}\b|\bgithub_pat_[A-Za-z0-9_]{60,}\b")),
    ("slack_token", re.compile(rb"\bxox[baprs]-[A-Za-z0-9-]{20,}\b")),
)
_MAX_PATCH_BYTES = 24 * 1024 * 1024


class HistoryAuditError(RuntimeError):
    pass


def _git(root: Path, *args: str) -> bytes:
    try:
        return subprocess.run(
            ["git", "-C", str(root), *args], check=True, capture_output=True,
            timeout=90,
        ).stdout
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired, OSError) as exc:
        raise HistoryAuditError("could not read repository history") from exc


def audit(root: Path) -> dict:
    root = Path(root).resolve()
    tracked = _git(root, "log", "--all", "--format=", "--name-only", "--no-renames").decode("utf-8", "replace")
    paths = {line.strip() for line in tracked.splitlines() if line.strip()}
    path_flags = sorted(p for p in paths if any(pattern.search(p) for pattern in _FORBIDDEN_PATHS))

    patch = _git(root, "log", "--all", "--format=", "-p", "--no-ext-diff", "--no-renames")
    if len(patch) > _MAX_PATCH_BYTES:
        raise HistoryAuditError("history exceeds bounded preflight scan")
    marker_counts = {
        name: len(pattern.findall(patch)) for name, pattern in _SECRET_MARKERS
    }

    findings = {
        "sensitive_paths": len(path_flags),
        "high_confidence_secret_markers": sum(marker_counts.values()),
        "secret_categories": sorted(name for name, count in marker_counts.items() if count),
    }
    return {
        "result": "BLOCKED" if any((findings["sensitive_paths"], findings["high_confidence_secret_markers"])) else "PASS",
        "scope": "TRACKED_GIT_HISTORY_PATTERN_SCAN",
        "findings": findings,
        "manual_secret_review_required": True,
        "public_release_authorized": False,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Check Git history for tracked sensitive paths and secret signatures")
    parser.add_argument("--repo", default=".")
    args = parser.parse_args(argv)
    try:
        result = audit(Path(args.repo))
    except HistoryAuditError:
        print(json.dumps({"result": "ERROR", "reason": "history audit unavailable", "public_release_authorized": False}))
        return 2
    print(json.dumps(result, sort_keys=True))
    return 0 if result["result"] == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())

"""Operator-controlled hosted Copilot qualification. No calls to providers by default."""
from __future__ import annotations

import argparse
from http.cookiejar import CookieJar
import json
import secrets
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import (
    HTTPCookieProcessor, HTTPRedirectHandler, Request, build_opener,
)

_APPROVAL = "APPROVE_ONE_BOUNDED_COPILOT_E2E"


class QualificationFailure(RuntimeError):
    pass


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def _client():
    return build_opener(NoRedirect(), HTTPCookieProcessor(CookieJar()))


def _request(client, base: str, path: str, data: dict | None = None):
    headers = {}
    if data is not None:
        headers = {
            "Content-Type": "application/json",
            "Origin": base,
            "Sec-Fetch-Site": "same-origin",
        }
    request = Request(
        base + path,
        data=json.dumps(data).encode() if data is not None else None,
        headers=headers,
        method="POST" if data is not None else "GET",
    )
    try:
        with client.open(request, timeout=120) as response:
            status = response.status
            raw = response.read(262145)
    except HTTPError as exc:
        status = exc.code
        raw = exc.read(262145)
    except (OSError, URLError, TimeoutError):
        raise QualificationFailure("HOSTED_REQUEST_UNAVAILABLE") from None
    if len(raw) > 262144:
        raise QualificationFailure("HOSTED_RESPONSE_TOO_LARGE")
    try:
        payload = json.loads(raw)
    except (UnicodeError, ValueError):
        raise QualificationFailure("HOSTED_RESPONSE_NOT_JSON") from None
    if not isinstance(payload, dict):
        raise QualificationFailure("HOSTED_RESPONSE_NOT_OBJECT")
    return status, payload


def validate_saved_result(result: dict) -> None:
    if result.get("schema_version") != "copilot.v1":
        raise QualificationFailure("COPILOT_SCHEMA_MISMATCH")
    if result.get("status") != "REVIEW_REQUIRED":
        raise QualificationFailure("COPILOT_SCOPE_NOT_COMPLETED")
    off, on = result.get("off"), result.get("on")
    if not isinstance(off, dict) or not isinstance(on, dict):
        raise QualificationFailure("MISSING_COMPARISON_ARMS")
    if (
        off.get("llm_enabled") is not True
        or on.get("llm_enabled") is not True
        or off.get("ofc_enabled") is not False
        or on.get("ofc_enabled") is not True
        or off.get("qloo") != on.get("qloo")
        or off.get("plan_digest") != result.get("baseline_digest")
        or on.get("plan_digest") != result.get("reviewed_digest")
        or on.get("release_authorized") is not False
        or result.get("release_authorized") is not False
        or result.get("external_action_executed") is not False
        or result.get("full_plan_independently_verified") is not False
    ):
        raise QualificationFailure("OFC_ONLY_COMPARISON_INVARIANT_FAILED")
    if (
        not isinstance(result.get("original_answer"), str)
        or not result["original_answer"].strip()
        or not isinstance(result.get("ofc_answer"), str)
        or not result["ofc_answer"].strip()
        or result.get("qloo_requests_this_run", 0) < 1
        or result.get("llm_requests_this_run") != 2
        or not isinstance(result.get("plan"), dict)
        or not isinstance(result.get("journal_head"), str)
    ):
        raise QualificationFailure("PROVIDER_EVIDENCE_OR_ANSWER_MISSING")
    evidence = off["qloo"]
    if evidence.get("source_mode") != "LIVE_HTTP_UNVERIFIED":
        raise QualificationFailure("GENUINE_QLOO_EVIDENCE_NOT_PRESENT")
    if not evidence.get("observations") or not evidence.get("evidence_digest"):
        raise QualificationFailure("QLOO_SCOPE_PROVENANCE_MISSING")


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Read-only by default; explicit operator gate for live provider spend."
    )
    parser.add_argument("--base-url", required=True)
    parser.add_argument("--expected-sha", required=True)
    parser.add_argument("--execute", action="store_true")
    parser.add_argument("--approve-spend", default="")
    parser.add_argument("--prompt", default="")
    args = parser.parse_args()

    base = args.base_url.rstrip("/")
    parts = urlsplit(base)
    if (
        parts.username or parts.password or parts.query or parts.fragment
        or parts.path not in ("", "/")
        or not parts.hostname
        or (parts.scheme != "https" and not (
            parts.scheme == "http" and parts.hostname in ("localhost", "127.0.0.1")
        ))
        or any(c not in "0123456789abcdef" for c in args.expected_sha)
        or not 7 <= len(args.expected_sha) <= 40
    ):
        parser.error("invalid target URL or expected source SHA")
    if args.execute and (
        args.approve_spend != _APPROVAL
        or not 1 <= len(args.prompt.strip()) <= 4000
    ):
        parser.error("live provider test requires exact approval and explicit bounded prompt")
    if not args.execute and (args.approve_spend or args.prompt):
        parser.error("read-only preflight cannot take provider approval or prompt")

    client = _client()
    status, ready = _request(client, base, "/readyz")
    if status != 200 or ready.get("build_sha") != args.expected_sha:
        raise QualificationFailure("DEPLOYMENT_SHA_OR_READINESS_MISMATCH")
    status, caps = _request(client, base, "/api/capabilities")
    if status != 200:
        raise QualificationFailure("COPILOT_CAPABILITIES_UNAVAILABLE")
    print("READ_ONLY_PREFLIGHT_PASS; backend:", ready["build_sha"])
    print("CAPABILITIES:", json.dumps({
        k: caps.get(k) for k in ("product", "ofc", "qloo", "llm", "storage")
    }, sort_keys=True))
    if not args.execute:
        print("NO_PROVIDER_REQUESTS_OR_PUBLIC_SESSION_CREATED")
        return

    if caps.get("product") != "ACTIVE":
        raise QualificationFailure("PROVIDER_CONNECTIONS_NOT_ACTIVELY_TESTED")
    status, session = _request(
        client, base, "/api/copilot/sessions",
        {"confirm": "CREATE_COPILOT_SESSION"},
    )
    if status != 201 or not isinstance(session.get("session_id"), str):
        raise QualificationFailure("SESSION_ADMISSION_FAILED")
    session_id = session["session_id"]
    request_id = "qualification-" + secrets.token_hex(8)
    msg_path = f"/api/copilot/sessions/{session_id}/messages"
    payload = {"request_id": request_id, "prompt": args.prompt.strip()}
    status, result = _request(client, base, msg_path, payload)
    if status != 200:
        raise QualificationFailure("LIVE_COPILOT_PROMPT_NOT_COMPLETED")
    validate_saved_result(result)

    before = {
        k: result.get(k) for k in (
            "qloo_requests_this_run", "llm_requests_this_run", "journal_head"
        )
    }
    replay_code, replay = _request(client, base, msg_path, payload)
    if replay_code != 200 or replay != result:
        raise QualificationFailure("IDEMPOTENT_REPLAY_MISMATCH")
    code, history = _request(client, base, f"/api/copilot/sessions/{session_id}")
    if code != 200 or len(history.get("messages", [])) != 1:
        raise QualificationFailure("SAVED_SESSION_HISTORY_UNAVAILABLE")
    if history["messages"][0].get("prompt") != args.prompt.strip():
        raise QualificationFailure("SAVED_PROMPT_NOT_RESTORED")
    after = {
        k: replay.get(k) for k in (
            "qloo_requests_this_run", "llm_requests_this_run", "journal_head"
        )
    }
    if before != after:
        raise QualificationFailure("READONLY_TOGGLE_OR_REPLAY_CONSUMED_PROVIDER")

    # A fresh anonymous visitor must not read another session's saved evidence.
    intruder = _client()
    other_code, _ = _request(
        intruder, base, f"/api/copilot/sessions/{session_id}"
    )
    if other_code != 403:
        raise QualificationFailure("CROSS_SESSION_ACCESS_NOT_BLOCKED")
    print("LIVE_E2E_PASS; SAME_QLOO_EVIDENCE; OFC_ONLY_DELTA")
    print("QLOO_REQUESTS:", result["qloo_requests_this_run"])
    print("LLM_REQUESTS:", result["llm_requests_this_run"])
    print("SAVED_REPLAY_AND_CROSS_SESSION_PROTECTION_PASS")


if __name__ == "__main__":
    try:
        main()
    except QualificationFailure as exc:
        raise SystemExit(str(exc)) from None

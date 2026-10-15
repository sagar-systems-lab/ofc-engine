"""One explicitly approved, server-side Qloo rank observation per process."""
from __future__ import annotations

from hashlib import sha256
import hmac
from pathlib import Path
from threading import Lock
from typing import Mapping

from .qloo import ProbeSpec, QlooProbeClient, QlooProbeError, brand_affinity_probe
from .qloo_affinity import AffinityCaptureError, normalize_affinity_capture
from .hosted_attempt import HostedAttemptLedger


class HostedLiveReview:
    def __init__(
        self, *, api_key: str, artist_id: str, brand_ids: tuple[str, ...],
        selected_brand_id: str, client: QlooProbeClient | None = None,
        ledger: HostedAttemptLedger | None = None,
        operator_token: str | None = None,
    ) -> None:
        self.spec: ProbeSpec = brand_affinity_probe(artist_id, brand_ids)
        if selected_brand_id not in brand_ids:
            raise ValueError("baseline selection is not in the fixed Qloo pool")
        self._candidate_ids = brand_ids
        self._selected = selected_brand_id
        self._client = client or QlooProbeClient(api_key=api_key)
        if operator_token is not None and (
            not 32 <= len(operator_token) <= 256
            or not operator_token.isascii()
            or any(ord(char) < 33 or ord(char) > 126 for char in operator_token)
        ):
            raise ValueError("unsafe operator token")
        self._operator_token = operator_token
        self._ledger = ledger
        self._lock = Lock()
        self._used = False
        self._finished: dict | None = None

    def authorize(self, token: str) -> bool:
        return (
            self._operator_token is not None
            and isinstance(token, str)
            and hmac.compare_digest(token, self._operator_token)
        )

    def state(self) -> dict:
        with self._lock:
            state = self._ledger.get()[0] if self._ledger else (
                "READY" if not self._used else
                "SPENT" if self._finished is None else self._finished["status"]
            )
            return {
                "status": state,
                "capability": "LIVE_QLOO_RANK_OBSERVATION_ONLY",
                "requests_remaining_this_process": 0 if state != "READY" else 1,
                "scope": "ONE_FROZEN_ARTIST_BRAND_POOL",
                "provider_authenticated": False,
                "independent_recheck_completed": False,
                "release_authorized": False,
                "external_action_executed": False,
            }

    def public_result(self) -> tuple[int, dict]:
        with self._lock:
            state, report = self._ledger.get() if self._ledger else (
                ("READY", None) if not self._used else (
                    ("SPENT", None) if self._finished is None
                    else (self._finished["status"], self._finished)
                )
            )
        if report is None:
            return 404, self._error("NO_PUBLISHED_OBSERVATION")
        if state != "LIVE_RANK_OBSERVED":
            return 404, self._error("NO_QUALIFIED_OBSERVATION")
        return 200, {**report, "cached": True, "requests_this_call": 0}

    def observe(self) -> tuple[int, dict]:
        with self._lock:
            if self._ledger:
                state, prior = self._ledger.get()
                if state != "READY":
                    if prior is None:
                        return 409, self._error("REQUEST_ALREADY_RESERVED")
                    return (200 if state == "LIVE_RANK_OBSERVED" else 503), {
                        **prior, "cached": True, "requests_this_call": 0,
                    }
                if not self._ledger.reserve():
                    return 409, self._error("REQUEST_ALREADY_RESERVED")
            elif self._used:
                if self._finished is None:
                    return 409, self._error("REQUEST_ALREADY_RESERVED")
                return 200 if self._finished["status"] == "LIVE_RANK_OBSERVED" else 503, {
                    **self._finished, "cached": True, "requests_this_call": 0,
                }
            # The reservation is consumed before network I/O, even after a timeout.
            self._used = True

        try:
            result = self._client.probe(self.spec)
            observed = normalize_affinity_capture({
                "summary": {
                    "endpoint": result.path,
                    "query_fingerprint": result.query_fingerprint,
                    "utc_collected_at": result.utc_collected_at,
                    "http_status": result.http_status,
                    "real_qloo_requests": 1,
                    "mode": "LIVE_RESPONSE_UNQUALIFIED",
                },
                "payload": result.payload,
            }, self.spec)
            order = tuple(item["entity_id"] for item in observed["ranking"])
            current_rank = order.index(self._selected) + 1
            report = {
                "status": "LIVE_RANK_OBSERVED",
                "source_mode": "LIVE_HTTP_UNVERIFIED",
                "scope": "ONE_FROZEN_ARTIST_BRAND_POOL",
                "decision_id": "brand",
                "candidate_count": len(order),
                "baseline_rank": current_rank,
                "verdict": "RANK_PASS_ONLY" if current_rank == 1 else "RANK_REPAIR_SUGGESTED",
                "evidence_off": "UNCERTAIN",
                "evidence_on": "PASS" if current_rank == 1 else "REPAIR",
                "selected_candidate_is_top": current_rank == 1,
                "collected_at": observed["collected_at"],
                "historical_replay": False,
                "provider_authenticated": False,
                "independent_recheck_completed": False,
                "overall_plan_verified": False,
                "release_authorized": False,
                "external_action_executed": False,
                "cached": False,
                "requests_this_call": 1,
            }
            code = 200
        except (QlooProbeError, AffinityCaptureError, ValueError, TypeError):
            report = self._error("PROVIDER_RANK_UNAVAILABLE", attempt_reserved=True)
            code = 503
        except Exception:
            report = self._error("PROVIDER_RANK_UNAVAILABLE", attempt_reserved=True)
            code = 503
        with self._lock:
            if self._ledger is not None:
                try:
                    self._ledger.complete(report)
                except (OSError, ValueError):
                    return 503, self._error("RESULT_PERSISTENCE_UNAVAILABLE", attempt_reserved=True)
            self._finished = report
        return code, report

    @staticmethod
    def _error(reason: str, *, attempt_reserved: bool = False) -> dict:
        return {
            "status": "BLOCKED",
            "reason": reason,
            "provider_authenticated": False,
            "independent_recheck_completed": False,
            "release_authorized": False,
            "external_action_executed": False,
            "requests_this_call": 1 if attempt_reserved else 0,
            "attempt_outcome_unknown": attempt_reserved,
        }


def configured_live_review(environment: Mapping[str, str]) -> HostedLiveReview | None:
    enabled = environment.get("OFC_HOSTED_QLOO_MODE", "")
    fields = (
        "OFC_HOSTED_QLOO_MODE", "OFC_HOSTED_QLOO_ARTIST_ID",
        "OFC_HOSTED_QLOO_BRAND_IDS", "OFC_HOSTED_QLOO_SELECTED_ID",
    )
    if not enabled:
        if any(environment.get(field) for field in fields[1:]):
            raise ValueError("incomplete hosted Qloo configuration")
        return None
    if enabled != "ONE_REQUEST_EXPLICITLY_APPROVED":
        raise ValueError("hosted Qloo mode is not approved")
    if not environment.get("QLOO_API_KEY", "").strip():
        raise ValueError("server-side Qloo API key missing")
    token = environment.get("OFC_HOSTED_OPERATOR_TOKEN", "")
    ledger_path = environment.get("OFC_HOSTED_QLOO_LEDGER_PATH", "")
    if not token or not ledger_path or not Path(ledger_path).is_absolute():
        raise ValueError("operator authorization and persistent ledger required")
    artist = environment.get("OFC_HOSTED_QLOO_ARTIST_ID", "")
    brands = tuple(environment.get("OFC_HOSTED_QLOO_BRAND_IDS", "").split(","))
    selected = environment.get("OFC_HOSTED_QLOO_SELECTED_ID", "")
    spec = brand_affinity_probe(artist, brands)
    scope = sha256((spec.fingerprint + ":" + selected).encode("utf-8")).hexdigest()
    ledger = HostedAttemptLedger(Path(ledger_path), scope=scope)
    return HostedLiveReview(
        api_key=environment["QLOO_API_KEY"],
        artist_id=artist, brand_ids=brands,
        selected_brand_id=selected, ledger=ledger, operator_token=token,
    )

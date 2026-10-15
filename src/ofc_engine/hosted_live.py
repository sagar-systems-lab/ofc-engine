"""One explicitly approved, server-side Qloo rank observation per process."""
from __future__ import annotations

from threading import Lock
from typing import Mapping

from .qloo import ProbeSpec, QlooProbeClient, QlooProbeError, brand_affinity_probe
from .qloo_affinity import AffinityCaptureError, normalize_affinity_capture


class HostedLiveReview:
    def __init__(
        self, *, api_key: str, artist_id: str, brand_ids: tuple[str, ...],
        selected_brand_id: str, client: QlooProbeClient | None = None,
    ) -> None:
        self.spec: ProbeSpec = brand_affinity_probe(artist_id, brand_ids)
        if selected_brand_id not in brand_ids:
            raise ValueError("baseline selection is not in the fixed Qloo pool")
        self._candidate_ids = brand_ids
        self._selected = selected_brand_id
        self._client = client or QlooProbeClient(api_key=api_key)
        self._lock = Lock()
        self._used = False
        self._finished: dict | None = None

    def state(self) -> dict:
        with self._lock:
            state = (
                "READY" if not self._used else
                "SPENT" if self._finished is None else
                self._finished["status"]
            )
            return {
                "status": state,
                "capability": "LIVE_QLOO_RANK_OBSERVATION_ONLY",
                "requests_remaining_this_process": 0 if self._used else 1,
                "scope": "ONE_FROZEN_ARTIST_BRAND_POOL",
                "provider_authenticated": False,
                "independent_recheck_completed": False,
                "release_authorized": False,
                "external_action_executed": False,
            }

    def observe(self) -> tuple[int, dict]:
        with self._lock:
            if self._used:
                if self._finished is None:
                    return 409, self._error("REQUEST_ALREADY_RESERVED")
                return 200 if self._finished["status"] == "LIVE_RANK_OBSERVED" else 503, {
                    **self._finished, "cached": True, "requests_this_call": 0,
                }
            # Never refund a reservation: timeouts and errors may have reached Qloo.
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
            report = self._error("PROVIDER_RANK_UNAVAILABLE")
            code = 503
        with self._lock:
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
    return HostedLiveReview(
        api_key=environment["QLOO_API_KEY"],
        artist_id=environment.get("OFC_HOSTED_QLOO_ARTIST_ID", ""),
        brand_ids=tuple(environment.get("OFC_HOSTED_QLOO_BRAND_IDS", "").split(",")),
        selected_brand_id=environment.get("OFC_HOSTED_QLOO_SELECTED_ID", ""),
    )

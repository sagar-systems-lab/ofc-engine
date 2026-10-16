"""Restricted Qloo transport and observable probe requests.

Responses remain unnormalized and must not be used directly as policy verdicts.
The event endpoint and request grammar require live qualification.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from hashlib import sha256
import json
import os
import socket
from time import monotonic
from typing import Any, Mapping, Protocol
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import HTTPRedirectHandler, Request, build_opener

HOST = "https://hackathon.api.qloo.com"
MAX_BYTES = 512 * 1024
_ENTITY_TYPES = frozenset({"urn:entity:" + kind for kind in (
    "artist", "book", "brand", "destination", "movie", "person",
    "place", "podcast", "tv_show", "videogame",
)})


class QlooProbeError(RuntimeError):
    """Stable, redacted error code; upstream bodies and headers are never exposed."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


@dataclass(frozen=True)
class ProbeSpec:
    path: str
    params: tuple[tuple[str, str], ...]

    def __post_init__(self) -> None:
        if self.path not in {"/search", "/v2/insights"}:
            raise ValueError("unsupported probe endpoint")
        keys = [key for key, _ in self.params]
        if len(keys) != len(set(keys)):
            raise ValueError("duplicate parameters")
        if not self.params or any(not k or not v or len(v) > 600 for k, v in self.params):
            raise ValueError("invalid probe parameter")
        if any("\r" in v or "\n" in v for _, v in self.params):
            raise ValueError("invalid control characters")

    @property
    def fingerprint(self) -> str:
        canonical = json.dumps([self.path, sorted(self.params)], separators=(",", ":"), ensure_ascii=True)
        return sha256(canonical.encode("utf-8")).hexdigest()

    @property
    def url(self) -> str:
        return HOST + self.path + "?" + urlencode(sorted(self.params))


def _ids(ids: tuple[str, ...]) -> str:
    if not 2 <= len(ids) <= 5 or any(not s or len(s) > 120 or any(c in s for c in ",\r\n") for s in ids):
        raise ValueError("provide 2–5 non-empty distinct candidate IDs")
    if len(ids) != len(set(ids)):
        raise ValueError("duplicate candidate IDs")
    return ",".join(ids)


def search_probe(query: str, *, entity_type: str, take: int = 8) -> ProbeSpec:
    if entity_type not in _ENTITY_TYPES:
        raise ValueError("entity type is not in the Qloo catalog")
    if not query or not query.strip() or len(query) > 120:
        raise ValueError("invalid search query")
    if type(take) is not int or not 1 <= take <= 20:
        raise ValueError("search size outside bounded range")
    return ProbeSpec("/search", (("query", query.strip()), ("types", entity_type), ("take", str(take))))


def brand_affinity_probe(
    artist_id: str,
    candidate_ids: tuple[str, ...],
    *,
    locality: str | None = None,
    locality_mode: str | None = None,
    explainability: bool = True,
) -> ProbeSpec:
    """Propose one fixed-pool request; locality support is not presumed.

    Filter mode constrains geographic results and signal mode introduces geographic
    influence. Their availability for brand results must be verified live.
    """
    if not artist_id or len(artist_id) > 120 or any(c in artist_id for c in ",\r\n"):
        raise ValueError("invalid artist ID")
    candidates = _ids(candidate_ids)
    if locality_mode not in (None, "filter", "signal"):
        raise ValueError("locality mode must be filter or signal")
    if (locality_mode is None) != (locality is None):
        raise ValueError("locality and mode must be given together")
    params = [
        ("filter.type", "urn:entity:brand"),
        ("signal.interests.entities", artist_id),
        ("filter.results.entities", candidates),
        ("take", str(len(candidate_ids))),
    ]
    if locality is not None:
        if not locality.strip() or len(locality) > 100:
            raise ValueError("invalid locality")
        params.append((f"{locality_mode}.location.query", locality.strip()))
        if locality_mode == "filter":
            params.append(("filter.location.radius", "0"))
    if explainability:
        params.append(("feature.explainability", "true"))
    return ProbeSpec("/v2/insights", tuple(params))


@dataclass(frozen=True)
class TransportReply:
    status: int
    body: bytes = field(repr=False)


class Transport(Protocol):
    def get(self, url: str, headers: Mapping[str, str], *, timeout: float, max_bytes: int) -> TransportReply: ...


class _RejectRedirect(HTTPRedirectHandler):
    def redirect_request(self, request, fp, code, msg, headers, newurl):  # type: ignore[override]
        return None


class HTTPSGet:
    """HTTPS client with disabled redirects and bounded response bodies."""

    def get(self, url: str, headers: Mapping[str, str], *, timeout: float, max_bytes: int) -> TransportReply:
        if not url.startswith(HOST + "/"):
            raise QlooProbeError("DISALLOWED_HOST")
        req = Request(url, headers=dict(headers), method="GET")
        opener = build_opener(_RejectRedirect())
        try:
            with opener.open(req, timeout=timeout) as response:
                data = response.read(max_bytes + 1)
                status = response.status
        except HTTPError as exc:
            # Do not include raw response body, URL, or headers in errors.
            exc.close()
            raise QlooProbeError(f"HTTP_{exc.code}") from None
        except (URLError, TimeoutError, OSError, socket.timeout):
            raise QlooProbeError("NETWORK_ERROR") from None
        if len(data) > max_bytes:
            raise QlooProbeError("RESPONSE_TOO_LARGE")
        return TransportReply(status, data)


@dataclass(frozen=True)
class ProbeObservation:
    path: str
    query_fingerprint: str
    utc_collected_at: str
    duration_ms: float
    http_status: int
    top_level_fields: tuple[str, ...]
    payload: dict[str, Any] = field(repr=False)
    authenticated_by_adapter: bool = False
    normalized_for_policy: bool = False

    def safe_summary(self) -> dict[str, Any]:
        return {
            "endpoint": self.path,
            "query_fingerprint": self.query_fingerprint,
            "utc_collected_at": self.utc_collected_at,
            "duration_ms": self.duration_ms,
            "http_status": self.http_status,
            "top_level_fields": list(self.top_level_fields),
            "authenticated_by_adapter": False,
            "normalized_for_policy": False,
            "live_capability_qualified": False,
        }


class QlooProbeClient:
    """Explicitly initiated exploratory reads; never feeds policy or external actions."""

    def __init__(self, *, api_key: str, transport: Transport | None = None, timeout: float = 8) -> None:
        if not api_key or not api_key.strip() or "\r" in api_key or "\n" in api_key:
            raise QlooProbeError("API_KEY_MISSING")
        if type(timeout) not in (float, int) or not 0 < timeout <= 20:
            raise ValueError("timeout must be between 0 and 20 seconds")
        self._api_key = api_key.strip()
        self._transport = transport if transport is not None else HTTPSGet()
        self._timeout = timeout

    @classmethod
    def from_environment(cls, *, transport: Transport | None = None) -> "QlooProbeClient":
        return cls(api_key=os.environ.get("QLOO_API_KEY", ""), transport=transport)

    def probe(self, spec: ProbeSpec) -> ProbeObservation:
        if not isinstance(spec, ProbeSpec):
            raise TypeError("expected a ProbeSpec")
        start = monotonic()
        try:
            response = self._transport.get(
                spec.url, {"X-Api-Key": self._api_key, "Accept": "application/json"},
                timeout=self._timeout, max_bytes=MAX_BYTES,
            )
        except QlooProbeError:
            raise
        except Exception:
            raise QlooProbeError("TRANSPORT_ERROR") from None
        if response.status != 200:
            raise QlooProbeError(f"HTTP_{response.status}")
        if len(response.body) > MAX_BYTES:
            raise QlooProbeError("RESPONSE_TOO_LARGE")
        try:
            raw = json.loads(response.body)
        except (ValueError, UnicodeDecodeError):
            raise QlooProbeError("INVALID_JSON") from None
        if not isinstance(raw, dict):
            raise QlooProbeError("UNEXPECTED_TOP_LEVEL_JSON")
        return ProbeObservation(
            path=spec.path,
            query_fingerprint=spec.fingerprint,
            utc_collected_at=datetime.now(timezone.utc).isoformat(),
            duration_ms=round((monotonic() - start) * 1000, 3),
            http_status=response.status,
            top_level_fields=tuple(sorted(raw)),
            payload=raw,
        )

from __future__ import annotations

import base64
from datetime import timedelta
import json
from math import isfinite
from urllib.parse import urlencode, urlsplit
from urllib.request import Request, build_opener, HTTPRedirectHandler

from .canonical import canonical_bytes, digest
from .models import Evidence, instant
from .policies import POLICY_VERSION


class SourceTransportUnavailable(ValueError):
    pass


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, *args):
        return None


class SourceHTTP:
    offline = False

    def __init__(self, origin, *, token, header="Authorization"):
        parsed = urlsplit(origin)
        if (
            parsed.scheme != "https"
            or not parsed.hostname
            or parsed.username
            or parsed.password
            or parsed.query
            or parsed.fragment
            or parsed.path not in ("", "/")
            or header not in {"Authorization", "X-Api-Key"}
            or not token
            or any(c in token for c in "\r\n")
        ):
            raise ValueError("invalid pinned source origin")
        self.origin, self.token, self.header = origin.rstrip("/"), token, header

    def request(self, method, path, payload, *, timeout):
        if (
            method not in {"GET", "POST"}
            or not path.startswith("/")
            or path.startswith("//")
            or "#" in path
        ):
            raise ValueError("invalid source request")
        headers = {self.header: self.token, "Accept": "application/json"}
        raw = None if method == "GET" else canonical_bytes(payload)
        if raw is not None:
            headers["Content-Type"] = "application/json"
        try:
            with build_opener(_NoRedirect()).open(
                Request(self.origin + path, data=raw, method=method, headers=headers),
                timeout=timeout,
            ) as response:
                body = response.read(512 * 1024 + 1)
                if len(body) > 512 * 1024:
                    raise ValueError("SOURCE_RESPONSE_TOO_LARGE")
                return response.status, body
        except Exception:
            raise SourceTransportUnavailable("SOURCE_TRANSPORT_UNAVAILABLE") from None


class QlooGraphSource:
    claims = frozenset(
        {
            "cultural_brand_rank",
            "place_cultural_relevance",
            "place_cultural_observation",
        }
    )

    def __init__(self, transport, *, issuer, signing_key):
        self.transport, self.issuer, self.key = transport, issuer, signing_key
        self.offline = getattr(transport, "offline", False) is True

    def requests(self, scope):
        if (
            scope["claim"] not in self.claims
            or not scope["subject_id"]
            or not 2 <= len(scope["pool"]) <= 8
        ):
            raise ValueError("QLOO_SCOPE_UNSUPPORTED")
        kind = "brand" if scope["claim"] == "cultural_brand_rank" else "place"
        if kind == "brand" and scope["locality"] is not None:
            raise ValueError("BRAND_LOCALITY_NOT_QUALIFIED")
        if kind == "place" and not scope["locality"]:
            raise ValueError("PLACE_LOCALITY_REQUIRED")
        params = {
            "filter.type": "urn:entity:" + kind,
            "signal.interests.entities": scope["subject_id"],
            "filter.results.entities": ",".join(scope["pool"]),
            "take": str(len(scope["pool"])),
            "feature.explainability": "true",
        }
        if kind == "place":
            params.update(
                {
                    "filter.location.query": scope["locality"],
                    "filter.location.radius": "0",
                }
            )
        path = "/v2/insights?" + urlencode(sorted(params.items()))
        count = 1 if scope["claim"] == "place_cultural_observation" else 2
        return [("rank", i, "GET", path, None) for i in range(count)]

    def collect(self, scope, execute):
        output = []
        for stage, index, method, path, payload in self.requests(scope):
            response = execute(stage, index, method, path, payload, self.transport)
            body = json.loads(base64.b64decode(response["body"]))
            entities = (
                body.get("results", {}).get("entities")
                if isinstance(body, dict)
                else None
            )
            if (
                body.get("success") is not True
                or not isinstance(entities, list)
                or len(entities) != len(scope["pool"])
            ):
                raise ValueError("INCOMPLETE_QLOO_CANDIDATES")
            rows = []
            for item in entities:
                affinity = item.get("query", {}).get("affinity")
                if (
                    item.get("entity_id") not in scope["pool"]
                    or item.get("type") != "urn:entity"
                    or type(affinity) not in (int, float)
                    or not isfinite(affinity)
                ):
                    raise ValueError("QLOO_RESULT_SCOPE_MISMATCH")
                rows.append((item["entity_id"], affinity))
            if (
                len({i for i, _ in rows}) != len(rows)
                or len({v for _, v in rows}) != len(rows)
                or [v for _, v in rows] != sorted((v for _, v in rows), reverse=True)
            ):
                raise ValueError("QLOO_RANK_AMBIGUOUS")
            observed = response["observed_at"]
            item = Evidence(
                id="q-" + response["call_id"][:32],
                tenant_id=scope["tenant_id"],
                plan_digest=scope["anchor_digest"],
                node_id=scope["node_id"],
                claim=scope["claim"],
                subject_id=scope["subject_id"],
                candidate_pool=tuple(scope["pool"]),
                locality=scope["locality"],
                input_digest=scope["input_digest"],
                source="qloo",
                mode="CAPTURED" if self.offline else "LIVE",
                attempt_id=response["call_id"],
                query_fingerprint=response["request_digest"],
                source_digest=response["body_digest"],
                observed_at=observed,
                expires_at=(
                    instant(observed)
                    + timedelta(seconds=3600 if scope["locality"] else 86400)
                )
                .isoformat()
                .replace("+00:00", "Z"),
                policy_version=POLICY_VERSION,
                license_scope="private_review",
                ranks=tuple(i for i, _ in rows),
                issuer=self.issuer,
                provider_account_scope=scope["account_scope"],
                entitlement_id=scope["entitlement_id"],
                extraction_rule_version="qloo-fixed-pool-1",
                raw_payload_ref=response["body_digest"],
                facts={
                    "origin_assurance": "ADAPTER_ATTESTATION",
                    "upstream_signature": False,
                },
            )
            output.append(
                item.model_copy(
                    update={
                        "signature": base64.b64encode(
                            self.key.sign(canonical_bytes(item.signing_fields()))
                        ).decode()
                    }
                )
            )
        return tuple(output)

    def rebind(self, records, scope):
        result = []
        for original in records:
            item = original.model_copy(
                update={
                    "id": "cached-"
                    + digest({"original": original.id, "scope": scope["scope_id"]})[
                        :32
                    ],
                    "plan_digest": scope["anchor_digest"],
                    "mode": "CAPTURED",
                    "signature": None,
                    "facts": original.facts
                    | {
                        "cache_replay": True,
                        "original_evidence_digest": digest(original.data()),
                    },
                }
            )
            result.append(
                item.model_copy(
                    update={
                        "signature": base64.b64encode(
                            self.key.sign(canonical_bytes(item.signing_fields()))
                        ).decode()
                    }
                )
            )
        return tuple(result)

    def resolve(self, query, entity_type, execute):
        if (
            entity_type not in {"artist", "brand", "place"}
            or not 1 <= len(query.strip()) <= 120
        ):
            raise ValueError("invalid entity resolution scope")
        path = "/search?" + urlencode(
            {"query": query.strip(), "types": "urn:entity:" + entity_type, "take": 8}
        )
        response = execute("search", 0, "GET", path, None, self.transport)
        body = json.loads(base64.b64decode(response["body"]))
        entities = body.get("results")
        entities = entities.get("entities") if isinstance(entities, dict) else entities
        if not isinstance(entities, list) or len(entities) > 8:
            raise ValueError("ENTITY_RESOLUTION_UNQUALIFIED")
        matches = [
            e
            for e in entities
            if e.get("name", "").strip().casefold() == query.strip().casefold()
            and isinstance(e.get("entity_id"), str)
        ]
        if len(matches) != 1:
            raise ValueError("ENTITY_CONFIRMATION_REQUIRED")
        return {
            "entity_id": matches[0]["entity_id"],
            "type": entity_type,
            "source_digest": response["body_digest"],
        }


class SignedBusinessSource:
    claims = frozenset(
        {
            "venue_capacity",
            "budget_quote",
            "contract_rights",
            "schedule_window",
            "channel_suitability",
            "media_suitability",
            "collaborator_suitability",
            "audience_suitability",
        }
    )

    def __init__(self, transport, *, path="/evidence"):
        if not path.startswith("/") or "?" in path or path.startswith("//"):
            raise ValueError("invalid evidence feed path")
        self.transport, self.path = transport, path
        self.offline = getattr(transport, "offline", False) is True

    def requests(self, scope):
        if scope["claim"] not in self.claims:
            raise ValueError("BUSINESS_FEED_CLAIM_UNSUPPORTED")
        payload = {
            k: v
            for k, v in scope.items()
            if k not in {"variant", "scope_id", "cache_key", "state"}
        }
        return [("attestation", 0, "POST", self.path, payload)]

    def collect(self, scope, execute):
        stage, index, method, path, payload = self.requests(scope)[0]
        response = execute(stage, index, method, path, payload, self.transport)
        body = json.loads(base64.b64decode(response["body"]))
        if (
            body.get("schema") != "ofc.source-evidence/1"
            or body.get("account_scope") != scope["account_scope"]
            or body.get("entitlement_id") != scope["entitlement_id"]
            or not isinstance(body.get("evidence"), list)
            or len(body["evidence"]) > 8
        ):
            raise ValueError("BUSINESS_FEED_SCOPE_MISMATCH")
        # Preserve upstream signatures; the service cannot acquire authority by returning a new key.
        return tuple(
            Evidence.model_validate_json(json.dumps(e)) for e in body["evidence"]
        )

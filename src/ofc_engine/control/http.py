from __future__ import annotations

from dataclasses import dataclass
from hashlib import sha256
import hmac
from importlib.resources import files
import re
from threading import Lock
from time import monotonic
from urllib.parse import parse_qs, urlsplit

from pydantic import ValidationError
import psycopg
import sqlite3

from .adapters import evidence_plan
from .canonical import canonical_bytes, strict_json
from .engine import review
from .fixtures import campaign
from .models import ReviewRequest
from .policies import capabilities
from .storage import CapacityReached, Conflict, IntegrityError, NotFound
from .verifier import ProofRejected

_REVIEW = re.compile(r"^/api/control/reviews/(pr-[a-f0-9]{32})(?:/(events|proof|cancel|resume|approvals|proposals))?$")
_MAX_BODY = 512 * 1024


@dataclass(frozen=True)
class Identity:
    tenant_id: str
    actor_id: str
    roles: frozenset[str] = frozenset({"read", "submit"})
    kind: str = "agent"

    def __post_init__(self):
        pattern = r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,95}"
        if (not re.fullmatch(pattern, self.tenant_id) or not re.fullmatch(pattern, self.actor_id)
                or not self.roles <= {"read", "submit", "approve"} or self.kind not in {"agent", "human"}
                or "approve" in self.roles and self.kind != "human"):
            raise ValueError("invalid control identity")


class ControlAccess:
    def __init__(self, tokens: dict[str, Identity] | None = None):
        tokens = tokens or {}
        if len(tokens) > 32 or any(not 32 <= len(token) <= 256 for token in tokens):
            raise ValueError("bounded strong control tokens required")
        self.tokens = {sha256(token.encode()).digest(): identity for token, identity in tokens.items()}
        self._rates, self._lock = {}, Lock()

    def authenticate(self, raw: str):
        if not raw.startswith("Bearer ") or not 32 <= len(raw[7:]) <= 256:
            raise PermissionError("ACCESS_DENIED")
        supplied = sha256(raw[7:].encode()).digest()
        for token_hash, identity in self.tokens.items():
            if hmac.compare_digest(supplied, token_hash):
                return identity
        raise PermissionError("ACCESS_DENIED")

    def admit_public(self, key):
        with self._lock:
            now = monotonic()
            if len(self._rates) > 1024:
                self._rates = {key: value for key, value in self._rates.items() if now - value[0] < 60}
            if len(self._rates) > 1024:
                raise CapacityReached("public capacity reached")
            started, count = self._rates.get(key, (now, 0))
            if now - started >= 60:
                started, count = now, 0
            if count >= 30:
                raise CapacityReached("public rate limit reached")
            self._rates[key] = (started, count + 1)


class ControlHTTPMixin:
    def _control_identity(self, role="read"):
        if self.headers.get("Authorization"):
            identity = self.server.control_access.authenticate(self.headers["Authorization"])
        else:
            runtime = self.server.copilot
            session_id = self.headers.get("X-OFC-Session", "")
            if runtime is None:
                raise PermissionError("ACCESS_DENIED")
            try:
                runtime._check_session(session_id, self._copilot_cookie("ofc_visitor"))
            except ValueError:
                raise PermissionError("ACCESS_DENIED") from None
            identity = Identity(tenant_id="copilot", actor_id=session_id)
            if role == "approve":
                try:
                    runtime.vault.authorize(self._copilot_cookie("ofc_owner"))
                except ValueError:
                    raise PermissionError("ACCESS_DENIED") from None
                identity = Identity(tenant_id="copilot", actor_id=session_id,
                                    roles=frozenset({"read", "submit", "approve"}), kind="human")
        if role not in identity.roles:
            raise PermissionError("ACCESS_DENIED")
        return identity

    def _control_input(self):
        origin = self.headers.get("Origin")
        if origin is not None:
            if not self._copilot_origin_ok():
                raise PermissionError("ORIGIN_REJECTED")
        elif self.headers.get("Sec-Fetch-Site") not in (None, "none"):
            raise PermissionError("ORIGIN_REJECTED")
        lengths = self.headers.get_all("Content-Length", [])
        if (self.headers.get("Transfer-Encoding") is not None or len(lengths) != 1
                or not lengths[0].isascii() or not lengths[0].isdecimal()
                or not 1 <= int(lengths[0]) <= _MAX_BODY
                or self.headers.get("Content-Type") != "application/json"):
            raise ValueError("INVALID_REQUEST")
        return strict_json(self.rfile.read(int(lengths[0])))

    def _control_error(self, exc):
        if isinstance(exc, PermissionError): status, code = 403, "ACCESS_DENIED"
        elif isinstance(exc, NotFound): status, code = 404, "REVIEW_UNAVAILABLE"
        elif isinstance(exc, Conflict): status, code = 409, "REVIEW_CONFLICT"
        elif isinstance(exc, CapacityReached): status, code = 429, "CAPACITY_UNAVAILABLE"
        elif isinstance(exc, (IntegrityError, ProofRejected, psycopg.Error, sqlite3.Error)): status, code = 503, "CONTROL_STORAGE_UNAVAILABLE"
        else: status, code = 422, "INVALID_CONTROL_REQUEST"
        details = [{"field": ".".join(str(value) for value in error["loc"]), "code": error["type"]}
                   for error in exc.errors(include_input=False)[:12]] if isinstance(exc, ValidationError) else []
        self._copilot_reply(status, {"error": code, "validation_errors": details,
                                    "external_action_executed": False, "provider_calls": 0})

    def _control_get(self):
        parts = urlsplit(self.path)
        path = parts.path
        assets = {"/plans": ("plans.html", "text/html; charset=utf-8"),
                  "/plans.js": ("plans.js", "text/javascript; charset=utf-8"),
                  "/plans.css": ("plans.css", "text/css; charset=utf-8"),
                  "/control-sdk.js": ("control-sdk.js", "text/javascript; charset=utf-8"),
                  "/plan-entry.js": ("plan-entry.js", "text/javascript; charset=utf-8")}
        if path not in assets and not path.startswith("/api/control/"):
            return False
        if not self.server.control_enabled:
            self._copilot_reply(404, {"error": "CONTROL_NOT_ENABLED"})
            return True
        try:
            if path in assets:
                if parts.query:
                    raise ValueError("INVALID_QUERY")
                asset, content_type = assets[path]
                self._send(200, files("ofc_engine").joinpath("web", asset).read_bytes(), content_type)
            elif path == "/api/control/capabilities" and not parts.query:
                self._copilot_reply(200, capabilities() | {"private_api_enabled": self.server.control is not None})
            elif path == "/api/control/schema" and not parts.query:
                self._copilot_reply(200, ReviewRequest.model_json_schema())
            elif path.startswith("/api/control/examples/") and not parts.query:
                values = path.removeprefix("/api/control/examples/").split("/")
                if len(values) not in (1, 2) or len(values) == 2 and values[1] not in {"nike", "adidas", "puma"}:
                    raise ValueError("INVALID_SCENARIO")
                self._copilot_reply(200, campaign(values[0], original=values[1] if len(values) == 2 else "nike").data())
            else:
                match = _REVIEW.fullmatch(path)
                if match is None or match[2] not in (None, "events", "proof") or self.server.control is None:
                    raise NotFound("review unavailable")
                identity = self._control_identity()
                runtime, review_id, operation = self.server.control, match[1], match[2]
                scope = (review_id, identity.tenant_id, identity.actor_id)
                if operation == "events":
                    params = parse_qs(parts.query, keep_blank_values=True)
                    if set(params) - {"after"} or "after" in params and (len(params["after"]) != 1 or not params["after"][0].isascii() or not params["after"][0].isdecimal()):
                        raise ValueError("INVALID_CURSOR")
                    after = int(params.get("after", [0])[0])
                    if not 0 <= after <= 10_000:
                        raise ValueError("INVALID_CURSOR")
                    events = runtime.store.events(*scope, after=after)
                    if self.headers.get("Accept") == "text/event-stream":
                        text = "retry: 1000\n" + "".join(f"id: {e['sequence']}\nevent: review\ndata: {canonical_bytes(e).decode()}\n\n" for e in events)
                        self._send(200, text.encode(), "text/event-stream; charset=utf-8")
                    else:
                        self._copilot_reply(200, {"events": events, "cursor": events[-1]["sequence"] if events else after})
                elif parts.query:
                    raise ValueError("INVALID_QUERY")
                elif operation == "proof":
                    self._copilot_reply(200, runtime.store.proof(*scope, authorities=runtime.authorities))
                else:
                    self._copilot_reply(200, runtime.store.snapshot(*scope))
        except (ValueError, TypeError, PermissionError, OSError, psycopg.Error, sqlite3.Error) as exc:
            self._control_error(exc)
        return True

    def _control_post(self):
        parts = urlsplit(self.path)
        path = parts.path
        if not path.startswith("/api/control/"):
            return False
        if not self.server.control_enabled:
            self._copilot_reply(404, {"error": "CONTROL_NOT_ENABLED"})
            return True
        try:
            if parts.query or self.command != "POST":
                raise ValueError("INVALID_REQUEST")
            if path in {"/api/control/simulate", "/api/control/estimate"}:
                self.server.control_access.admit_public(self.client_address[0])
                request = ReviewRequest.parse(self._control_input())
                if request.mode != "SYNTHETIC":
                    raise PermissionError("PRIVATE_INPUT_REQUIRES_AUTH")
                if request.as_of is None:
                    from .models import utc_now
                    request = request.model_copy(update={"as_of": utc_now()})
                result = evidence_plan(request) if path.endswith("/estimate") else review(request)
                self._copilot_reply(200, result)
                return True
            if self.server.control is None:
                raise NotFound("private runtime unavailable")
            runtime = self.server.control
            match = _REVIEW.fullmatch(path)
            role = "approve" if match and match[2] == "approvals" else "submit"
            identity = self._control_identity(role)
            data = self._control_input()
            if path == "/api/control/plans":
                request = ReviewRequest.parse(data)
                if request.plan.tenant_id != identity.tenant_id or request.plan.actor.id != identity.actor_id:
                    raise PermissionError("ACTOR_SCOPE_MISMATCH")
                receipt = runtime.store.admit(request)
                self._copilot_reply(202, receipt)
            elif match and match[2] in {"cancel", "resume"}:
                if data != {"confirm": match[2].upper()}:
                    raise ValueError("CONFIRMATION_REQUIRED")
                action = runtime.store.cancel if match[2] == "cancel" else runtime.store.resume
                self._copilot_reply(200, action(match[1], identity.tenant_id, identity.actor_id))
            elif match and match[2] == "approvals":
                from .approvals import create_intent, revoke_intent
                if set(data) == {"action", "intent_id"} and data["action"] == "REVOKE":
                    self._copilot_reply(200, revoke_intent(runtime, match[1], identity, data["intent_id"]))
                else:
                    self._copilot_reply(201, create_intent(runtime, match[1], identity, data))
            elif match and match[2] == "proposals":
                from .proposals import preview
                self._copilot_reply(200, preview(runtime, match[1], identity, data))
            else:
                raise NotFound("endpoint unavailable")
        except (ValueError, TypeError, PermissionError, OSError, RecursionError, psycopg.Error, sqlite3.Error) as exc:
            self._control_error(exc)
        return True

"""Isolated HTTP contract for hosted Copilot and owner-only connectivity."""
from __future__ import annotations

from http.cookies import SimpleCookie
from importlib.resources import files
import json
import re
from urllib.parse import urlsplit

import psycopg

from .copilot_credentials import OwnerUnauthorized
from .copilot_hosted import CopilotNotReady, SessionDenied, SessionLimitReached
from .copilot_hosted_quota import CopilotQuotaReached
from .durable import StorageIntegrityError

_SESSION = re.compile(r"^/api/copilot/sessions/(cp-[a-f0-9]{32})(/messages)?$")
_ROUTES = frozenset({
    "/api/owner/login", "/api/owner/logout", "/api/owner/credentials",
    "/api/owner/test-connection", "/api/copilot/sessions",
})
_MAX_BODY = 8192


class CopilotHTTPMixin:
    def _copilot_cookie(self, name: str) -> str:
        raw = self.headers.get("Cookie", "")
        if not raw or len(raw) > 2048:
            return ""
        try:
            cookies = SimpleCookie()
            cookies.load(raw)
        except Exception:
            return ""
        entry = cookies.get(name)
        return entry.value if entry is not None else ""

    def _copilot_reply(self, status: int, value: dict, *, cookie: str | None = None):
        body = json.dumps(
            value, separators=(",", ":"), sort_keys=True,
            allow_nan=False,
        ).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Content-Security-Policy", "default-src 'none'")
        if cookie is not None:
            self.send_header("Set-Cookie", cookie)
        self.end_headers()
        self.wfile.write(body)

    def _copilot_origin_ok(self):
        host = self.headers.get("Host", "")
        if (
            not host or len(host) > 253
            or any(c in host for c in "/\\@ ,;\r\n")
        ):
            return False
        local = host.startswith(("127.0.0.1:", "localhost:"))
        expected = ("http://" if local else "https://") + host
        return (
            self.headers.get("Origin") == expected
            and self.headers.get("Sec-Fetch-Site") in (None, "same-origin")
            and self.headers.get("Content-Type") == "application/json"
        )

    def _copilot_json_input(self) -> dict:
        lengths = self.headers.get_all("Content-Length", [])
        if (
            not self._copilot_origin_ok()
            or self.headers.get("Transfer-Encoding") is not None
            or len(lengths) != 1 or not lengths[0].isascii()
            or not lengths[0].isdecimal()
            or not 1 <= int(lengths[0]) <= _MAX_BODY
        ):
            raise ValueError("INVALID_REQUEST")
        data = json.loads(self.rfile.read(int(lengths[0])))
        if not isinstance(data, dict):
            raise ValueError("INVALID_REQUEST")
        return data

    def _copilot_cookie_header(self, name: str, value: str, *, max_age: int):
        host = self.headers.get("Host", "")
        secure = "" if host.startswith(("localhost:", "127.0.0.1:")) else "; Secure"
        return (
            f"{name}={value}; Path=/api; HttpOnly; SameSite=Strict"
            f"; Max-Age={max_age}{secure}"
        )

    def _copilot_post(self) -> bool:
        path = urlsplit(self.path).path
        found = _SESSION.fullmatch(path)
        if path not in _ROUTES and not found:
            return False
        runtime = self.server.copilot
        if runtime is None:
            self._copilot_reply(405, {"error": "COPILOT_NOT_CONFIGURED"})
            return True
        try:
            data = self._copilot_json_input()
        except (ValueError, UnicodeError, OverflowError):
            self._copilot_reply(403, {"error": "INVALID_REQUEST"})
            return True

        try:
            if path == "/api/owner/login":
                if set(data) != {"owner_secret"}:
                    raise ValueError("INVALID_REQUEST")
                ticket = runtime.vault.login(data["owner_secret"])
                self._copilot_reply(
                    200, {"authenticated": True, "expires_in_minutes": 30},
                    cookie=self._copilot_cookie_header(
                        "ofc_owner", ticket, max_age=1800,
                    ),
                )
                return True
            if path in (
                "/api/owner/logout", "/api/owner/credentials",
                "/api/owner/test-connection",
            ):
                owner = self._copilot_cookie("ofc_owner")
                runtime.vault.authorize(owner)
                if path == "/api/owner/logout":
                    runtime.vault.logout(owner)
                    self._copilot_reply(
                        200, {"authenticated": False},
                        cookie=self._copilot_cookie_header(
                            "ofc_owner", "", max_age=0,
                        ),
                    )
                    return True
                if path == "/api/owner/credentials":
                    if data.get("action") == "set" and set(data) == {
                        "action", "provider", "api_key"
                    }:
                        runtime.vault.configure(
                            owner, data["provider"], data["api_key"],
                        )
                    elif data.get("action") == "revoke" and set(data) == {
                        "action", "provider"
                    }:
                        runtime.vault.revoke(owner, data["provider"])
                    else:
                        raise ValueError("INVALID_CREDENTIAL_ACTION")
                    self._copilot_reply(
                        200, {"providers": runtime.vault.status(private=True)},
                    )
                    return True
                if data != {
                    "provider": data.get("provider"),
                    "confirm": "SPEND_ONE_PROVIDER_REQUEST",
                }:
                    raise ValueError("CONNECTION_TEST_CONFIRMATION_REQUIRED")
                if data["provider"] not in ("qloo", "gemini", "groq"):
                    raise ValueError("UNSUPPORTED_PROVIDER")
                self._copilot_reply(
                    200, runtime.test_connection(owner, data["provider"]),
                )
                return True
            if path == "/api/copilot/sessions":
                if data != {"confirm": "CREATE_COPILOT_SESSION"}:
                    raise ValueError("INVALID_REQUEST")
                session = runtime.session()
                ticket = session.pop("ticket")
                self._copilot_reply(
                    201, session,
                    cookie=self._copilot_cookie_header(
                        "ofc_visitor", ticket, max_age=86400,
                    ),
                )
                return True
            if found and found.group(2) == "/messages":
                if set(data) != {"request_id", "prompt"}:
                    raise ValueError("INVALID_COPILOT_MESSAGE")
                result = runtime.submit(
                    session_id=found.group(1),
                    ticket=self._copilot_cookie("ofc_visitor"),
                    request_id=data["request_id"],
                    prompt=data["prompt"],
                )
                self._copilot_reply(
                    202 if result.get("status") == "PROCESSING" else 200,
                    result,
                )
                return True
            raise ValueError("INVALID_REQUEST")
        except (OwnerUnauthorized, SessionDenied):
            self._copilot_reply(403, {"error": "ACCESS_DENIED"})
        except (SessionLimitReached, CopilotQuotaReached):
            self._copilot_reply(429, {"error": "COPILOT_CAPACITY_REACHED"})
        except CopilotNotReady:
            self._copilot_reply(503, {"error": "COPILOT_NOT_CONFIGURED"})
        except (ValueError, TypeError, KeyError):
            self._copilot_reply(422, {"error": "INVALID_REQUEST"})
        except (psycopg.Error, StorageIntegrityError):
            self._copilot_reply(503, {"error": "COPILOT_STORAGE_UNAVAILABLE"})
        return True

    def _copilot_get(self) -> bool:
        parts = urlsplit(self.path)
        path = parts.path
        if path in ("/connect", "/connect.js", "/connect.css") and not parts.query:
            if self.server.copilot is None:
                self._copilot_reply(404, {"error": "COPILOT_NOT_CONFIGURED"})
                return True
            asset, content_type = {
                "/connect": ("connect.html", "text/html; charset=utf-8"),
                "/connect.js": ("connect.js", "text/javascript; charset=utf-8"),
                "/connect.css": ("connect.css", "text/css; charset=utf-8"),
            }[path]
            self._send(
                200,
                files("ofc_engine").joinpath("web", asset).read_bytes(),
                content_type,
            )
            return True
        match = _SESSION.fullmatch(path)
        if path not in ("/api/capabilities", "/api/owner/credentials") and not match:
            return False
        if parts.query:
            self._copilot_reply(400, {"error": "INVALID_QUERY"})
            return True
        runtime = self.server.copilot
        if runtime is None:
            self._copilot_reply(
                200 if path == "/api/capabilities" else 405,
                {
                    "ofc": "NOT_CONFIGURED", "qloo": "NOT_CONFIGURED",
                    "llm": "NOT_CONFIGURED", "storage": "NOT_CONFIGURED",
                } if path == "/api/capabilities" else
                {"error": "COPILOT_NOT_CONFIGURED"},
            )
            return True
        try:
            if path == "/api/capabilities":
                self._copilot_reply(200, runtime.capabilities())
            elif path == "/api/owner/credentials":
                runtime.vault.authorize(self._copilot_cookie("ofc_owner"))
                self._copilot_reply(
                    200, {"providers": runtime.vault.status(private=True)},
                )
            elif match and not match.group(2):
                self._copilot_reply(
                    200, runtime.messages(
                        match.group(1), self._copilot_cookie("ofc_visitor"),
                    ),
                )
            else:
                self._copilot_reply(405, {"error": "METHOD_NOT_ALLOWED"})
        except (OwnerUnauthorized, SessionDenied):
            self._copilot_reply(403, {"error": "ACCESS_DENIED"})
        except (psycopg.Error, StorageIntegrityError):
            self._copilot_reply(503, {"error": "COPILOT_STORAGE_UNAVAILABLE"})
        return True

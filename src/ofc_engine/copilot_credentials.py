"""Encrypted OFC credential vault and short-lived owner sessions."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from hashlib import sha256
import hmac
import secrets

from cryptography.fernet import Fernet, InvalidToken

from .durable import StorageIntegrityError
from .postgres_store import PostgresRevisionStore

PROVIDERS = frozenset({"qloo", "gemini", "groq"})
_SAFE_TEST_REASONS = frozenset({
    "API_KEY_MISSING", "TRANSPORT_ERROR", "NETWORK_ERROR",
    "RESPONSE_TOO_LARGE", "INVALID_JSON", "UNEXPECTED_TOP_LEVEL_JSON",
    "QLOO_SEARCH_RESPONSE_INVALID", "DISALLOWED_HOST", "QLOO_TEST_FAILED",
    "MODEL_NETWORK_UNAVAILABLE", "MODEL_TRANSPORT_UNAVAILABLE",
    "MODEL_RESPONSE_TOO_LARGE", "INVALID_MODEL_ENVELOPE",
    "INVALID_MODEL_JSON", "MODEL_DRAFT_INVALID", "MODEL_NARRATION_INVALID",
    "DISALLOWED_MODEL_ENDPOINT", "LLM_TEST_FAILED",
    "PROVIDER_DAILY_QUOTA_EXHAUSTED", "PROVIDER_TEST_VALIDATION_FAILED",
}) | frozenset(f"HTTP_{n}" for n in range(400, 600)) | frozenset(
    f"PROVIDER_HTTP_{n}" for n in range(400, 600)
)


class OwnerUnauthorized(ValueError):
    pass


class OwnerRateLimited(OwnerUnauthorized):
    pass


class CredentialVault:
    def __init__(self, store: PostgresRevisionStore, *, master_key: str,
                 owner_secret: str):
        if not isinstance(store, PostgresRevisionStore):
            raise TypeError("Postgres store required")
        if not isinstance(owner_secret, str) or len(owner_secret) < 40:
            raise ValueError("owner bootstrap secret must be at least 40 characters")
        if not isinstance(master_key, str):
            raise ValueError("Fernet master key required")
        try:
            self.cipher = Fernet(master_key.encode("ascii"))
        except (ValueError, TypeError, UnicodeError):
            raise ValueError("invalid Fernet master key") from None
        self.store = store
        self._owner_digest = sha256(owner_secret.encode()).digest()
        with store._connect() as db:
            db.execute("select provider from ofc_private.copilot_credentials limit 0")
            db.execute("select token_digest from ofc_private.copilot_owner_sessions limit 0")
            db.execute("select id from ofc_private.copilot_owner_login_budget limit 0")

    def login(self, owner_secret: str) -> str:
        # Lock owner guessing across processes, even for incorrect credentials.
        with self.store._connect() as db:
            row = db.execute(
                "select window_start,attempts from "
                "ofc_private.copilot_owner_login_budget where id=1 for update"
            ).fetchone()
            if row is None:
                raise StorageIntegrityError("owner login quota migration missing")
            now = datetime.now(timezone.utc)
            within_window = now - row[0] < timedelta(minutes=15)
            attempts = row[1] if within_window else 0
            if attempts >= 20:
                raise OwnerRateLimited("OWNER_LOGIN_RATE_LIMITED")
            db.execute(
                "update ofc_private.copilot_owner_login_budget "
                "set window_start=%s,attempts=%s where id=1",
                (row[0] if within_window else now, attempts + 1),
            )
        if not isinstance(owner_secret, str) or not hmac.compare_digest(
            sha256(owner_secret.encode()).digest(), self._owner_digest
        ):
            raise OwnerUnauthorized("INVALID_OWNER_CREDENTIALS")
        ticket = secrets.token_urlsafe(48)
        with self.store._connect() as db:
            db.execute(
                "insert into ofc_private.copilot_owner_sessions(token_digest,expires_at) "
                "values(%s,now()+interval '30 minutes')",
                (sha256(ticket.encode()).hexdigest(),),
            )
        return ticket

    def authorize(self, ticket: str) -> None:
        if not isinstance(ticket, str) or not 1 <= len(ticket) <= 128:
            raise OwnerUnauthorized("OWNER_SESSION_REQUIRED")
        with self.store._connect() as db:
            row = db.execute(
                "select expires_at from ofc_private.copilot_owner_sessions "
                "where token_digest=%s",
                (sha256(ticket.encode()).hexdigest(),),
            ).fetchone()
        if row is None or row[0] <= datetime.now(timezone.utc):
            raise OwnerUnauthorized("OWNER_SESSION_EXPIRED")

    def logout(self, ticket: str) -> None:
        self.authorize(ticket)
        with self.store._connect() as db:
            db.execute(
                "delete from ofc_private.copilot_owner_sessions where token_digest=%s",
                (sha256(ticket.encode()).hexdigest(),),
            )

    @staticmethod
    def _provider(provider: str) -> str:
        if provider not in PROVIDERS:
            raise ValueError("UNSUPPORTED_PROVIDER")
        return provider

    def configure(self, ticket: str, provider: str, key: str) -> None:
        self.authorize(ticket)
        self._provider(provider)
        if (
            not isinstance(key, str) or not 8 <= len(key) <= 512
            or any(ord(c) < 33 or ord(c) > 126 for c in key)
        ):
            raise ValueError("INVALID_PROVIDER_CREDENTIAL")
        encrypted = self.cipher.encrypt(key.encode("ascii"))
        with self.store._connect() as db:
            db.execute(
                "insert into ofc_private.copilot_credentials"
                "(provider,ciphertext,masked_suffix,checked_at,check_status,check_reason_code) "
                "values(%s,%s,%s,null,'NOT_TESTED',null) "
                "on conflict(provider) do update set ciphertext=excluded.ciphertext,"
                "masked_suffix=excluded.masked_suffix,updated_at=now(),"
                "checked_at=null,check_status='NOT_TESTED',check_reason_code=null",
                (provider, encrypted, key[-4:]),
            )

    def revoke(self, ticket: str, provider: str) -> None:
        self.authorize(ticket)
        self._provider(provider)
        with self.store._connect() as db:
            db.execute(
                "delete from ofc_private.copilot_credentials where provider=%s",
                (provider,),
            )

    def read_for_server(self, provider: str) -> str | None:
        self._provider(provider)
        with self.store._connect() as db:
            row = db.execute(
                "select ciphertext from ofc_private.copilot_credentials "
                "where provider=%s", (provider,),
            ).fetchone()
        if row is None:
            return None
        try:
            return self.cipher.decrypt(bytes(row[0])).decode("ascii")
        except (InvalidToken, UnicodeError, ValueError):
            raise StorageIntegrityError("encrypted provider credential is invalid") from None

    def mark_test(self, provider: str, success: bool, *, reason_code: str | None = None) -> None:
        self._provider(provider)
        if reason_code is not None and (
            success or not isinstance(reason_code, str)
            or reason_code not in _SAFE_TEST_REASONS
        ):
            raise ValueError("invalid provider test reason")
        with self.store._connect() as db:
            db.execute(
                "update ofc_private.copilot_credentials "
                "set checked_at=now(),check_status=%s,check_reason_code=%s "
                "where provider=%s",
                ("ACTIVE" if success else "UNAVAILABLE", reason_code, provider),
            )

    def status(self, *, private: bool = False) -> dict:
        with self.store._connect() as db:
            rows = db.execute(
                "select provider,masked_suffix,checked_at,check_status,check_reason_code "
                "from ofc_private.copilot_credentials"
            ).fetchall()
        found = {r[0]: r for r in rows}
        now = datetime.now(timezone.utc)
        result = {}
        for provider in sorted(PROVIDERS):
            row = found.get(provider)
            if row is None:
                result[provider] = {"status": "NOT_CONFIGURED", "configured": False}
                continue
            status = row[3]
            if status == "ACTIVE" and (
                row[2] is None or now - row[2] > timedelta(minutes=30)
            ):
                status = "CHECK_REQUIRED"
            result[provider] = {
                "status": status,
                "configured": True,
                **({"masked_suffix": row[1]} if private else {}),
                **({"reason_code": row[4]} if private and status == "UNAVAILABLE"
                    and row[4] in _SAFE_TEST_REASONS else {}),
            }
        return result

    def connection_test_budget(self, ticket: str, *, daily_limit: int = 3) -> dict:
        self.authorize(ticket)
        with self.store._connect() as db:
            row = db.execute(
                "select utc_day,reserved from ofc_private.copilot_connection_attempts "
                "where id=1"
            ).fetchone()
        if row is None:
            raise StorageIntegrityError("connection test quota not initialized")
        today = datetime.now(timezone.utc).date()
        used = row[1] if row[0] == today else 0
        return {
            "limit": daily_limit, "used": used,
            "remaining": max(0, daily_limit - used),
            "resets_at_utc": (today + timedelta(days=1)).isoformat() + "T00:00:00Z",
        }

    def reserve_test(self, ticket: str, *, daily_limit: int = 3) -> None:
        self.authorize(ticket)
        if type(daily_limit) is not int or not 1 <= daily_limit <= 8:
            raise ValueError("invalid connection test budget")
        with self.store._connect() as db:
            row = db.execute(
                "select utc_day,reserved from ofc_private.copilot_connection_attempts "
                "where id=1 for update"
            ).fetchone()
            if row is None:
                raise StorageIntegrityError("connection test quota not initialized")
            today = datetime.now(timezone.utc).date()
            used = row[1] if row[0] == today else 0
            if used >= daily_limit:
                raise ValueError("CONNECTION_TEST_BUDGET_EXHAUSTED")
            db.execute(
                "update ofc_private.copilot_connection_attempts "
                "set utc_day=%s,reserved=%s where id=1",
                (today, used + 1),
            )

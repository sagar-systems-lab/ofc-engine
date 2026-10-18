"""Hosted Copilot: private Postgres sessions, bounded live providers and safe projections."""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from dataclasses import replace as replace_dataclass
from datetime import datetime, timedelta, timezone
from hashlib import sha256
from cryptography.fernet import InvalidToken
import hmac
import json
import logging
import re
import secrets
import psycopg
from time import monotonic

from .copilot_activity import activity_projection
from .business_assurance import assess_business_readiness
from .copilot_receipt import decision_receipt, grounded_explanations
from .copilot_compare import build_copilot_pair
from .copilot_domain_observations import HostedDomainObservations
from .copilot_credentials import CredentialVault
from .copilot_hosted_quota import (
    CopilotQuotaReached, HostedQlooResolver,
    reserve_model_call, reserve_qloo_request,
)
from .copilot_intake import EntityResolutionRequired, QlooSelectionRequired, ResolvedEntity, draft_plan
from .copilot_llm import CopilotDraft, DraftChoice, CopilotLLM, LLMUnavailable
from .dynamic_postgres import PostgresDynamicQlooReview
from .durable import StorageIntegrityError
from .intake import freeze_proposal
from .postgres_store import PostgresRevisionStore
from .qloo import QlooProbeClient, QlooProbeError, search_probe
from .qloo_catalog import capability_registry, category_for
from .copilot_resolver import normalize_qloo_search
from .taste_intelligence import TasteContext, policy_for
from .revisions import digest_obj


_log = logging.getLogger(__name__)
# Render's default last-resort logger discards INFO without an attached handler.
if not _log.handlers:
    _log.addHandler(logging.StreamHandler())
_log.setLevel(logging.INFO)
_log.propagate = False


class SessionDenied(ValueError):
    pass


class SessionLimitReached(ValueError):
    pass


class CopilotNotReady(RuntimeError):
    pass


class HostedCopilot:
    _MAX_PENDING_AGE = timedelta(minutes=3)

    @classmethod
    def _pending_status(cls, run_id: str, created_at: datetime) -> dict:
        if datetime.now(timezone.utc) - created_at < cls._MAX_PENDING_AGE:
            return {"run_id": run_id, "status": "PROCESSING"}
        return {
            "run_id": run_id,
            "status": "BLOCKED",
            "reason_code": "WORKFLOW_INTERRUPTED",
            "release_authorized": False,
            "external_action_executed": False,
        }

    def __init__(
        self, store: PostgresRevisionStore, vault: CredentialVault, *,
        qloo_daily_limit: int, llm_daily_limit: int,
        browser_daily_limit: int = 20, session_message_limit: int = 4,
        model_transport=None, qloo_transport=None,
    ):
        if not isinstance(store, PostgresRevisionStore) or vault.store is not store:
            raise TypeError("shared private PostgreSQL store and credential vault required")
        if type(qloo_daily_limit) is not int or not 1 <= qloo_daily_limit <= 256:
            raise ValueError("Qloo daily budget must be 1–256 requests")
        if type(llm_daily_limit) is not int or not 1 <= llm_daily_limit <= 128:
            raise ValueError("model daily budget must be 1–128 requests")
        if type(browser_daily_limit) is not int or not 1 <= browser_daily_limit <= 80:
            raise ValueError("invalid session budget")
        if type(session_message_limit) is not int or not 1 <= session_message_limit <= 12:
            raise ValueError("invalid visitor message limit")
        self.store = store
        self.vault = vault
        self.qloo_daily_limit = qloo_daily_limit
        self.llm_daily_limit = llm_daily_limit
        self.browser_daily_limit = browser_daily_limit
        self.session_message_limit = session_message_limit
        self.model_transport = model_transport
        self.qloo_transport = qloo_transport
        self.control_runtime = None
        self.graph_intake = False
        with store._connect() as db:
            for table in (
                "copilot_sessions", "copilot_messages", "copilot_search",
                "copilot_llm_budget", "copilot_browser_budget",
            ):
                db.execute("select 1 from ofc_private." + table + " limit 0")
            db.execute("select 1 from ofc_private.dynamic_qloo_budget limit 0")

    @staticmethod
    def _explicit_brand_scope(draft: CopilotDraft, prompt: str) -> CopilotDraft:
        if (
            len(draft.choices) != 1 or not draft.artist_name
            or (draft.taste_seed_name is not None
                and draft.taste_seed_name != draft.artist_name)
            or not re.search(
                r"\bartist\s*:\s*" + re.escape(draft.artist_name) + r"\b",
                prompt, flags=re.IGNORECASE,
            )
            or not re.search(r"\bcandidate\s+brands?\s*:", prompt, re.IGNORECASE)
            or not re.search(r"\boriginal\s+brand\s+choice\s*:", prompt, re.IGNORECASE)
        ):
            return draft
        choice = draft.choices[0]
        domain = " ".join(choice.domain.lower().replace("_", " ").replace("-", " ").split())
        if domain in {
            "sponsorship", "artist sponsorship", "sponsorship decision",
            "brand endorsement", "artist brand endorsement",
        }:
            choice = choice.model_copy(update={"domain": "brand"})
            draft = draft.model_copy(update={"choices": (choice,)})
        if draft.taste_seed_category in {
            "celebrity", "performer", "musician", "singer",
        }:
            draft = draft.model_copy(update={"taste_seed_category": "artist"})
        return draft

    @staticmethod
    def _digest(token: str) -> str:
        return sha256(token.encode("utf-8")).hexdigest()

    def _require_credentials(self) -> None:
        status = self.vault.status()
        if (
            not status["qloo"]["configured"]
            or not (
                status["gemini"]["configured"] or status["groq"]["configured"]
            )
        ):
            raise CopilotNotReady("COPILOT_PROVIDERS_NOT_CONFIGURED")

    def session(self) -> dict:
        self.cleanup_expired(limit=8)
        self._require_credentials()
        token = secrets.token_urlsafe(40)
        session_id = "cp-" + secrets.token_hex(16)
        today = datetime.now(timezone.utc).date()
        with self.store._connect() as db:
            quota = db.execute(
                "select utc_day,sessions from ofc_private.copilot_browser_budget "
                "where id=1 for update"
            ).fetchone()
            if quota is None:
                raise StorageIntegrityError("session quota migration missing")
            used = quota[1] if quota[0] == today else 0
            if used >= self.browser_daily_limit:
                raise SessionLimitReached("COPILOT_SESSION_QUOTA_EXHAUSTED")
            db.execute(
                "update ofc_private.copilot_browser_budget "
                "set utc_day=%s,sessions=%s where id=1", (today, used + 1),
            )
            db.execute(
                "insert into ofc_private.copilot_sessions"
                "(session_id,ticket_digest,expires_at) "
                "values(%s,%s,now()+interval '24 hours')",
                (session_id, self._digest(token)),
            )
        return {"session_id": session_id, "ticket": token, "expires_in_hours": 24,
                "session_message_limit": self.session_message_limit}

    def _check_session(self, session_id: str, ticket: str) -> None:
        if (
            not isinstance(session_id, str) or not session_id.startswith("cp-")
            or len(session_id) != 35 or
            any(c not in "0123456789abcdef" for c in session_id[3:])
            or not isinstance(ticket, str) or not 16 <= len(ticket) <= 160
        ):
            raise SessionDenied("INVALID_SESSION")
        with self.store._connect() as db:
            row = db.execute(
                "select ticket_digest,expires_at from ofc_private.copilot_sessions "
                "where session_id=%s", (session_id,),
            ).fetchone()
        if (
            row is None or row[1] <= datetime.now(timezone.utc)
            or not hmac.compare_digest(row[0], self._digest(ticket))
        ):
            raise SessionDenied("INVALID_SESSION")

    @staticmethod
    def _delete_session_tx(db, session_id: str) -> int:
        ids = [
            row[0] for row in db.execute(
                "select run_id from ofc_private.copilot_messages "
                "where session_id=%s for update", (session_id,),
            ).fetchall()
        ]
        if ids:
            db.execute(
                "delete from ofc_private.copilot_business_proofs "
                "where run_id=any(%s)", (ids,),
            )
            for table in (
                "copilot_discoveries", "copilot_domain_observations",
                "copilot_search",
            ):
                db.execute(
                    "delete from ofc_private." + table + " where run_id=any(%s)",
                    (ids,),
                )
            for table in (
                "dynamic_qloo_attempts", "browser_runs",
                "requests", "events",
            ):
                db.execute(
                    "delete from ofc_private." + table + " where plan_id=any(%s)",
                    (ids,),
                )
            db.execute(
                "delete from ofc_private.plans where plan_id=any(%s)", (ids,),
            )
            db.execute(
                "delete from ofc_private.copilot_messages "
                "where session_id=%s", (session_id,),
            )
        removed = db.execute(
            "delete from ofc_private.copilot_sessions "
            "where session_id=%s", (session_id,),
        )
        if removed.rowcount != 1:
            raise SessionDenied("SESSION_ALREADY_DELETED")
        return len(ids)

    def delete_session(self, session_id: str, ticket: str) -> dict:
        self._check_session(session_id, ticket)
        with self.store._connect() as db:
            count = self._delete_session_tx(db, session_id)
        return {
            "status": "SESSION_DELETED",
            "removed_conversation_runs": count,
            "provider_quota_history_preserved": True,
        }

    def cleanup_expired(self, *, limit: int = 8) -> int:
        if type(limit) is not int or not 1 <= limit <= 20:
            raise ValueError("invalid cleanup batch")
        with self.store._connect() as db:
            expired = [
                row[0] for row in db.execute(
                    "select session_id from ofc_private.copilot_sessions "
                    "where expires_at<=now() order by expires_at "
                    "limit %s for update skip locked", (limit,),
                ).fetchall()
            ]
            for session_id in expired:
                self._delete_session_tx(db, session_id)
        return len(expired)

    @staticmethod
    def _validate_message(request_id: str, prompt: str) -> str:
        if (
            not isinstance(request_id, str) or not 8 <= len(request_id) <= 96
            or any(c not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-_."
                   for c in request_id)
            or not isinstance(prompt, str)
            or not 1 <= len(prompt.strip()) <= 4000
            or "\x00" in prompt
        ):
            raise ValueError("INVALID_COPILOT_MESSAGE")
        return sha256(prompt.strip().encode()).hexdigest()

    def _decode_result(self, raw, ciphertext, digest: str) -> dict:
        try:
            if ciphertext is not None:
                decoded = self.vault.cipher.decrypt(bytes(ciphertext)).decode("utf-8")
            elif isinstance(raw, str):
                decoded = raw
            else:
                raise ValueError("missing result")
            value = json.loads(decoded)
        except (InvalidToken, ValueError, TypeError, UnicodeError) as exc:
            raise StorageIntegrityError("invalid private Copilot result") from exc
        if not isinstance(value, dict) or digest_obj(value) != digest:
            raise StorageIntegrityError("saved Copilot result integrity mismatch")
        return value

    def messages(self, session_id: str, ticket: str) -> dict:
        self._check_session(session_id, ticket)
        with self.store._connect() as db:
            rows = db.execute(
                "select run_id,request_id,status,result,result_digest,"
                "prompt_ciphertext,created_at,result_ciphertext from "
                "ofc_private.copilot_messages where session_id=%s "
                "order by created_at,run_id", (session_id,),
            ).fetchall()
        outputs = []
        for run_id, request_id, status, raw, digest, encrypted_prompt, created_at, encrypted_result in rows:
            try:
                prompt = self.vault.cipher.decrypt(
                    bytes(encrypted_prompt)
                ).decode("utf-8")
            except (InvalidToken, ValueError, UnicodeError):
                raise StorageIntegrityError("invalid saved user prompt") from None
            if status == "RESERVED":
                result = self._pending_status(run_id, created_at)
            else:
                result = self._decode_result(raw, encrypted_result, digest)
                if result.get("run_id") != run_id:
                    raise StorageIntegrityError("saved Copilot projection corrupted")
            with self.store._connect() as db:
                activity = activity_projection(db, run_id, result)
            outputs.append({"prompt": prompt, **result, "activity": activity})
        return {"session_id": session_id, "messages": outputs,
                "session_message_limit": self.session_message_limit}

    def _validate_entity_selection(
        self, session_id: str, parent_run_id: str | None,
        selection_ref: str | None,
    ) -> None:
        if selection_ref is None:
            return
        if (
            not isinstance(selection_ref, str)
            or len(selection_ref) != 16
            or any(c not in "0123456789abcdef" for c in selection_ref)
            or parent_run_id is None
        ):
            raise SessionDenied("INVALID_QLOO_SELECTION")
        with self.store._connect() as db:
            row = db.execute(
                "select status,result,result_digest,result_ciphertext from "
                "ofc_private.copilot_messages where run_id=%s and session_id=%s",
                (parent_run_id, session_id),
            ).fetchone()
        if row is None or row[0] != "COMPLETE":
            raise SessionDenied("QLOO_SELECTION_PARENT_NOT_FOUND")
        record = self._decode_result(row[1], row[3], row[2])
        if (
            not isinstance(record, dict) or digest_obj(record) != row[2]
            or record.get("status") != "CLARIFICATION_REQUIRED"
            or not any(
                isinstance(option, dict)
                and option.get("option_ref") == selection_ref
                for option in record.get("suggestions", [])
            )
        ):
            raise SessionDenied("QLOO_SELECTION_SCOPE_MISMATCH")

    def _parent_context(self, session_id: str, parent_run_id: str | None):
        if parent_run_id is None:
            return None
        if (
            not isinstance(parent_run_id, str)
            or not parent_run_id.startswith("cp-run-")
            or len(parent_run_id) != 39
            or any(c not in "0123456789abcdef" for c in parent_run_id[7:])
        ):
            raise SessionDenied("INVALID_CONVERSATION_PARENT")
        with self.store._connect() as db:
            row = db.execute(
                "select status,result,result_digest,result_ciphertext from "
                "ofc_private.copilot_messages where run_id=%s and session_id=%s",
                (parent_run_id, session_id),
            ).fetchone()
        if row is None or row[0] != "COMPLETE":
            raise SessionDenied("CONVERSATION_PARENT_NOT_COMPLETE")
        prior = self._decode_result(row[1], row[3], row[2])
        if (
            not isinstance(prior, dict) or digest_obj(prior) != row[2]
            or prior.get("run_id") != parent_run_id
            or prior.get("status") not in (
                "REVIEW_REQUIRED", "CLARIFICATION_REQUIRED",
                "CAPABILITY_UNAVAILABLE"
            ) or not isinstance(prior.get("plan"), dict)
        ):
            raise StorageIntegrityError("parent plan provenance invalid")
        return prior["plan"]

    def _cached_message(self, session_id: str, request_id: str,
                        prompt_digest: str, parent_run_id: str | None):
        """Previously saved answers remain readable even after key revocation."""
        with self.store._connect() as db:
            row = db.execute(
                "select run_id,prompt_digest,status,result,result_digest,created_at,"
                "parent_run_id,result_ciphertext from ofc_private.copilot_messages "
                "where session_id=%s and request_id=%s",
                (session_id, request_id),
            ).fetchone()
        if row is None:
            return None
        if row[1] != prompt_digest or row[6] != parent_run_id:
            raise SessionDenied("MESSAGE_IDEMPOTENCY_CONFLICT")
        if row[2] == "RESERVED":
            return self._pending_status(row[0], row[5])
        value = self._decode_result(row[3], row[7], row[4])
        if value.get("run_id") != row[0]:
            raise StorageIntegrityError("saved message digest mismatch")
        return value

    def _admit_message(self, session_id: str, request_id: str,
                       prompt_digest: str, model_id: str, prompt: str,
                       parent_run_id: str | None) -> tuple[str, dict | None]:
        with self.store._connect() as db:
            row = db.execute(
                "select prompt_count from ofc_private.copilot_sessions "
                "where session_id=%s for update", (session_id,),
            ).fetchone()
            if row is None:
                raise SessionDenied("INVALID_SESSION")
            existing = db.execute(
                "select run_id,prompt_digest,model_id,status,result,result_digest,"
                "parent_run_id,result_ciphertext from ofc_private.copilot_messages "
                "where session_id=%s and request_id=%s",
                (session_id, request_id),
            ).fetchone()
            if existing:
                if (
                    existing[1] != prompt_digest or existing[2] != model_id
                    or existing[6] != parent_run_id
                ):
                    raise SessionDenied("MESSAGE_IDEMPOTENCY_CONFLICT")
                if existing[3] == "RESERVED":
                    return existing[0], {"run_id": existing[0], "status": "PROCESSING"}
                saved = self._decode_result(existing[4], existing[7], existing[5])
                if saved.get("run_id") != existing[0]:
                    raise StorageIntegrityError("saved message digest mismatch")
                return existing[0], saved
            if row[0] >= self.session_message_limit:
                raise SessionLimitReached("COPILOT_SESSION_MESSAGE_LIMIT")
            run_id = "cp-run-" + secrets.token_hex(16)
            db.execute(
                "update ofc_private.copilot_sessions set prompt_count=prompt_count+1 "
                "where session_id=%s", (session_id,),
            )
            db.execute(
                "insert into ofc_private.copilot_messages "
                "(run_id,session_id,request_id,parent_run_id,prompt_digest,"
                "prompt_ciphertext,model_id,status) "
                "values(%s,%s,%s,%s,%s,%s,%s,'RESERVED')",
                (run_id, session_id, request_id, parent_run_id, prompt_digest,
                 self.vault.cipher.encrypt(prompt.encode("utf-8")), model_id),
            )
            return run_id, None

    def _complete(self, run_id: str, value: dict, *, blocked: bool = False):
        raw = json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)
        with self.store._connect() as db:
            cursor = db.execute(
                "update ofc_private.copilot_messages "
                "set status=%s,result=null,result_ciphertext=%s,result_digest=%s "
                "where run_id=%s and status='RESERVED'",
                ("BLOCKED" if blocked else "COMPLETE",
                 self.vault.cipher.encrypt(raw.encode("utf-8")),
                 digest_obj(value), run_id),
            )
            if cursor.rowcount != 1:
                raise StorageIntegrityError("Copilot result reservation lost")

    def _selected_model(self) -> str:
        statuses = self.vault.status()
        if statuses["gemini"]["status"] == "ACTIVE":
            return "gemini"
        if statuses["groq"]["status"] == "ACTIVE":
            return "groq"
        return "gemini" if statuses["gemini"]["configured"] else "groq"

    def _capacity(self, *, required_llm: int = 0, required_qloo: int = 0) -> dict:
        with self.store._connect() as db:
            llm = db.execute(
                "select utc_day,reserved from ofc_private.copilot_llm_budget "
                "where id=1"
            ).fetchone()
            qloo = db.execute(
                "select utc_day,reserved from ofc_private.dynamic_qloo_budget "
                "where id=1"
            ).fetchone()
        if not llm or not qloo:
            raise StorageIntegrityError("provider request budgets missing")
        today = datetime.now(timezone.utc).date()
        model_remaining = max(
            0, self.llm_daily_limit - (llm[1] if llm[0] == today else 0)
        )
        qloo_remaining = max(
            0, self.qloo_daily_limit - (qloo[1] if qloo[0] == today else 0)
        )
        if model_remaining < required_llm:
            raise CopilotQuotaReached("LLM_DAILY_QUOTA_EXHAUSTED")
        if qloo_remaining < required_qloo:
            raise CopilotQuotaReached("QLOO_DAILY_QUOTA_EXHAUSTED")
        return {
            "qloo_requests_remaining": qloo_remaining,
            "llm_requests_remaining": model_remaining,
            "llm_comparison_ready": model_remaining >= 3,
            "utc_day": str(today),
        }

    def _model(self):
        selected = self._selected_model()
        key = self.vault.read_for_server(selected)
        if key is None:
            raise ValueError("LLM_NOT_CONFIGURED")
        return CopilotLLM(
            provider=selected, api_key=key, transport=self.model_transport,
            intake_profile="graph" if self.graph_intake else "standard",
        )

    def _reviewer(self, trace=None):
        key = self.vault.read_for_server("qloo")
        if key is None:
            raise ValueError("QLOO_NOT_CONFIGURED")
        return PostgresDynamicQlooReview(
            self.store,
            QlooProbeClient(api_key=key, transport=self.qloo_transport,trace=trace),
            daily_limit=self.qloo_daily_limit,
        )

    @staticmethod
    def _place_city_scope(choice: DraftChoice, cities: tuple[str, ...]) -> str | None:
        if not cities:
            return None
        node = choice.decision_id.replace("_", " ").replace("-", " ").casefold()
        options = [name.casefold() for name in choice.candidate_names]
        matches = []
        for city in cities:
            token = r"(?<!\\w)" + re.escape(city.casefold()) + r"(?!\\w)"
            if re.search(token, node) or (
                options and all(re.search(token, option) for option in options)
            ):
                matches.append(city)
        return matches[0] if len(matches) == 1 else None

    @staticmethod
    def _qloo_budget_projection(draft: CopilotDraft, catalog_domains: dict,
                                seed_category: str) -> tuple[int, int]:
        qualified = [
            choice for choice in draft.choices
            if seed_category == "artist" and choice.domain.lower() in (
                "brand", "partner", "brand_partner"
            )
        ]
        core_names = {name for choice in qualified for name in choice.candidate_names}
        brand_rechecks = 2 * min(4, len(qualified))
        core_required = 1 + len(core_names) + brand_rechecks
        combined_names = {("brand", name) for name in core_names}
        combined_names.update(
            (catalog_domains[choice.decision_id], name)
            for choice in draft.choices
            if choice.decision_id in catalog_domains
            for name in choice.candidate_names
        )
        full_required = 1 + len(combined_names) + len(catalog_domains) + brand_rechecks
        return core_required, full_required

    @classmethod
    def _catalog_budget_scope(cls, draft: CopilotDraft, catalog_domains: dict,
                              seed_category: str, qloo_remaining: int
                              ) -> tuple[dict, dict, int]:
        core, full = cls._qloo_budget_projection(
            draft, catalog_domains, seed_category,
        )
        supports_brand = seed_category == "artist" and any(
            choice.domain.lower() in ("brand", "partner", "brand_partner")
            for choice in draft.choices
        )
        if supports_brand and catalog_domains and core <= qloo_remaining < full:
            return {}, dict(catalog_domains), core
        return dict(catalog_domains), {}, full

    @staticmethod
    def _public_pair(pair, draft, brands: dict, artist) -> dict:
        """Expose human names, not private Qloo entity IDs or raw provider captures."""
        data = deepcopy(pair.as_dict())
        aliases = {artist.entity_id: artist.name}
        aliases.update({resolved.entity_id: name for name, resolved in brands.items()})
        for choice in draft.choices:
            if choice.domain.lower() not in ("brand", "partner", "brand_partner"):
                for name in choice.candidate_names:
                    provisional = "unverified:" + sha256(
                        (choice.decision_id + ":" + name).encode()
                    ).hexdigest()[:32]
                    aliases[provisional] = name
        for mode in ("off", "on"):
            for decision in data[mode]["decisions"]:
                decision["candidate_id"] = aliases.get(
                    decision["candidate_id"], decision["candidate_id"]
                )
                decision["candidate_pool"] = [
                    aliases.get(c, c) for c in decision["candidate_pool"]
                ]
                obs = decision.get("qloo_observation")
                if obs:
                    obs["ranked_candidate_ids"] = [
                        aliases.get(c, c) for c in obs["ranked_candidate_ids"]
                    ]
            for obs in data[mode]["qloo"]["observations"]:
                obs["ranked_candidate_ids"] = [
                    aliases.get(c, c) for c in obs["ranked_candidate_ids"]
                ]
        return data

    @staticmethod
    def _draft_plan_projection(draft) -> dict:
        return {
            "objective": draft.objective,
            "artist_name": draft.artist_name,
            "budget_usd": draft.budget_usd,
            "cities": list(draft.cities),
            "choices": [
                {
                    "decision_id": choice.decision_id,
                    "domain": choice.domain,
                    "selected_name": choice.selected_name,
                    "candidate_names": list(choice.candidate_names),
                    "depends_on": list(choice.depends_on),
                    "source": "UNVERIFIED_LLM_PROPOSAL",
                }
                for choice in draft.choices
            ],
        }

    def _run_usage(self, run_id: str) -> dict:
        with self.store._connect() as db:
            searches = db.execute(
                "select count(*) from ofc_private.copilot_search where run_id=%s",
                (run_id,),
            ).fetchone()[0]
            insights = db.execute(
                "select count(*) from ofc_private.dynamic_qloo_attempts "
                "where plan_id=%s", (run_id,),
            ).fetchone()[0]
            catalog = db.execute(
                "select count(*) from ofc_private.copilot_domain_observations "
                "where run_id=%s", (run_id,),
            ).fetchone()[0]
            discovered = db.execute(
                "select count(*) from ofc_private.copilot_discoveries "
                "where run_id=%s", (run_id,),
            ).fetchone()[0]
            model = db.execute(
                "select llm_attempts from ofc_private.copilot_messages "
                "where run_id=%s", (run_id,),
            ).fetchone()
        if model is None:
            raise StorageIntegrityError("missing Copilot request admission")
        return {
            "qloo_search_requests": searches,
            "qloo_rank_requests": insights + catalog,
            "qloo_catalog_requests": catalog,
            "qloo_discovery_requests": discovered,
            "qloo_requests_this_run": searches + insights + catalog + discovered,
            "llm_requests_this_run": model[0],
        }

    def _needs_input(self, run_id: str, draft, model_id: str, *,
                     status: str, reason: str, question: str) -> dict:
        result = {
            "schema_version": "copilot.v1",
            "run_id": run_id,
            "status": status,
            "reason_code": reason,
            "question": question,
            "plan": self._draft_plan_projection(draft),
            "model_id": model_id,
            "qloo_status": "NOT_QUERIED",
            "ofc_review_completed": False,
            "release_authorized": False,
            "external_action_executed": False,
            **self._run_usage(run_id),
        }
        self._complete(run_id, result)
        return result

    def _narrate_arms(self, *, model: CopilotLLM, pair, prompt: str,
                      run_id: str) -> tuple[str, str]:
        # Reserve both calls before launching either independent model arm.
        reserve_model_call(
            self.store, daily_limit=self.llm_daily_limit, run_id=run_id,
        )
        reserve_model_call(
            self.store, daily_limit=self.llm_daily_limit, run_id=run_id,
        )
        started = monotonic()
        with ThreadPoolExecutor(max_workers=2) as pool:
            off = pool.submit(model.narrate_arm, pair, prompt=prompt, mode="off")
            on = pool.submit(model.narrate_arm, pair, prompt=prompt, mode="on")
            off_answer, on_answer = off.result(), on.result()
        _log.info("COPILOT_STAGE stage=two_narrations duration_ms=%d",
                  round((monotonic() - started) * 1000))
        return off_answer, on_answer

    def submit(self, *, session_id: str, ticket: str,
               request_id: str, prompt: str,
               parent_run_id: str | None = None,
               entity_selection_ref: str | None = None) -> dict:
        self._check_session(session_id, ticket)
        digest = self._validate_message(request_id, prompt)
        if entity_selection_ref is not None:
            digest = digest_obj({
                "prompt": prompt.strip(),
                "entity_selection_ref": entity_selection_ref,
            })
        cached = self._cached_message(
            session_id, request_id, digest, parent_run_id,
        )
        if cached is not None:
            return cached
        self._require_credentials()
        model = self._model()
        prior_plan = self._parent_context(session_id, parent_run_id)
        self._validate_entity_selection(
            session_id, parent_run_id, entity_selection_ref,
        )
        # A qualified artist/brand review needs artist + two brand lookups
        # and two independent rank observations. Do not spend a model call
        # or consume a visitor slot when the shared Qloo gate cannot fit it.
        # Exact plan cost is checked again after model extraction.
        self._capacity(required_llm=3, required_qloo=5)
        run_id, cached = self._admit_message(
            session_id, request_id, digest, model.model, prompt, parent_run_id,
        )
        if cached is not None:
            return cached
        source_trace=None
        if self.control_runtime is not None:
            from .control.telemetry import Trace
            source_trace=Trace(run_id,simulation=self.model_transport is not None and getattr(self.model_transport,"offline",False))
            if hasattr(model,"trace"): model.trace=source_trace
        try:
            started = monotonic()
            self._capacity(required_llm=3)
            reserve_model_call(
                self.store, daily_limit=self.llm_daily_limit, run_id=run_id,
            )
            draft = self._explicit_brand_scope(
                model.extract(prompt, prior_plan=prior_plan), prompt,
            )
            _log.info("COPILOT_STAGE stage=plan_extraction duration_ms=%d",
                      round((monotonic() - started) * 1000))
            reviewed_at = monotonic()
            native_discovery = None
            discovered_ids = {}
            catalog_skipped = {}
            catalog_skip_reasons = {}
            place_city_scopes = {}
            context = None
            seed_name = draft.taste_seed_name or draft.artist_name
            seed_category = draft.taste_seed_category or "artist"
            if not draft.choices and draft.discovery_category:
                if not seed_name:
                    return self._needs_input(
                        run_id, draft, model.model,
                        status="CLARIFICATION_REQUIRED",
                        reason="TASTE_SEED_REQUIRED",
                        question="Which existing artist, brand, movie or place should anchor the recommendations?",
                    )
                context = TasteContext(
                    seed_name=seed_name, seed_category=seed_category,
                    target_category=category_for(draft.discovery_category),
                    location=draft.taste_location,
                )
                self._capacity(
                    required_qloo=2 + (
                        2 if seed_category == "artist"
                        and context.target_category == "brand" else 0
                    ),
                )
                reviewer = self._reviewer(trace=source_trace) if source_trace is not None else self._reviewer()
                resolver = HostedQlooResolver(reviewer)
                seed = resolver.resolve(
                    run_id=run_id, name=seed_name, kind=seed_category,
                    selection_ref=entity_selection_ref,
                )
                native_discovery = HostedDomainObservations(reviewer).discover(
                    run_id=run_id, context=context, seed_id=seed.entity_id,
                )
                if native_discovery.get("status") != "CULTURAL_CANDIDATES_OBSERVED":
                    return self._needs_input(
                        run_id, draft, model.model,
                        status="CAPABILITY_UNAVAILABLE",
                        reason=native_discovery.get(
                            "reason_code", "QLOO_DISCOVERY_UNAVAILABLE"
                        ),
                        question="Qloo could not return a qualifying candidate shortlist.",
                    )
                candidates = native_discovery["candidates"]
                discovered_ids = {item["name"]: item["entity_id"] for item in candidates}
                discovered_choice = DraftChoice(
                    decision_id="taste_recommendation",
                    domain=context.target_category,
                    selected_name=candidates[0]["name"],
                    candidate_names=tuple(item["name"] for item in candidates),
                    mutable=(seed_category == "artist" and context.target_category == "brand"),
                    depends_on=(),
                )
                draft = CopilotDraft.model_validate({
                    **draft.model_dump(),
                    "taste_seed_name": seed_name,
                    "taste_seed_category": seed_category,
                    "choices": (discovered_choice,),
                })
            if not draft.choices:
                return self._needs_input(
                    run_id, draft, model.model,
                    status="CLARIFICATION_REQUIRED",
                    reason="DECISIONS_MISSING",
                    question=(
                        draft.clarification_question
                        or "What choices should the cultural review compare?"
                    ),
                )
            supported = seed_category == "artist" and any(
                choice.domain.lower() in ("brand", "partner", "brand_partner")
                for choice in draft.choices
            )
            catalog_domains = {}
            for choice in draft.choices:
                if (
                    choice.domain.lower() in ("brand", "partner", "brand_partner")
                    and seed_category == "artist"
                ):
                    continue
                try:
                    catalog_domains[choice.decision_id] = category_for(choice.domain)
                except ValueError:
                    continue
            if not supported and not catalog_domains:
                return self._needs_input(
                    run_id, draft, model.model,
                    status="CAPABILITY_UNAVAILABLE",
                    reason="QLOO_DECISION_DOMAIN_UNQUALIFIED",
                    question=(
                        "Only artist-to-brand cultural affinity is currently "
                        "qualified. A venue, contract or other domain needs "
                        "a separately verified evidence adapter."
                    ),
                )
            if not seed_name:
                return self._needs_input(
                    run_id, draft, model.model,
                    status="CLARIFICATION_REQUIRED",
                    reason="ARTIST_NAME_REQUIRED",
                    question="Which named Qloo entity should anchor the taste comparison?",
                )

            for choice in draft.choices:
                if catalog_domains.get(choice.decision_id) != "place":
                    continue
                # Discovery has its own locality-bound request fingerprint.
                if native_discovery is not None and choice.decision_id == "taste_recommendation":
                    continue
                city = self._place_city_scope(choice, draft.cities)
                if city is None:
                    catalog_skipped[choice.decision_id] = catalog_domains.pop(choice.decision_id)
                    catalog_skip_reasons[choice.decision_id] = "PLACE_CITY_SCOPE_UNQUALIFIED"
                else:
                    place_city_scopes[choice.decision_id] = city
            if not supported and not catalog_domains:
                return self._needs_input(
                    run_id, draft, model.model,
                    status="CAPABILITY_UNAVAILABLE",
                    reason="PLACE_CITY_SCOPE_UNQUALIFIED",
                    question="Which explicit city does each proposed place decision belong to?",
                )

            if native_discovery is None:
                catalog_domains, quota_skipped, required = self._catalog_budget_scope(
                    draft, catalog_domains, seed_category,
                    self._capacity()["qloo_requests_remaining"],
                )
                catalog_skipped.update(quota_skipped)
                self._capacity(required_qloo=required)
            reviewer = self._reviewer(trace=source_trace) if source_trace is not None else self._reviewer()
            resolver = HostedQlooResolver(reviewer)
            artist = (
                seed if native_discovery is not None
                else resolver.resolve(
                    run_id=run_id, name=seed_name, kind=seed_category,
                    selection_ref=entity_selection_ref,
                )
            )
            brand_names = list(dict.fromkeys(
                name for choice in draft.choices
                if choice.domain.lower() in ("brand", "partner", "brand_partner")
                for name in choice.candidate_names
            ))
            brands = {}
            if native_discovery is None and entity_selection_ref is None and len(brand_names) > 1:
                search_started = monotonic()

                def lookup_brand(name: str) -> ResolvedEntity:
                    # Each worker owns an independent reviewer; all admissions
                    # still serialize through the durable shared quota row.
                    return HostedQlooResolver(self._reviewer(trace=source_trace) if source_trace is not None else self._reviewer()).resolve(
                        run_id=run_id, name=name, kind="brand",
                    )

                with ThreadPoolExecutor(max_workers=min(4, len(brand_names))) as pool:
                    results = list(pool.map(lookup_brand, brand_names))
                brands = dict(zip(brand_names, results))
                _log.info("COPILOT_STAGE stage=brand_searches duration_ms=%d count=%d",
                          round((monotonic() - search_started) * 1000), len(brands))
            else:
                for name in brand_names:
                    brands[name] = (
                        ResolvedEntity(
                            name=name, entity_id=discovered_ids[name],
                            entity_type="urn:entity:brand",
                            query_fingerprint=native_discovery["query_fingerprint"],
                            source="QLOO_DISCOVERY",
                        )
                        if name in discovered_ids
                        else resolver.resolve(
                            run_id=run_id, name=name, kind="brand",
                            selection_ref=entity_selection_ref,
                        )
                    )
            catalog_identities = {}
            for choice in draft.choices:
                if choice.decision_id not in catalog_domains:
                    continue
                category = catalog_domains[choice.decision_id]
                entities = [
                    ResolvedEntity(
                        name=name, entity_id=discovered_ids[name],
                        entity_type="urn:entity:" + category,
                        query_fingerprint=native_discovery["query_fingerprint"],
                        source="QLOO_DISCOVERY",
                    ) if name in discovered_ids else resolver.resolve(
                        run_id=run_id, name=name, kind=category,
                        selection_ref=entity_selection_ref,
                    )
                    for name in choice.candidate_names
                ]
                if len({item.entity_id for item in entities}) != len(entities):
                    raise EntityResolutionRequired("AMBIGUOUS_CATALOG_IDENTITY")
                catalog_identities[choice.decision_id] = (
                    choice, category, entities,
                )
            if entity_selection_ref is not None and not resolver.selection_consumed:
                raise EntityResolutionRequired("QLOO_SELECTION_SCOPE_MISMATCH")
            proposal = draft_plan(
                draft, run_id=run_id, request_id=request_id,
                resolved_artist=artist, resolved_brands=brands,
            )
            evaluated = reviewer.evaluate(proposal)
            _log.info("COPILOT_STAGE stage=qloo_resolution_review duration_ms=%d",
                      round((monotonic() - reviewed_at) * 1000))
            baseline, _ = freeze_proposal(proposal)
            reviewed = self.store.get(run_id)
            requirements = {
                choice.decision_id: (
                    ("budget_feasibility", "contract_rights", "local_audience")
                    if choice.domain.lower() in ("brand", "partner", "brand_partner")
                    else ("venue_availability",)
                )
                for choice in draft.choices
                if choice.domain.lower() in (
                    "brand", "partner", "brand_partner", "venue", "place", "restaurant"
                )
            }
            business_review = assess_business_readiness(
                reviewed, required=requirements,
            )
            pair = build_copilot_pair(
                prompt=prompt, model_id=model.model,
                baseline=baseline, reviewed=reviewed, evaluated=evaluated,
            )
            if catalog_identities or catalog_skipped:
                engine = HostedDomainObservations(reviewer) if catalog_identities else None
                catalog_results = [
                    {
                        "decision_id": decision_id,
                        "category": category,
                        "source_mode": "NOT_REQUESTED",
                        "status": "NOT_EVALUATED",
                        "reason_code": catalog_skip_reasons.get(
                            decision_id, "OPTIONAL_EVIDENCE_QUOTA_GUARD"
                        ),
                        "requested_locality": place_city_scopes.get(decision_id),
                        "locality_independently_verified": False,
                        "ranked_candidates": [],
                        "policy_qualified": False,
                        "external_action_authorized": False,
                    }
                    for decision_id, category in catalog_skipped.items()
                ]
                for decision_id, (choice, category, entities) in catalog_identities.items():
                    observation = (
                        {
                            "status": "OBSERVED_NOT_POLICY_QUALIFIED",
                            "source_mode": "LIVE_HTTP_UNVERIFIED",
                            "ranking": [item["entity_id"] for item in native_discovery["candidates"]],
                            "query_fingerprint": native_discovery["query_fingerprint"],
                        }
                        if native_discovery is not None
                        and decision_id == "taste_recommendation"
                        else engine.inspect(
                            run_id=run_id, decision_id=decision_id, category=category,
                            subject_id=artist.entity_id,
                            candidate_ids=tuple(item.entity_id for item in entities),
                            location=place_city_scopes.get(decision_id),
                        )
                    )
                    name_map = {item.entity_id: item.name for item in entities}
                    catalog_results.append({
                        "decision_id": decision_id,
                        "category": category,
                        "source_mode": observation.get(
                            "source_mode", "EVIDENCE_UNAVAILABLE"
                        ),
                        "status": observation["status"],
                        "reason_code": observation.get(
                            "reason_code", "POLICY_SCOPE_NOT_LIVE_QUALIFIED"
                        ),
                        "query_fingerprint": observation.get("query_fingerprint"),
                        "requested_locality": observation.get("requested_locality"),
                        "locality_independently_verified": False,
                        "ranked_candidates": [
                            name_map[item] for item in observation.get("ranking", [])
                        ],
                        "policy_qualified": False,
                        "external_action_authorized": False,
                    })
                # Both arms receive the identical catalog observations.
                shared = {
                    **pair.off["qloo"],
                    "catalog_observations": catalog_results,
                }
                if any(
                    item["source_mode"] == "LIVE_HTTP_UNVERIFIED"
                    for item in catalog_results
                ):
                    shared["source_mode"] = "LIVE_HTTP_UNVERIFIED"
                shared["evidence_digest"] = digest_obj({
                    "original": pair.qloo_evidence_digest,
                    "catalog": catalog_results,
                })
                off = {**pair.off, "qloo": shared}
                on = {**pair.on, "qloo": shared}
                for decision in on["decisions"]:
                    if decision["decision_id"] in catalog_identities:
                        decision["ofc_verdict"] = "UNCERTAIN"
                        decision["ofc_reason"] = "CATALOG_POLICY_NOT_LIVE_QUALIFIED"
                pair = replace_dataclass(
                    pair, off=off, on=on,
                    qloo_evidence_digest=shared["evidence_digest"],
                )
            if native_discovery is not None:
                sanitized = {
                    key: value for key, value in native_discovery.items()
                    if key != "candidates"
                }
                sanitized["candidates"] = [
                    {k: v for k, v in item.items() if k != "entity_id"}
                    for item in native_discovery["candidates"]
                ]
                sanitized["taste_seed_category"] = seed_category
                sanitized["taste_seed_name"] = seed_name
                shared = {**pair.off["qloo"], "native_discovery": sanitized}
                shared["evidence_digest"] = digest_obj({
                    "prior": pair.qloo_evidence_digest, "native": sanitized,
                })
                pair = replace_dataclass(
                    pair,
                    off={**pair.off, "qloo": shared},
                    on={**pair.on, "qloo": shared},
                    qloo_evidence_digest=shared["evidence_digest"],
                )
            projection = self._public_pair(pair, draft, brands, artist)
            narrated_pair = replace_dataclass(
                pair, off=projection["off"], on=projection["on"],
            )
            off_answer, on_answer = self._narrate_arms(
                model=model, pair=narrated_pair, prompt=prompt, run_id=run_id,
            )
            receipt = decision_receipt(
                projection, business_review=business_review,
            )
            result = {
                **projection,
                "value_receipt": receipt,
                "grounded_explanations": grounded_explanations(receipt),
                "narrative_trust": "MODEL_NARRATIVE_NOT_INDEPENDENTLY_VERIFIED",
                "schema_version": "copilot.v1",
                "session_id": session_id,
                "request_id": request_id,
                "parent_run_id": parent_run_id,
                "journal_head": evaluated["journal_head"],
                "revision": evaluated["revision"],
                "rank_evidence_rechecked": evaluated["rank_evidence_rechecked"],
                "full_plan_independently_verified": False,
                "business_verification": business_review,
                "plan": self._draft_plan_projection(draft),
                "taste_policy": policy_for(
                    context.target_category if context is not None
                    else "brand" if supported else list(catalog_domains.values())[0]
                ),
                "unapplied_constraints": (
                    ["LOCATION_NOT_IN_RANK_QUERY"]
                    if draft.cities and context is None else []
                ),
                "status": "REVIEW_REQUIRED",
                "original_answer": off_answer,
                "ofc_answer": on_answer,
                "comparison_method": "INDEPENDENT_LLM_ARMS_SHARED_QLOO",
                **self._run_usage(run_id),
                "release_authorized": False,
                "external_action_executed": False,
            }
            if self.control_runtime is not None:
                try:
                    aliases = {item.entity_id: name for name, item in brands.items()}
                    result["control_review"] = self.control_runtime.import_proposal(
                        proposal, self.store, actor_id=session_id, labels=aliases, source_trace=source_trace,
                    )
                except (ValueError, psycopg.Error):
                    result["control_review"] = {
                        "status": "UNAVAILABLE", "additional_provider_calls": 0,
                        "external_action_executed": False,
                    }
            self._complete(run_id, result)
            _log.info("COPILOT_STAGE stage=total_run duration_ms=%d",
                      round((monotonic() - started) * 1000))
            return result
        except QlooSelectionRequired as exc:
            result = {
                "schema_version": "copilot.v1",
                "run_id": run_id,
                "status": "CLARIFICATION_REQUIRED",
                "reason_code": exc.reason_code,
                "question": "Please select a matching Qloo entity or enter its exact name.",
                "suggestions": exc.suggestions,
                "plan": self._draft_plan_projection(draft),
                "qloo_status": "ENTITY_RESOLUTION_PENDING",
                "model_id": model.model,
                "release_authorized": False,
                "external_action_executed": False,
                **self._run_usage(run_id),
            }
            self._complete(run_id, result)
            return result
        except (LLMUnavailable, QlooProbeError, EntityResolutionRequired,
                CopilotQuotaReached, ValueError) as exc:
            # Provider bodies and secrets must never be presented in public errors.
            code = str(exc)
            known = {
                "QLOO_SEARCH_UNAVAILABLE", "DAILY_QLOO_BUDGET_EXHAUSTED",
                "QLOO_DAILY_QUOTA_EXHAUSTED", "LLM_DAILY_QUOTA_EXHAUSTED",
                "ARTIST_NAME_REQUIRED", "QLOO_ARTIST_RESOLUTION_REQUIRED",
                "QLOO_BRAND_RESOLUTION_REQUIRED", "SEARCH_AMBIGUOUS_ENTITY",
                "SEARCH_EXACT_NAME_NOT_FOUND", "QLOO_NOT_CONFIGURED",
                "LLM_NOT_CONFIGURED", "MODEL_DRAFT_INVALID",
                "MODEL_NARRATION_INVALID", "PROVIDER_HTTP_429",
                "MODEL_RESPONSE_MISSING", "MODEL_OUTPUT_TRUNCATED",
                "MODEL_FINISH_REJECTED", "MODEL_DRAFT_JSON_INVALID",
                "MODEL_DRAFT_SCHEMA_INVALID",
            }
            safe = code if code in known else "COPILOT_EVIDENCE_UNAVAILABLE"
            result = {
                "schema_version": "copilot.v1",
                "run_id": run_id, "status": "BLOCKED", "reason_code": safe,
                "model_id": model.model, "release_authorized": False,
                "external_action_executed": False,
                **self._run_usage(run_id),
            }
            self._complete(run_id, result, blocked=True)
            return result

    def capabilities(self) -> dict:
        statuses = self.vault.status()
        chosen = self._selected_model()
        model = statuses[chosen]
        with self.store._connect() as db:
            db.execute("select 1")
        qloo_state = statuses["qloo"]["status"]
        model_state = model["status"]
        if "NOT_CONFIGURED" in (qloo_state, model_state):
            product_state = "NOT_CONFIGURED"
        elif "UNAVAILABLE" in (qloo_state, model_state):
            product_state = "DEGRADED"
        elif qloo_state == model_state == "ACTIVE":
            product_state = "ACTIVE"
        else:
            product_state = "CHECK_REQUIRED"
        return {
            "storage": "ACTIVE",
            "ofc": "ACTIVE",
            "qloo": qloo_state,
            "llm": model_state,
            "product": product_state,
            "selected_llm_provider": chosen,
            "capacity": self._capacity(),
            "session_message_limit": self.session_message_limit,
            "max_extracted_decisions": 64 if self.graph_intake else 6,
            "qloo_catalog": capability_registry(),
            "release_authorized": False,
        }

    def test_connection(self, ticket: str, provider: str) -> dict:
        self.vault.reserve_test(ticket, daily_limit=6)
        credential = self.vault.read_for_server(provider)
        if credential is None:
            raise ValueError("PROVIDER_NOT_CONFIGURED")
        ok = False
        try:
            if provider == "qloo":
                if not reserve_qloo_request(
                    self.store, daily_limit=self.qloo_daily_limit
                ):
                    raise CopilotQuotaReached("QLOO_DAILY_QUOTA_EXHAUSTED")
                spec = search_probe(
                    "Nike", entity_type="urn:entity:brand", take=8,
                )
                observation = QlooProbeClient(
                    api_key=credential, transport=self.qloo_transport,
                ).probe(spec)
                try:
                    normalize_qloo_search(
                        "Nike", entity_type="brand",
                        spec=spec, observation=observation,
                    )
                except (EntityResolutionRequired, QlooSelectionRequired):
                    raise QlooProbeError("QLOO_SEARCH_RESPONSE_INVALID") from None
            else:
                reserve_model_call(self.store, daily_limit=self.llm_daily_limit)
                client = CopilotLLM(
                    provider=provider, api_key=credential,
                    transport=self.model_transport,
                )
                if provider == "gemini":
                    client.probe_connectivity()
                else:
                    client.extract(
                        "Extract this hypothetical proposal only: For the fictional "
                        "artist Example Artist, compare the fictional brand candidates "
                        "Brand A and Brand B as sponsors. The original selection is "
                        "Brand A. The budget is 60000 USD, the city is Chicago, and "
                        "no booking or real-world action is authorized."
                    )
            ok = True
        except QlooProbeError as exc:
            raw = exc.code
            reason = raw if (
                raw in (
                    "API_KEY_MISSING", "TRANSPORT_ERROR", "NETWORK_ERROR",
                    "RESPONSE_TOO_LARGE", "INVALID_JSON",
                    "UNEXPECTED_TOP_LEVEL_JSON", "QLOO_SEARCH_RESPONSE_INVALID",
                    "DISALLOWED_HOST",
                )
                or raw in {f"HTTP_{code}" for code in range(400, 600)}
            ) else "QLOO_TEST_FAILED"
        except LLMUnavailable as exc:
            raw = str(exc)
            reason = raw if (
                raw in (
                    "MODEL_NETWORK_UNAVAILABLE", "MODEL_TRANSPORT_UNAVAILABLE",
                    "MODEL_RESPONSE_TOO_LARGE", "INVALID_MODEL_ENVELOPE",
                    "INVALID_MODEL_JSON", "MODEL_DRAFT_INVALID",
                    "MODEL_NARRATION_INVALID", "DISALLOWED_MODEL_ENDPOINT",
                    "MODEL_RESPONSE_MISSING", "MODEL_OUTPUT_TRUNCATED",
                    "MODEL_FINISH_REJECTED", "MODEL_DRAFT_JSON_INVALID",
                    "MODEL_DRAFT_SCHEMA_INVALID", "MODEL_PROBE_INVALID",
                )
                or raw in {f"PROVIDER_HTTP_{code}" for code in range(400, 600)}
            ) else "LLM_TEST_FAILED"
        except CopilotQuotaReached:
            reason = "PROVIDER_DAILY_QUOTA_EXHAUSTED"
        except ValueError:
            reason = "PROVIDER_TEST_VALIDATION_FAILED"
        limited = provider == "gemini" and ok
        self.vault.mark_test(
            provider, ok, reason_code=reason if not ok else None,
            connection_only=limited,
        )
        return {
            "provider": provider,
            "status": "CONNECTED" if limited else "ACTIVE" if ok else "UNAVAILABLE",
            **({"qualification": "CONNECTIVITY_ONLY"} if limited else {}),
            **({"reason_code": reason} if not ok else {}),
        }

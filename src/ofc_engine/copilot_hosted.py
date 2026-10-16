"""Hosted Copilot: private Postgres sessions, bounded live providers and safe projections."""
from __future__ import annotations

from datetime import datetime, timezone
from hashlib import sha256
import hmac
import json
import secrets

from .copilot_compare import build_copilot_pair
from .copilot_credentials import CredentialVault
from .copilot_hosted_quota import (
    CopilotQuotaReached, HostedQlooResolver,
    reserve_model_call, reserve_qloo_request,
)
from .copilot_intake import EntityResolutionRequired, draft_plan
from .copilot_llm import CopilotLLM, LLMUnavailable
from .dynamic_postgres import PostgresDynamicQlooReview
from .durable import StorageIntegrityError
from .intake import freeze_proposal
from .postgres_store import PostgresRevisionStore
from .qloo import QlooProbeClient, QlooProbeError, search_probe
from .revisions import digest_obj


class SessionDenied(ValueError):
    pass


class SessionLimitReached(ValueError):
    pass


class HostedCopilot:
    def __init__(
        self, store: PostgresRevisionStore, vault: CredentialVault, *,
        qloo_daily_limit: int, llm_daily_limit: int,
        browser_daily_limit: int = 20,
        model_transport=None, qloo_transport=None,
    ):
        if not isinstance(store, PostgresRevisionStore) or vault.store is not store:
            raise TypeError("shared private PostgreSQL store and credential vault required")
        for limit in (qloo_daily_limit, llm_daily_limit):
            if type(limit) is not int or not 1 <= limit <= 64:
                raise ValueError("provider budgets must be 1–64 daily requests")
        if type(browser_daily_limit) is not int or not 1 <= browser_daily_limit <= 80:
            raise ValueError("invalid session budget")
        self.store = store
        self.vault = vault
        self.qloo_daily_limit = qloo_daily_limit
        self.llm_daily_limit = llm_daily_limit
        self.browser_daily_limit = browser_daily_limit
        self.model_transport = model_transport
        self.qloo_transport = qloo_transport
        with store._connect() as db:
            for table in (
                "copilot_sessions", "copilot_messages", "copilot_search",
                "copilot_llm_budget", "copilot_browser_budget",
            ):
                db.execute("select 1 from ofc_private." + table + " limit 0")
            db.execute("select 1 from ofc_private.dynamic_qloo_budget limit 0")

    @staticmethod
    def _digest(token: str) -> str:
        return sha256(token.encode("utf-8")).hexdigest()

    def session(self) -> dict:
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
        return {"session_id": session_id, "ticket": token, "expires_in_hours": 24}

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

    def messages(self, session_id: str, ticket: str) -> dict:
        self._check_session(session_id, ticket)
        with self.store._connect() as db:
            rows = db.execute(
                "select run_id,request_id,status,result,result_digest from "
                "ofc_private.copilot_messages where session_id=%s "
                "order by created_at,run_id", (session_id,),
            ).fetchall()
        outputs = []
        for run_id, request_id, status, raw, digest in rows:
            if status == "RESERVED":
                outputs.append({"run_id": run_id, "status": "PROCESSING"})
                continue
            try:
                result = json.loads(raw)
            except (ValueError, TypeError) as exc:
                raise StorageIntegrityError("invalid saved Copilot projection") from exc
            if (
                not isinstance(result, dict) or digest_obj(result) != digest
                or result.get("run_id") != run_id
            ):
                raise StorageIntegrityError("saved Copilot projection corrupted")
            outputs.append(result)
        return {"session_id": session_id, "messages": outputs}

    def _admit_message(self, session_id: str, request_id: str,
                       prompt_digest: str, model_id: str) -> tuple[str, dict | None]:
        with self.store._connect() as db:
            row = db.execute(
                "select prompt_count from ofc_private.copilot_sessions "
                "where session_id=%s for update", (session_id,),
            ).fetchone()
            if row is None:
                raise SessionDenied("INVALID_SESSION")
            existing = db.execute(
                "select run_id,prompt_digest,model_id,status,result,result_digest "
                "from ofc_private.copilot_messages "
                "where session_id=%s and request_id=%s",
                (session_id, request_id),
            ).fetchone()
            if existing:
                if existing[1] != prompt_digest or existing[2] != model_id:
                    raise SessionDenied("MESSAGE_IDEMPOTENCY_CONFLICT")
                if existing[3] == "RESERVED":
                    return existing[0], {"run_id": existing[0], "status": "PROCESSING"}
                try:
                    saved = json.loads(existing[4])
                except (TypeError, ValueError) as exc:
                    raise StorageIntegrityError("invalid saved message") from exc
                if (
                    not isinstance(saved, dict) or digest_obj(saved) != existing[5]
                    or saved.get("run_id") != existing[0]
                ):
                    raise StorageIntegrityError("saved message digest mismatch")
                return existing[0], saved
            if row[0] >= 4:
                raise SessionLimitReached("COPILOT_SESSION_MESSAGE_LIMIT")
            run_id = "cp-run-" + secrets.token_hex(16)
            db.execute(
                "update ofc_private.copilot_sessions set prompt_count=prompt_count+1 "
                "where session_id=%s", (session_id,),
            )
            db.execute(
                "insert into ofc_private.copilot_messages "
                "(run_id,session_id,request_id,prompt_digest,model_id,status) "
                "values(%s,%s,%s,%s,%s,'RESERVED')",
                (run_id, session_id, request_id, prompt_digest, model_id),
            )
            return run_id, None

    def _complete(self, run_id: str, value: dict, *, blocked: bool = False):
        raw = json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)
        with self.store._connect() as db:
            cursor = db.execute(
                "update ofc_private.copilot_messages "
                "set status=%s,result=%s,result_digest=%s "
                "where run_id=%s and status='RESERVED'",
                ("BLOCKED" if blocked else "COMPLETE", raw,
                 digest_obj(value), run_id),
            )
            if cursor.rowcount != 1:
                raise StorageIntegrityError("Copilot result reservation lost")

    def _model(self):
        selected = "gemini" if self.vault.read_for_server("gemini") else "groq"
        key = self.vault.read_for_server(selected)
        if key is None:
            raise ValueError("LLM_NOT_CONFIGURED")
        return CopilotLLM(
            provider=selected, api_key=key, transport=self.model_transport,
        )

    def _reviewer(self):
        key = self.vault.read_for_server("qloo")
        if key is None:
            raise ValueError("QLOO_NOT_CONFIGURED")
        return PostgresDynamicQlooReview(
            self.store,
            QlooProbeClient(api_key=key, transport=self.qloo_transport),
            daily_limit=self.qloo_daily_limit,
        )

    @staticmethod
    def _public_pair(pair, draft, brands: dict, artist) -> dict:
        """Expose human names, not private Qloo entity IDs or raw provider captures."""
        data = pair.as_dict()
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

    def submit(self, *, session_id: str, ticket: str,
               request_id: str, prompt: str) -> dict:
        self._check_session(session_id, ticket)
        digest = self._validate_message(request_id, prompt)
        model = self._model()
        run_id, cached = self._admit_message(session_id, request_id, digest, model.model)
        if cached is not None:
            return cached
        try:
            reserve_model_call(self.store, daily_limit=self.llm_daily_limit)
            draft = model.extract(prompt)
            if draft.clarification_question and not draft.choices:
                report = {
                    "run_id": run_id, "status": "CLARIFICATION_REQUIRED",
                    "question": draft.clarification_question,
                    "model_id": model.model,
                    "qloo_status": "NOT_QUERIED",
                    "release_authorized": False,
                }
                self._complete(run_id, report)
                return report
            if not draft.artist_name:
                raise EntityResolutionRequired("ARTIST_NAME_REQUIRED")

            reviewer = self._reviewer()
            resolver = HostedQlooResolver(reviewer)
            artist = resolver.resolve(
                run_id=run_id, name=draft.artist_name, kind="artist"
            )
            brands = {}
            for choice in draft.choices:
                if choice.domain.lower() in ("brand", "partner", "brand_partner"):
                    for name in choice.candidate_names:
                        if name not in brands:
                            brands[name] = resolver.resolve(
                                run_id=run_id, name=name, kind="brand"
                            )
            proposal = draft_plan(
                draft, run_id=run_id, request_id=request_id,
                resolved_artist=artist, resolved_brands=brands,
            )
            evaluated = reviewer.evaluate(proposal)
            baseline, _ = freeze_proposal(proposal)
            reviewed = self.store.get(run_id)
            pair = build_copilot_pair(
                prompt=prompt, model_id=model.model,
                baseline=baseline, reviewed=reviewed, evaluated=evaluated,
            )
            reserve_model_call(self.store, daily_limit=self.llm_daily_limit)
            words = model.narrate_pair(pair, prompt=prompt)
            result = {
                **self._public_pair(pair, draft, brands, artist),
                "status": "REVIEW_REQUIRED",
                "original_answer": words.off_answer,
                "ofc_answer": words.on_answer,
                "qloo_requests_this_run": evaluated["new_qloo_requests"],
                "release_authorized": False,
                "external_action_executed": False,
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
            }
            safe = code if code in known else "COPILOT_EVIDENCE_UNAVAILABLE"
            result = {
                "run_id": run_id, "status": "BLOCKED", "reason_code": safe,
                "model_id": model.model, "release_authorized": False,
                "external_action_executed": False,
            }
            self._complete(run_id, result, blocked=True)
            return result

    def capabilities(self) -> dict:
        statuses = self.vault.status()
        model = statuses["gemini"]
        if model["status"] == "NOT_CONFIGURED":
            model = statuses["groq"]
        with self.store._connect() as db:
            db.execute("select 1")
        return {
            "storage": "ACTIVE",
            "ofc": "ACTIVE",
            "qloo": statuses["qloo"]["status"],
            "llm": model["status"],
            "release_authorized": False,
        }

    def test_connection(self, ticket: str, provider: str) -> dict:
        self.vault.reserve_test(ticket, daily_limit=3)
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
                QlooProbeClient(
                    api_key=credential, transport=self.qloo_transport,
                ).probe(search_probe(
                    "Taylor Swift", entity_type="urn:entity:artist", take=2,
                ))
            else:
                reserve_model_call(self.store, daily_limit=self.llm_daily_limit)
                CopilotLLM(
                    provider=provider, api_key=credential,
                    transport=self.model_transport,
                ).extract(
                    "Propose two fictional brands for an imaginary artist, "
                    "with no real-world booking or action."
                )
            ok = True
        except (ValueError, QlooProbeError, LLMUnavailable, CopilotQuotaReached):
            pass
        self.vault.mark_test(provider, ok)
        return {"provider": provider, "status": "ACTIVE" if ok else "UNAVAILABLE"}

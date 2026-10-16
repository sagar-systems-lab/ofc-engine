"""Hosted Copilot: private Postgres sessions, bounded live providers and safe projections."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from hashlib import sha256
from cryptography.fernet import InvalidToken
import hmac
import json
import secrets

from .business_assurance import assess_business_readiness
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
                "select run_id,request_id,status,result,result_digest,"
                "prompt_ciphertext,created_at from "
                "ofc_private.copilot_messages where session_id=%s "
                "order by created_at,run_id", (session_id,),
            ).fetchall()
        outputs = []
        for run_id, request_id, status, raw, digest, encrypted_prompt, created_at in rows:
            try:
                prompt = self.vault.cipher.decrypt(
                    bytes(encrypted_prompt)
                ).decode("utf-8")
            except (InvalidToken, ValueError, UnicodeError):
                raise StorageIntegrityError("invalid saved user prompt") from None
            if status == "RESERVED":
                outputs.append({
                    "prompt": prompt,
                    **self._pending_status(run_id, created_at),
                })
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
            outputs.append({"prompt": prompt, **result})
        return {"session_id": session_id, "messages": outputs}

    def _cached_message(self, session_id: str, request_id: str, prompt_digest: str):
        """Previously saved answers remain readable even after key revocation."""
        with self.store._connect() as db:
            row = db.execute(
                "select run_id,prompt_digest,status,result,result_digest,created_at "
                "from ofc_private.copilot_messages "
                "where session_id=%s and request_id=%s",
                (session_id, request_id),
            ).fetchone()
        if row is None:
            return None
        if row[1] != prompt_digest:
            raise SessionDenied("MESSAGE_IDEMPOTENCY_CONFLICT")
        if row[2] == "RESERVED":
            return self._pending_status(row[0], row[5])
        try:
            value = json.loads(row[3])
        except (ValueError, TypeError) as exc:
            raise StorageIntegrityError("invalid saved message") from exc
        if (
            not isinstance(value, dict) or value.get("run_id") != row[0]
            or digest_obj(value) != row[4]
        ):
            raise StorageIntegrityError("saved message digest mismatch")
        return value

    def _admit_message(self, session_id: str, request_id: str,
                       prompt_digest: str, model_id: str, prompt: str) -> tuple[str, dict | None]:
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
                "(run_id,session_id,request_id,prompt_digest,"
                "prompt_ciphertext,model_id,status) "
                "values(%s,%s,%s,%s,%s,%s,'RESERVED')",
                (run_id, session_id, request_id, prompt_digest,
                 self.vault.cipher.encrypt(prompt.encode("utf-8")), model_id),
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
            model = db.execute(
                "select llm_attempts from ofc_private.copilot_messages "
                "where run_id=%s", (run_id,),
            ).fetchone()
        if model is None:
            raise StorageIntegrityError("missing Copilot request admission")
        return {
            "qloo_search_requests": searches,
            "qloo_rank_requests": insights,
            "qloo_requests_this_run": searches + insights,
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

    def submit(self, *, session_id: str, ticket: str,
               request_id: str, prompt: str) -> dict:
        self._check_session(session_id, ticket)
        digest = self._validate_message(request_id, prompt)
        cached = self._cached_message(session_id, request_id, digest)
        if cached is not None:
            return cached
        self._require_credentials()
        model = self._model()
        run_id, cached = self._admit_message(
            session_id, request_id, digest, model.model, prompt,
        )
        if cached is not None:
            return cached
        try:
            reserve_model_call(
                self.store, daily_limit=self.llm_daily_limit, run_id=run_id,
            )
            draft = model.extract(prompt)
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
            supported = any(
                choice.domain.lower() in ("brand", "partner", "brand_partner")
                for choice in draft.choices
            )
            if not supported:
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
            if not draft.artist_name:
                return self._needs_input(
                    run_id, draft, model.model,
                    status="CLARIFICATION_REQUIRED",
                    reason="ARTIST_NAME_REQUIRED",
                    question="Which artist should Qloo use for cultural affinity?",
                )

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
            requirements = {}
            for choice in draft.choices:
                if choice.domain.lower() in ("brand", "partner", "brand_partner"):
                    requirements[choice.decision_id] = (
                        "budget_feasibility", "contract_rights", "local_audience",
                    )
                elif choice.domain.lower() in ("venue", "place", "restaurant"):
                    requirements[choice.decision_id] = ("venue_availability",)
            business_review = assess_business_readiness(
                reviewed, required=requirements,
            )
            pair = build_copilot_pair(
                prompt=prompt, model_id=model.model,
                baseline=baseline, reviewed=reviewed, evaluated=evaluated,
            )
            reserve_model_call(
                self.store, daily_limit=self.llm_daily_limit, run_id=run_id,
            )
            words = model.narrate_pair(pair, prompt=prompt)
            result = {
                **self._public_pair(pair, draft, brands, artist),
                "schema_version": "copilot.v1",
                "session_id": session_id,
                "request_id": request_id,
                "journal_head": evaluated["journal_head"],
                "revision": evaluated["revision"],
                "rank_evidence_rechecked": evaluated["rank_evidence_rechecked"],
                "full_plan_independently_verified": False,
                "business_verification": business_review,
                "plan": self._draft_plan_projection(draft),
                "status": "REVIEW_REQUIRED",
                "original_answer": words.off_answer,
                "ofc_answer": words.on_answer,
                **self._run_usage(run_id),
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
        model = statuses["gemini"]
        if model["status"] == "NOT_CONFIGURED":
            model = statuses["groq"]
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
                observation = QlooProbeClient(
                    api_key=credential, transport=self.qloo_transport,
                ).probe(search_probe(
                    "Taylor Swift", entity_type="urn:entity:artist", take=2,
                ))
                body = observation.payload
                entities = (
                    body.get("results", {}).get("entities")
                    if isinstance(body, dict)
                    and isinstance(body.get("results"), dict)
                    and body.get("success") is True
                    else None
                )
                if (
                    not isinstance(entities, list) or not entities
                    or not any(
                        isinstance(entity, dict)
                        and isinstance(entity.get("entity_id"), str)
                        and isinstance(entity.get("name"), str)
                        and entity.get("entity_id")
                        and entity.get("name")
                        for entity in entities
                    )
                ):
                    raise QlooProbeError("QLOO_SEARCH_RESPONSE_INVALID")
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

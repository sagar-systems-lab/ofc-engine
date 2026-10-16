"""Durable local Copilot transaction: one request, one shared Qloo sample, two views."""
from __future__ import annotations

from hashlib import sha256
import json
import re
from typing import Callable

from .copilot_compare import build_copilot_pair
from .copilot_intake import draft_plan, EntityResolutionRequired
from .intake import freeze_proposal
from .copilot_llm import CopilotLLM
from .copilot_resolver import normalize_qloo_search
from .dynamic import DynamicQlooReview
from .durable import SQLiteRevisionStore, StorageIntegrityError
from .qloo import ProbeObservation, search_probe
from .revisions import digest_obj

_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$")


class CopilotRunConflict(ValueError):
    pass


class CopilotRunReserved(RuntimeError):
    pass


class LocalCopilotService:
    """Private deterministic backend; public admission belongs to the hosted layer."""

    def __init__(
        self, *, llm: CopilotLLM, engine: DynamicQlooReview,
        search: Callable[[str, str], ProbeObservation],
    ):
        if not isinstance(llm, CopilotLLM) or not isinstance(engine, DynamicQlooReview):
            raise TypeError("configured LLM and OFC reviewer required")
        if not isinstance(engine.store, SQLiteRevisionStore):
            raise TypeError("local workflow requires SQLite durability")
        if not callable(search):
            raise TypeError("budgeted search transport required")
        self.llm = llm
        self.engine = engine
        self.search = search
        with self.engine.store._connect() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS copilot_reviews (
                    run_id TEXT PRIMARY KEY,
                    client_request_id TEXT NOT NULL,
                    prompt_digest TEXT NOT NULL,
                    model_id TEXT NOT NULL,
                    state TEXT NOT NULL CHECK(state IN ('RESERVED','COMPLETE')),
                    projection TEXT,
                    projection_digest TEXT
                );
            """)

    def _reserve(self, run_id: str, request_id: str, prompt_digest: str):
        db = self.engine.store._connect()
        try:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute(
                "SELECT client_request_id,prompt_digest,model_id,state,"
                "projection,projection_digest FROM copilot_reviews WHERE run_id=?",
                (run_id,),
            ).fetchone()
            if row is not None:
                if (
                    row[0] != request_id or row[1] != prompt_digest
                    or row[2] != self.llm.model
                ):
                    raise CopilotRunConflict("run ID already belongs to another task")
                if row[3] != "COMPLETE":
                    db.execute("COMMIT")
                    raise CopilotRunReserved("existing workflow has no completed result")
                try:
                    result = json.loads(row[4])
                except (ValueError, TypeError) as exc:
                    raise StorageIntegrityError("invalid stored Copilot projection") from exc
                if (
                    not isinstance(result, dict)
                    or digest_obj(result) != row[5]
                    or result.get("run_id") != run_id
                    or result.get("model_id") != self.llm.model
                    or result.get("prompt_digest") != prompt_digest
                ):
                    raise StorageIntegrityError("stored Copilot scope or digest mismatch")
                db.execute("COMMIT")
                return result
            db.execute(
                "INSERT INTO copilot_reviews "
                "(run_id,client_request_id,prompt_digest,model_id,state) "
                "VALUES(?,?,?,?,'RESERVED')",
                (run_id, request_id, prompt_digest, self.llm.model),
            )
            db.execute("COMMIT")
            return None
        except BaseException:
            if db.in_transaction:
                db.execute("ROLLBACK")
            raise
        finally:
            db.close()

    def _commit(self, run_id: str, result: dict):
        db = self.engine.store._connect()
        try:
            db.execute("BEGIN IMMEDIATE")
            record = json.dumps(result, separators=(",", ":"), sort_keys=True)
            updated = db.execute(
                "UPDATE copilot_reviews SET state='COMPLETE',projection=?,"
                "projection_digest=? WHERE run_id=? AND state='RESERVED'",
                (record, digest_obj(result), run_id),
            )
            if updated.rowcount != 1:
                raise StorageIntegrityError("Copilot reservation lost")
            db.execute("COMMIT")
        except BaseException:
            if db.in_transaction:
                db.execute("ROLLBACK")
            raise
        finally:
            db.close()

    def submit(self, *, run_id: str, request_id: str, prompt: str) -> dict:
        if (
            not isinstance(run_id, str) or not _ID.fullmatch(run_id)
            or not isinstance(request_id, str) or not _ID.fullmatch(request_id)
            or not isinstance(prompt, str) or not 1 <= len(prompt.strip()) <= 4000
        ):
            raise ValueError("invalid bounded request")
        digest = sha256(prompt.strip().encode()).hexdigest()
        cached = self._reserve(run_id, request_id, digest)
        if cached is not None:
            return cached

        draft = self.llm.extract(prompt)
        if draft.clarification_question is not None and not draft.choices:
            result = {
                "status": "CLARIFICATION_REQUIRED",
                "run_id": run_id,
                "model_id": self.llm.model,
                "prompt_digest": digest,
                "question": draft.clarification_question,
                "llm_enabled": True,
                "qloo_source": "NOT_QUERIED",
                "ofc_enabled": False,
                "release_authorized": False,
            }
            self._commit(run_id, result)
            return result

        if draft.artist_name is None:
            raise EntityResolutionRequired("QLOO_ARTIST_NAME_REQUIRED")

        def resolve(name: str, kind: str):
            spec = search_probe(name, entity_type="urn:entity:" + kind)
            # The supplied search callback MUST reserve its provider quota before I/O.
            captured = self.search(name, kind)
            return normalize_qloo_search(
                name, entity_type=kind, spec=spec, observation=captured,
            )

        artist = resolve(draft.artist_name, "artist")
        brands = {}
        for choice in draft.choices:
            if choice.domain.lower() in ("brand", "partner", "brand_partner"):
                for name in choice.candidate_names:
                    if name not in brands:
                        brands[name] = resolve(name, "brand")

        proposal = draft_plan(
            draft, run_id=run_id, request_id=request_id,
            resolved_artist=artist, resolved_brands=brands,
        )
        evaluated = self.engine.evaluate(proposal)
        baseline, _ = freeze_proposal(proposal)
        reviewed = self.engine.store.get(run_id)
        pair = build_copilot_pair(
            prompt=prompt, model_id=self.llm.model,
            baseline=baseline, reviewed=reviewed, evaluated=evaluated,
        )
        narration = self.llm.narrate_pair(pair, prompt=prompt)
        result = {
            **pair.as_dict(),
            "status": "REVIEW_REQUIRED",
            "original_answer": narration.off_answer,
            "ofc_answer": narration.on_answer,
            "qloo_status": pair.off["qloo"]["source_mode"],
            "qloo_requests_this_run": evaluated["new_qloo_requests"],
            "release_authorized": False,
            "external_action_executed": False,
        }
        self._commit(run_id, result)
        return result

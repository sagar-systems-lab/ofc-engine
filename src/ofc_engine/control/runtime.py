from __future__ import annotations

import logging
import secrets
from threading import Event, Thread, Lock
from time import monotonic_ns

from .engine import review
from .storage import Conflict, ControlStore

_log = logging.getLogger(__name__)


class ControlRuntime:
    def __init__(
        self,
        store: ControlStore,
        *,
        authorities=None,
        workers=2,
        capture_signer=None,
        capture_issuer=None,
        delegations=(),
        approval_keys=None,
        acquisition=None,
        projection_signer=None,
        tenant_weights=None,
    ):
        if not 1 <= workers <= 4:
            raise ValueError("worker limit is 1–4")
        self.store, self.authorities = store, authorities or {}
        self.delegations, self.approval_keys, self.acquisition = (
            tuple(delegations),
            approval_keys or {},
            acquisition,
        )
        weights = tenant_weights or {}
        if any(type(v) is not int or not 1 <= v <= 10 for v in weights.values()):
            raise ValueError("tenant weights must be integers in 1..10")
        self.store.tenant_weights = weights
        self.projection_signer = projection_signer
        self.store.delegations = self.delegations
        self.store.collection_keys = getattr(acquisition, "public_keys", {})
        self.capture_signer, self.capture_issuer = capture_signer, capture_issuer
        if (capture_signer is None) != (capture_issuer is None):
            raise ValueError("capture signer and issuer must be configured together")
        if capture_signer is not None:
            authority = self.authorities.get(capture_issuer)
            if (
                authority is None
                or authority.source != "qloo"
                or authority.tenant_id != "copilot"
                or authority.public_key
                != capture_signer.public_key().public_bytes_raw()
            ):
                raise ValueError(
                    "capture signer must match a pinned Qloo adapter authority"
                )
        self._temperature_lock, self._has_run = Lock(), False
        self.worker_count, self._stop, self._threads = workers, Event(), []

    def import_proposal(
        self, proposal, source_store, *, actor_id, labels=None, source_trace=None
    ):
        from .adapters import from_proposal, saved_qloo_evidence
        from .models import ReviewRequest

        plan = from_proposal(
            proposal, tenant_id="copilot", actor_id=actor_id, labels=labels
        )
        evidence = (
            saved_qloo_evidence(
                plan,
                proposal,
                source_store,
                issuer=self.capture_issuer,
                signing_key=self.capture_signer,
            )
            if self.capture_signer
            else ()
        )
        receipt = self.store.admit(
            ReviewRequest(plan=plan, evidence=evidence, mode="PRIVATE")
        )
        if source_trace is not None:
            self.store.save_trace(
                receipt["review_id"],
                source_trace.export() | {"otlp": source_trace.otlp()},
            )
        return receipt | {
            "capture_status": "SIGNED_CAPTURE_REPLAY"
            if evidence
            else "NO_QUALIFIED_CAPTURE",
            "additional_provider_calls": 0,
        }

    def run_once(self, *, owner=None, checkpoint=None, finalize_errors=False):
        started = monotonic_ns()
        job = self.store.claim(owner or "worker-" + secrets.token_hex(8))
        if job is None:
            return False
        lease_stop = Event()

        def heartbeat():
            while not lease_stop.wait(5):
                if not self.store.renew(job):
                    break

        lease_thread = Thread(target=heartbeat, name="ofc-review-lease", daemon=True)
        lease_thread.start()
        try:
            from .telemetry import Trace

            with self._temperature_lock:
                host_class = "warm" if self._has_run else "cold"
                self._has_run = True
            upstream_trace = None
            try:
                upstream_trace = self.store.trace(
                    job["review_id"], job["tenant_id"], job["actor_id"]
                )
            except Conflict:
                pass
            trace = Trace(
                job["review_id"],
                simulation=not any(
                    not b.adapter.offline
                    for b in getattr(self.acquisition, "bindings", ())
                ),
                host_class=host_class,
            )
            with trace.span("queue"):
                pass
            with self.store._tx() as db:
                created = self.store._run(
                    db,
                    f"select created_at from {self.store.prefix}reviews where review_id=?",
                    (job["review_id"],),
                ).fetchone()[0]
            trace.spans[-1]["duration_us"] = max(
                0, int((self.store.clock() - created) * 1000000)
            )
            request = self.store.request(
                job["review_id"], job["tenant_id"], job["actor_id"]
            )
            for state in ("FROZEN", "EVALUATING"):
                job = self.store.advance(
                    job, state, elapsed_ms=(monotonic_ns() - started) // 1_000_000
                )
                if checkpoint:
                    checkpoint(state, job)
            graph_collection = self.acquisition is not None and hasattr(
                self.acquisition, "collect_round"
            )
            if self.acquisition is not None:
                job = self.store.advance(
                    job, "SEARCHING", elapsed_ms=(monotonic_ns() - started) // 1_000_000
                )
                if checkpoint:
                    checkpoint("SEARCHING", job)
                if not graph_collection:
                    request = request.model_copy(
                        update={
                            "evidence": self.acquisition.collect(
                                request.plan, request.evidence, as_of=request.as_of
                            )
                        }
                    )

            def source_checkpoint(kind, payload):
                nonlocal job
                from .collection import CoordinatorStopped

                try:
                    job = self.store.note(job, kind, payload)
                except Conflict as exc:
                    raise CoordinatorStopped("COLLECTION_COORDINATOR_STOPPED") from exc
                if checkpoint:
                    checkpoint(kind, job)

            def reconcile(plan, evidence, number, as_of):
                with trace.span("recheck", parent=controller_span) as recheck_span:
                    values, at, states = self.acquisition.collect_round(
                        job["review_id"],
                        plan,
                        evidence,
                        anchor_digest=request.plan.digest,
                        round_index=number,
                        as_of=as_of,
                        trace=trace,
                        parent=recheck_span,
                        checkpoint=source_checkpoint,
                        frozen_as_of=request.as_of,
                    )
                source_checkpoint(
                    "EVIDENCE_RECONCILED", {"round": number, "states": states}
                )
                return values, at

            def commit(number, step):
                nonlocal job
                with trace.span("cas", parent=controller_span):
                    job = self.store.commit_round(
                        job, number, step, authorities=self.authorities
                    )
                if checkpoint:
                    checkpoint("ROUND_COMMITTED", job)

            with trace.span("controller") as controller_span:
                proof = review(
                    request,
                    authorities=self.authorities,
                    on_round=commit,
                    delegations=self.delegations,
                    reconcile=reconcile if graph_collection else None,
                    trace=trace,
                    parent=controller_span,
                )
            if graph_collection:
                from .collection import collected_record

                proof = collected_record(
                    proof, self.acquisition.record(job["review_id"], request.plan)
                )
            if proof["patches"]:
                for state in ("REPAIR_PROPOSED", "RECHECKING"):
                    job = self.store.advance(
                        job, state, elapsed_ms=(monotonic_ns() - started) // 1_000_000
                    )
                    if checkpoint:
                        checkpoint(state, job)
            job = self.store.advance(
                job, "HANDOFF_READY", elapsed_ms=(monotonic_ns() - started) // 1_000_000
            )
            if checkpoint:
                checkpoint("HANDOFF_READY", job)
            with trace.span("proof"):
                self.store.finish(job, proof, authorities=self.authorities)
            self.store.save_trace(
                job["review_id"],
                trace.export() | {"otlp": trace.otlp(), "upstream": upstream_trace},
            )
            return True
        except Exception:
            if finalize_errors:
                try:
                    self.store.fail(job)
                except Conflict:
                    pass
            raise
        finally:
            lease_stop.set()
            lease_thread.join(timeout=0.5)

    def start(self):
        if self._threads:
            return
        self._stop.clear()
        self.store.recover_expired()
        for index in range(self.worker_count):

            def loop(worker=index):
                while not self._stop.is_set():
                    try:
                        if not self.run_once(
                            owner=f"worker-{worker}-{secrets.token_hex(4)}",
                            finalize_errors=True,
                        ):
                            self._stop.wait(0.2)
                        self.store.recover_expired()
                    except Conflict:
                        self._stop.wait(0.1)
                    except Exception as exc:
                        # A lost lease is reconciled; provider work is never automatically reissued.
                        _log.error("CONTROL_WORKER_FAILURE type=%s", type(exc).__name__)
                        self._stop.wait(0.5)

            thread = Thread(target=loop, name=f"ofc-control-{index}", daemon=True)
            thread.start()
            self._threads.append(thread)

    def close(self):
        self._stop.set()
        for thread in self._threads:
            thread.join(timeout=3)
        self._threads.clear()

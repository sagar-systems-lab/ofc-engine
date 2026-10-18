from __future__ import annotations

import logging
import secrets
from threading import Event, Thread
from time import monotonic_ns

from .engine import review
from .storage import Conflict, ControlStore

_log = logging.getLogger(__name__)


class ControlRuntime:
    def __init__(self, store: ControlStore, *, authorities=None, workers=2,
                 capture_signer=None, capture_issuer=None):
        if not 1 <= workers <= 4:
            raise ValueError("worker limit is 1–4")
        self.store, self.authorities = store, authorities or {}
        self.capture_signer, self.capture_issuer = capture_signer, capture_issuer
        if (capture_signer is None) != (capture_issuer is None):
            raise ValueError("capture signer and issuer must be configured together")
        if capture_signer is not None:
            authority = self.authorities.get(capture_issuer)
            if (authority is None or authority.source != "qloo" or authority.tenant_id != "copilot"
                    or authority.public_key != capture_signer.public_key().public_bytes_raw()):
                raise ValueError("capture signer must match a pinned Qloo adapter authority")
        self.worker_count, self._stop, self._threads = workers, Event(), []

    def import_proposal(self, proposal, source_store, *, actor_id, labels=None):
        from .adapters import from_proposal, saved_qloo_evidence
        from .models import ReviewRequest
        plan = from_proposal(proposal, tenant_id="copilot", actor_id=actor_id, labels=labels)
        evidence = saved_qloo_evidence(plan, proposal, source_store, issuer=self.capture_issuer,
                                       signing_key=self.capture_signer) if self.capture_signer else ()
        receipt = self.store.admit(ReviewRequest(plan=plan, evidence=evidence, mode="PRIVATE"))
        return receipt | {"capture_status": "SIGNED_CAPTURE_REPLAY" if evidence else "NO_QUALIFIED_CAPTURE",
                          "additional_provider_calls": 0}

    def run_once(self, *, owner=None, checkpoint=None):
        started = monotonic_ns()
        job = self.store.claim(owner or "worker-" + secrets.token_hex(8))
        if job is None:
            return False
        request = self.store.request(job["review_id"], job["tenant_id"], job["actor_id"])
        for state in ("FROZEN", "EVALUATING"):
            job = self.store.advance(job, state, elapsed_ms=(monotonic_ns() - started) // 1_000_000)
            if checkpoint:
                checkpoint(state, job)
        proof = review(request, authorities=self.authorities)
        if proof["patches"]:
            for state in ("REPAIR_PROPOSED", "RECHECKING"):
                job = self.store.advance(job, state, elapsed_ms=(monotonic_ns() - started) // 1_000_000)
                if checkpoint:
                    checkpoint(state, job)
        job = self.store.advance(job, "HANDOFF_READY", elapsed_ms=(monotonic_ns() - started) // 1_000_000)
        if checkpoint:
            checkpoint("HANDOFF_READY", job)
        self.store.finish(job, proof, authorities=self.authorities)
        return True

    def start(self):
        if self._threads:
            return
        self._stop.clear()
        self.store.recover_expired()
        for index in range(self.worker_count):
            def loop(worker=index):
                while not self._stop.is_set():
                    try:
                        if not self.run_once(owner=f"worker-{worker}-{secrets.token_hex(4)}"):
                            self._stop.wait(.2)
                        self.store.recover_expired()
                    except Conflict:
                        self._stop.wait(.1)
                    except Exception as exc:
                        # A lost lease is reconciled; provider work is never automatically reissued.
                        _log.error("CONTROL_WORKER_FAILURE type=%s", type(exc).__name__)
                        self._stop.wait(.5)
            thread = Thread(target=loop, name=f"ofc-control-{index}", daemon=True)
            thread.start()
            self._threads.append(thread)

    def close(self):
        self._stop.set()
        for thread in self._threads:
            thread.join(timeout=3)
        self._threads.clear()

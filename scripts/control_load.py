"""A bounded offline queue/worker profile; output never describes live provider latency."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path
from tempfile import TemporaryDirectory

from ofc_engine.control.fixtures import cascading_campaign
from ofc_engine.control.models import ReviewRequest
from ofc_engine.control.runtime import ControlRuntime
from ofc_engine.control.storage import ControlStore
from ofc_engine.control.telemetry import summarize


def run(count=12):
    if not 1 <= count <= 64:
        raise ValueError("bounded load required")
    with TemporaryDirectory(prefix="ofc-load-") as directory:
        store = ControlStore(Path(directory) / "load.db")
        receipts = []
        for i in range(count):
            req = cascading_campaign().data()
            req["plan"]["idempotency_key"] = f"load-{i}"
            parsed = ReviewRequest.parse(req)
            for item in req["evidence"]:
                item["plan_digest"] = parsed.plan.digest
            receipts.append(store.admit(ReviewRequest.parse(req)))
        runtime = ControlRuntime(store)
        with ThreadPoolExecutor(max_workers=2) as workers:
            list(workers.map(lambda _: runtime.run_once(), range(count)))
        traces = [
            store.trace(r["review_id"], "sandbox", "campaign-agent") for r in receipts
        ]
        return {
            "schema": "ofc.load-profile/1",
            "scope": "LOCAL_CONTROLLER",
            "n": count,
            "metrics": summarize(traces),
            "provider_calls": 0,
            "spend_minor": 0,
            "live_provider_latency": "NOT_MEASURED",
        }


if __name__ == "__main__":
    print(json.dumps(run(), sort_keys=True))

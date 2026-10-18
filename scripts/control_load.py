"""A bounded offline queue/worker profile; output never describes live provider latency."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
import argparse
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Lock
from time import sleep
from urllib.parse import parse_qs, urlsplit

from cryptography.fernet import Fernet
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from ofc_engine.control.fixtures import cascading_campaign
from ofc_engine.control.models import ReviewRequest
from ofc_engine.control.runtime import ControlRuntime
from ofc_engine.control.storage import ControlStore
from ofc_engine.control.telemetry import summarize


def run(count=12, *, sources=False):
    if not 1 <= count <= 64:
        raise ValueError("bounded load required")
    with TemporaryDirectory(prefix="ofc-load-") as directory:
        store = ControlStore(
            Path(directory) / "load.db", encryption_key=Fernet.generate_key()
        )
        acquisition, authorities, transport = None, {}, None
        if sources:
            from ofc_engine.control.collection import GraphCollector, SourceBinding
            from ofc_engine.control.collection_contracts import CollectionGrant
            from ofc_engine.control.feeds import QlooGraphSource
            from ofc_engine.control.policies import Authority

            class DelayedSource:
                offline = True

                def __init__(self):
                    self.requests, self.lock = 0, Lock()

                def request(self, method, path, payload, *, timeout):
                    with self.lock:
                        self.requests += 1
                    sleep(min(0.005, timeout))
                    pool = parse_qs(urlsplit(path).query)["filter.results.entities"][
                        0
                    ].split(",")
                    return 200, json.dumps(
                        {
                            "success": True,
                            "results": {
                                "entities": [
                                    {
                                        "entity_id": entity,
                                        "query": {"affinity": len(pool) - i},
                                    }
                                    for i, entity in enumerate(sorted(pool))
                                ]
                            },
                        }
                    ).encode()

            key, journal = Ed25519PrivateKey.generate(), Ed25519PrivateKey.generate()
            expiry = (
                (datetime.now(timezone.utc) + timedelta(days=1))
                .isoformat()
                .replace("+00:00", "Z")
            )
            transport = DelayedSource()
            authorities = {
                "load-source": Authority(
                    "load-source",
                    "sandbox",
                    key.public_key().public_bytes_raw(),
                    QlooGraphSource.claims,
                    "qloo",
                    expiry,
                    provider_account_scope="load-account",
                    entitlement_id="load-free",
                )
            }
            grant = CollectionGrant(
                id="load-grant",
                tenant_id="sandbox",
                provider="qloo",
                account_scope="load-account",
                entitlement_id="load-free",
                claims=tuple(QlooGraphSource.claims),
                expires_at=expiry,
                rpm=1000,
                rpd=10000,
                total=10000,
                per_review=128,
                concurrency=2,
            )
            acquisition = GraphCollector(
                store,
                [
                    SourceBinding(
                        grant,
                        QlooGraphSource(
                            transport, issuer="load-source", signing_key=key
                        ),
                    )
                ],
                authorities=authorities,
                issuer="load-journal",
                signing_key=journal,
            )
        receipts = []
        for i in range(count):
            req = cascading_campaign().data()
            req["plan"]["idempotency_key"] = f"load-{i}"
            parsed = ReviewRequest.parse(req)
            for item in req["evidence"]:
                item["plan_digest"] = parsed.plan.digest
            if sources:
                req["evidence"], req["mode"] = [], "PRIVATE"
                req.pop("as_of", None)
            receipts.append(store.admit(ReviewRequest.parse(req)))
        runtime = ControlRuntime(
            store, authorities=authorities, acquisition=acquisition
        )
        with ThreadPoolExecutor(max_workers=2) as workers:
            list(workers.map(lambda _: runtime.run_once(), range(count)))
        traces = [
            store.trace(r["review_id"], "sandbox", "campaign-agent") for r in receipts
        ]
        return {
            "schema": "ofc.load-profile/1",
            "scope": "OFFLINE_DELAYED_SOURCE" if sources else "LOCAL_CONTROLLER",
            "n": count,
            "metrics": summarize(traces),
            "provider_calls": 0,
            "spend_minor": 0,
            "live_provider_latency": "NOT_MEASURED",
            "source_transport_requests": transport.requests if transport else 0,
        }


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--count", type=int, default=12)
    parser.add_argument("--sources", action="store_true")
    args = parser.parse_args()
    print(json.dumps(run(args.count, sources=args.sources), sort_keys=True))

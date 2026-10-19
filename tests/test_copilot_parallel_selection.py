"""Selected Qloo entity lookups retain scope while sharing a bounded worker pool."""
from threading import Barrier

import pytest

from ofc_engine.copilot_hosted import HostedCopilot
from ofc_engine.copilot_intake import EntityResolutionRequired, ResolvedEntity


def test_selected_lookup_parallel_and_consumed_once():
    barrier = Barrier(2, timeout=5)
    looked_up = []

    class Resolver:
        def __init__(self):
            self.selection_consumed = False

        def resolve(self, *, run_id, name, kind, selection_ref):
            assert run_id == "offline-run"
            assert kind == "brand" and selection_ref == "a" * 16
            looked_up.append(name)
            barrier.wait()
            self.selection_consumed = name == "Levi's"
            return ResolvedEntity(
                name=name,
                entity_id="id-" + name,
                entity_type="urn:entity:brand",
                query_fingerprint="e" * 64,
            )

    result, consumed = HostedCopilot._parallel_brand_searches(
        ["Zara", "Levi's"], run_id="offline-run",
        selection_ref="a" * 16, resolver_factory=Resolver,
    )
    assert consumed is True
    assert list(result) == ["Zara", "Levi's"]
    assert set(looked_up) == {"Zara", "Levi's"}
    assert result["Zara"].name == "Zara"


def test_multiple_consumed_matches_fail_closed():
    class Resolver:
        def __init__(self):
            self.selection_consumed = False

        def resolve(self, *, run_id, name, kind, selection_ref):
            self.selection_consumed = True
            return ResolvedEntity(
                name=name, entity_id="id-" + name,
                entity_type="urn:entity:brand",
                query_fingerprint="e" * 64,
            )

    with pytest.raises(EntityResolutionRequired, match="QLOO_SELECTION_SCOPE_MISMATCH"):
        HostedCopilot._parallel_brand_searches(
            ["Zara", "Levi's"], run_id="offline-run",
            selection_ref="a" * 16, resolver_factory=Resolver,
        )

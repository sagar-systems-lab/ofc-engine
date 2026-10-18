import pytest
import ipaddress
import os
import socket
from ofc_engine.contracts import Decision, PlanSnapshot
from ofc_engine.revisions import digest_obj


@pytest.fixture(autouse=True)
def offline_provider_guard(monkeypatch):
    if os.environ.get("OFC_OFFLINE_TESTS") != "1":
        return
    original = socket.socket.connect

    def connect(sock, address):
        if isinstance(address, tuple):
            host = address[0]
            try:
                local = ipaddress.ip_address(host).is_loopback
            except ValueError:
                local = host == "localhost"
            if not local:
                raise AssertionError("external network is disabled in offline tests")
        return original(sock, address)

    monkeypatch.setattr(socket.socket, "connect", connect)

@pytest.fixture
def plan():
    return PlanSnapshot(
        plan_id="tour-001", revision=0,
        baseline_digest=digest_obj({"frozen":"baseline"}),
        constraints_digest=digest_obj({"budget_usd":60000}),
        decisions=(
            Decision(decision_id="brand",domain="partner",candidate_id="brand-a",candidate_pool=("brand-a","brand-b"),mutable_fields=("candidate_id",)),
            Decision(decision_id="afterparty",domain="experience",candidate_id="exp-a",candidate_pool=("exp-a",),depends_on=("brand",)),
            Decision(decision_id="venue",domain="venue",candidate_id="venue-a",candidate_pool=("venue-a",)),
        ),
    )

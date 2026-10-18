import json
from pathlib import Path
import shutil
import subprocess

import pytest

from ofc_engine.control.canonical import canonical_bytes, digest
from ofc_engine.control.fixtures import campaign
from ofc_engine.control.sdk import ControlClient
from test_control_http import TOKEN, finished, request, serve

ROOT = Path(__file__).resolve().parents[1]


def test_python_raw_http_and_javascript_clients_share_one_durable_receipt(tmp_path):
    if not shutil.which("node"):
        pytest.skip("Node is required for the JavaScript integration harness")
    with serve(tmp_path) as origin:
        client = ControlClient(origin, TOKEN)
        source = campaign()
        python_receipt = client.submit(source)
        code, raw, _ = request(origin, "/api/control/plans", source.data(), token=TOKEN)
        assert code == 202 and json.loads(raw) == python_receipt
        module = (ROOT / "src/ofc_engine/web/control-sdk.js").as_uri()
        script = f"import fs from 'node:fs'; import {{ControlClient}} from {json.dumps(module)};const client=new ControlClient({{origin:{json.dumps(origin)},token:{json.dumps(TOKEN)}}}); console.log(JSON.stringify(await client.submit(JSON.parse(fs.readFileSync(0,'utf8')))));"
        process = subprocess.run(
            ["node", "--input-type=module", "-e", script],
            input=canonical_bytes(source.data()),
            capture_output=True,
            timeout=10,
        )
        assert process.returncode == 0, process.stderr.decode()
        assert json.loads(process.stdout) == python_receipt
        finished(client, python_receipt["review_id"])
        proof = client.proof(python_receipt["review_id"])
        assert proof["impact"]["changed"] == ["B01"]


def test_python_javascript_canonical_digest_parity():
    if not shutil.which("node"):
        pytest.skip("Node is required for cross-language vectors")
    vectors = [
        {},
        {"z": -1, "a": "é🧭"},
        {"array": [None, True, False, 9007199254740991, 'line\nquote"']},
        {"a": {"z": 2, "nested": ["a", "b"]}},
    ]
    module = (ROOT / "src/ofc_engine/web/control-sdk.js").as_uri()
    script = f"import fs from 'node:fs'; import {{webcrypto}} from 'node:crypto'; globalThis.crypto ||= webcrypto; import {{canonicalStringify,contentDigest}} from {json.dumps(module)}; const values=JSON.parse(fs.readFileSync(0,'utf8')); console.log(JSON.stringify(await Promise.all(values.map(async v=>({{canonical:canonicalStringify(v),digest:await contentDigest(v)}})))));"
    result = subprocess.run(
        ["node", "--input-type=module", "-e", script],
        input=json.dumps(vectors).encode(),
        capture_output=True,
        timeout=10,
    )
    assert result.returncode == 0, result.stderr.decode()
    assert json.loads(result.stdout) == [
        {"canonical": canonical_bytes(v).decode(), "digest": digest(v)} for v in vectors
    ]


def test_distinct_agent_identities_share_only_explicitly_delegated_proof(tmp_path):
    from threading import Thread
    from urllib.error import HTTPError
    from ofc_engine.control.http import ControlAccess, Identity
    from ofc_engine.control.runtime import ControlRuntime
    from ofc_engine.control.storage import ControlStore
    from ofc_engine.control.ownership import Delegation
    from ofc_engine.hosted_preview import HostedFixtureServer

    source = campaign()
    runtime = ControlRuntime(ControlStore(tmp_path / "agents.db"))
    tokens = {
        ("agent-" + str(i) + "-") * 5: Identity("sandbox", actor)
        for i, actor in enumerate(("campaign-agent", "creative-agent", "budget-agent"))
    }
    server = HostedFixtureServer(
        ("127.0.0.1", 0),
        control=runtime,
        control_enabled=True,
        control_access=ControlAccess(tokens),
    )
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    origin = "http://127.0.0.1:" + str(server.server_port)
    keys = list(tokens)
    try:
        client = ControlClient(origin, keys[0])
        rid = client.submit(source)["review_id"]
        finished(client, rid)
        proof = client.proof(rid)
        with pytest.raises(HTTPError):
            ControlClient(origin, keys[1]).proof(rid)
        runtime.delegations = tuple(
            Delegation(
                "sandbox",
                "campaign-agent",
                actor,
                proof["current_digest"],
                frozenset(n.id for n in source.plan.nodes),
                frozenset({"read"}),
                "2026-12-01T00:00:00Z",
            )
            for actor in ("creative-agent", "budget-agent")
        )
        assert (
            ControlClient(origin, keys[1]).proof(rid)["proof_digest"]
            == proof["proof_digest"]
        )
        code, raw, _ = request(
            origin, "/api/control/reviews/" + rid + "/proof", token=keys[2]
        )
        assert code == 200 and json.loads(raw)["proof_digest"] == proof["proof_digest"]
        assert (
            request(origin, "/api/control/plans", source.data(), token=keys[1])[0]
            == 403
        )
        runtime.delegations = ()
        with pytest.raises(HTTPError):
            ControlClient(origin, keys[2]).proof(rid)
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=3)

from io import BytesIO
import json
import subprocess
import sys
import zipfile

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
import pytest

from ofc_engine.control.benchmark import BUILTIN_GOLD, evaluate_cases
from ofc_engine.control.engine import review
from ofc_engine.control.fixtures import campaign
from ofc_engine.control.release import build_bundle, verify_bundle


def test_reproducible_signed_verifier_bundle_and_key_pinning(tmp_path):
    key = Ed25519PrivateKey.generate()
    first = build_bundle("a" * 40, signing_key=key)
    assert first == build_bundle("a" * 40, signing_key=key)
    assert verify_bundle(first, public_key=key.public_key().public_bytes_raw())["signature_status"] == "ED25519_SIGNED"
    with pytest.raises(ValueError): verify_bundle(first)
    with pytest.raises(ValueError): verify_bundle(first, public_key=Ed25519PrivateKey.generate().public_key().public_bytes_raw())
    with pytest.raises(ValueError): verify_bundle(first, expected_sha256="0" * 64)
    bundle = zipfile.ZipFile(BytesIO(first))
    for name in bundle.namelist():
        target = tmp_path / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(bundle.read(name))
    proof = review(campaign())
    path = tmp_path / "proof.json"
    path.write_text(json.dumps(proof))
    completed = subprocess.run([sys.executable, str(tmp_path / "verify.py"), str(path), "--expected-digest", proof["proof_digest"]], capture_output=True, text=True)
    assert completed.returncode == 0, completed.stdout + completed.stderr
    assert json.loads(completed.stdout)["valid"]


def test_bundle_detects_changed_verifier_source():
    raw = build_bundle("a" * 40)
    source, output = zipfile.ZipFile(BytesIO(raw)), BytesIO()
    with zipfile.ZipFile(output, "w") as changed:
        for name in source.namelist():
            changed.writestr(name, source.read(name) + (b"\n# changed" if name.endswith("verifier.py") else b""))
    with pytest.raises(ValueError, match="digest"): verify_bundle(output.getvalue())


def test_missing_comparator_arms_are_reported_without_fabricated_scores():
    cases = [{"id": name, "request": campaign(name).data(), "gold": gold} for name, gold in BUILTIN_GOLD.items()]
    report = evaluate_cases(cases)
    assert report["comparators"]["controlled"]["passed"] == 5
    assert report["comparators"]["llm_only"]["measured"] == 0
    assert report["comparators"]["llm_only"]["missing"] == 5
    assert report["acceptance_index"] is None
    assert report["external_pilot"] == "NOT_RUN"
    assert report["provider_calls"] == 0

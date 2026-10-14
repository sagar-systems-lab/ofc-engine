"""Static submission release gate; does not publish or call Qloo."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
ASSETS = {
    "studio.html": "73b8a292021d82e93856351ff98ad8356d79bf24",
    "studio.css": "959cff030bf3e82f5a1c153d607e1b88aec002eb",
    "studio.js": "d13ff8d80206a4bb7b0c9cbe64a6e83e03aea39e",
    "product.js": "a5edd06c5424e34a6f216ed055a3dbe5915ed863",
    "optical.css": "75cd09c0b5a9682083759d905a778f5e104cb176",
    "calm.css": "3af084cbb9a24a1ff663cd657b704e62df01ba53",
    "recorded.html": "0f70d97c15e4edee425760bd6f6c52ff3c41e64a",
    "recorded.css": "f84fdac3f35e6df4a366792c152a403c35876ae4",
    "recorded.js": "fd8db2b8e5038077fb59cb9be75de133b5e3c2eb",
    "index.html": "b07532e8e3f2a503f50ff32b326b68d5c93eb49d",
    "proof-premium.css": "dcdfcf726200e962f701926c53b0d6fb7abbd04f",
}


def _git_blob_sha(data: bytes) -> str:
    return hashlib.sha1(b"blob " + str(len(data)).encode() + b"\0" + data).hexdigest()


def inspect(root: Path = ROOT) -> dict:
    assets = root / "src/ofc_engine/web"
    mismatches = []
    for name, expected in ASSETS.items():
        path = assets / name
        if not path.is_file() or _git_blob_sha(path.read_bytes()) != expected:
            mismatches.append(name)

    checks = {
        "frozen_ui_unchanged": not mismatches,
        "license_present": (root / "LICENSE").is_file()
        and (root / "LICENSE").read_text(encoding="utf-8").startswith("MIT License"),
        "qloo_adapter_present": all(
            (root / name).is_file()
            for name in (
                "src/ofc_engine/qloo.py",
                "src/ofc_engine/qloo_live_recheck.py",
                "src/ofc_engine/qloo_decision_gate.py",
                "src/ofc_engine/agent_workspace.py",
            )
        ),
        "deploy_stays_synthetic_and_keyless": (
            (root / "Dockerfile").is_file()
            and 'ofc_engine.hosted_preview' in (root / "Dockerfile").read_text(encoding="utf-8")
            and "QLOO_API_KEY" not in (root / "Dockerfile").read_text(encoding="utf-8")
            and "ofc_engine.agent_workspace" not in (root / "Dockerfile").read_text(encoding="utf-8")
        ),
        "render_requires_explicit_deploy": (
            (root / "render.yaml").is_file()
            and 'autoDeployTrigger: "off"' in (root / "render.yaml").read_text(encoding="utf-8")
        ),
        "submission_docs_present": all(
            (root / name).is_file()
            for name in ("docs/SUBMISSION.md", "docs/JUDGE_QUICKSTART.md")
        ),
    }
    return {
        "result": "PREPARATION_PASS" if all(checks.values()) else "BLOCKED",
        "checks": checks,
        "changed_locked_assets": mismatches,
        "repo_public_verified": False,
        "external_hosted_demo_verified": False,
        "hosted_qloo_decision_experience_verified": False,
        "source_history_secret_audit": "SEPARATE_CI_GATE",
        "provider_data_publication_approved": False,
        "submission_ready": False,
        "real_qloo_requests": 0,
    }


def main() -> int:
    result = inspect()
    print(json.dumps(result, sort_keys=True))
    return 0 if result["result"] == "PREPARATION_PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())

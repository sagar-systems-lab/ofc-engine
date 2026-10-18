from __future__ import annotations

import argparse
from hashlib import sha256
import json
import os
from pathlib import Path
import subprocess
from tempfile import TemporaryDirectory
import xml.etree.ElementTree as ET

from .canonical import digest, strict_json

CATEGORIES = {
    "contract": 60,
    "graph": 80,
    "evidence": 60,
    "repair": 60,
    "attacker": 60,
    "temporal_budget": 30,
    "workflow": 30,
    "agent": 30,
}
ORACLES = (
    "tests/test_control_acceptance.py",
    "tests/test_control_graph.py",
    "tests/test_control_oracles.py",
    "tests/test_control_evidence.py",
)


def manifest(root):
    root = Path(root)
    from .policies import POLICY_DIGEST

    cases = [
        {
            "id": f"{category}-{i:03}",
            "category": category,
            "oracle": f"tests/test_control_acceptance.py::test_registered_{category}[{i}]",
        }
        for category, count in CATEGORIES.items()
        for i in range(count)
    ]
    return {
        "schema": "ofc.acceptance-corpus/1",
        "scorer": "registered-junit/1",
        "development_count": 410,
        "policy_digest": POLICY_DIGEST,
        "holdout_minimum": 150,
        "holdout_status": "EXTERNALLY_SEALED_LABELS_REQUIRED",
        "categories": CATEGORIES,
        "source_digests": {
            p: sha256((root / p).read_bytes()).hexdigest() for p in ORACLES
        },
        "cases": cases,
    }


def run_registered(root, document, *, expected_digest):
    root = Path(root)
    if digest(document) != expected_digest or document != manifest(root):
        raise ValueError("pre-registered corpus drift")
    with TemporaryDirectory(prefix="ofc-acceptance-") as directory:
        report = Path(directory) / "result.xml"
        env = os.environ.copy()
        env["OFC_OFFLINE_TESTS"] = "1"
        env["PYTHONPATH"] = str(root / "src")
        result = subprocess.run(
            [
                __import__("sys").executable,
                "-m",
                "pytest",
                "-q",
                *[c["oracle"] for c in document["cases"]],
                f"--junitxml={report}",
            ],
            cwd=root,
            env=env,
            capture_output=True,
            timeout=300,
        )
        if not report.exists():
            raise ValueError("registered oracle report unavailable")
        cases = ET.parse(report).findall(".//testcase")
        counts = {"passed": 0, "failed": 0, "skipped": 0}
        for case in cases:
            counts[
                "failed"
                if case.find("failure") is not None or case.find("error") is not None
                else "skipped"
                if case.find("skipped") is not None
                else "passed"
            ] += 1
        if len(cases) != 410:
            raise ValueError("registered denominator mismatch")
        return {
            "schema": "ofc.acceptance-run/1",
            "corpus_digest": expected_digest,
            "scorer": "registered-junit/1",
            "sample_size": 410,
            **counts,
            "subprocess_exit": result.returncode,
            "provider_calls": 0,
            "spend_minor": 0,
            "independent_holdout": "NOT_RUN",
            "acceptance_index": None,
        }


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--expected-digest", required=True)
    parser.add_argument("--root", type=Path, default=Path.cwd())
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    result = run_registered(
        args.root,
        strict_json(args.manifest.read_bytes()),
        expected_digest=args.expected_digest,
    )
    raw = json.dumps(result, sort_keys=True, indent=2) + "\n"
    if args.output:
        args.output.write_text(raw)
    else:
        print(raw, end="")
    return int(result["failed"] > 0 or result["skipped"] > 0)


if __name__ == "__main__":
    raise SystemExit(main())

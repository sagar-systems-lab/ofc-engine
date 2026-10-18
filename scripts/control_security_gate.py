"""Offline source gates for control records, diagnostics and dependency licenses."""

from __future__ import annotations

import ast
import importlib.metadata
import json
from pathlib import Path
import tomllib

EXPECTED_LICENSES = {
    "pydantic": ("MIT",),
    "cryptography": ("Apache-2.0", "BSD-3-Clause"),
    "psycopg": ("LGPL-3.0",),
}


def check():
    root = Path(__file__).resolve().parents[1]
    issues = []
    for path in (root / "src/ofc_engine/control").glob("*.py"):
        tree = ast.parse(path.read_text())
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr
                in {"debug", "info", "warning", "error", "exception", "critical"}
            ):
                # Logs must be fixed diagnostics; exception bodies and input dictionaries stay private.
                if not node.args or not isinstance(node.args[0], ast.Constant):
                    issues.append(str(path.relative_to(root)) + ": dynamic diagnostic")
                for arg in node.args[1:]:
                    if not (isinstance(arg, ast.Attribute) and arg.attr == "__name__"):
                        issues.append(
                            str(path.relative_to(root))
                            + ": unapproved diagnostic argument"
                        )
    project = tomllib.loads((root / "pyproject.toml").read_text())
    if any(
        ">" not in requirement or "<" not in requirement
        for requirement in project["project"]["dependencies"]
    ):
        issues.append("dependency must declare lower and upper bounds")
    licenses = {}
    for package, expected in EXPECTED_LICENSES.items():
        meta = importlib.metadata.metadata(package)
        licenses[package] = {
            "version": meta["Version"],
            "license_expression": meta.get("License-Expression"),
            "classifiers": [
                x for x in meta.get_all("Classifier", []) if x.startswith("License ::")
            ],
            "expected_spdx": expected,
        }
        advertised = " ".join(
            [meta.get("License-Expression", ""), meta.get("License", "")]
            + meta.get_all("Classifier", [])
        )
        if not advertised or not any(
            token.split("-")[0].lower() in advertised.lower() for token in expected
        ):
            issues.append(package + ": expected license metadata unavailable")
    if issues:
        raise ValueError("; ".join(issues))
    return {
        "result": "PASS",
        "licenses": licenses,
        "provider_calls": 0,
        "network_requests": 0,
    }


if __name__ == "__main__":
    print(json.dumps(check(), sort_keys=True))

from __future__ import annotations

import argparse
import base64
from hashlib import sha256
from io import BytesIO
import json
from pathlib import Path
import re
import zipfile

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey,
    Ed25519PublicKey,
)

from .canonical import canonical_bytes, strict_json
from .policies import POLICY_DIGEST

_MODULES = (
    "canonical.py",
    "models.py",
    "graph.py",
    "policies.py",
    "engine.py",
    "verifier.py",
    "ownership.py",
    "collection_contracts.py",
    "feeds.py",
    "collection.py",
)


def build_bundle(
    source_revision: str, *, signing_key: Ed25519PrivateKey | None = None
) -> bytes:
    if not re.fullmatch(r"[0-9a-f]{40}", source_revision):
        raise ValueError("full source revision required")
    root = Path(__file__).parent
    files = {
        "src/ofc_engine/__init__.py": b"",
        "src/ofc_engine/control/__init__.py": b"",
        "verify.py": b"from pathlib import Path\nimport sys\nsys.path.insert(0,str(Path(__file__).parent/'src'))\nfrom ofc_engine.control.verifier import main\nraise SystemExit(main())\n",
        "requirements.txt": b"pydantic>=2.10,<3\ncryptography>=43,<47\n",
    }
    files.update(
        {
            "src/ofc_engine/control/" + name: (root / name).read_bytes()
            for name in _MODULES
        }
    )
    manifest = {
        "schema": "ofc.verifier-bundle/1",
        "source_revision": source_revision,
        "policy_digest": POLICY_DIGEST,
        "files": {name: sha256(raw).hexdigest() for name, raw in sorted(files.items())},
        "release_qualification": "SOFTWARE_ARTIFACT_ONLY",
        "external_action_authorized": False,
    }
    signature = (
        base64.b64encode(signing_key.sign(canonical_bytes(manifest))).decode()
        if signing_key
        else None
    )
    files["manifest.json"] = canonical_bytes(
        {
            "manifest": manifest,
            "signature": signature,
            "signature_status": "ED25519_SIGNED" if signature else "UNSIGNED",
        }
    )
    output = BytesIO()
    with zipfile.ZipFile(
        output, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=9
    ) as archive:
        for name, raw in sorted(files.items()):
            info = zipfile.ZipInfo(name, date_time=(1980, 1, 1, 0, 0, 0))
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = 0o100644 << 16
            archive.writestr(info, raw)
    return output.getvalue()


def verify_bundle(
    raw: bytes,
    *,
    public_key: bytes | None = None,
    expected_sha256: str | None = None,
    expected_policy_digest: str | None = None,
):
    if (
        len(raw) > 5_000_000
        or expected_sha256
        and sha256(raw).hexdigest() != expected_sha256
    ):
        raise ValueError("bundle anchor mismatch")
    with zipfile.ZipFile(BytesIO(raw)) as archive:
        names = archive.namelist()
        if (
            len(names) > 32
            or len(names) != len(set(names))
            or any(
                name.startswith("/") or ".." in name.split("/") or "\\" in name
                for name in names
            )
            or sum(item.file_size for item in archive.infolist()) > 5_000_000
            or any(
                (item.external_attr >> 16) & 0o170000 == 0o120000
                for item in archive.infolist()
            )
        ):
            raise ValueError("invalid bundle contents")
        record = strict_json(archive.read("manifest.json"))
        manifest = record["manifest"]
        expected_files = {
            "src/ofc_engine/__init__.py",
            "src/ofc_engine/control/__init__.py",
            "verify.py",
            "requirements.txt",
        }
        expected_files.update("src/ofc_engine/control/" + name for name in _MODULES)
        if (
            manifest.get("schema") != "ofc.verifier-bundle/1"
            or manifest.get("policy_digest")
            != (expected_policy_digest or POLICY_DIGEST)
            or manifest.get("release_qualification") != "SOFTWARE_ARTIFACT_ONLY"
            or manifest.get("external_action_authorized") is not False
            or (
                expected_policy_digest in (None, POLICY_DIGEST)
                and set(manifest["files"]) != expected_files
            )
            or (
                expected_policy_digest not in (None, POLICY_DIGEST)
                and expected_sha256 is None
            )
        ):
            raise ValueError("unsupported verifier manifest")
        if set(manifest["files"]) != set(names) - {"manifest.json"}:
            raise ValueError("bundle file set mismatch")
        for name, expected in manifest["files"].items():
            if sha256(archive.read(name)).hexdigest() != expected:
                raise ValueError("bundle file digest mismatch")
        if public_key is not None and record["signature_status"] != "ED25519_SIGNED":
            raise ValueError("signed verifier bundle required")
        if record["signature_status"] == "ED25519_SIGNED":
            if public_key is None:
                raise ValueError("independent public key required")
            try:
                Ed25519PublicKey.from_public_bytes(public_key).verify(
                    base64.b64decode(record["signature"], validate=True),
                    canonical_bytes(manifest),
                )
            except (InvalidSignature, ValueError) as exc:
                raise ValueError("bundle signature mismatch") from exc
        elif (
            record["signature_status"] != "UNSIGNED" or record["signature"] is not None
        ):
            raise ValueError("invalid signature declaration")
        return {
            "valid": True,
            "sha256": sha256(raw).hexdigest(),
            "signature_status": record["signature_status"],
            "external_action_authorized": False,
        }


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Build a reproducible offline proof-verifier bundle"
    )
    parser.add_argument("--source-revision", required=True)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--signing-key-file", type=Path)
    args = parser.parse_args(argv)
    key = (
        Ed25519PrivateKey.from_private_bytes(
            base64.b64decode(args.signing_key_file.read_bytes().strip(), validate=True)
        )
        if args.signing_key_file
        else None
    )
    raw = build_bundle(args.source_revision, signing_key=key)
    args.output.write_bytes(raw)
    print(
        json.dumps(
            {
                "sha256": sha256(raw).hexdigest(),
                "signature_status": "ED25519_SIGNED" if key else "UNSIGNED",
            }
        )
    )


if __name__ == "__main__":
    main()

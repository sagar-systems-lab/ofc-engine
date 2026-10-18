from __future__ import annotations


class VerifierRegistry:
    def __init__(self):
        self._verifiers = {}

    def register(self, policy_digest, verifier):
        if (
            policy_digest in self._verifiers
            or len(policy_digest) != 64
            or not callable(verifier)
        ):
            raise ValueError("invalid retained verifier registration")
        self._verifiers[policy_digest] = verifier

    def verify(self, proof, **anchors):
        verifier = self._verifiers.get(proof.get("policy_digest"))
        if verifier is None:
            raise ValueError("PINNED_RETAINED_VERIFIER_REQUIRED")
        return verifier(proof, **anchors)

    def register_bundle(self, policy_digest, path, *, expected_sha256, public_key=None):
        from pathlib import Path
        from .release import verify_bundle

        raw = Path(path).read_bytes()
        verify_bundle(
            raw,
            public_key=public_key,
            expected_sha256=expected_sha256,
            expected_policy_digest=policy_digest,
        )

        def verifier(proof, **anchors):
            import json, os, subprocess, sys, zipfile
            from io import BytesIO
            from tempfile import TemporaryDirectory
            from .canonical import canonical_bytes

            with TemporaryDirectory(prefix="ofc-retained-") as directory:
                root = Path(directory)
                with zipfile.ZipFile(BytesIO(raw)) as archive:
                    archive.extractall(root)
                saved = root / "private-proof.json"
                saved.touch(mode=0o600)
                saved.write_bytes(canonical_bytes(proof))
                command = [sys.executable, str(root / "verify.py"), str(saved)]
                if anchors.get("expected_digest"):
                    command.extend(["--expected-digest", anchors["expected_digest"]])
                authorities = anchors.get("trusted_authorities")
                if authorities is not None:
                    keys = root / "authorities.json"
                    keys.write_bytes(
                        canonical_bytes([a.data() for a in authorities.values()])
                    )
                    command.extend(["--authorities", str(keys)])
                collection_keys = anchors.get("trusted_collection_keys")
                if collection_keys is not None:
                    import base64

                    keys = root / "collection-keys.json"
                    keys.write_bytes(
                        canonical_bytes(
                            {
                                name: base64.b64encode(key).decode("ascii")
                                for name, key in collection_keys.items()
                            }
                        )
                    )
                    command.extend(["--collection-keys", str(keys)])
                delegations = anchors.get("trusted_delegations")
                if delegations is not None:
                    from dataclasses import asdict, is_dataclass

                    keys = root / "delegations.json"
                    rows = []
                    for grant in delegations:
                        row = asdict(grant) if is_dataclass(grant) else dict(grant)
                        rows.append(
                            {
                                name: sorted(value)
                                if isinstance(value, (set, frozenset))
                                else value
                                for name, value in row.items()
                            }
                        )
                    keys.write_bytes(canonical_bytes(rows))
                    command.extend(["--delegations", str(keys)])
                run = subprocess.run(
                    command,
                    cwd=root,
                    capture_output=True,
                    timeout=10,
                    env={**os.environ, "PYTHONPATH": str(root / "src")},
                )
                if run.returncode != 0:
                    raise ValueError("retained verifier rejected proof")
                result = json.loads(run.stdout)
                if result.get("valid") is not True:
                    raise ValueError("retained proof invalid")
                return result

        self.register(policy_digest, verifier)

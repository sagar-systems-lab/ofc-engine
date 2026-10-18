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

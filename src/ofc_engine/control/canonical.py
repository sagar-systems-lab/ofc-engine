from __future__ import annotations

from hashlib import sha256
import json
import re

_KEY = re.compile(r"^[\x20-\x7e]+$")
_SAFE_INTEGER = 2**53 - 1


def canonical_bytes(value) -> bytes:
    # ASCII keys and safe integers give Python and JavaScript identical ordering/numbers.
    def check(item):
        if item is None or type(item) is bool:
            return
        if type(item) is int and abs(item) <= _SAFE_INTEGER:
            return
        if type(item) is str:
            item.encode("utf-8", errors="strict")
            return
        if type(item) in (list, tuple):
            for child in item:
                check(child)
            return
        if type(item) is dict and all(
            type(k) is str and _KEY.fullmatch(k) for k in item
        ):
            for child in item.values():
                check(child)
            return
        raise ValueError("unsupported canonical value")

    check(value)
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def digest(value) -> str:
    return sha256(canonical_bytes(value)).hexdigest()


def strict_json(raw: str | bytes):
    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise ValueError("duplicate JSON key")
            result[key] = value
        return result

    value = json.loads(
        raw,
        object_pairs_hook=pairs,
        parse_constant=lambda _: (_ for _ in ()).throw(ValueError("non-finite JSON")),
    )
    canonical_bytes(value)
    return value

"""§3 of docs/gateway与Agent新通信流程.md: the one scene-digest implementation.

RFC 8785 (JSON Canonicalization Scheme) over the document exactly as the transport
received it, then SHA-256, 64 lowercase hex characters. The gateway is the only party
on our side that computes a digest: the browser never does (§12.17 ①), and no second
implementation may appear in the editor bundle or in the tests (spec R5).

The input is *that JSON value*, not a parsed model: nothing here fills in a default,
drops an unknown key, or normalizes `scene_sub_version` when the document omits it --
`digest(parse(x).document) == digest(x)` is the constraint Brain relies on.
"""

from __future__ import annotations

import hashlib
from decimal import Decimal

__all__ = ["canonical_json", "scene_digest"]


def _as_double(value: int | float) -> float:
    """A JSON number is an IEEE-754 double, so an integer literal gets `JSON.parse`'s rounding."""
    if isinstance(value, bool):
        raise ValueError("a JSON number cannot be a bool")
    if isinstance(value, int):
        try:
            return float(value)
        except OverflowError as error:
            raise ValueError(f"{value} is outside the double range RFC 8785 defines") from error
    return value


def _number_to_string(value: float) -> str:
    """ECMAScript `Number::toString`, so a Python gateway and a JS peer agree on bytes."""
    if value != value or value in (float("inf"), float("-inf")):
        raise ValueError("RFC 8785 rejects NaN and Infinity")
    if value == 0:
        # -0 renders as "0", exactly like JSON.stringify.
        return "0"
    sign = "-" if value < 0 else ""
    coefficient, exponent = _digits_and_exponent(Decimal(repr(abs(value))))
    n = len(coefficient)
    k = exponent + n  # position of the decimal point: value = coefficient * 10**exponent
    if 0 < k <= n:
        return sign + (coefficient if k == n else f"{coefficient[:k]}.{coefficient[k:]}")
    if -6 < k <= 0:
        return sign + f"0.{'0' * -k}{coefficient}"
    if k > n and k <= 21:
        return sign + coefficient + "0" * (k - n)
    mantissa = coefficient if n == 1 else f"{coefficient[0]}.{coefficient[1:]}"
    return sign + f"{mantissa}e{'+' if k - 1 >= 0 else '-'}{abs(k - 1)}"


def _digits_and_exponent(number: Decimal) -> tuple[str, int]:
    """Shortest round-trip digits plus the power of ten they need (value = digits * 10**exp)."""
    coefficient = "".join(str(item) for item in number.as_tuple().digits)
    text = coefficient.rstrip("0") or "0"
    return text, number.adjusted() - len(text) + 1


def _string(value: str) -> str:
    """JSON.stringify escaping: control characters only, no Unicode normalization."""
    out = ['"']
    for character in value:
        if character == '"' or character == "\\":
            out.append("\\" + character)
        elif character == "\n":
            out.append("\\n")
        elif character == "\r":
            out.append("\\r")
        elif character == "\t":
            out.append("\\t")
        elif character == "\b":
            out.append("\\b")
        elif character == "\f":
            out.append("\\f")
        elif ord(character) < 0x20:
            out.append(f"\\u{ord(character):04x}")
        else:
            out.append(character)
    out.append('"')
    return "".join(out)


def canonical_json(value: object) -> str:
    """Return the RFC 8785 canonical form of one JSON value."""
    if value is None:
        return "null"
    if value is True:
        return "true"
    if value is False:
        return "false"
    if isinstance(value, str):
        return _string(value)
    if isinstance(value, (int, float)):
        return _number_to_string(_as_double(value))
    if isinstance(value, (list, tuple)):
        return "[" + ",".join(canonical_json(item) for item in value) + "]"
    if isinstance(value, dict):
        # UTF-16 code unit order, which big-endian bytes give directly.
        keys = sorted(value, key=lambda key: str(key).encode("utf-16-be"))
        parts = []
        for key in keys:
            item = value[key]
            if callable(item) or isinstance(item, (set, frozenset, bytes)):
                raise ValueError(f"canonical form needs a JSON value, got {type(item).__name__}")
            parts.append(f"{_string(str(key))}:{canonical_json(item)}")
        return "{" + ",".join(parts) + "}"
    raise ValueError(f"canonical form needs a JSON value, got {type(value).__name__}")


def scene_digest(document: object) -> str:
    """64 lowercase hex: SHA-256 of the canonical bytes of `document` (spec §3.2)."""
    canonical = canonical_json(document)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()

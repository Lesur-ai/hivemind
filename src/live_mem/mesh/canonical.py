# -*- coding: utf-8 -*-
"""Hivemind Canonical JSON v1 (HCJ-1).

HCJ-1 is intentionally much smaller than general JSON.  It is the byte format
used by Project Mesh signatures, so accepting two spellings for one value would
create a protocol ambiguity.  Parsing therefore validates the I-JSON subset and
also requires the supplied bytes to be the exact canonical re-serialization.

This module has no storage, network, membership, or application-state imports.
"""

from __future__ import annotations

import hashlib
import json
import re
import unicodedata
from dataclasses import dataclass
from typing import TypeAlias


JSONScalar: TypeAlias = str | bool | None | int
JSONValue: TypeAlias = JSONScalar | list["JSONValue"] | dict[str, "JSONValue"]


HCJ_VERSION = 1
MAX_SAFE_INTEGER = (1 << 53) - 1
_KEY_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_]{0,63}$", re.ASCII)
_UTF8_BOM = b"\xef\xbb\xbf"


@dataclass(frozen=True, slots=True)
class HCJLimits:
    """Frozen HCJ-1 resource and shape limits."""

    max_depth: int = 16
    max_nodes: int = 512
    max_object_members: int = 64
    max_array_items: int = 128
    max_string_utf8_bytes: int = 2048
    max_total_string_utf8_bytes: int = 65536
    max_total_bytes: int = 65536


HCJ_LIMITS = HCJLimits()


class HCJError(ValueError):
    """A machine-readable HCJ refusal with a non-reflective safe message."""

    def __init__(self, code: str, safe_message: str) -> None:
        super().__init__(safe_message)
        self.code = code
        self.safe_message = safe_message


def _fail(code: str, message: str) -> "None":
    raise HCJError(code, message)


def _validate_text(value: str, *, key: bool, limits: HCJLimits) -> None:
    if unicodedata.normalize("NFC", value) != value:
        _fail("non_nfc", "HCJ text must already be NFC-normalized")
    if any(0xD800 <= ord(char) <= 0xDFFF for char in value):
        _fail("surrogate", "HCJ text must not contain Unicode surrogates")
    try:
        encoded = value.encode("utf-8", "strict")
    except UnicodeEncodeError:
        _fail("invalid_utf8", "HCJ text must be valid UTF-8")
    if len(encoded) > limits.max_string_utf8_bytes:
        _fail("string_too_long", "HCJ text exceeds the UTF-8 byte limit")
    if key and _KEY_RE.fullmatch(value) is None:
        _fail("invalid_key", "HCJ object keys must match the frozen ASCII grammar")


def _validate_tree(value: object, *, limits: HCJLimits) -> JSONValue:
    """Validate an already-decoded value without coercing any Python type."""

    nodes = 0
    string_bytes = 0
    active_containers: set[int] = set()

    def walk(item: object, depth: int) -> JSONValue:
        nonlocal nodes, string_bytes
        if depth > limits.max_depth:
            _fail("too_deep", "HCJ nesting exceeds the maximum depth")

        nodes += 1
        if nodes > limits.max_nodes:
            _fail("too_many_nodes", "HCJ value exceeds the node limit")

        # bool must be checked before int because bool subclasses int in Python.
        if item is None or isinstance(item, bool):
            return item
        if type(item) is int:
            if not -MAX_SAFE_INTEGER <= item <= MAX_SAFE_INTEGER:
                _fail("integer_out_of_range", "HCJ integer is outside the safe range")
            return item
        if type(item) is str:
            _validate_text(item, key=False, limits=limits)
            string_bytes += len(item.encode("utf-8"))
            if string_bytes > limits.max_total_string_utf8_bytes:
                _fail(
                    "string_budget_exceeded",
                    "HCJ strings exceed the cumulative UTF-8 byte budget",
                )
            return item
        if type(item) is list:
            if len(item) > limits.max_array_items:
                _fail("array_too_large", "HCJ array exceeds the item limit")
            marker = id(item)
            if marker in active_containers:
                _fail("cycle", "HCJ values must be acyclic")
            active_containers.add(marker)
            try:
                return [walk(child, depth + 1) for child in item]
            finally:
                active_containers.remove(marker)
        if type(item) is dict:
            if len(item) > limits.max_object_members:
                _fail("object_too_large", "HCJ object exceeds the member limit")
            marker = id(item)
            if marker in active_containers:
                _fail("cycle", "HCJ values must be acyclic")
            active_containers.add(marker)
            checked: dict[str, JSONValue] = {}
            try:
                for raw_key, child in item.items():
                    # No key coercion: subclasses and non-strings are refused.
                    if type(raw_key) is not str:
                        _fail("invalid_key_type", "HCJ object keys must be plain strings")
                    nodes += 1  # Object keys count toward the 512-node budget.
                    if nodes > limits.max_nodes:
                        _fail("too_many_nodes", "HCJ value exceeds the node limit")
                    _validate_text(raw_key, key=True, limits=limits)
                    string_bytes += len(raw_key.encode("utf-8"))
                    if string_bytes > limits.max_total_string_utf8_bytes:
                        _fail(
                            "string_budget_exceeded",
                            "HCJ strings exceed the cumulative UTF-8 byte budget",
                        )
                    checked[raw_key] = walk(child, depth + 1)
                return checked
            finally:
                active_containers.remove(marker)

        # Explicitly rejects floats, Decimal, tuples, enums, dataclasses, and
        # user-defined subclasses instead of silently coercing them.
        _fail("unsupported_type", "HCJ value contains an unsupported type")

    return walk(value, 1)


def _serialize(value: JSONValue) -> bytes:
    try:
        text = json.dumps(
            value,
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        return text.encode("utf-8", "strict")
    except (TypeError, ValueError, UnicodeError) as exc:
        raise HCJError("serialization_failed", "HCJ serialization failed") from exc


def canonical_dumps(value: object, *, limits: HCJLimits = HCJ_LIMITS) -> bytes:
    """Return the one HCJ-1 byte representation for ``value``.

    Input types are checked by exact type, so callers cannot smuggle coercible
    objects into signed protocol bytes.
    """

    checked = _validate_tree(value, limits=limits)
    encoded = _serialize(checked)
    if len(encoded) > limits.max_total_bytes:
        _fail("document_too_large", "HCJ document exceeds the byte limit")
    return encoded


def _reject_float(_raw: str) -> "None":
    _fail("float_forbidden", "HCJ numbers must be safe integers")


def _reject_constant(_raw: str) -> "None":
    _fail("non_finite_forbidden", "HCJ non-finite numbers are forbidden")


def _parse_int(raw: str) -> int:
    # json.JSONDecoder has already enforced JSON's integer token grammar, but
    # CPython caps integer<->string conversion at sys.get_int_max_str_digits()
    # (4300 by default) and raises a *bare* ValueError for a longer token.  On
    # the wide-limit ingest paths (artifact/replay/event bodies up to tens of
    # KiB) an attacker can supply such a token; left unnormalized that ValueError
    # escapes canonical_loads and surfaces as an unhandled exception out of the
    # *.from_bytes parsers.  Normalize it here to a machine-readable HCJError.
    # The try wraps *only* int(); the safe-range check stays outside so a normal
    # oversized-magnitude token keeps its distinct integer_out_of_range code.
    try:
        value = int(raw, 10)
    except ValueError:
        raise HCJError("integer_too_long", "HCJ integer token has too many digits") from None
    if not -MAX_SAFE_INTEGER <= value <= MAX_SAFE_INTEGER:
        _fail("integer_out_of_range", "HCJ integer is outside the safe range")
    return value


def _object_pairs(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            _fail("duplicate_key", "HCJ objects must not contain duplicate keys")
        result[key] = value
    return result


def canonical_loads(raw: bytes, *, limits: HCJLimits = HCJ_LIMITS) -> JSONValue:
    """Parse exact canonical HCJ-1 bytes.

    Merely valid JSON is insufficient: BOMs, whitespace, alternate escapes,
    unsorted keys, and every other non-canonical spelling are rejected by the
    final byte-for-byte re-serialization check.
    """

    if type(raw) is not bytes:
        _fail("invalid_input_type", "HCJ input must be plain bytes")
    if raw.startswith(_UTF8_BOM):
        _fail("bom_forbidden", "HCJ input must not contain a UTF-8 BOM")
    if len(raw) > limits.max_total_bytes:
        _fail("document_too_large", "HCJ document exceeds the byte limit")
    try:
        text = raw.decode("utf-8", "strict")
    except UnicodeDecodeError as exc:
        raise HCJError("invalid_utf8", "HCJ input must be valid UTF-8") from exc

    try:
        decoded = json.loads(
            text,
            object_pairs_hook=_object_pairs,
            parse_int=_parse_int,
            parse_float=_reject_float,
            parse_constant=_reject_constant,
        )
    except HCJError:
        raise
    except (json.JSONDecodeError, RecursionError, UnicodeError, ValueError) as exc:
        # ValueError is belt-and-suspenders: a hardened _parse_int already raises
        # HCJError (caught above), but any residual bare ValueError from a
        # decode hook must still fail closed as invalid HCJ, never escape.
        raise HCJError("invalid_json", "HCJ input is not valid canonical JSON") from exc

    checked = _validate_tree(decoded, limits=limits)
    canonical = _serialize(checked)
    if len(canonical) > limits.max_total_bytes:
        _fail("document_too_large", "HCJ document exceeds the byte limit")
    if canonical != raw:
        _fail("non_canonical", "HCJ input is not the exact canonical serialization")
    return checked


def canonical_sha256(value_or_raw: object, *, limits: HCJLimits = HCJ_LIMITS) -> str:
    """Return a raw 64-character lowercase SHA-256 digest of HCJ bytes."""

    if type(value_or_raw) is bytes:
        canonical_loads(value_or_raw, limits=limits)
        raw = value_or_raw
    else:
        raw = canonical_dumps(value_or_raw, limits=limits)
    return hashlib.sha256(raw).hexdigest()


__all__ = [
    "HCJError",
    "HCJLimits",
    "HCJ_LIMITS",
    "HCJ_VERSION",
    "JSONScalar",
    "JSONValue",
    "MAX_SAFE_INTEGER",
    "canonical_dumps",
    "canonical_loads",
    "canonical_sha256",
]

"""Deterministic serialization for multi-agent contracts.

The central function is :func:`canonicalize` — a recursive converter that
produces sort-key-ordered, UTC-normalised JSON from any Phase 2 contract
(and nested dicts / lists / sets / Enums / Decimals / datetimes).
Every consumer that needs a stable hash or snapshot version MUST go through
this single canonicalizer.
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from decimal import Decimal
from enum import Enum
from typing import Any

from pydantic import BaseModel


def _canonical_value(obj: Any) -> Any:
    """Recursively convert *obj* into a JSON-native, deterministic representation.

    Rules (in priority order):
    1. ``None`` → ``None``
    2. ``bool`` → ``bool`` (MUST come before int — bool is a subclass)
    3. ``int`` / ``float`` / ``str`` → as-is, but ``float('nan')`` / ``inf`` are rejected
    4. ``Decimal`` → string with 2 decimal places for cost, full precision otherwise
    5. ``datetime`` → UTC-normalised ISO-8601 with ``Z`` suffix
    6. ``Enum`` → ``.value``
    7. ``set`` / ``frozenset`` → sorted list (sorted by canonical string key)
    8. ``bytes`` → hex string
    9. ``BaseModel`` → canonicalize(model_dump(mode="json"))
    10. ``dict`` → sorted-by-key dict of canonicalized values
    11. ``list`` / ``tuple`` → list of canonicalized values
    12. anything else → ``TypeError``
    """
    if obj is None:
        return None
    if isinstance(obj, bool):
        return obj
    if isinstance(obj, int):
        return obj
    if isinstance(obj, float):
        import math

        if math.isnan(obj) or math.isinf(obj):
            raise ValueError(f"float value {obj!r} is not JSON-serializable")
        return obj
    if isinstance(obj, str):
        return obj
    if isinstance(obj, Decimal):
        # Preserve full precision as a JSON number string
        return str(obj)
    if isinstance(obj, datetime):
        if obj.tzinfo is None:
            obj = obj.replace(tzinfo=timezone.utc)
        return obj.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
    if isinstance(obj, Enum):
        return obj.value
    if isinstance(obj, (set, frozenset)):
        return sorted(
            (_canonical_value(v) for v in obj),
            key=lambda x: str(x),
        )
    if isinstance(obj, bytes):
        return obj.hex()
    if isinstance(obj, BaseModel):
        return _canonical_value(obj.model_dump(mode="json"))
    if isinstance(obj, dict):
        return {
            str(k): _canonical_value(v)
            for k, v in sorted(obj.items(), key=lambda kv: str(kv[0]))
        }
    if isinstance(obj, (list, tuple)):
        return [_canonical_value(v) for v in obj]
    raise TypeError(f"Cannot canonicalize type {type(obj).__name__}: {obj!r}")


def canonicalize(obj: Any) -> Any:
    """Return a fully canonical (JSON-native, sorted, UTC) representation of *obj*."""
    return _canonical_value(obj)


# ---------------------------------------------------------------------------
# High-level helpers
# ---------------------------------------------------------------------------


def serialize_contract(obj: BaseModel) -> str:
    """Serialize a contract model to a deterministic JSON string."""
    data = canonicalize(obj)
    return json.dumps(data, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def deserialize_contract(raw: str, model_cls: type[BaseModel]) -> Any:
    """Deserialize a JSON string back into a contract model."""
    data = json.loads(raw)
    return model_cls.model_validate(data)


def stable_hash(obj: Any, *, exclude: set[str] | None = None) -> str:
    """Return a SHA-256 hex digest of *obj*'s canonical form.

    When *obj* is a BaseModel its fields named in *exclude* are dropped first.
    """
    if isinstance(obj, BaseModel):
        data = obj.model_dump(mode="json")
        if exclude:
            for key in exclude:
                data.pop(key, None)
        canonical_data = canonicalize(data)
    else:
        canonical_data = canonicalize(obj)
    canonical_json = json.dumps(
        canonical_data, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    )
    return hashlib.sha256(canonical_json.encode("utf-8")).hexdigest()


def serialize_set_for_json(value: set[Any]) -> list[Any]:
    """Return a sorted list suitable for JSON serialization."""
    return sorted(value, key=lambda x: str(x) if not isinstance(x, str) else x)


def content_hash(obj: Any) -> str:
    """Return a stable SHA-256 hash of *obj*'s canonical form (full content hash)."""
    return stable_hash(obj)


def validate_strict_json(value: Any) -> Any:
    """Validate that *value* is strict JSON-compatible.

    Only these types are allowed at the contract boundary:
      - None
      - bool
      - int
      - finite float (no NaN, no Infinity)
      - str
      - list of strict-JSON values
      - dict with **string** keys and strict-JSON values

    Rejected types (MUST fail):
      - bytes / bytearray
      - set / frozenset
      - tuple
      - Decimal
      - datetime
      - Enum
      - custom objects
      - NaN / Infinity
      - non-string dict keys
    """
    if value is None:
        return None
    if isinstance(value, bool):
        return value
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        import math

        if math.isnan(value) or math.isinf(value):
            raise ValueError(f"float value {value!r} is not valid JSON")
        return value
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        return [validate_strict_json(v) for v in value]
    if isinstance(value, dict):
        result: dict[str, Any] = {}
        for k, v in value.items():
            if not isinstance(k, str):
                raise ValueError(
                    f"JSON object keys must be strings; got {type(k).__name__}: {k!r}"
                )
            result[k] = validate_strict_json(v)
        return result
    # Everything else is rejected
    raise ValueError(
        f"Value of type {type(value).__name__} is not valid strict JSON: {value!r}"
    )


# ---------------------------------------------------------------------------
# Canonicalizer-based validator (still used for hash/payload validation)
# ---------------------------------------------------------------------------


def validate_json_value(value: Any) -> Any:
    """Validate via canonicalizer — used for payload fields that may contain
    Decimal/datetime/Enum which are fine inside ActionProposal payloads but
    will be canonicalized before hashing."""
    return _canonical_value(value)

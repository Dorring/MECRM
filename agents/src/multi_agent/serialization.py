"""Serialization helpers for multi-agent contracts.

All helpers produce deterministic output: sorted keys, stable separators,
and explicit UTC formatting.  Two calls with the same Pydantic model always
produce the same JSON bytes.
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from enum import Enum
from typing import Any, TypeVar

from pydantic import BaseModel

T = TypeVar("T", bound=BaseModel)


# ---------------------------------------------------------------------------
# JSON
# ---------------------------------------------------------------------------


def _default_encoder(obj: Any) -> Any:
    """Convert non-JSON-native types to serializable values."""
    if isinstance(obj, datetime):
        if obj.tzinfo is None:
            obj = obj.replace(tzinfo=timezone.utc)
        return obj.isoformat().replace("+00:00", "Z")
    if isinstance(obj, Enum):
        return obj.value
    if isinstance(obj, set):
        return sorted(obj)
    if isinstance(obj, bytes):
        return obj.hex()
    raise TypeError(f"Object of type {type(obj).__name__} is not JSON serializable")


def serialize_contract(obj: BaseModel) -> str:
    """Serialize a contract model to a deterministic JSON string.

    Keys are sorted, separators are compact, and datetime / enum / set types
    are handled transparently.
    """
    return json.dumps(
        obj.model_dump(mode="json"),
        sort_keys=True,
        separators=(",", ":"),
        default=_default_encoder,
        ensure_ascii=False,
    )


def deserialize_contract(raw: str, model_cls: type[T]) -> T:
    """Deserialize a JSON string back into a contract model."""
    data = json.loads(raw)
    return model_cls.model_validate(data)


# ---------------------------------------------------------------------------
# Stable hash
# ---------------------------------------------------------------------------


def stable_hash(obj: BaseModel, *, exclude: set[str] | None = None) -> str:
    """Return a SHA-256 hex digest of *obj*'s canonical JSON.

    The *exclude* set names fields to drop before hashing (e.g.
    ``created_at`` or ``proposal_hash``).  The hash is deterministic as long
    as the model's content is unchanged.
    """
    data = obj.model_dump(mode="json")
    if exclude:
        for key in exclude:
            data.pop(key, None)
    canonical = json.dumps(
        data,
        sort_keys=True,
        separators=(",", ":"),
        default=_default_encoder,
        ensure_ascii=False,
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------
# Set helper
# ---------------------------------------------------------------------------


def serialize_set_for_json(value: set[Any]) -> list[Any]:
    """Return a sorted list suitable for JSON serialization."""
    return sorted(value, key=lambda x: str(x) if not isinstance(x, str) else x)

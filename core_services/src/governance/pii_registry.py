from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

PiiCategory = Literal["identity", "contact", "location", "sensitive", "free_text"]


@dataclass(frozen=True)
class PiiField:
    entity_type: str
    field: str
    category: PiiCategory


class PIIRegistry:
    PII_FIELDS: dict[str, PiiCategory] = {
        "users.email": "contact",
        "users.name": "identity",
        "customers.name": "identity",
        "customers.email": "contact",
        "customers.phone": "contact",
        "leads.name": "identity",
        "leads.email": "contact",
        "leads.phone": "contact",
        "tickets.subject": "free_text",
        "tickets.description": "free_text",
    }

    @classmethod
    def classify(cls, *, entity_type: str, field: str) -> PiiCategory | None:
        key = f"{entity_type}.{field}"
        return cls.PII_FIELDS.get(key)

    @classmethod
    def is_pii(cls, *, entity_type: str, field: str) -> bool:
        return cls.classify(entity_type=entity_type, field=field) is not None

    @classmethod
    def pii_fields_for_entity(cls, *, entity_type: str) -> list[PiiField]:
        out: list[PiiField] = []
        prefix = f"{entity_type}."
        for k, cat in cls.PII_FIELDS.items():
            if not k.startswith(prefix):
                continue
            out.append(PiiField(entity_type=entity_type, field=k[len(prefix) :], category=cat))
        out.sort(key=lambda f: (f.category, f.field))
        return out

    @classmethod
    def erase_pii_in_record(cls, *, entity_type: str, record: dict) -> dict:
        if not record:
            return {}
        out = dict(record)
        for f in cls.pii_fields_for_entity(entity_type=entity_type):
            if f.field in out:
                out[f.field] = None
        return out

    @classmethod
    def filter_to_declared_fields(cls, *, entity_type: str, record: dict, include_non_pii: bool = True) -> dict:
        if not record:
            return {}
        out: dict = {}
        for k, v in record.items():
            if cls.is_pii(entity_type=entity_type, field=str(k)):
                out[k] = v
                continue
            if include_non_pii:
                out[k] = v
        return out

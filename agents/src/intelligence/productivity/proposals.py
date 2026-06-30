from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Any, Literal
from uuid import uuid4


Priority = Literal["low", "medium", "high"]
ActionType = Literal["reminder", "followup", "task"]


@dataclass(frozen=True)
class ProductivitySignal:
    type: str
    tenant_id: str
    entity_type: str
    entity_id: str
    details: dict[str, Any]
    detected_at: str


@dataclass(frozen=True)
class Drafts:
    email_subject: str | None = None
    email_body: str | None = None
    whatsapp_message: str | None = None
    task_description: str | None = None

    def to_json(self) -> dict[str, Any]:
        return {
            "email": {"subject": self.email_subject, "body": self.email_body} if (self.email_subject or self.email_body) else None,
            "whatsapp": {"message": self.whatsapp_message} if self.whatsapp_message else None,
            "task": {"description": self.task_description} if self.task_description else None,
        }


@dataclass(frozen=True)
class ActionProposal:
    proposal_id: str
    tenant_id: str
    user_id: str
    action_type: ActionType
    target_entity: str
    target_id: str
    priority: Priority
    justification: str
    drafts: dict[str, Any]
    created_at: str
    dedupe_key: str
    signal_type: str
    signal: dict[str, Any]


def new_proposal_id() -> str:
    return str(uuid4())


def compute_dedupe_key(*, tenant_id: str, user_id: str, action_type: str, target_entity: str, target_id: str, signal_type: str) -> str:
    payload = json.dumps(
        {
            "tenant_id": tenant_id,
            "user_id": user_id,
            "action_type": action_type,
            "target_entity": target_entity,
            "target_id": target_id,
            "signal_type": signal_type,
        },
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


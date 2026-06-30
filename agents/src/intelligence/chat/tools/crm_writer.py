from __future__ import annotations

from dataclasses import dataclass
from typing import Any
from uuid import uuid4

from pydantic import BaseModel, Field

from ..memory import utc_now_iso


@dataclass
class ActionProposal:
    proposal_id: str
    entity: str
    operation: str
    payload: dict[str, Any]
    requires_approval: bool
    created_at: str


class ProposedWrite(BaseModel):
    entity: str = Field(default="unknown")
    operation: str = Field(default="unknown")
    payload: dict[str, Any] = Field(default_factory=dict)


class CrmWriter:
    async def propose(self, *, raw: str) -> dict[str, Any]:
        parsed = self._best_effort_parse(raw)
        proposal = ActionProposal(
            proposal_id=str(uuid4()),
            entity=parsed.entity,
            operation=parsed.operation,
            payload=parsed.payload,
            requires_approval=True,
            created_at=utc_now_iso(),
        )
        return {"proposal": proposal.__dict__}

    def _best_effort_parse(self, raw: str) -> ProposedWrite:
        text = (raw or "").lower()
        entity = "unknown"
        if "lead" in text:
            entity = "lead"
        elif "ticket" in text:
            entity = "ticket"
        elif "customer" in text:
            entity = "customer"

        operation = "unknown"
        if any(k in text for k in ("create", "add", "new")):
            operation = "create"
        elif any(k in text for k in ("update", "change", "edit")):
            operation = "update"
        elif any(k in text for k in ("delete", "remove")):
            operation = "delete"

        return ProposedWrite(entity=entity, operation=operation, payload={"raw": raw})


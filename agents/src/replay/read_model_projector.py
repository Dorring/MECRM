from __future__ import annotations

import copy
from dataclasses import dataclass
from typing import Any
from uuid import UUID

from .models import CanonicalEvent


def _safe_uuid(value: Any) -> UUID | None:
    try:
        if value is None:
            return None
        return UUID(str(value))
    except Exception:
        return None


def _as_dict(state: dict[str, Any] | None) -> dict[str, Any]:
    return {} if state is None else copy.deepcopy(state)


def _apply_lead_event(event: CanonicalEvent, state: dict[str, Any]) -> dict[str, Any]:
    payload = event.payload
    if event.event_type.endswith("leads.created") or event.event_type.endswith("lead.created"):
        lead_id = _safe_uuid(payload.get("aggregate_id") or payload.get("leadId") or payload.get("id") or event.aggregate_id)
        state.update(
            {
                "id": str(lead_id) if lead_id else str(event.aggregate_id),
                "tenantId": str(event.tenant_id),
                "name": payload.get("name"),
                "email": payload.get("email"),
                "phone": payload.get("phone"),
                "company": payload.get("company"),
                "source": payload.get("source"),
                "status": payload.get("status") or "new",
                "score": payload.get("score"),
                "assignedTo": payload.get("assignedTo"),
                "metadata": payload.get("metadata") or {},
                "createdBy": payload.get("createdBy"),
            }
        )
        return state

    if event.event_type.endswith("leads.updated") or event.event_type.endswith("lead.updated"):
        changes_raw = payload.get("changes")
        changes: dict[str, Any] = changes_raw if isinstance(changes_raw, dict) else (payload if isinstance(payload, dict) else {})
        for key in (
            "name",
            "email",
            "phone",
            "company",
            "source",
            "status",
            "score",
            "assignedTo",
            "metadata",
        ):
            if key in changes:
                state[key] = changes[key]
        return state

    return state


def _apply_ticket_event(event: CanonicalEvent, state: dict[str, Any]) -> dict[str, Any]:
    payload = event.payload
    if event.event_type.endswith("tickets.created") or event.event_type.endswith("ticket.created"):
        ticket_id = _safe_uuid(payload.get("aggregate_id") or payload.get("ticketId") or payload.get("id") or event.aggregate_id)
        state.update(
            {
                "id": str(ticket_id) if ticket_id else str(event.aggregate_id),
                "tenantId": str(event.tenant_id),
                "subject": payload.get("subject"),
                "description": payload.get("description"),
                "customerId": payload.get("customerId"),
                "priority": payload.get("priority") or "medium",
                "status": payload.get("status") or "open",
                "category": payload.get("category"),
                "assignedTo": payload.get("assignedTo"),
                "slaDueAt": payload.get("slaDueAt"),
                "resolvedAt": payload.get("resolvedAt"),
                "resolution": payload.get("resolution"),
                "metadata": payload.get("metadata") or {},
                "createdBy": payload.get("createdBy"),
            }
        )
        return state

    if event.event_type.endswith("tickets.updated") or event.event_type.endswith("ticket.updated"):
        changes_raw = payload.get("changes")
        changes: dict[str, Any] = changes_raw if isinstance(changes_raw, dict) else (payload if isinstance(payload, dict) else {})
        for key in (
            "subject",
            "description",
            "customerId",
            "priority",
            "status",
            "category",
            "assignedTo",
            "slaDueAt",
            "resolvedAt",
            "resolution",
            "metadata",
        ):
            if key in changes:
                state[key] = changes[key]
        return state

    return state


def apply_event(event: CanonicalEvent, current_state: dict[str, Any] | None) -> dict[str, Any]:
    state = _as_dict(current_state)
    aggregate_type = event.aggregate_type.lower()
    if aggregate_type == "lead":
        return _apply_lead_event(event, state)
    if aggregate_type == "ticket":
        return _apply_ticket_event(event, state)
    return state


@dataclass(frozen=True)
class ProjectResult:
    state: dict[str, Any]
    applied_versions: list[int]


def project_events(events: list[CanonicalEvent], initial_state: dict[str, Any] | None = None) -> ProjectResult:
    state = _as_dict(initial_state)
    seen: set[UUID] = set()
    applied_versions: list[int] = []

    for event in events:
        if event.event_id in seen:
            continue
        seen.add(event.event_id)
        state = apply_event(event, state)
        if event.version is not None:
            applied_versions.append(int(event.version))

    return ProjectResult(state=state, applied_versions=applied_versions)


def rebuild_from_scratch(aggregate_type: str, tenant_id: str, events: list[CanonicalEvent]) -> dict[str, Any]:
    filtered = [e for e in events if e.aggregate_type.lower() == aggregate_type.lower() and str(e.tenant_id) == tenant_id]
    return project_events(filtered).state


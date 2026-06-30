from datetime import datetime, timezone
from uuid import UUID

from replay.models import CanonicalEvent
from replay.read_model_projector import project_events


def _t(ts: str) -> datetime:
    return datetime.fromisoformat(ts.replace("Z", "+00:00")).astimezone(timezone.utc)


def test_projector_is_deterministic_for_same_event_sequence():
    tenant_id = UUID("11111111-1111-4111-8111-111111111111")
    lead_id = UUID("aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa")

    events = [
        CanonicalEvent(
            event_id=UUID("00000000-0000-4000-8000-000000000001"),
            tenant_id=tenant_id,
            aggregate_type="lead",
            aggregate_id=lead_id,
            event_type="lead.created",
            payload={"leadId": str(lead_id), "name": "Alice", "status": "new"},
            version=1,
            ts=_t("2026-01-01T00:00:00Z"),
        ),
        CanonicalEvent(
            event_id=UUID("00000000-0000-4000-8000-000000000002"),
            tenant_id=tenant_id,
            aggregate_type="lead",
            aggregate_id=lead_id,
            event_type="lead.updated",
            payload={"leadId": str(lead_id), "changes": {"status": "qualified", "score": 90}},
            version=2,
            ts=_t("2026-01-01T00:00:10Z"),
        ),
        CanonicalEvent(
            event_id=UUID("00000000-0000-4000-8000-000000000002"),
            tenant_id=tenant_id,
            aggregate_type="lead",
            aggregate_id=lead_id,
            event_type="lead.updated",
            payload={"leadId": str(lead_id), "changes": {"status": "qualified", "score": 90}},
            version=2,
            ts=_t("2026-01-01T00:00:10Z"),
        ),
    ]

    a = project_events(events).state
    b = project_events(events).state

    assert a == b
    assert a["status"] == "qualified"
    assert a["score"] == 90


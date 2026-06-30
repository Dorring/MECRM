import pytest

from intelligence.journey.timeline_builder import build_timeline_entries


class FakeConn:
    def __init__(self, *, ticket_row=None, deal_row=None, deal_lead_row=None):
        self._ticket_row = ticket_row
        self._deal_row = deal_row
        self._deal_lead_row = deal_lead_row

    async def fetchrow(self, query, *args):
        q = str(query)
        if "FROM tickets" in q:
            return self._ticket_row
        if "FROM deals" in q and "id::text as deal_id" in q:
            return self._deal_row
        if "FROM deals" in q and "SELECT lead_id" in q:
            return self._deal_lead_row
        if "SELECT customer_id::text as customer_id FROM tickets" in q:
            return self._ticket_row
        if "SELECT customer_id::text as customer_id FROM deals" in q:
            return self._deal_row
        return None


@pytest.mark.asyncio
async def test_ticket_created_is_mapped_to_timeline_entry():
    conn = FakeConn(ticket_row={"customer_id": "c", "subject": "S", "priority": "high", "sla_due_at": None})
    event = {"type": "crm.tickets.created", "tenantid": "t", "data": {"ticketId": "tt"}}
    entries = await build_timeline_entries(tenant_id="t", event=event, conn=conn)  # type: ignore
    assert len(entries) == 1
    assert entries[0].customer_id == "c"
    assert entries[0].event_type == "ticket.created"
    assert entries[0].event_payload["ticket_id"] == "tt"


@pytest.mark.asyncio
async def test_deal_stage_change_is_mapped_to_timeline_entry():
    conn = FakeConn(deal_row={"customer_id": "c", "name": "Deal", "amount": None})
    event = {"type": "crm.deals.stage-changed", "tenantid": "t", "data": {"dealId": "d", "previousStage": "proposal", "newStage": "negotiation"}}
    entries = await build_timeline_entries(tenant_id="t", event=event, conn=conn)  # type: ignore
    assert len(entries) == 1
    assert entries[0].customer_id == "c"
    assert entries[0].event_type == "deal.stage_changed"


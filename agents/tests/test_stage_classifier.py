import pytest

from intelligence.journey.stage_classifier import classify_stage


class FakeConn:
    def __init__(self, *, deals_row, tickets_row):
        self._deals = deals_row
        self._tickets = tickets_row

    async def fetchrow(self, query, *args):
        q = str(query)
        if "FROM deals" in q:
            return self._deals
        if "FROM tickets" in q:
            return self._tickets
        return None


@pytest.mark.asyncio
async def test_stage_is_converted_when_closed_won_exists():
    conn = FakeConn(
        deals_row={"won_count": 1, "lost_count": 0, "last_lost_at": None, "latest_stage": "closed_won"},
        tickets_row={"open_count": 0, "open_high_count": 0, "overdue_count": 0, "last_open_update_at": None},
    )
    r = await classify_stage(tenant_id="t", customer_id="c", conn=conn)  # type: ignore
    assert r.stage == "converted"
    assert r.confidence >= 0.8


@pytest.mark.asyncio
async def test_stage_is_churn_risk_when_overdue_tickets_present():
    conn = FakeConn(
        deals_row={"won_count": 0, "lost_count": 0, "last_lost_at": None, "latest_stage": "prospecting"},
        tickets_row={"open_count": 3, "open_high_count": 1, "overdue_count": 2, "last_open_update_at": None},
    )
    r = await classify_stage(tenant_id="t", customer_id="c", conn=conn)  # type: ignore
    assert r.stage == "churn_risk"


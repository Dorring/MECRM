import pytest

from orchestrator.main import intelligence_query_handler


class _Search:
    async def search(self, **_):
        return {"search_id": "s1", "results": [], "suggestions": [], "intent": {"intent": "read", "entity": "lead", "confidence": 1.0}}


class _Chat:
    async def chat(self, **_):
        return {"conversation_id": "c1", "intent": {"intent": "question", "entity": "unknown", "confidence": 0.7}, "message": "ok", "suggested_replies": [], "action_proposals": [], "debug": {}}

class _Req:
    def __init__(self, *, app, headers, body):
        self.app = app
        self.headers = headers
        self._body = body

    async def json(self):
        return self._body


@pytest.mark.asyncio
async def test_intelligence_query_routes_to_chat_when_mode_chat():
    app = {"search_agent": _Search(), "chat_agent": _Chat()}
    req = _Req(
        app=app,
        headers={"X-Tenant-Id": "t1", "X-User-Id": "u1", "X-User-Roles": "sales", "Authorization": "Bearer x"},
        body={"query": "hello", "mode": "chat", "conversation_id": "c1"},
    )
    resp = await intelligence_query_handler(req)
    assert resp.status == 200
    data = (resp.body or b"{}").decode("utf-8")
    data = __import__("json").loads(data)
    assert data.get("conversation_id") == "c1"


@pytest.mark.asyncio
async def test_intelligence_query_routes_to_search_when_no_chat_fields():
    app = {"search_agent": _Search(), "chat_agent": _Chat()}
    req = _Req(
        app=app,
        headers={"X-Tenant-Id": "t1", "X-User-Id": "u1", "X-User-Roles": "sales"},
        body={"query": "Acme"},
    )
    resp = await intelligence_query_handler(req)
    assert resp.status == 200
    data = (resp.body or b"{}").decode("utf-8")
    data = __import__("json").loads(data)
    assert data.get("search_id") == "s1"


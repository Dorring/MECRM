import pytest

from intelligence.chat.graph import ChatDeps, ChatState, ToolCall, ToolResult, build_chat_graph


class _Msg:
    def __init__(self, content: str):
        self.content = content


class FakeLLM:
    def __init__(self, *, reply_json: str):
        self._reply_json = reply_json

    async def ainvoke(self, _: str):
        return _Msg(self._reply_json)


class InMemoryMemory:
    def __init__(self):
        self._items: list[dict] = []

    async def load_window(self, *, tenant_id: str, conversation_id: str, limit: int):
        items = [x for x in self._items if x["tenant_id"] == tenant_id and x["conversation_id"] == conversation_id]
        return items[-max(1, min(int(limit or 10), 50)) :]

    async def append(self, *, item):
        self._items.append(
            {
                "tenant_id": item.tenant_id,
                "conversation_id": item.conversation_id,
                "user_id": item.user_id,
                "role": item.role,
                "message": item.message,
                "timestamp": item.timestamp,
            }
        )


class RecordingExecutor:
    def __init__(self, *, result: ToolResult):
        self.calls: list[ToolCall] = []
        self._result = result

    async def execute(self, *, call: ToolCall, **_):
        self.calls.append(call)
        return self._result


@pytest.mark.asyncio
async def test_read_intent_routes_to_reader_and_renders_list():
    llm = FakeLLM(reply_json='{"intent":"read","entity":"lead","confidence":0.9}')
    memory = InMemoryMemory()
    executor = RecordingExecutor(result=ToolResult(tool="crm_reader.get_leads", ok=True, data={"data": [{"id": "l1", "name": "Acme Prospect"}]}))
    deps = ChatDeps(llm=llm, tool_executor=executor, memory=memory, memory_window=12)
    graph = build_chat_graph(deps=deps)

    state = ChatState(query="show my leads", tenant_id="t1", user_id="u1", roles=["sales"], authorization="Bearer x", correlation_id="c1", conversation_id="conv1")
    out = await graph.ainvoke(state)

    assert executor.calls[0].tool.startswith("crm_reader.get_")
    assert out.get("response_text") and "latest" in str(out.get("response_text")).lower()
    assert out.get("action_proposals") == []


@pytest.mark.asyncio
async def test_write_intent_generates_proposal_and_never_executes_write():
    llm = FakeLLM(reply_json='{"intent":"write","entity":"lead","confidence":0.9}')
    memory = InMemoryMemory()
    executor = RecordingExecutor(
        result=ToolResult(
            tool="crm_writer.propose",
            ok=True,
            data={"proposal": {"proposal_id": "p1", "entity": "lead", "operation": "create", "payload": {"raw": "create lead"}, "requires_approval": True, "created_at": "x"}},
        )
    )
    deps = ChatDeps(llm=llm, tool_executor=executor, memory=memory, memory_window=12)
    graph = build_chat_graph(deps=deps)

    state = ChatState(query="create a lead for Acme", tenant_id="t1", user_id="u1", roles=["sales"], authorization="Bearer x", correlation_id="c1", conversation_id="conv1")
    out = await graph.ainvoke(state)

    assert executor.calls[0].tool == "crm_writer.propose"
    assert isinstance(out.get("action_proposals"), list) and len(out.get("action_proposals")) == 1


@pytest.mark.asyncio
async def test_multi_turn_persists_and_loads_history_window():
    llm = FakeLLM(reply_json='{"intent":"question","entity":"unknown","confidence":0.7}')
    memory = InMemoryMemory()
    executor = RecordingExecutor(result=ToolResult(tool="vector_search.search", ok=True, data=[]))
    deps = ChatDeps(llm=llm, tool_executor=executor, memory=memory, memory_window=12)
    graph = build_chat_graph(deps=deps)

    state1 = ChatState(query="what is the latest activity", tenant_id="t1", user_id="u1", roles=["sales"], authorization="Bearer x", correlation_id="c1", conversation_id="conv1")
    out1 = await graph.ainvoke(state1)
    assert out1.get("history") == []

    state2 = ChatState(query="and what about open tickets", tenant_id="t1", user_id="u1", roles=["sales"], authorization="Bearer x", correlation_id="c2", conversation_id="conv1")
    out2 = await graph.ainvoke(state2)
    assert isinstance(out2.get("history"), list)
    assert len(out2.get("history")) >= 2


from __future__ import annotations


import structlog

from .graph import ToolCall, ToolResult
from .tools import CrmReader, CrmWriter, SearchAdapter, VectorSearch
from governance.agent_telemetry import inc_proposal, inc_tool_call, inc_tool_success


logger = structlog.get_logger()


class ChatToolExecutor:
    def __init__(
        self,
        *,
        crm_reader: CrmReader,
        crm_writer: CrmWriter,
        vector_search: VectorSearch,
        search_adapter: SearchAdapter,
        agent_id: str = "chat-agent",
    ):
        self._crm_reader = crm_reader
        self._crm_writer = crm_writer
        self._vector_search = vector_search
        self._search_adapter = search_adapter
        self._agent_id = agent_id

    async def execute(
        self,
        *,
        tenant_id: str,
        user_id: str,
        roles: list[str],
        authorization: str | None,
        correlation_id: str | None,
        call: ToolCall,
    ) -> ToolResult:
        try:
            inc_tool_call(agent_id=self._agent_id, tool_name=call.tool)
            if call.tool.startswith("crm_reader.get_"):
                method = call.tool.split("crm_reader.", 1)[1]
                fn = getattr(self._crm_reader, method, None)
                if not fn:
                    inc_tool_success(agent_id=self._agent_id, tool_name=call.tool, status="error")
                    return ToolResult(tool=call.tool, ok=False, error="unknown_reader_tool")
                data = await fn(
                    tenant_id=tenant_id,
                    user_id=user_id,
                    authorization=authorization,
                    correlation_id=correlation_id,
                    **(call.args or {}),
                )
                inc_tool_success(agent_id=self._agent_id, tool_name=call.tool, status="ok")
                return ToolResult(tool=call.tool, ok=True, data=data)

            if call.tool == "crm_writer.propose":
                data = await self._crm_writer.propose(**(call.args or {}))
                if isinstance(data, dict) and isinstance(data.get("proposal"), dict):
                    p = data["proposal"]
                    inc_proposal(agent_id=self._agent_id, entity=str(p.get("entity") or "unknown"), operation=str(p.get("operation") or "unknown"))
                inc_tool_success(agent_id=self._agent_id, tool_name=call.tool, status="ok")
                return ToolResult(tool=call.tool, ok=True, data=data)

            if call.tool == "vector_search.search":
                data = await self._vector_search.search(
                    tenant_id=tenant_id,
                    query=str((call.args or {}).get("query") or ""),
                    top_k=int((call.args or {}).get("top_k") or 8),
                    entity=(call.args or {}).get("entity"),
                )
                inc_tool_success(agent_id=self._agent_id, tool_name=call.tool, status="ok")
                return ToolResult(tool=call.tool, ok=True, data=data)

            if call.tool == "search_adapter.search":
                data = await self._search_adapter.search(
                    tenant_id=tenant_id,
                    user_id=user_id,
                    roles=roles,
                    query=str((call.args or {}).get("query") or ""),
                    module=(call.args or {}).get("module"),
                    correlation_id=correlation_id,
                )
                inc_tool_success(agent_id=self._agent_id, tool_name=call.tool, status="ok")
                return ToolResult(tool=call.tool, ok=True, data=data)

            inc_tool_success(agent_id=self._agent_id, tool_name=call.tool, status="error")
            return ToolResult(tool=call.tool, ok=False, error="unknown_tool")
        except Exception as e:
            inc_tool_success(agent_id=self._agent_id, tool_name=call.tool, status="error")
            logger.warning("chat.tool_failed", tool=call.tool, error=str(e))
            return await self._fallback(
                tenant_id=tenant_id,
                user_id=user_id,
                roles=roles,
                correlation_id=correlation_id,
                call=call,
                error=str(e),
            )

    async def _fallback(
        self,
        *,
        tenant_id: str,
        user_id: str,
        roles: list[str],
        correlation_id: str | None,
        call: ToolCall,
        error: str,
    ) -> ToolResult:
        if call.tool == "search_adapter.search":
            return ToolResult(tool=call.tool, ok=False, error=error)
        query = str((call.args or {}).get("query") or (call.args or {}).get("raw") or "")
        if not query:
            return ToolResult(tool=call.tool, ok=False, error=error)
        try:
            data = await self._search_adapter.search(
                tenant_id=tenant_id,
                user_id=user_id,
                roles=roles,
                query=query,
                module=None,
                correlation_id=correlation_id,
            )
            inc_tool_success(agent_id=self._agent_id, tool_name="search_adapter.search", status="ok")
            return ToolResult(tool="search_adapter.search", ok=True, data=data)
        except Exception as e:
            inc_tool_success(agent_id=self._agent_id, tool_name="search_adapter.search", status="error")
            return ToolResult(tool=call.tool, ok=False, error=f"{error}; fallback_failed: {e}")


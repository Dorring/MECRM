from __future__ import annotations

from typing import Any


class SearchAdapter:
    def __init__(self, *, search_agent: Any):
        self._search_agent = search_agent

    async def search(
        self,
        *,
        tenant_id: str,
        user_id: str,
        roles: list[str],
        query: str,
        module: str | None,
        correlation_id: str | None,
    ) -> dict[str, Any]:
        return await self._search_agent.search(
            tenant_id=tenant_id,
            user_id=user_id,
            roles=roles,
            query=query,
            module=module,
            correlation_id=correlation_id,
        )


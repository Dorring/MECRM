from __future__ import annotations

from typing import Any

import httpx


class CrmReader:
    def __init__(self, *, gateway_url: str, timeout_seconds: float = 3.0):
        self._gateway_url = gateway_url.rstrip("/")
        self._timeout_seconds = timeout_seconds

    async def get_leads(
        self,
        *,
        tenant_id: str,
        user_id: str,
        authorization: str | None,
        correlation_id: str | None,
        limit: int = 10,
        page: int = 1,
        status: str | None = None,
        assigned_to: str | None = None,
    ) -> dict[str, Any]:
        params: dict[str, Any] = {"page": page, "limit": max(1, min(int(limit or 10), 100))}
        if status:
            params["status"] = status
        if assigned_to:
            params["assignedTo"] = assigned_to
        return await self._get(
            path="/api/v1/leads",
            tenant_id=tenant_id,
            user_id=user_id,
            authorization=authorization,
            correlation_id=correlation_id,
            params=params,
        )

    async def get_tickets(
        self,
        *,
        tenant_id: str,
        user_id: str,
        authorization: str | None,
        correlation_id: str | None,
        limit: int = 10,
        page: int = 1,
        status: str | None = None,
        priority: str | None = None,
        assigned_to: str | None = None,
    ) -> dict[str, Any]:
        params: dict[str, Any] = {"page": page, "limit": max(1, min(int(limit or 10), 100))}
        if status:
            params["status"] = status
        if priority:
            params["priority"] = priority
        if assigned_to:
            params["assignedTo"] = assigned_to
        return await self._get(
            path="/api/v1/tickets",
            tenant_id=tenant_id,
            user_id=user_id,
            authorization=authorization,
            correlation_id=correlation_id,
            params=params,
        )

    async def get_customers(
        self,
        *,
        tenant_id: str,
        user_id: str,
        authorization: str | None,
        correlation_id: str | None,
        limit: int = 10,
        page: int = 1,
        segment: str | None = None,
        status: str | None = None,
    ) -> dict[str, Any]:
        params: dict[str, Any] = {"page": page, "limit": max(1, min(int(limit or 10), 100))}
        if segment:
            params["segment"] = segment
        if status:
            params["status"] = status
        return await self._get(
            path="/api/v1/customers",
            tenant_id=tenant_id,
            user_id=user_id,
            authorization=authorization,
            correlation_id=correlation_id,
            params=params,
        )

    async def get_customer_risks(
        self,
        *,
        tenant_id: str,
        user_id: str,
        authorization: str | None,
        correlation_id: str | None,
        limit: int = 10,
        page: int = 1,
    ) -> dict[str, Any]:
        customers = await self.get_customers(
            tenant_id=tenant_id,
            user_id=user_id,
            authorization=authorization,
            correlation_id=correlation_id,
            limit=limit,
            page=page,
        )
        rows = customers.get("data") or []
        if not isinstance(rows, list):
            rows = []
        ids = [str(c.get("id") or "") for c in rows if isinstance(c, dict)]
        ids = [i for i in ids if i]
        if not ids:
            return {"customers": rows, "predictions": {}}
        pred = await self._get(
            path="/api/v1/predictions/latest",
            tenant_id=tenant_id,
            user_id=user_id,
            authorization=authorization,
            correlation_id=correlation_id,
            params={"entityType": "customer", "entityIds": ",".join(ids)},
        )
        return {"customers": rows, "predictions": pred.get("data") or {}}

    async def get_invoices(self, *, tenant_id: str, user_id: str, authorization: str | None, correlation_id: str | None, **_: Any) -> dict[str, Any]:
        return {"data": [], "pagination": {"page": 1, "limit": 0, "total": 0, "totalPages": 0}, "note": "Invoices API not implemented in this repo yet."}

    async def get_tasks(self, *, tenant_id: str, user_id: str, authorization: str | None, correlation_id: str | None, **_: Any) -> dict[str, Any]:
        return {"data": [], "pagination": {"page": 1, "limit": 0, "total": 0, "totalPages": 0}, "note": "Tasks API not implemented in this repo yet."}

    async def _get(
        self,
        *,
        path: str,
        tenant_id: str,
        user_id: str,
        authorization: str | None,
        correlation_id: str | None,
        params: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        headers = {
            "X-Tenant-Id": tenant_id,
            "X-User-Id": user_id,
        }
        if authorization:
            headers["Authorization"] = authorization
        if correlation_id:
            headers["X-Correlation-Id"] = correlation_id
        async with httpx.AsyncClient(timeout=self._timeout_seconds) as client:
            resp = await client.get(f"{self._gateway_url}{path}", headers=headers, params=params)
            resp.raise_for_status()
            return resp.json()


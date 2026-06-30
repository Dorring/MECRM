import uuid

import asyncpg
import pytest
import pytest_asyncio

from intelligence.search.ranker import rank_results
from intelligence.search.retriever import HybridRetriever, RetrievedResult


def _uuid() -> str:
    return str(uuid.uuid4())


@pytest_asyncio.fixture()
async def pool(database_url: str):
    pool = await asyncpg.create_pool(database_url, min_size=1, max_size=3)
    try:
        yield pool
    finally:
        await pool.close()


@pytest_asyncio.fixture()
async def seeded(pool: asyncpg.Pool):
    tenant_a = _uuid()
    tenant_b = _uuid()
    lead_a = _uuid()
    lead_b = _uuid()
    slug_a = f"tenant-a-{tenant_a[:8]}"
    slug_b = f"tenant-b-{tenant_b[:8]}"

    async with pool.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO tenants (id, name, slug, status, created_at, updated_at)
            VALUES ($1, 'Tenant A', $3, 'active', NOW(), NOW()),
                   ($2, 'Tenant B', $4, 'active', NOW(), NOW())
            """,
            tenant_a,
            tenant_b,
            slug_a,
            slug_b,
        )

        async with conn.transaction():
            await conn.execute(f"SET LOCAL app.tenant_id = '{tenant_a}'")
            await conn.execute(
                """
                INSERT INTO leads (id, tenant_id, name, email, company, status, created_at, updated_at)
                VALUES ($1, $2, 'Acme Prospect', 'acme-a@example.com', 'Acme', 'new', NOW(), NOW())
                """,
                lead_a,
                tenant_a,
            )

        async with conn.transaction():
            await conn.execute(f"SET LOCAL app.tenant_id = '{tenant_b}'")
            await conn.execute(
                """
                INSERT INTO leads (id, tenant_id, name, email, company, status, created_at, updated_at)
                VALUES ($1, $2, 'Acme Prospect', 'acme-b@example.com', 'Acme', 'new', NOW(), NOW())
                """,
                lead_b,
                tenant_b,
            )

    return {"tenant_a": tenant_a, "tenant_b": tenant_b, "lead_a": lead_a, "lead_b": lead_b}


@pytest.mark.asyncio
async def test_structured_search_is_tenant_scoped(database_url: str, seeded: dict[str, str]):
    r = HybridRetriever(
        database_url=database_url,
        weaviate_url="http://localhost:9999",
        ollama_url="http://localhost:11434",
        embedding_model="nomic-embed-text",
    )
    await r.start()
    try:
        results = await r.structured_search(
            tenant_id=seeded["tenant_a"],
            query="Acme",
            entity=None,
            filters=None,
            limit=20,
        )
        ids = {x.entity_id for x in results}
        assert seeded["lead_a"] in ids
        assert seeded["lead_b"] not in ids
        assert all(x.tenant_id == seeded["tenant_a"] for x in results)
    finally:
        await r.close()


def test_ranking_prefers_role_and_recency():
    now = None
    items = [
        RetrievedResult(
            entity_type="ticket",
            entity_id="t1",
            tenant_id="tenant",
            title="Ticket",
            description=None,
            created_at=now,
            updated_at=now,
            source="structured",
            structured_score=1.0,
            semantic_score=0.0,
            metadata=None,
        ),
        RetrievedResult(
            entity_type="lead",
            entity_id="l1",
            tenant_id="tenant",
            title="Lead",
            description=None,
            created_at=now,
            updated_at=now,
            source="structured",
            structured_score=1.0,
            semantic_score=0.0,
            metadata=None,
        ),
    ]
    ranked = rank_results(query="test", roles=["support_agent"], module="/tickets", results=items, limit=10)
    assert ranked[0].entity_type == "ticket"


@pytest.mark.asyncio
async def test_semantic_search_weaviate_down_returns_empty(database_url: str, seeded: dict[str, str]):
    r = HybridRetriever(
        database_url=database_url,
        weaviate_url="http://localhost:9999",
        ollama_url="http://localhost:11434",
        embedding_model="nomic-embed-text",
    )
    await r.start()
    try:
        results = await r.semantic_search(tenant_id=seeded["tenant_a"], query="Acme", entity=None, limit=5)
        assert results == []
    finally:
        await r.close()


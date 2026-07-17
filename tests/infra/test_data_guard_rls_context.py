"""Static regression guards for transactional RLS context in DataGuard."""

from pathlib import Path


DATA_GUARD = Path("agents/src/governance/data_guard.py")


def test_customer_guard_keeps_rls_setting_and_query_in_one_transaction():
    source = DATA_GUARD.read_text(encoding="utf-8")
    start = source.index("async def _ensure_customer_allowed")
    end = source.index("async def _ensure_user_allowed", start)
    method = source[start:end]

    assert "async with conn.transaction():" in method
    assert method.index("async with conn.transaction():") < method.index("set_config('app.tenant_id'")
    assert method.index("set_config('app.tenant_id'") < method.index("SELECT deletion_type")


def test_user_guard_keeps_rls_setting_and_query_in_one_transaction():
    source = DATA_GUARD.read_text(encoding="utf-8")
    start = source.index("async def _ensure_user_allowed")
    end = source.index("async def _audit_violation", start)
    method = source[start:end]

    assert "async with conn.transaction():" in method
    assert method.index("async with conn.transaction():") < method.index("set_config('app.tenant_id'")
    assert method.index("set_config('app.tenant_id'") < method.index("SELECT deletion_type")

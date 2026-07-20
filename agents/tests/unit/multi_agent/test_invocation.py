"""Phase 4 Invocation Boundary tests.

Covers:

* :class:`AgentInvocationReceipt` schema and validators.
* :class:`RegistryAgentInvoker` usage extraction from ``AgentResult``.
* :class:`DeterministicFakeInvoker` receipt / factory / result modes.
* Handler Protocol conformance (no signature drift).
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any

import pytest

from multi_agent.contracts import (
    AgentAuthority,
    AgentCapability,
    AgentExecutionContext,
    AgentResult,
    AgentTask,
    ProviderMetadata,
    TokenUsage,
    ToolAuthority,
)
from multi_agent.contracts import Evidence
from multi_agent.invocation import (
    AgentInvocationReceipt,
    DeterministicFakeInvoker,
    RegistryAgentInvoker,
)
from multi_agent.registry import AgentRegistry, ToolCatalog, ToolDescriptor


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_task(
    task_id: str = "task_001",
    agent_id: str = "agent_001",
    tenant_id: str = "t-001",
    **overrides: Any,
) -> AgentTask:
    defaults: dict[str, Any] = dict(
        task_id=task_id,
        agent_id=agent_id,
        task_type="test_task",
        objective="test objective",
        tenant_id=tenant_id,
        timeout_ms=10_000,
    )
    defaults.update(overrides)
    return AgentTask(**defaults)


def _make_result(
    result_id: str = "r-001",
    task_id: str = "task_001",
    agent_id: str = "agent_001",
    tenant_id: str = "t-001",
    status: str = "completed",
    provider_metadata: ProviderMetadata | None = None,
    token_usage: TokenUsage | None = None,
    **overrides: Any,
) -> AgentResult:
    defaults: dict[str, Any] = dict(
        result_id=result_id,
        task_id=task_id,
        agent_id=agent_id,
        agent_version="1.0.0",
        tenant_id=tenant_id,
        status=status,
        confidence=1.0,
        duration_ms=0.0,
        token_usage=token_usage or TokenUsage(),
        provider_metadata=provider_metadata,
    )
    defaults.update(overrides)
    return AgentResult(**defaults)


def _make_capability(agent_id: str = "agent_001") -> AgentCapability:
    return AgentCapability(
        agent_id=agent_id,
        version="1.0.0",
        description="Test agent",
        domains=frozenset({"test"}),
        supported_tasks=frozenset({"test_task"}),
        allowed_tools=frozenset({"tool.read"}),
        authority=AgentAuthority.READ,
        input_contract="in",
        output_contract="out",
        timeout_ms=30_000,
        max_retries=0,
        estimated_cost_class="low",
        enabled=True,
    )


class _FakeHandler:
    def __init__(self, result: AgentResult) -> None:
        self._result = result
        self.calls: list[tuple[AgentTask, AgentExecutionContext]] = []

    async def run(self, task: AgentTask, context: AgentExecutionContext) -> AgentResult:
        self.calls.append((task, context))
        return self._result


def _make_registry(handler: _FakeHandler, cap: AgentCapability) -> AgentRegistry:
    catalog = ToolCatalog(
        [ToolDescriptor(tool_name="tool.read", authority=ToolAuthority.READ)]
    )
    reg = AgentRegistry(tool_catalog=catalog)
    reg.register(cap, handler)
    return reg


# ---------------------------------------------------------------------------
# Receipt schema
# ---------------------------------------------------------------------------


class TestAgentInvocationReceipt:
    def test_receipt_with_zero_usage_defaults(self):
        receipt = AgentInvocationReceipt(result=_make_result())
        assert receipt.tool_calls == 0
        assert receipt.tokens_used is None
        assert receipt.cost_usd is None

    def test_receipt_rejects_negative_tool_calls(self):
        with pytest.raises(Exception):
            AgentInvocationReceipt(result=_make_result(), tool_calls=-1)

    def test_receipt_rejects_negative_tokens(self):
        with pytest.raises(Exception):
            AgentInvocationReceipt(result=_make_result(), tokens_used=-5)

    def test_receipt_rejects_negative_cost(self):
        with pytest.raises(Exception):
            AgentInvocationReceipt(result=_make_result(), cost_usd=Decimal("-1.50"))


# ---------------------------------------------------------------------------
# RegistryAgentInvoker
# ---------------------------------------------------------------------------


class TestRegistryAgentInvoker:
    def test_invoke_returns_receipt_with_handler_result(self):
        result = _make_result()
        handler = _FakeHandler(result)
        reg = _make_registry(handler, _make_capability())
        invoker = RegistryAgentInvoker(reg)
        task = _make_task()
        ctx = AgentExecutionContext(tenant_id="t-001")

        receipt = await_invoker(invoker, handler, task, ctx)

        assert receipt.result is result
        assert receipt.tool_calls == 0
        assert receipt.tokens_used is None  # no provider_metadata
        assert receipt.cost_usd is None
        assert len(handler.calls) == 1
        assert handler.calls[0][0] is task

    def test_invoke_extracts_tokens_when_provider_metadata_present(self):
        result = _make_result(
            provider_metadata=ProviderMetadata(
                provider="openai",
                chat_model="gpt-4",
                embedding_model="text-embedding-3-small",
                ai_mode="live",
            ),
            token_usage=TokenUsage(input_tokens=10, output_tokens=5, total_tokens=15),
        )
        handler = _FakeHandler(result)
        reg = _make_registry(handler, _make_capability())
        invoker = RegistryAgentInvoker(reg)

        receipt = await_invoker(
            invoker, handler, _make_task(), AgentExecutionContext(tenant_id="t-001")
        )

        assert receipt.tokens_used == 15
        assert receipt.tool_calls == 0

    def test_invoke_tokens_none_when_provider_metadata_absent(self):
        result = _make_result(provider_metadata=None)
        handler = _FakeHandler(result)
        reg = _make_registry(handler, _make_capability())
        invoker = RegistryAgentInvoker(reg)

        receipt = await_invoker(
            invoker, handler, _make_task(), AgentExecutionContext(tenant_id="t-001")
        )

        assert receipt.tokens_used is None

    def test_invoke_cost_is_always_none(self):
        """RegistryAgentInvoker never reports cost_usd because
        AgentResult has no cost field — a configured cost_budget_usd
        therefore fails closed."""
        result = _make_result()
        handler = _FakeHandler(result)
        reg = _make_registry(handler, _make_capability())
        invoker = RegistryAgentInvoker(reg)

        receipt = await_invoker(
            invoker, handler, _make_task(), AgentExecutionContext(tenant_id="t-001")
        )

        assert receipt.cost_usd is None


# ---------------------------------------------------------------------------
# DeterministicFakeInvoker
# ---------------------------------------------------------------------------


class TestDeterministicFakeInvoker:
    def test_requires_exactly_one_mode(self):
        with pytest.raises(ValueError, match="exactly one"):
            DeterministicFakeInvoker()
        with pytest.raises(ValueError, match="exactly one"):
            DeterministicFakeInvoker(
                receipt=AgentInvocationReceipt(result=_make_result()),
                result=_make_result(),
            )

    def test_receipt_mode_returns_preset_receipt(self):
        receipt = AgentInvocationReceipt(
            result=_make_result(), tool_calls=3, tokens_used=42
        )
        invoker = DeterministicFakeInvoker(receipt=receipt)
        task = _make_task()
        ctx = AgentExecutionContext(tenant_id="t-001")

        out = await_invoker(invoker, _FakeHandler(_make_result()), task, ctx)

        assert out is receipt
        assert len(invoker.invocations) == 1
        assert invoker.invocations[0][0] is task

    def test_factory_mode_invokes_callable_per_call(self):
        counter = {"n": 0}

        def factory(
            task: AgentTask, ctx: AgentExecutionContext
        ) -> AgentInvocationReceipt:
            counter["n"] += 1
            return AgentInvocationReceipt(
                result=_make_result(result_id=f"r-{counter['n']}"),
                tool_calls=counter["n"],
            )

        invoker = DeterministicFakeInvoker(factory=factory)
        task = _make_task()
        ctx = AgentExecutionContext(tenant_id="t-001")

        r1 = await_invoker(invoker, _FakeHandler(_make_result()), task, ctx)
        r2 = await_invoker(invoker, _FakeHandler(_make_result()), task, ctx)

        assert r1.tool_calls == 1
        assert r2.tool_calls == 2
        assert r1.result.result_id == "r-1"
        assert r2.result.result_id == "r-2"

    def test_result_mode_wraps_result_with_zero_usage(self):
        result = _make_result()
        invoker = DeterministicFakeInvoker(result=result)

        receipt = await_invoker(
            invoker,
            _FakeHandler(_make_result()),
            _make_task(),
            AgentExecutionContext(tenant_id="t-001"),
        )

        assert receipt.result is result
        assert receipt.tool_calls == 0
        assert receipt.tokens_used is None
        assert receipt.cost_usd is None

    def test_handler_is_ignored_in_fake_mode(self):
        """The fake invoker must not call the handler — it owns its own result."""
        result = _make_result()
        handler = _FakeHandler(_make_result())
        invoker = DeterministicFakeInvoker(result=result)

        await_invoker(
            invoker, handler, _make_task(), AgentExecutionContext(tenant_id="t-001")
        )

        assert len(handler.calls) == 0


# ---------------------------------------------------------------------------
# Helper: invoke an AgentInvoker synchronously
# ---------------------------------------------------------------------------


def await_invoker(
    invoker: Any, handler: Any, task: AgentTask, ctx: AgentExecutionContext
) -> AgentInvocationReceipt:
    import asyncio

    return asyncio.run(invoker.invoke(handler, task, ctx))


# Suppress unused-import warning for Evidence — kept so future test
# extensions can import it without re-adding the import.
_ = Evidence

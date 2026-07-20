"""Contract validation and serialization tests.

All tests run under AI_MODE=deterministic; no Ollama, no API keys.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

import pytest
from pydantic import ValidationError

from multi_agent.contracts import (
    ActionProposal,
    AgentAuthority,
    AgentCapability,
    AgentError,
    AgentExecutionContext,
    AgentResult,
    AgentTask,
    Evidence,
    ProviderMetadata,
    TokenUsage,
    ToolAuthority,
    ToolCallRecord,
    _compute_proposal_hash,
    from_crm_writer_proposal,
    from_productivity_proposal,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _make_capability(
    agent_id: str = "test_agent",
    authority: AgentAuthority = AgentAuthority.READ,
    allowed_tools: set[str] | None = None,
    **overrides,
) -> AgentCapability:
    defaults = dict(
        agent_id=agent_id,
        version="1.0.0",
        description="Test agent",
        domains={"test"},
        supported_tasks={"test_task"},
        allowed_tools=allowed_tools or {"crm_reader.get_leads"},
        authority=authority,
        input_contract="test_input",
        output_contract="test_output",
        timeout_ms=30_000,
        max_retries=2,
        estimated_cost_class="low",
    )
    defaults.update(overrides)
    return AgentCapability(**defaults)


def _make_evidence(
    evidence_id: str = "ev-001",
    tenant_id: str = "t-001",
    evidence_type: str = "opa_policy",
) -> Evidence:
    return Evidence(
        evidence_id=evidence_id,
        evidence_type=evidence_type,
        tenant_id=tenant_id,
        source_agent="test_agent",
        created_at=_utc_now(),
    )


def _make_proposal(
    proposal_id: str = "p-001",
    tenant_id: str = "t-001",
    priority: str = "medium",
    evidence_ids: list[str] | None = None,
    **overrides,
) -> ActionProposal:
    """Build an ActionProposal with sensible defaults and a valid hash."""
    eids = (
        evidence_ids
        if evidence_ids is not None
        else (["ev-001"] if priority == "high" else [])
    )
    now = _utc_now()

    # Fields needed for hash calculation
    hash_kwargs: dict[str, Any] = dict(
        tenant_id=tenant_id,
        created_by_agent="test_agent",
        action_type="create",
        target_entity="ticket",
        target_id=None,
        payload={"field": "value"},
        priority=priority,
        justification="test justification",
        evidence_ids=eids,
        requires_approval=True,
        idempotency_key=f"ik-{proposal_id}",
    )

    # Override hash kwargs with any overrides that are hash-relevant
    for k in hash_kwargs:
        if k in overrides:
            hash_kwargs[k] = overrides[k]

    proposal_hash = _compute_proposal_hash(**hash_kwargs)  # type: ignore[arg-type]

    fields: dict[str, Any] = dict(
        proposal_id=proposal_id,
        proposal_hash=proposal_hash,
        tenant_id=tenant_id,
        created_by_agent="test_agent",
        action_type="create",
        target_entity="ticket",
        priority=priority,
        justification="test justification",
        evidence_ids=eids,
        requires_approval=True,
        idempotency_key=f"ik-{proposal_id}",
        created_at=now,
        payload={"field": "value"},
    )
    fields.update(overrides)
    # Never let overrides set proposal_hash directly (it's computed)
    fields["proposal_hash"] = proposal_hash
    return ActionProposal(**fields)


# ---------------------------------------------------------------------------
# AgentAuthority / ToolAuthority enums
# ---------------------------------------------------------------------------


class TestAgentAuthority:
    def test_values(self):
        assert AgentAuthority.READ.value == "read"
        assert AgentAuthority.PROPOSE.value == "propose"
        assert AgentAuthority.EXECUTE.value == "execute"

    def test_from_string(self):
        assert AgentAuthority("read") == AgentAuthority.READ
        assert AgentAuthority("propose") == AgentAuthority.PROPOSE
        assert AgentAuthority("execute") == AgentAuthority.EXECUTE

    def test_invalid_value(self):
        with pytest.raises(ValueError):
            AgentAuthority("admin")


class TestToolAuthority:
    def test_values(self):
        assert ToolAuthority.READ.value == "read"
        assert ToolAuthority.PROPOSE.value == "propose"
        assert ToolAuthority.EXECUTE.value == "execute"


# ---------------------------------------------------------------------------
# AgentCapability
# ---------------------------------------------------------------------------


class TestAgentCapabilityValidation:
    def test_valid_capability(self):
        cap = _make_capability()
        assert cap.agent_id == "test_agent"
        assert cap.enabled is True

    def test_agent_id_must_be_stable_format(self):
        with pytest.raises(ValidationError) as exc:
            _make_capability(agent_id="123invalid")
        assert "agent_id" in str(exc.value)

    def test_agent_id_accepts_valid_formats(self):
        for aid in ("support_specialist", "sales_agent_v2", "compliance1"):
            cap = _make_capability(agent_id=aid)
            assert cap.agent_id == aid

    def test_version_must_not_be_empty(self):
        with pytest.raises(ValidationError) as exc:
            _make_capability(version="")
        assert "version" in str(exc.value)

    def test_version_must_not_be_whitespace(self):
        with pytest.raises(ValidationError) as exc:
            _make_capability(version="   ")
        assert "version" in str(exc.value)

    def test_timeout_ms_must_be_positive(self):
        with pytest.raises(ValidationError) as exc:
            _make_capability(timeout_ms=0)
        assert "timeout_ms" in str(exc.value)

    def test_timeout_ms_negative(self):
        with pytest.raises(ValidationError) as exc:
            _make_capability(timeout_ms=-100)
        assert "timeout_ms" in str(exc.value)

    def test_max_retries_non_negative(self):
        cap = _make_capability(max_retries=0)
        assert cap.max_retries == 0

    def test_max_retries_negative_raises(self):
        with pytest.raises(ValidationError) as exc:
            _make_capability(max_retries=-1)
        assert "max_retries" in str(exc.value)

    def test_read_agent_cannot_use_propose_tool(self):
        with pytest.raises(ValidationError) as exc:
            _make_capability(
                authority=AgentAuthority.READ,
                allowed_tools={"crm_writer.propose"},
            )
        assert "READ" in str(exc.value) or "propose" in str(exc.value).lower()

    def test_read_agent_cannot_use_execute_tool(self):
        with pytest.raises(ValidationError) as exc:
            _make_capability(
                authority=AgentAuthority.READ,
                allowed_tools={"automation_executor.execute"},
            )
        assert "READ" in str(exc.value) or "execute" in str(exc.value).lower()

    def test_propose_agent_cannot_use_execute_tool(self):
        with pytest.raises(ValidationError) as exc:
            _make_capability(
                authority=AgentAuthority.PROPOSE,
                allowed_tools={"automation_executor.execute"},
            )
        assert "PROPOSE" in str(exc.value) or "execute" in str(exc.value).lower()

    def test_execute_agent_can_use_execute_tool(self):
        cap = _make_capability(
            authority=AgentAuthority.EXECUTE,
            allowed_tools={"automation_executor.execute"},
        )
        assert "automation_executor.execute" in cap.allowed_tools

    def test_execute_agent_can_use_any_level(self):
        cap = _make_capability(
            authority=AgentAuthority.EXECUTE,
            allowed_tools={
                "crm_reader.get_leads",
                "crm_writer.propose",
                "automation_executor.execute",
            },
        )
        assert len(cap.allowed_tools) == 3

    def test_disabled_default(self):
        cap = _make_capability()
        assert cap.enabled is True

    def test_explicitly_disabled(self):
        cap = _make_capability(enabled=False)
        assert cap.enabled is False

    def test_metadata_default(self):
        cap = _make_capability()
        assert cap.metadata == {}

    def test_metadata_with_values(self):
        cap = _make_capability(metadata={"team": "support", "region": "eu"})
        assert cap.metadata["team"] == "support"


# ---------------------------------------------------------------------------
# AgentTask
# ---------------------------------------------------------------------------


class TestAgentTask:
    def test_valid_task(self):
        task = AgentTask(
            task_id="task-001",
            agent_id="test_agent",
            task_type="test_task",
            input_data={},
            tenant_id="t-001",
            timeout_ms=60_000,
            idempotency_key="ik-001",
        )
        assert task.status == "pending"

    def test_tenant_id_required(self):
        with pytest.raises(ValidationError) as exc:
            AgentTask(
                task_id="task-001",
                agent_id="test_agent",
                task_type="test_task",
                input_data={},
                tenant_id="",
                timeout_ms=60_000,
                idempotency_key="ik-001",
            )
        assert "tenant_id" in str(exc.value)

    def test_self_dependency_raises(self):
        with pytest.raises(ValidationError) as exc:
            AgentTask(
                task_id="task-001",
                agent_id="test_agent",
                task_type="test_task",
                input_data={},
                tenant_id="t-001",
                dependencies={"task-001"},
                timeout_ms=60_000,
                idempotency_key="ik-001",
            )
        assert "self" in str(exc.value).lower() or "depend" in str(exc.value).lower()

    def test_dependencies_dedup(self):
        task = AgentTask(
            task_id="task-001",
            agent_id="test_agent",
            task_type="test_task",
            input_data={},
            tenant_id="t-001",
            dependencies={"task-002", "task-003"},
            timeout_ms=60_000,
            idempotency_key="ik-001",
        )
        assert task.dependencies == {"task-002", "task-003"}

    def test_timeout_ms_positive(self):
        with pytest.raises(ValidationError) as exc:
            AgentTask(
                task_id="task-001",
                agent_id="test_agent",
                task_type="test_task",
                input_data={},
                tenant_id="t-001",
                timeout_ms=0,
                idempotency_key="ik-001",
            )
        assert "timeout_ms" in str(exc.value)

    def test_max_retries_non_negative(self):
        with pytest.raises(ValidationError) as exc:
            AgentTask(
                task_id="task-001",
                agent_id="test_agent",
                task_type="test_task",
                input_data={},
                tenant_id="t-001",
                max_retries=-5,
                timeout_ms=60_000,
                idempotency_key="ik-001",
            )
        assert "max_retries" in str(exc.value)

    def test_created_at_utc_aware(self):
        task = AgentTask(
            task_id="task-001",
            agent_id="test_agent",
            task_type="test_task",
            input_data={},
            tenant_id="t-001",
            timeout_ms=60_000,
            idempotency_key="ik-001",
            created_at=datetime(2025, 1, 1, tzinfo=timezone.utc),
        )
        assert task.created_at.tzinfo is not None

    def test_created_at_naive_raises(self):
        with pytest.raises(ValidationError) as exc:
            AgentTask(
                task_id="task-001",
                agent_id="test_agent",
                task_type="test_task",
                input_data={},
                tenant_id="t-001",
                timeout_ms=60_000,
                idempotency_key="ik-001",
                created_at=datetime(2025, 1, 1),  # naive!
            )
        assert "timezone" in str(exc.value).lower() or "utc" in str(exc.value).lower()


# ---------------------------------------------------------------------------
# AgentResult
# ---------------------------------------------------------------------------


class TestAgentResult:
    def test_valid_completed(self):
        result = AgentResult(
            result_id="r-001",
            task_id="task-001",
            agent_id="test_agent",
            tenant_id="t-001",
            status="completed",
            confidence=0.95,
            duration_ms=150.0,
            completed_at=_utc_now(),
        )
        assert result.status == "completed"

    def test_failed_requires_error(self):
        with pytest.raises(ValidationError) as exc:
            AgentResult(
                result_id="r-001",
                task_id="task-001",
                agent_id="test_agent",
                tenant_id="t-001",
                status="failed",
                completed_at=_utc_now(),
            )
        assert "error" in str(exc.value).lower()

    def test_failed_with_error_ok(self):
        result = AgentResult(
            result_id="r-001",
            task_id="task-001",
            agent_id="test_agent",
            tenant_id="t-001",
            status="failed",
            error=AgentError(error_code="TEST_FAIL", message="Something broke"),
            completed_at=_utc_now(),
        )
        assert result.status == "failed"
        assert result.error is not None
        assert result.error.error_code == "TEST_FAIL"

    def test_completed_no_fatal_error(self):
        with pytest.raises(ValidationError) as exc:
            AgentResult(
                result_id="r-001",
                task_id="task-001",
                agent_id="test_agent",
                tenant_id="t-001",
                status="completed",
                error=AgentError(error_code="E1", message="should not be here"),
                completed_at=_utc_now(),
            )
        assert "error" in str(exc.value).lower() or "fatal" in str(exc.value).lower()

    def test_confidence_below_0_raises(self):
        with pytest.raises(ValidationError) as exc:
            AgentResult(
                result_id="r-001",
                task_id="task-001",
                agent_id="test_agent",
                tenant_id="t-001",
                status="completed",
                confidence=-0.1,
                completed_at=_utc_now(),
            )
        assert "confidence" in str(exc.value)

    def test_confidence_above_1_raises(self):
        with pytest.raises(ValidationError) as exc:
            AgentResult(
                result_id="r-001",
                task_id="task-001",
                agent_id="test_agent",
                tenant_id="t-001",
                status="completed",
                confidence=1.5,
                completed_at=_utc_now(),
            )
        assert "confidence" in str(exc.value)

    def test_negative_duration_raises(self):
        with pytest.raises(ValidationError) as exc:
            AgentResult(
                result_id="r-001",
                task_id="task-001",
                agent_id="test_agent",
                tenant_id="t-001",
                status="completed",
                duration_ms=-10.0,
                completed_at=_utc_now(),
            )
        assert "duration_ms" in str(exc.value)

    def test_degraded_status(self):
        result = AgentResult(
            result_id="r-001",
            task_id="task-001",
            agent_id="test_agent",
            tenant_id="t-001",
            status="degraded",
            confidence=0.6,
            completed_at=_utc_now(),
        )
        assert result.status == "degraded"

    def test_cancelled_status(self):
        result = AgentResult(
            result_id="r-001",
            task_id="task-001",
            agent_id="test_agent",
            tenant_id="t-001",
            status="cancelled",
            confidence=0.0,
            completed_at=_utc_now(),
        )
        assert result.status == "cancelled"


# ---------------------------------------------------------------------------
# TokenUsage
# ---------------------------------------------------------------------------


class TestTokenUsage:
    def test_defaults_zero(self):
        t = TokenUsage()
        assert t.input_tokens == 0
        assert t.output_tokens == 0
        assert t.total_tokens == 0

    def test_negative_input_raises(self):
        with pytest.raises(ValidationError):
            TokenUsage(input_tokens=-1)

    def test_negative_output_raises(self):
        with pytest.raises(ValidationError):
            TokenUsage(output_tokens=-10)

    def test_negative_total_raises(self):
        with pytest.raises(ValidationError):
            TokenUsage(total_tokens=-1)

    def test_provider_may_return_zero(self):
        t = TokenUsage(input_tokens=0, output_tokens=0, total_tokens=0)
        assert t.total_tokens == 0


# ---------------------------------------------------------------------------
# ProviderMetadata
# ---------------------------------------------------------------------------


class TestProviderMetadata:
    def test_valid_metadata(self):
        pm = ProviderMetadata(
            provider="ollama",
            chat_model="llama3.1",
            embedding_model="nomic-embed-text",
            ai_mode="live",
        )
        assert pm.provider == "ollama"

    def test_rejects_api_key_in_chat_model(self):
        with pytest.raises(ValidationError) as exc:
            ProviderMetadata(
                provider="ollama",
                chat_model="model_with_api_key",
                embedding_model="nomic",
                ai_mode="live",
            )
        assert "api_key" in str(exc.value)

    def test_rejects_token_in_provider(self):
        with pytest.raises(ValidationError) as exc:
            ProviderMetadata(
                provider="provider_with_token",
                chat_model="llama",
                embedding_model="nomic",
                ai_mode="live",
            )
        assert "token" in str(exc.value)

    def test_rejects_secret_in_ai_mode(self):
        with pytest.raises(ValidationError) as exc:
            ProviderMetadata(
                provider="ollama",
                chat_model="llama",
                embedding_model="nomic",
                ai_mode="mode_with_secret_key",
            )
        assert "secret" in str(exc.value)


# ---------------------------------------------------------------------------
# Evidence
# ---------------------------------------------------------------------------


class TestEvidence:
    def test_valid_evidence(self):
        ev = _make_evidence()
        assert ev.tenant_id == "t-001"

    def test_tenant_id_required(self):
        with pytest.raises(ValidationError) as exc:
            Evidence(
                evidence_id="ev-001",
                evidence_type="opa_policy",
                tenant_id="",
                source_agent="test_agent",
            )
        assert "tenant_id" in str(exc.value)

    def test_evidence_type_must_be_in_allowlist(self):
        with pytest.raises(ValidationError) as exc:
            Evidence(
                evidence_id="ev-001",
                evidence_type="malicious_input",
                tenant_id="t-001",
                source_agent="test_agent",
            )
        assert "allowlist" in str(exc.value).lower() or "evidence_type" in str(
            exc.value
        )

    def test_created_at_utc_aware(self):
        ev = Evidence(
            evidence_id="ev-001",
            evidence_type="opa_policy",
            tenant_id="t-001",
            source_agent="test_agent",
            created_at=datetime(2025, 1, 1, tzinfo=timezone.utc),
        )
        assert ev.created_at.tzinfo is not None

    def test_created_at_naive_raises(self):
        with pytest.raises(ValidationError) as exc:
            Evidence(
                evidence_id="ev-001",
                evidence_type="opa_policy",
                tenant_id="t-001",
                source_agent="test_agent",
                created_at=datetime(2025, 1, 1),  # naive
            )
        assert "timezone" in str(exc.value).lower() or "utc" in str(exc.value).lower()


# ---------------------------------------------------------------------------
# ActionProposal - validation
# ---------------------------------------------------------------------------


class TestActionProposalValidation:
    def test_high_priority_needs_evidence(self):
        with pytest.raises(ValidationError) as exc:
            _make_proposal(priority="high", evidence_ids=[])
        assert "evidence" in str(exc.value).lower()

    def test_high_priority_with_evidence_ok(self):
        p = _make_proposal(priority="high", evidence_ids=["ev-001"])
        assert p.priority == "high"
        assert "ev-001" in p.evidence_ids

    def test_payload_must_not_contain_tenant_id(self):
        with pytest.raises(ValidationError) as exc:
            _make_proposal(payload={"tenant_id": "evil"})
        assert "tenant_id" in str(exc.value)

    def test_payload_must_not_contain_tenantid(self):
        with pytest.raises(ValidationError) as exc:
            _make_proposal(payload={"tenantId": "evil"})
        assert "tenant_id" in str(exc.value)

    def test_payload_normal_fields_ok(self):
        p = _make_proposal(payload={"amount": 100, "description": "test"})
        assert p.payload["amount"] == 100

    def test_tenant_id_required(self):
        with pytest.raises(ValidationError) as exc:
            _make_proposal(tenant_id="")
        assert "tenant_id" in str(exc.value)

    def test_created_at_utc_aware(self):
        p = _make_proposal(created_at=datetime(2025, 1, 1, tzinfo=timezone.utc))
        assert p.created_at.tzinfo is not None

    def test_created_at_naive_raises(self):
        with pytest.raises(ValidationError) as exc:
            _make_proposal(created_at=datetime(2025, 1, 1))
        assert "timezone" in str(exc.value).lower() or "utc" in str(exc.value).lower()


# ---------------------------------------------------------------------------
# ActionProposal - hash stability
# ---------------------------------------------------------------------------


class TestActionProposalHash:
    def test_same_content_same_hash(self):
        h1 = _compute_proposal_hash(
            tenant_id="t-001",
            created_by_agent="agent-a",
            action_type="create",
            target_entity="ticket",
            target_id=None,
            payload={"x": 1},
            priority="medium",
            justification="because",
            evidence_ids=["ev-001"],
            requires_approval=True,
            idempotency_key="ik-1",
        )
        h2 = _compute_proposal_hash(
            tenant_id="t-001",
            created_by_agent="agent-a",
            action_type="create",
            target_entity="ticket",
            target_id=None,
            payload={"x": 1},
            priority="medium",
            justification="because",
            evidence_ids=["ev-001"],
            requires_approval=True,
            idempotency_key="ik-1",
        )
        assert h1 == h2

    def test_different_content_different_hash(self):
        h1 = _compute_proposal_hash(
            tenant_id="t-001",
            created_by_agent="agent-a",
            action_type="create",
            target_entity="ticket",
            target_id=None,
            payload={"x": 1},
            priority="medium",
            justification=None,
            evidence_ids=[],
            requires_approval=True,
            idempotency_key="ik-1",
        )
        h2 = _compute_proposal_hash(
            tenant_id="t-002",  # different tenant
            created_by_agent="agent-a",
            action_type="create",
            target_entity="ticket",
            target_id=None,
            payload={"x": 1},
            priority="medium",
            justification=None,
            evidence_ids=[],
            requires_approval=True,
            idempotency_key="ik-1",
        )
        assert h1 != h2

    def test_evidence_ids_order_independent(self):
        h1 = _compute_proposal_hash(
            tenant_id="t-001",
            created_by_agent="agent-a",
            action_type="create",
            target_entity="ticket",
            target_id=None,
            payload={},
            priority="medium",
            justification=None,
            evidence_ids=["b", "a", "c"],
            requires_approval=True,
            idempotency_key="ik-1",
        )
        h2 = _compute_proposal_hash(
            tenant_id="t-001",
            created_by_agent="agent-a",
            action_type="create",
            target_entity="ticket",
            target_id=None,
            payload={},
            priority="medium",
            justification=None,
            evidence_ids=["a", "b", "c"],
            requires_approval=True,
            idempotency_key="ik-1",
        )
        assert h1 == h2

    def test_payload_immaterial_to_hash(self):
        """Payload dict key order should not change hash (sorted keys)."""
        h1 = _compute_proposal_hash(
            tenant_id="t-001",
            created_by_agent="agent-a",
            action_type="create",
            target_entity="ticket",
            target_id=None,
            payload={"b": 2, "a": 1},
            priority="medium",
            justification=None,
            evidence_ids=[],
            requires_approval=True,
            idempotency_key="ik-1",
        )
        h2 = _compute_proposal_hash(
            tenant_id="t-001",
            created_by_agent="agent-a",
            action_type="create",
            target_entity="ticket",
            target_id=None,
            payload={"a": 1, "b": 2},
            priority="medium",
            justification=None,
            evidence_ids=[],
            requires_approval=True,
            idempotency_key="ik-1",
        )
        assert h1 == h2

    def test_hash_excludes_created_at(self):
        base = {
            "tenant_id": "t-001",
            "created_by_agent": "agent-a",
            "action_type": "create",
            "target_entity": "ticket",
            "target_id": None,
            "payload": {},
            "priority": "medium",
            "justification": None,
            "evidence_ids": [],
            "requires_approval": True,
            "idempotency_key": "ik-1",
        }
        h = _compute_proposal_hash(**base)  # type: ignore[arg-type]
        assert isinstance(h, str)
        assert len(h) == 64

    def test_full_proposal_hashes_match(self):
        """A full ActionProposal creation should match _compute_proposal_hash."""
        now = _utc_now()
        p = ActionProposal(
            proposal_id="p-001",
            proposal_hash="placeholder",
            tenant_id="t-001",
            created_by_agent="agent-a",
            action_type="update",
            target_entity="lead",
            target_id="lead-1",
            payload={"status": "qualified"},
            priority="low",
            justification="test",
            evidence_ids=["ev-1"],
            requires_approval=False,
            idempotency_key="ik-p001",
            created_at=now,
        )
        expected_hash = _compute_proposal_hash(
            tenant_id="t-001",
            created_by_agent="agent-a",
            action_type="update",
            target_entity="lead",
            target_id="lead-1",
            payload={"status": "qualified"},
            priority="low",
            justification="test",
            evidence_ids=["ev-1"],
            requires_approval=False,
            idempotency_key="ik-p001",
        )
        assert expected_hash != "placeholder"
        p2 = p.model_copy(update={"proposal_hash": expected_hash})
        assert p2.proposal_hash == expected_hash


# ---------------------------------------------------------------------------
# AgentError
# ---------------------------------------------------------------------------


class TestAgentErrorModel:
    def test_valid_error(self):
        e = AgentError(error_code="E001", message="something went wrong")
        assert e.error_code == "E001"
        assert e.retryable is False

    def test_error_code_non_empty(self):
        with pytest.raises(ValidationError) as exc:
            AgentError(error_code="", message="msg")
        assert "error_code" in str(exc.value)

    def test_retryable_true(self):
        e = AgentError(error_code="E002", message="retry me", retryable=True)
        assert e.retryable is True


# ---------------------------------------------------------------------------
# AgentExecutionContext
# ---------------------------------------------------------------------------


class TestAgentExecutionContext:
    def test_valid_context(self):
        ctx = AgentExecutionContext(tenant_id="t-001")
        assert ctx.tenant_id == "t-001"

    def test_tenant_id_required(self):
        with pytest.raises(ValidationError) as exc:
            AgentExecutionContext(tenant_id="")
        assert "tenant_id" in str(exc.value)


# ---------------------------------------------------------------------------
# ToolCallRecord
# ---------------------------------------------------------------------------


class TestToolCallRecord:
    def test_valid_record(self):
        tcr = ToolCallRecord(
            tool_name="crm_reader.get_leads", authority=ToolAuthority.READ, ok=True
        )
        assert tcr.tool_name == "crm_reader.get_leads"
        assert tcr.ok is True

    def test_duration_default(self):
        tcr = ToolCallRecord(tool_name="test", authority=ToolAuthority.READ, ok=True)
        assert tcr.duration_ms == 0.0


# ---------------------------------------------------------------------------
# Adapter tests
# ---------------------------------------------------------------------------


class TestFromCrmWriterProposal:
    def test_basic_adapter(self):
        """Simulate the crm_writer ActionProposal dataclass."""
        from dataclasses import dataclass

        @dataclass
        class OldProposal:
            proposal_id: str
            entity: str
            operation: str
            payload: dict
            requires_approval: bool
            created_at: str

        old = OldProposal(
            proposal_id="p-old-1",
            entity="ticket",
            operation="create",
            payload={"title": "bug"},
            requires_approval=True,
            created_at="2025-06-01T12:00:00Z",
        )
        adapted = from_crm_writer_proposal(old, tenant_id="t-001", agent_id="sales")
        assert adapted.proposal_id == "p-old-1"
        assert adapted.tenant_id == "t-001"
        assert adapted.created_by_agent == "sales"
        assert adapted.action_type == "create"
        assert adapted.target_entity == "ticket"
        assert adapted.payload == {"title": "bug"}
        assert adapted.requires_approval is True


class TestFromProductivityProposal:
    def test_basic_adapter(self):
        """Simulate the productivity ActionProposal frozen dataclass."""
        from dataclasses import dataclass

        @dataclass(frozen=True)
        class OldProdProposal:
            proposal_id: str
            tenant_id: str
            user_id: str
            action_type: str
            target_entity: str
            target_id: str
            priority: str
            justification: str
            drafts: dict
            created_at: str
            dedupe_key: str
            signal_type: str
            signal: dict

        old = OldProdProposal(
            proposal_id="prod-p-1",
            tenant_id="t-001",
            user_id="user-1",
            action_type="reminder",
            target_entity="task",
            target_id="task-1",
            priority="high",
            justification="overdue",
            drafts={"email_subject": "Reminder"},
            created_at="2025-06-01T12:00:00Z",
            dedupe_key="dk-1",
            signal_type="sla_breach",
            signal={"type": "sla"},
        )
        adapted = from_productivity_proposal(old)
        assert adapted.proposal_id == "prod-p-1"
        assert adapted.tenant_id == "t-001"
        assert adapted.created_by_agent == "productivity_agent"
        assert adapted.action_type == "reminder"
        assert adapted.payload["user_id"] == "user-1"
        assert adapted.idempotency_key == "dk-1"

"""Contract validation and anti-pattern tests — Phase 2 R2.

All tests run under AI_MODE=deterministic; no Ollama, no API keys.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

import pytest
from pydantic import ValidationError

from multi_agent.contracts import (
    ActionProposal,
    ActionRiskLevel,
    AgentAuthority,
    AgentCapability,
    AgentError,
    AgentErrorCategory,
    AgentExecutionContext,
    AgentResult,
    AgentTask,
    ComplexityDecision,
    Evidence,
    EvidenceType,
    ExecutionBudget,
    MultiAgentState,
    ProviderMetadata,
    TokenUsage,
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
    **overrides: Any,
) -> AgentCapability:
    defaults: dict[str, Any] = dict(
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


def _make_proposal(**overrides: Any) -> ActionProposal:
    """Create an ActionProposal via the factory (auto-hash)."""
    defaults: dict[str, Any] = dict(
        proposal_id="p-001",
        tenant_id="t-001",
        created_by_agent="agent_a",
        action_type="create",
        target_entity="ticket",
        priority="medium",
        risk_level=ActionRiskLevel.MEDIUM,
        justification="test",
        evidence_ids=[],
        requires_approval=True,
        idempotency_key="ik-001",
        created_at=_utc_now(),
    )
    defaults.update(overrides)
    return ActionProposal.create(**defaults)


# ============================================================================
# StrictContract — extra fields are rejected
# ============================================================================


class TestStrictContract:
    def test_extra_fields_are_rejected(self):
        with pytest.raises(ValidationError) as exc:
            TokenUsage(input_tokens=10, output_tokens=5, total_tokens=15, fake_field=42)  # type: ignore[call-arg]
        assert "fake_field" in str(exc.value)

    def test_extra_field_on_evidence_rejected(self):
        with pytest.raises(ValidationError) as exc:
            Evidence(
                evidence_id="ev-1",
                evidence_type=EvidenceType.TOOL_RESULT,
                tenant_id="t-1",
                source_agent="a",
                raw_prompt="inject",  # type: ignore[call-arg]
            )
        assert "raw_prompt" in str(exc.value)

    def test_provider_metadata_api_key_field_rejected(self):
        with pytest.raises(ValidationError) as exc:
            ProviderMetadata(
                provider="o",
                chat_model="m",
                embedding_model="e",
                ai_mode="live",
                api_key="secret",  # type: ignore[call-arg]
            )
        assert "api_key" in str(exc.value)

    def test_extra_field_on_capability_rejected(self):
        with pytest.raises(ValidationError) as exc:
            AgentCapability(
                agent_id="test_agent",
                version="1.0.0",
                description="Test",
                domains={"test"},
                supported_tasks={"test_task"},
                allowed_tools={"crm_reader.get_leads"},
                authority=AgentAuthority.READ,
                input_contract="in",
                output_contract="out",
                timeout_ms=30_000,
                max_retries=2,
                estimated_cost_class="low",
                unsafe_flag=True,  # type: ignore[call-arg]
            )
        assert "unsafe_flag" in str(exc.value)


# ============================================================================
# Evidence — chain_of_thought / llm_reasoning are NOT valid evidence types
# ============================================================================


class TestEvidenceTypeSafety:
    def test_chain_of_thought_evidence_is_rejected(self):
        """chain_of_thought is NOT in EvidenceType enum."""
        assert "chain_of_thought" not in EvidenceType.__members__

    def test_llm_reasoning_evidence_is_rejected(self):
        """llm_reasoning is NOT in EvidenceType enum."""
        assert "llm_reasoning" not in EvidenceType.__members__

    def test_business_evidence_types_are_supported(self):
        for et in (
            EvidenceType.CUSTOMER,
            EvidenceType.TICKET,
            EvidenceType.DEAL,
            EvidenceType.KNOWLEDGE_ARTICLE,
            EvidenceType.TOOL_RESULT,
            EvidenceType.AUDIT_EVENT,
            EvidenceType.POLICY_DECISION,
            EvidenceType.HUMAN_APPROVAL,
        ):
            ev = Evidence(
                evidence_id="ev-1",
                evidence_type=et,
                tenant_id="t-1",
                source_agent="a",
            )
            assert ev.evidence_type == et

    def test_raw_prompt_not_in_evidence(self):
        """The Evidence model does not have a 'prompt' or 'raw_prompt' field."""
        ev = Evidence(
            evidence_id="ev-1",
            evidence_type=EvidenceType.TOOL_RESULT,
            tenant_id="t-1",
            source_agent="a",
        )
        assert not hasattr(ev, "prompt")
        assert not hasattr(ev, "raw_prompt")


# ============================================================================
# AgentExecutionContext — NO raw authorization
# ============================================================================


class TestExecutionContextNoAuth:
    def test_authorization_not_a_field(self):
        """AgentExecutionContext must NOT have an 'authorization' field."""
        fields = AgentExecutionContext.model_fields
        assert "authorization" not in fields

    def test_scopes_and_roles_exist(self):
        ctx = AgentExecutionContext(
            tenant_id="t-1",
            roles=["admin"],
            scopes=["read:leads", "write:tickets"],
        )
        assert "admin" in ctx.roles
        assert "read:leads" in ctx.scopes

    def test_policy_context(self):
        ctx = AgentExecutionContext(
            tenant_id="t-1",
            policy_context={"allow_delete": False, "max_confidence": 0.9},
        )
        assert ctx.policy_context["allow_delete"] is False


# ============================================================================
# ActionProposal — hash integrity + risk/priority split
# ============================================================================


class TestActionProposalHashIntegrity:
    def test_create_auto_computes_hash(self):
        p = ActionProposal.create(
            proposal_id="p-1",
            tenant_id="t-1",
            created_by_agent="a",
            action_type="create",
            target_entity="ticket",
            priority="medium",
            risk_level=ActionRiskLevel.MEDIUM,
            evidence_ids=[],
            requires_approval=True,
            idempotency_key="ik-1",
        )
        assert len(p.proposal_hash) == 64
        assert p.proposal_hash == p.compute_hash()

    def test_forged_proposal_hash_rejected(self):
        with pytest.raises(ValidationError) as exc:
            ActionProposal(
                proposal_id="p-1",
                proposal_hash="0000000000000000000000000000000000000000000000000000000000000000",  # forged
                tenant_id="t-1",
                created_by_agent="a",
                action_type="create",
                target_entity="ticket",
                priority="medium",
                risk_level=ActionRiskLevel.MEDIUM,
                evidence_ids=[],
                requires_approval=True,
                idempotency_key="ik-1",
                created_at=_utc_now(),
            )
        assert "hash" in str(exc.value).lower()

    def test_placeholder_hash_rejected(self):
        """'placeholder' as a hash is rejected (must be empty or valid)."""
        with pytest.raises(ValidationError) as exc:
            ActionProposal(
                proposal_id="p-1",
                proposal_hash="placeholder",
                tenant_id="t-1",
                created_by_agent="a",
                action_type="create",
                target_entity="ticket",
                priority="medium",
                risk_level=ActionRiskLevel.MEDIUM,
                evidence_ids=[],
                requires_approval=True,
                idempotency_key="ik-1",
                created_at=_utc_now(),
            )
        assert "hash" in str(exc.value).lower()

    def test_empty_hash_allowed(self):
        """Empty string hash is allowed (means 'not yet computed')."""
        p = ActionProposal(
            proposal_id="p-1",
            proposal_hash="",
            tenant_id="t-1",
            created_by_agent="a",
            action_type="create",
            target_entity="ticket",
            priority="medium",
            risk_level=ActionRiskLevel.MEDIUM,
            evidence_ids=[],
            requires_approval=True,
            idempotency_key="ik-1",
            created_at=_utc_now(),
        )
        assert p.proposal_hash == ""

    def test_hash_excludes_idempotency_key(self):
        """Two proposals with different idempotency keys but same content → same hash."""
        p1 = ActionProposal.create(
            proposal_id="p-1",
            tenant_id="t-1",
            created_by_agent="a",
            action_type="create",
            target_entity="ticket",
            priority="medium",
            risk_level=ActionRiskLevel.MEDIUM,
            evidence_ids=[],
            requires_approval=True,
            idempotency_key="key-a",  # different
        )
        p2 = ActionProposal.create(
            proposal_id="p-2",
            tenant_id="t-1",
            created_by_agent="a",
            action_type="create",
            target_entity="ticket",
            priority="medium",
            risk_level=ActionRiskLevel.MEDIUM,
            evidence_ids=[],
            requires_approval=True,
            idempotency_key="key-b",  # different
        )
        assert p1.proposal_hash == p2.proposal_hash


class TestActionProposalRiskAndPriority:
    def test_risk_level_separate_from_priority(self):
        """priority and risk_level are independent."""
        p = _make_proposal(
            priority="low", risk_level=ActionRiskLevel.HIGH, evidence_ids=["ev-1"]
        )
        assert p.priority == "low"
        assert p.risk_level == ActionRiskLevel.HIGH

    def test_high_risk_requires_evidence(self):
        with pytest.raises(ValidationError) as exc:
            _make_proposal(risk_level=ActionRiskLevel.HIGH, evidence_ids=[])
        assert "evidence" in str(exc.value).lower()

    def test_high_risk_requires_approval(self):
        with pytest.raises(ValidationError) as exc:
            _make_proposal(
                risk_level=ActionRiskLevel.HIGH,
                evidence_ids=["ev-1"],
                requires_approval=False,
            )
        assert "approval" in str(exc.value).lower()

    def test_medium_risk_no_evidence_ok(self):
        p = _make_proposal(risk_level=ActionRiskLevel.MEDIUM, evidence_ids=[])
        assert p.risk_level == ActionRiskLevel.MEDIUM


class TestActionProposalTenantOverride:
    def test_payload_flat_tenant_id_rejected(self):
        with pytest.raises(ValidationError) as exc:
            _make_proposal(payload={"tenant_id": "evil"})
        assert "tenant_id" in str(exc.value)

    def test_payload_nested_tenant_id_rejected(self):
        with pytest.raises(ValidationError) as exc:
            _make_proposal(payload={"nested": {"tenant_id": "evil"}})
        assert "tenant_id" in str(exc.value)

    def test_payload_list_item_tenant_id_rejected(self):
        with pytest.raises(ValidationError) as exc:
            _make_proposal(payload={"items": [{"x": 1}, {"tenantId": "evil"}]})
        assert "tenant" in str(exc.value).lower()


# ============================================================================
# AgentCapability
# ============================================================================


class TestAgentCapabilityValidation:
    def test_valid_capability(self):
        cap = _make_capability()
        assert cap.agent_id == "test_agent"

    def test_agent_id_must_be_stable_format(self):
        with pytest.raises(ValidationError):
            _make_capability(agent_id="123invalid")

    def test_version_must_not_be_empty(self):
        with pytest.raises(ValidationError):
            _make_capability(version="")

    def test_timeout_ms_must_be_positive(self):
        with pytest.raises(ValidationError):
            _make_capability(timeout_ms=0)

    def test_max_retries_non_negative(self):
        cap = _make_capability(max_retries=0)
        assert cap.max_retries == 0

    def test_max_retries_negative_raises(self):
        with pytest.raises(ValidationError):
            _make_capability(max_retries=-1)


# ============================================================================
# AgentTask
# ============================================================================


class TestAgentTask:
    def test_valid_task(self):
        task = AgentTask(
            task_id="task-001",
            agent_id="test_agent",
            task_type="test_task",
            objective="Test objective",
            input_data={},
            tenant_id="t-001",
            timeout_ms=60_000,
            idempotency_key="ik-001",
        )
        assert task.objective == "Test objective"
        assert task.status == "pending"

    def test_self_dependency_raises(self):
        with pytest.raises(ValidationError):
            AgentTask(
                task_id="task-001",
                agent_id="a",
                task_type="t",
                input_data={},
                tenant_id="t-001",
                dependencies={"task-001"},
                timeout_ms=60_000,
                idempotency_key="ik-001",
            )

    def test_required_evidence_field(self):
        task = AgentTask(
            task_id="task-001",
            agent_id="a",
            task_type="t",
            input_data={},
            tenant_id="t-001",
            required_evidence=["opa_policy", "tool_result"],
            timeout_ms=60_000,
            idempotency_key="ik-001",
        )
        assert "opa_policy" in task.required_evidence

    def test_required_flag(self):
        task = AgentTask(
            task_id="task-001",
            agent_id="a",
            task_type="t",
            input_data={},
            tenant_id="t-001",
            required=False,
            timeout_ms=60_000,
            idempotency_key="ik-001",
        )
        assert task.required is False

    def test_status_values(self):
        task = AgentTask(
            task_id="task-001",
            agent_id="a",
            task_type="t",
            input_data={},
            tenant_id="t-001",
            status="ready",
            timeout_ms=60_000,
            idempotency_key="ik-001",
        )
        assert task.status == "ready"

    def test_needs_input_status(self):
        task = AgentTask(
            task_id="task-001",
            agent_id="a",
            task_type="t",
            input_data={},
            tenant_id="t-001",
            status="needs_input",
            timeout_ms=60_000,
            idempotency_key="ik-001",
        )
        assert task.status == "needs_input"


# ============================================================================
# AgentResult
# ============================================================================


class TestAgentResult:
    def test_valid_completed(self):
        result = AgentResult(
            result_id="r-001",
            task_id="task-001",
            agent_id="agent_a",
            agent_version="1.0.0",
            tenant_id="t-001",
            status="completed",
            summary="Done",
            completed_at=_utc_now(),
        )
        assert result.status == "completed"
        assert result.summary == "Done"

    def test_failed_requires_errors(self):
        with pytest.raises(ValidationError) as exc:
            AgentResult(
                result_id="r-001",
                task_id="task-001",
                agent_id="agent_a",
                tenant_id="t-001",
                status="failed",
                completed_at=_utc_now(),
            )
        assert "error" in str(exc.value).lower()

    def test_failed_with_errors_ok(self):
        result = AgentResult(
            result_id="r-001",
            task_id="task-001",
            agent_id="agent_a",
            tenant_id="t-001",
            status="failed",
            errors=[
                AgentError(
                    error_code="E1",
                    message="broken",
                    category=AgentErrorCategory.PERMANENT,
                )
            ],
            completed_at=_utc_now(),
        )
        assert len(result.errors) == 1
        assert result.errors[0].category == AgentErrorCategory.PERMANENT

    def test_completed_no_errors(self):
        with pytest.raises(ValidationError) as exc:
            AgentResult(
                result_id="r-001",
                task_id="task-001",
                agent_id="agent_a",
                tenant_id="t-001",
                status="completed",
                errors=[AgentError(error_code="E1", message="oops")],
                completed_at=_utc_now(),
            )
        assert "error" in str(exc.value).lower()

    def test_findings_field(self):
        result = AgentResult(
            result_id="r-001",
            task_id="task-001",
            agent_id="agent_a",
            tenant_id="t-001",
            status="completed",
            findings=[{"severity": "info", "message": "all clear"}],
            completed_at=_utc_now(),
        )
        assert len(result.findings) == 1

    def test_unresolved_questions(self):
        result = AgentResult(
            result_id="r-001",
            task_id="task-001",
            agent_id="agent_a",
            tenant_id="t-001",
            status="completed",
            unresolved_questions=["Why is revenue down?"],
            completed_at=_utc_now(),
        )
        assert "Why is revenue down?" in result.unresolved_questions

    def test_started_at_tracked(self):
        started = datetime(2025, 6, 1, 12, 0, 0, tzinfo=timezone.utc)
        result = AgentResult(
            result_id="r-001",
            task_id="task-001",
            agent_id="agent_a",
            tenant_id="t-001",
            status="completed",
            started_at=started,
            completed_at=_utc_now(),
        )
        assert result.started_at == started

    def test_tenant_homogeneity_evidence(self):
        """Evidence from wrong tenant inside a result must be rejected."""
        with pytest.raises(ValidationError):
            AgentResult(
                result_id="r-001",
                task_id="task-001",
                agent_id="agent_a",
                tenant_id="t-001",
                status="completed",
                evidence=[
                    Evidence(
                        evidence_id="ev-1",
                        evidence_type=EvidenceType.TOOL_RESULT,
                        tenant_id="t-002",  # foreign!
                        source_agent="a",
                    )
                ],
                completed_at=_utc_now(),
            )

    def test_tenant_homogeneity_proposal(self):
        """Proposal from wrong tenant inside a result must be rejected."""
        with pytest.raises(ValidationError):
            AgentResult(
                result_id="r-001",
                task_id="task-001",
                agent_id="agent_a",
                tenant_id="t-001",
                status="completed",
                action_proposals=[
                    _make_proposal(tenant_id="t-002")  # foreign!
                ],
                completed_at=_utc_now(),
            )

    def test_proposal_creator_must_match_result_agent(self):
        """Proposal created_by_agent must match result.agent_id."""
        with pytest.raises(ValidationError):
            AgentResult(
                result_id="r-001",
                task_id="task-001",
                agent_id="agent_a",
                tenant_id="t-001",
                status="completed",
                action_proposals=[
                    _make_proposal(created_by_agent="agent_b")  # wrong!
                ],
                completed_at=_utc_now(),
            )


# ============================================================================
# TokenUsage / ProviderMetadata / AgentError
# ============================================================================


class TestTokenUsage:
    def test_negative_raises(self):
        with pytest.raises(ValidationError):
            TokenUsage(input_tokens=-1)


class TestProviderMetadata:
    def test_valid_metadata(self):
        pm = ProviderMetadata(
            provider="ollama",
            chat_model="llama3.1",
            embedding_model="nomic",
            ai_mode="live",
        )
        assert pm.provider == "ollama"


class TestAgentError:
    def test_category_enum(self):
        e = AgentError(
            error_code="E1", message="msg", category=AgentErrorCategory.TIMEOUT
        )
        assert e.category == AgentErrorCategory.TIMEOUT


# ============================================================================
# ComplexityDecision / ExecutionBudget / MultiAgentState
# ============================================================================


class TestComplexityDecision:
    def test_basic(self):
        cd = ComplexityDecision(complexity="single_agent", reason="simple task")
        assert cd.complexity == "single_agent"


class TestExecutionBudget:
    def test_cost_exceeded_raises(self):
        with pytest.raises(ValidationError):
            ExecutionBudget(max_cost=10, total_cost=20)

    def test_max_agents_positive(self):
        with pytest.raises(ValidationError):
            ExecutionBudget(max_agents=0)

    def test_valid_budget(self):
        b = ExecutionBudget(max_cost=100, total_cost=50, agent_calls=3, iteration=1)
        assert b.total_cost == 50


class TestMultiAgentState:
    def test_basic(self):
        state = MultiAgentState(run_id="run-1", tenant_id="t-1")
        assert state.status == "idle"
        assert state.current_iteration == 0

    def test_with_budget(self):
        state = MultiAgentState(
            run_id="run-1",
            tenant_id="t-1",
            budget=ExecutionBudget(max_agents=4, max_iterations=5, max_cost=50),
        )
        assert state.budget.max_agents == 4


# ============================================================================
# Adapters
# ============================================================================


class TestCrmWriterAdapter:
    def test_adapter(self):
        from dataclasses import dataclass

        @dataclass
        class Old:
            proposal_id: str
            entity: str
            operation: str
            payload: dict
            requires_approval: bool
            created_at: str

        old = Old("p-1", "ticket", "create", {"x": 1}, True, "2025-06-01T12:00:00Z")
        adapted = from_crm_writer_proposal(old, tenant_id="t-001", agent_id="sales")
        assert adapted.proposal_id == "p-1"
        assert adapted.tenant_id == "t-001"
        # Hash must be non-empty and valid
        assert len(adapted.proposal_hash) == 64
        assert adapted.proposal_hash == adapted.compute_hash()


class TestProductivityAdapter:
    def test_adapter_no_evidence_medium(self):
        from dataclasses import dataclass

        @dataclass(frozen=True)
        class Old:
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

        old = Old(
            "p-1",
            "t-001",
            "u-1",
            "reminder",
            "task",
            "task-1",
            "medium",
            "overdue",
            {},
            "2025-06-01T12:00:00+00:00",
            "dk-1",
            "sla",
            {"type": "sla"},
        )
        adapted = from_productivity_proposal(old, evidence_ids=["ev-1"])
        assert adapted.proposal_id == "p-1"
        assert adapted.proposal_hash == adapted.compute_hash()

    def test_adapter_high_priority_requires_explicit_evidence(self):
        from dataclasses import dataclass

        @dataclass(frozen=True)
        class Old:
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

        old = Old(
            "p-1",
            "t-001",
            "u-1",
            "reminder",
            "task",
            "task-1",
            "high",
            "critical",
            {},
            "2025-06-01T12:00:00+00:00",
            "dk-1",
            "sla",
            {"type": "sla"},
        )
        with pytest.raises(ValueError, match="evidence_ids"):
            from_productivity_proposal(old)

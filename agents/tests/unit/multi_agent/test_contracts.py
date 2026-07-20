"""Contract validation and anti-pattern tests — Phase 2 R3.

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
    AgentExecutionContext,
    AgentResult,
    AgentTask,
    ComplexityDecision,
    Evidence,
    EvidenceType,
    ExecutionBudget,
    ExecutionUsage,
    MultiAgentState,
    ProviderMetadata,
    TokenUsage,
    ToolAuthority,
    ToolDescriptor,
    from_crm_writer_proposal,
    from_productivity_proposal,
)
from multi_agent.errors import ProposalHashMismatchError

# Helpers ----------------------------------------------------------------


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _make_capability(
    agent_id: str = "test_agent",
    authority: AgentAuthority = AgentAuthority.READ,
    allowed_tools: frozenset[str] | None = None,
    **overrides: Any,
) -> AgentCapability:
    defaults: dict[str, Any] = dict(
        agent_id=agent_id,
        version="1.0.0",
        description="Test agent",
        domains=frozenset({"test"}),
        supported_tasks=frozenset({"test_task"}),
        allowed_tools=allowed_tools or frozenset({"crm_reader.get_leads"}),
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
    fields: dict[str, Any] = dict(
        proposal_id="p-001",
        tenant_id="t-001",
        created_by_agent="agent_a",
        action_type="create",
        target_entity="ticket",
        priority="medium",
        risk_level=ActionRiskLevel.MEDIUM,
        evidence_ids=[],
        requires_approval=True,
        idempotency_key="ik-001",
    )
    fields.update(overrides)
    return ActionProposal.create(**fields)


# ============================================================================
# StrictContract — extra fields rejected
# ============================================================================


class TestStrictContract:
    def test_extra_fields_are_rejected(self):
        with pytest.raises(ValidationError) as exc:
            TokenUsage(input_tokens=10, output_tokens=5, total_tokens=15, fake_field=42)  # type: ignore[call-arg]
        assert "fake_field" in str(exc.value)

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
                domains=frozenset({"test"}),
                supported_tasks=frozenset({"t"}),
                allowed_tools=frozenset({"crm_reader.get_leads"}),
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
# Evidence type safety
# ============================================================================


class TestEvidenceTypeSafety:
    def test_chain_of_thought_rejected(self):
        assert "chain_of_thought" not in EvidenceType.__members__

    def test_llm_reasoning_rejected(self):
        assert "llm_reasoning" not in EvidenceType.__members__

    def test_business_types_supported(self):
        for et in (
            EvidenceType.CUSTOMER,
            EvidenceType.TICKET,
            EvidenceType.DEAL,
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
        ev = Evidence(
            evidence_id="ev-1",
            evidence_type=EvidenceType.TOOL_RESULT,
            tenant_id="t-1",
            source_agent="a",
        )
        assert not hasattr(ev, "prompt")
        assert not hasattr(ev, "raw_prompt")

    def test_summary_field(self):
        ev = Evidence(
            evidence_id="ev-1",
            evidence_type=EvidenceType.TOOL_RESULT,
            tenant_id="t-1",
            source_agent="a",
            summary="Fetched 5 leads",
        )
        assert ev.summary == "Fetched 5 leads"

    def test_retrieved_at(self):
        now = _utc_now()
        ev = Evidence(
            evidence_id="ev-1",
            evidence_type=EvidenceType.TOOL_RESULT,
            tenant_id="t-1",
            source_agent="a",
            retrieved_at=now,
        )
        assert ev.retrieved_at == now


# ============================================================================
# AgentExecutionContext — no raw authorization
# ============================================================================


class TestExecutionContextNoAuth:
    def test_authorization_not_a_field(self):
        assert "authorization" not in AgentExecutionContext.model_fields

    def test_scopes_and_roles(self):
        ctx = AgentExecutionContext(
            tenant_id="t-1", roles=["admin"], scopes=["read:leads"]
        )
        assert "admin" in ctx.roles


# ============================================================================
# ActionProposal — hash integrity (R3)
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

    def test_empty_hash_is_auto_computed(self):
        """Empty hash is auto-computed at construction, not left empty."""
        now = _utc_now()
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
            created_at=now,
        )
        assert len(p.proposal_hash) == 64
        assert p.proposal_hash == p.compute_hash()

    def test_forged_hash_rejected(self):
        with pytest.raises(ValidationError) as exc:
            ActionProposal(
                proposal_id="p-1",
                proposal_hash="0000000000000000000000000000000000000000000000000000000000000000",
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

    def test_hash_excludes_idempotency_key(self):
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
            idempotency_key="key-a",
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
            idempotency_key="key-b",
        )
        assert p1.proposal_hash == p2.proposal_hash

    def test_mutated_payload_fails_integrity(self):
        p = _make_proposal(payload={"amount": 100})
        p.payload["amount"] = 999999  # type: ignore[index]
        with pytest.raises(ProposalHashMismatchError):
            p.verify_integrity()

    def test_mutated_evidence_fails_integrity(self):
        p = _make_proposal(evidence_ids=["ev-1"])
        p.evidence_ids.append("fake-ev")
        with pytest.raises(ProposalHashMismatchError):
            p.verify_integrity()

    def test_mutated_proposal_rejected_by_agent_result(self):
        p = _make_proposal(payload={"amount": 100})
        p.payload["amount"] = 999999  # type: ignore[index]
        with pytest.raises(ValidationError):
            AgentResult(
                result_id="r-1",
                task_id="t-1",
                agent_id="agent_a",
                agent_version="1.0.0",
                tenant_id="t-001",
                status="completed",
                action_proposals=[p],
                completed_at=_utc_now(),
            )

    def test_verify_integrity_passes_good_proposal(self):
        p = _make_proposal()
        p.verify_integrity()

    def test_proposal_hash_uses_shared_canonicalizer(self):
        h1 = _make_proposal(payload={"b": 2, "a": 1}).proposal_hash
        h2 = _make_proposal(payload={"a": 1, "b": 2}).proposal_hash
        assert h1 == h2


class TestActionProposalRiskAndPriority:
    def test_high_risk_requires_evidence(self):
        with pytest.raises(ValidationError):
            _make_proposal(risk_level=ActionRiskLevel.HIGH, evidence_ids=[])

    def test_high_risk_requires_approval(self):
        with pytest.raises(ValidationError):
            _make_proposal(
                risk_level=ActionRiskLevel.HIGH,
                evidence_ids=["ev-1"],
                requires_approval=False,
            )


class TestActionProposalTenantOverride:
    def test_nested_tenant_id_rejected(self):
        with pytest.raises(ValidationError):
            _make_proposal(payload={"nested": {"tenant_id": "evil"}})

    def test_list_item_tenant_id_rejected(self):
        with pytest.raises(ValidationError):
            _make_proposal(payload={"items": [{"tenantId": "evil"}]})


# ============================================================================
# AgentCapability — frozen (R3)
# ============================================================================


class TestCapabilityFrozen:
    def test_cannot_change_authority_after_construction(self):
        cap = _make_capability(agent_id="support1", authority=AgentAuthority.READ)
        with pytest.raises(ValidationError):
            cap.authority = AgentAuthority.EXECUTE  # type: ignore[misc]

    def test_cannot_add_tool_after_construction(self):
        cap = _make_capability(agent_id="support1")
        assert isinstance(cap.allowed_tools, frozenset)
        with pytest.raises(AttributeError):
            cap.allowed_tools.add("kafka.emit_event")  # type: ignore[union-attr]

    def test_domains_are_frozenset(self):
        cap = _make_capability(agent_id="support1")
        assert isinstance(cap.domains, frozenset)

    def test_registered_capability_cannot_escalate(self):
        from multi_agent.registry import AgentRegistry

        reg = AgentRegistry()
        cap = _make_capability(
            agent_id="support1",
            authority=AgentAuthority.READ,
            allowed_tools=frozenset({"crm_reader.get_leads"}),
        )
        reg.register(cap, object())
        resolved = reg.resolve_capability("support1")
        with pytest.raises(ValidationError):
            resolved.authority = AgentAuthority.EXECUTE  # type: ignore[misc]

    def test_snapshot_mutation_does_not_change_registry(self):
        from multi_agent.registry import AgentRegistry

        reg = AgentRegistry()
        reg.register(_make_capability(agent_id="a1"), object())
        snap = reg.snapshot()
        snap.agents.clear()
        assert reg.is_registered("a1")


# ============================================================================
# AgentTask / AgentResult
# ============================================================================


class TestAgentTask:
    def test_dependencies_frozenset(self):
        task = AgentTask(
            objective="test",
            task_id="t1",
            agent_id="a1",
            task_type="t",
            input_data={},
            tenant_id="t-1",
            timeout_ms=60_000,
            idempotency_key="ik-1",
            dependencies=frozenset({"t2", "t3"}),
        )
        assert "t2" in task.dependencies

    def test_objective_required_evidence(self):
        task = AgentTask(
            task_id="t1",
            agent_id="a1",
            task_type="t",
            input_data={},
            tenant_id="t-1",
            objective="Find leads",
            required_evidence=["tool_result"],
            timeout_ms=60_000,
            idempotency_key="ik-1",
        )
        assert task.objective == "Find leads"


# ============================================================================
# ComplexityDecision / ExecutionBudget / ExecutionUsage / MultiAgentState
# ============================================================================


class TestComplexityDecision:
    def test_routes_match_execution_modes(self):
        for route in ("deterministic_workflow", "single_agent", "multi_agent"):
            cd = ComplexityDecision(route=route)  # type: ignore[arg-type]
            assert cd.route == route

    def test_with_reasons_and_confidence(self):
        cd = ComplexityDecision(
            route="multi_agent",
            domains=["support"],
            reasons=["complex"],
            confidence=0.8,
        )
        assert cd.confidence == 0.8


class TestExecutionBudget:
    def test_budget_fields(self):
        b = ExecutionBudget(
            max_tasks=8,
            max_agent_calls=64,
            max_tool_calls=256,
            max_iterations=5,
            token_budget=10000,
            cost_budget_usd=5,
            deadline_ms=120_000,
        )
        assert b.max_tasks == 8
        assert b.token_budget == 10000

    def test_positive_validation(self):
        with pytest.raises(ValidationError):
            ExecutionBudget(deadline_ms=0)


class TestExecutionUsage:
    def test_tracks_usage(self):
        u = ExecutionUsage(
            tasks_dispatched=5, agent_calls=20, tokens_used=500, cost_usd=1
        )
        assert u.tasks_dispatched == 5


class TestMultiAgentState:
    def test_with_objective_and_user(self):
        state = MultiAgentState(
            run_id="r1",
            tenant_id="t-1",
            actor_type="user",
            actor_id="u1",
            objective="Analyze support tickets",
        )
        assert state.objective == "Analyze support tickets"

    def test_rejects_foreign_tenant_task(self):
        task = AgentTask(
            objective="test",
            task_id="t1",
            agent_id="a1",
            task_type="t",
            input_data={},
            tenant_id="t-999",
            timeout_ms=60_000,
            idempotency_key="ik-1",
        )
        with pytest.raises(ValidationError):
            MultiAgentState(
                objective="test",
                actor_id="test",
                run_id="r1",
                tenant_id="t-1",
                plan=[task],
            )

    def test_with_complexity_and_budget(self):
        state = MultiAgentState(
            objective="test",
            actor_id="test",
            run_id="r1",
            tenant_id="t-1",
            complexity=ComplexityDecision(route="multi_agent", reasons=["high"]),
            budget=ExecutionBudget(max_tasks=4),
        )
        assert state.complexity.route == "multi_agent"
        assert state.budget.max_tasks == 4


# ============================================================================
# Adapter tests
# ============================================================================


class TestAdapters:
    def test_crm_writer_adapter(self):
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
        assert adapted.proposal_hash == adapted.compute_hash()

    def test_productivity_adapter(self):
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
        assert adapted.proposal_hash == adapted.compute_hash()


# ============================================================================
# R4: Proposal hash uses shared canonicalizer
# ============================================================================


class TestProposalHashCanonicalizer:
    def test_proposal_hash_rejects_custom_object(self):
        class Custom:
            pass

        with pytest.raises(ValidationError):
            _make_proposal(payload={"obj": Custom()})  # type: ignore[arg-type]

    def test_proposal_hash_rejects_nan(self):
        with pytest.raises(ValidationError):
            _make_proposal(payload={"x": float("nan")})

    def test_proposal_hash_normalizes_utc(self):
        p1 = _make_proposal(
            created_at=datetime(2025, 6, 1, 12, 0, 0, tzinfo=timezone.utc)
        )
        assert len(p1.proposal_hash) == 64

    def test_proposal_hash_matches_content_hash(self):
        from multi_agent.integrity import compute_proposal_hash
        from multi_agent.serialization import content_hash

        payload = {"a": 1, "b": 2}
        h1 = compute_proposal_hash(
            tenant_id="t-1",
            created_by_agent="a",
            action_type="create",
            target_entity="ticket",
            target_id=None,
            payload=payload,
            priority="medium",
            risk_level="medium",
            justification=None,
            evidence_ids=[],
            requires_approval=True,
        )
        h2 = content_hash(
            {
                "tenant_id": "t-1",
                "created_by_agent": "a",
                "action_type": "create",
                "target_entity": "ticket",
                "target_id": None,
                "payload": payload,
                "priority": "medium",
                "risk_level": "medium",
                "justification": None,
                "evidence_ids": [],
                "requires_approval": True,
            }
        )
        assert h1 == h2


# ============================================================================
# R4: Budget None semantics
# ============================================================================


class TestBudgetNoneSemantics:
    def test_budget_none_means_unlimited(self):
        b = ExecutionBudget(token_budget=None, cost_budget_usd=None)
        assert b.token_budget is None
        assert b.cost_budget_usd is None

    def test_budget_zero_rejected(self):
        with pytest.raises(ValidationError):
            ExecutionBudget(token_budget=0)

    def test_budget_specific_value_ok(self):
        from decimal import Decimal

        b = ExecutionBudget(token_budget=5000, cost_budget_usd=Decimal("2.50"))
        assert b.token_budget == 5000
        assert b.cost_budget_usd == Decimal("2.50")


# ============================================================================
# R4: AgentResult new statuses
# ============================================================================


class TestAgentResultNewStatuses:
    def test_agent_result_supports_needs_input(self):
        r = AgentResult(
            result_id="r-1",
            task_id="t-1",
            agent_id="a1",
            agent_version="1.0.0",
            tenant_id="t-1",
            status="needs_input",
            completed_at=_utc_now(),
        )
        assert r.status == "needs_input"

    def test_agent_result_supports_skipped(self):
        r = AgentResult(
            result_id="r-1",
            task_id="t-1",
            agent_id="a1",
            agent_version="1.0.0",
            tenant_id="t-1",
            status="skipped",
            completed_at=_utc_now(),
        )
        assert r.status == "skipped"


# ============================================================================
# R4: MultiAgentState actor
# ============================================================================


class TestMultiAgentStateActor:
    def test_actor_type_and_id(self):
        state = MultiAgentState(
            run_id="r1",
            tenant_id="t-1",
            actor_type="service",
            actor_id="svc-pipeline",
            objective="Run pipeline",
        )
        assert state.actor_type == "service"
        assert state.actor_id == "svc-pipeline"

    def test_default_actor_is_user(self):
        state = MultiAgentState(
            run_id="r1", tenant_id="t-1", objective="Test", actor_id="test"
        )
        assert state.actor_type == "user"


# ============================================================================
# R5: Strict JSON boundary tests
# ============================================================================


class TestStrictJsonRejection:
    def test_bytes_rejected(self):
        with pytest.raises(ValidationError):
            _make_proposal(payload={"blob": b"binary"})

    def test_set_rejected(self):
        with pytest.raises(ValidationError):
            _make_proposal(payload={"values": {1, 2, 3}})

    def test_tuple_rejected(self):
        with pytest.raises(ValidationError):
            _make_proposal(payload={"coords": (1, 2)})

    def test_non_string_dict_key_rejected(self):
        with pytest.raises(ValidationError):
            AgentResult(
                result_id="r-1",
                task_id="t-1",
                agent_id="a1",
                agent_version="1.0.0",
                tenant_id="t-1",
                status="completed",
                output={1: "value"},  # type: ignore[arg-type]
                completed_at=_utc_now(),
            )

    def test_nan_rejected_in_metadata(self):
        with pytest.raises(ValidationError):
            Evidence(
                evidence_id="ev-1",
                evidence_type=EvidenceType.TOOL_RESULT,
                tenant_id="t-1",
                source_agent="a",
                metadata={"score": float("nan")},
            )

    def test_custom_object_rejected_in_details(self):
        class Custom:
            pass

        with pytest.raises((ValidationError, ValueError, TypeError)):
            AgentError(
                error_code="E1",
                message="msg",
                details={"obj": Custom()},  # type: ignore[arg-type]
            )

    def test_decimal_rejected_in_policy_context(self):
        from decimal import Decimal

        with pytest.raises(ValidationError):
            AgentExecutionContext(
                tenant_id="t-1",
                policy_context={"cost": Decimal("10.50")},
            )

    def test_datetime_rejected_in_run_metadata(self):
        with pytest.raises(ValidationError):
            AgentExecutionContext(
                tenant_id="t-1",
                run_metadata={"timestamp": _utc_now()},
            )


# ============================================================================
# R6: Blank string rejection
# ============================================================================


class TestBlankStringRejection:
    def test_multi_agent_state_rejects_blank_actor_id(self):
        with pytest.raises(ValidationError):
            MultiAgentState(
                run_id="r1",
                tenant_id="t-1",
                objective="Test",
                actor_id="   ",
            )

    def test_multi_agent_state_rejects_blank_objective(self):
        with pytest.raises(ValidationError):
            MultiAgentState(
                run_id="r1",
                tenant_id="t-1",
                objective="   ",
                actor_id="test",
            )

    def test_agent_task_rejects_blank_objective(self):
        with pytest.raises(ValidationError):
            AgentTask(
                objective="   ",
                task_id="t1",
                agent_id="a1",
                task_type="t",
                input_data={},
                tenant_id="t-1",
                timeout_ms=60_000,
                idempotency_key="ik-1",
            )


# ============================================================================
# R6: Secret key rejection in metadata
# ============================================================================


class TestMetadataSecretKeyRejection:
    def test_api_key_in_metadata_rejected(self):
        with pytest.raises(ValidationError):
            AgentCapability(
                agent_id="test_agent",
                version="1.0.0",
                description="Test",
                domains=frozenset({"test"}),
                supported_tasks=frozenset({"t"}),
                allowed_tools=frozenset({"crm_reader.get_leads"}),
                authority=AgentAuthority.READ,
                input_contract="in",
                output_contract="out",
                timeout_ms=30_000,
                max_retries=2,
                estimated_cost_class="low",
                metadata={"api_key": "secret123"},
            )

    def test_authorization_in_run_metadata_rejected(self):
        with pytest.raises(ValidationError):
            AgentExecutionContext(
                tenant_id="t-1",
                run_metadata={"authorization": "Bearer xyz"},
            )

    def test_password_in_evidence_metadata_rejected(self):
        with pytest.raises(ValidationError):
            Evidence(
                evidence_id="ev-1",
                evidence_type=EvidenceType.TOOL_RESULT,
                tenant_id="t-1",
                source_agent="a",
                metadata={"password": "1234"},
            )

    # -- R7: normalization consistency --------------------------------------

    def test_access_token_in_metadata_rejected(self):
        """access_token was previously bypassed because the pattern set
        stored the un-normalized form while the key under test was
        normalized to ``accesstoken``."""
        with pytest.raises(ValidationError):
            Evidence(
                evidence_id="ev-1",
                evidence_type=EvidenceType.TOOL_RESULT,
                tenant_id="t-1",
                source_agent="a",
                metadata={"access_token": "tok-xyz"},
            )

    def test_refresh_token_in_metadata_rejected(self):
        with pytest.raises(ValidationError):
            Evidence(
                evidence_id="ev-1",
                evidence_type=EvidenceType.TOOL_RESULT,
                tenant_id="t-1",
                source_agent="a",
                metadata={"refresh_token": "rt-xyz"},
            )

    def test_hyphenated_access_token_rejected(self):
        """``access-token`` must collapse to the same normalized form as
        ``access_token``."""
        with pytest.raises(ValidationError):
            Evidence(
                evidence_id="ev-1",
                evidence_type=EvidenceType.TOOL_RESULT,
                tenant_id="t-1",
                source_agent="a",
                metadata={"access-token": "tok"},
            )

    def test_mixed_case_access_token_rejected(self):
        with pytest.raises(ValidationError):
            Evidence(
                evidence_id="ev-1",
                evidence_type=EvidenceType.TOOL_RESULT,
                tenant_id="t-1",
                source_agent="a",
                metadata={"ACCESS_TOKEN": "tok"},
            )

    def test_sensitive_key_inside_list_rejected(self):
        """Sensitive keys inside a list element dict must be caught now
        that the recursive scan handles lists."""
        with pytest.raises(ValidationError):
            AgentExecutionContext(
                tenant_id="t-1",
                run_metadata={"providers": [{"access_token": "tok"}]},
            )

    def test_nested_sensitive_key_inside_list_rejected(self):
        with pytest.raises(ValidationError):
            AgentExecutionContext(
                tenant_id="t-1",
                run_metadata={
                    "config": {
                        "endpoints": [
                            {"name": "ok", "url": "https://x"},
                            {"name": "evil", "client_secret": "s"},
                        ]
                    }
                },
            )


# ============================================================================
# R6: Evidence reference validation
# ============================================================================


class TestEvidenceReferenceValidation:
    def test_high_risk_proposal_with_missing_evidence_rejected(self):
        with pytest.raises(ValidationError):
            AgentResult(
                result_id="r-1",
                task_id="t-1",
                agent_id="agent_a",
                agent_version="1.0.0",
                tenant_id="t-1",
                status="completed",
                evidence=[],  # empty!
                action_proposals=[
                    ActionProposal.create(
                        proposal_id="p-1",
                        tenant_id="t-1",
                        created_by_agent="agent_a",
                        action_type="delete",
                        target_entity="customer",
                        target_id="c1",
                        risk_level=ActionRiskLevel.HIGH,
                        evidence_ids=["ev-missing"],
                        requires_approval=True,
                        idempotency_key="ik-1",
                    )
                ],
                completed_at=_utc_now(),
            )

    def test_high_risk_proposal_with_valid_evidence_ok(self):
        p = ActionProposal.create(
            proposal_id="p-1",
            tenant_id="t-1",
            created_by_agent="agent_a",
            action_type="delete",
            target_entity="customer",
            target_id="c1",
            risk_level=ActionRiskLevel.HIGH,
            evidence_ids=["ev-1"],
            requires_approval=True,
            idempotency_key="ik-1",
        )
        result = AgentResult(
            result_id="r-1",
            task_id="t-1",
            agent_id="agent_a",
            agent_version="1.0.0",
            tenant_id="t-1",
            status="completed",
            evidence=[
                Evidence(
                    evidence_id="ev-1",
                    evidence_type=EvidenceType.TOOL_RESULT,
                    tenant_id="t-1",
                    source_agent="agent_a",
                ),
            ],
            action_proposals=[p],
            completed_at=_utc_now(),
        )
        assert result.status == "completed"

    # -- R7: Evidence references can be broken post-construction -------------

    def test_result_evidence_removed_after_construction(self):
        """AgentResult.evidence is a mutable list.  Clearing it after
        construction must not silently leave dangling references — but
        because R6 only validates at construction, the mutation itself
        succeeds.  This test documents the gap and asserts the proposal
        still references the now-missing evidence id (the merge/state
        layers are responsible for catching it)."""
        ev = Evidence(
            evidence_id="ev-1",
            evidence_type=EvidenceType.TOOL_RESULT,
            tenant_id="t-1",
            source_agent="agent_a",
        )
        p = ActionProposal.create(
            proposal_id="p-1",
            tenant_id="t-1",
            created_by_agent="agent_a",
            action_type="delete",
            target_entity="customer",
            target_id="c1",
            risk_level=ActionRiskLevel.HIGH,
            evidence_ids=["ev-1"],
            requires_approval=True,
            idempotency_key="ik-1",
        )
        result = AgentResult(
            result_id="r-1",
            task_id="t-1",
            agent_id="agent_a",
            agent_version="1.0.0",
            tenant_id="t-1",
            status="completed",
            evidence=[ev],
            action_proposals=[p],
            completed_at=_utc_now(),
        )
        # Mutate: remove all evidence AFTER construction.
        result.evidence.clear()
        # The proposal still references ev-1, but the result no longer
        # carries it.  The merge layer must catch this.
        assert result.action_proposals[0].evidence_ids == ["ev-1"]
        assert result.evidence == []


# ============================================================================
# R7: MultiAgentState evidence reference integrity
# ============================================================================


class TestMultiAgentStateEvidenceReference:
    def _make_proposal(self, evidence_ids: list[str]) -> ActionProposal:
        return ActionProposal.create(
            proposal_id="p-state-1",
            tenant_id="t-1",
            created_by_agent="agent_a",
            action_type="delete",
            target_entity="customer",
            target_id="c1",
            risk_level=ActionRiskLevel.HIGH if evidence_ids else ActionRiskLevel.MEDIUM,
            evidence_ids=evidence_ids,
            requires_approval=True,
            idempotency_key="ik-state-1",
        )

    def _make_evidence(self, evidence_id: str = "ev-state-1") -> Evidence:
        return Evidence(
            evidence_id=evidence_id,
            evidence_type=EvidenceType.TOOL_RESULT,
            tenant_id="t-1",
            source_agent="agent_a",
        )

    def _make_result_with_evidence(self, evidence_id: str) -> AgentResult:
        return AgentResult(
            result_id="r-1",
            task_id="t-1",
            agent_id="agent_a",
            agent_version="1.0.0",
            tenant_id="t-1",
            status="completed",
            evidence=[self._make_evidence(evidence_id)],
            completed_at=_utc_now(),
        )

    def test_multi_agent_state_rejects_missing_evidence_reference(self):
        """State-level proposed_actions referencing evidence that is absent
        from both state.evidence and all result.evidence must be rejected."""
        p = self._make_proposal(["ev-missing"])
        with pytest.raises(ValidationError):
            MultiAgentState(
                run_id="run-1",
                tenant_id="t-1",
                actor_id="user-1",
                objective="test",
                proposed_actions=[p],
            )

    def test_multi_agent_state_accepts_state_level_evidence(self):
        """Evidence provided at the state level satisfies proposal refs."""
        ev = self._make_evidence("ev-state-1")
        p = self._make_proposal(["ev-state-1"])
        state = MultiAgentState(
            run_id="run-1",
            tenant_id="t-1",
            actor_id="user-1",
            objective="test",
            evidence=[ev],
            proposed_actions=[p],
        )
        assert state.proposed_actions[0].evidence_ids == ["ev-state-1"]

    def test_multi_agent_state_accepts_result_level_evidence(self):
        """Evidence provided inside an AgentResult also satisfies refs on
        state-level proposals."""
        ev_id = "ev-result-1"
        r = self._make_result_with_evidence(ev_id)
        p = self._make_proposal([ev_id])
        state = MultiAgentState(
            run_id="run-1",
            tenant_id="t-1",
            actor_id="user-1",
            objective="test",
            agent_results=[r],
            proposed_actions=[p],
        )
        assert state.proposed_actions[0].evidence_ids == [ev_id]


# ============================================================================
# R8: Core ID / action fields must not be blank
# ============================================================================


class TestCoreIdNonBlank:
    """Every identifier that flows into Merge, dependency graph, Registry,
    Trace or approval must be non-blank after strip."""

    # -- Evidence ------------------------------------------------------------

    def test_evidence_rejects_blank_id(self):
        with pytest.raises(ValidationError):
            Evidence(
                evidence_id="   ",
                evidence_type=EvidenceType.TOOL_RESULT,
                tenant_id="t-1",
                source_agent="agent_a",
            )

    # -- AgentTask -----------------------------------------------------------

    def test_task_rejects_blank_task_id(self):
        with pytest.raises(ValidationError):
            AgentTask(
                task_id="",
                agent_id="agent_a",
                task_type="triage",
                objective="o",
                tenant_id="t-1",
            )

    def test_task_rejects_blank_agent_id(self):
        with pytest.raises(ValidationError):
            AgentTask(
                task_id="task-1",
                agent_id="  ",
                task_type="triage",
                objective="o",
                tenant_id="t-1",
            )

    def test_task_rejects_blank_task_type(self):
        with pytest.raises(ValidationError):
            AgentTask(
                task_id="task-1",
                agent_id="agent_a",
                task_type="",
                objective="o",
                tenant_id="t-1",
            )

    # -- AgentResult ---------------------------------------------------------

    def test_result_rejects_blank_result_id(self):
        with pytest.raises(ValidationError):
            AgentResult(
                result_id="",
                task_id="task-1",
                agent_id="agent_a",
                agent_version="1.0.0",
                tenant_id="t-1",
                status="completed",
                completed_at=_utc_now(),
            )

    def test_result_requires_agent_version(self):
        with pytest.raises(ValidationError):
            AgentResult(
                result_id="r-1",
                task_id="task-1",
                agent_id="agent_a",
                agent_version="",
                tenant_id="t-1",
                status="completed",
                completed_at=_utc_now(),
            )

    # -- ActionProposal ------------------------------------------------------

    def test_proposal_rejects_blank_proposal_id(self):
        with pytest.raises(ValidationError):
            ActionProposal.create(
                proposal_id="  ",
                tenant_id="t-1",
                created_by_agent="agent_a",
                action_type="create",
                target_entity="ticket",
                idempotency_key="ik-1",
            )

    def test_proposal_rejects_blank_creator(self):
        with pytest.raises(ValidationError):
            ActionProposal.create(
                proposal_id="p-1",
                tenant_id="t-1",
                created_by_agent="",
                action_type="create",
                target_entity="ticket",
                idempotency_key="ik-1",
            )

    def test_proposal_rejects_blank_action_type(self):
        with pytest.raises(ValidationError):
            ActionProposal.create(
                proposal_id="p-1",
                tenant_id="t-1",
                created_by_agent="agent_a",
                action_type="   ",
                target_entity="ticket",
                idempotency_key="ik-1",
            )

    def test_proposal_rejects_blank_target_entity(self):
        with pytest.raises(ValidationError):
            ActionProposal.create(
                proposal_id="p-1",
                tenant_id="t-1",
                created_by_agent="agent_a",
                action_type="create",
                target_entity="",
                idempotency_key="ik-1",
            )

    def test_proposal_requires_idempotency_key(self):
        with pytest.raises(ValidationError):
            ActionProposal.create(
                proposal_id="p-1",
                tenant_id="t-1",
                created_by_agent="agent_a",
                action_type="create",
                target_entity="ticket",
                idempotency_key="",
            )

    # -- MultiAgentState -----------------------------------------------------

    def test_state_rejects_blank_run_id(self):
        with pytest.raises(ValidationError):
            MultiAgentState(
                run_id="  ",
                tenant_id="t-1",
                actor_id="user-1",
                objective="test",
            )

    # -- ToolDescriptor ------------------------------------------------------

    def test_tool_rejects_blank_name(self):
        with pytest.raises(ValidationError):
            ToolDescriptor(
                tool_name="",
                authority=ToolAuthority.READ,
            )

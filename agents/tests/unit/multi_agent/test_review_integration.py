"""Phase 5A End-to-End Integration tests.

Phase 5A Section 17 — Phase 4 Integration:

    Customer Recovery SupervisorRunResult
    → build_review_request
    → ProposalReviewer.review
    → ReviewBatchResult

Uses real Phase 4 contracts (no placeholder Dicts).  Builds a realistic
SupervisorRunResult for a Customer Recovery scenario with multiple
proposals, evidence pieces, and capability bindings, then runs the
Reviewer end-to-end and verifies the ReviewBatchResult.

Also includes the Side-effect Guard tests (Section 17):
patches database write, Kafka publish, CRM update, Tool execute,
AutomationExecutor, email/SMS, and external HTTP — proving Phase 5A
never touches any of these during review.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any
from unittest.mock import patch

import pytest

from multi_agent.contracts import (
    ActionProposal,
    ActionRiskLevel,
    AgentAuthority,
    AgentCapability,
    AgentResult,
    Evidence,
    EvidenceType,
    ExecutionUsage,
    ProviderMetadata,
    TokenUsage,
    ToolAuthority,
    ToolCallRecord,
)
from multi_agent.execution import (
    ExecutionCapabilitySnapshot,
    ExecutionRunIdentity,
    ExecutionTraceEvent,
    ResultOriginSnapshot,
    SupervisorRunResult,
    SupervisorRunStatus,
    TaskAttemptRecord,
    TaskExecutionRecord,
    TRACE_RUN_STARTED,
    TRACE_TASK_COMPLETED,
    TRACE_RUN_COMPLETED,
)
from multi_agent.state import MergedState
from multi_agent.review_contracts import (
    ReviewBatchResult,
    ReviewBatchStatus,
    ReviewDecisionStatus,
)
from multi_agent.review_evaluation import build_review_request
from multi_agent.evidence_review import compute_review_evidence_hash
from multi_agent.policy import DeterministicPolicyEvaluator
from multi_agent.reviewer import ProposalReviewer
from multi_agent.serialization import stable_hash


_TS = datetime(2026, 1, 1, 0, 0, 0, tzinfo=timezone.utc)


# ---------------------------------------------------------------------------
# Customer Recovery fixture — realistic Phase 4 SupervisorRunResult
# ---------------------------------------------------------------------------


def _recovery_evidence(
    evidence_id: str,
    *,
    tenant_id: str = "tenant-recovery",
    source_agent: str = "agent_recovery",
    evidence_type: EvidenceType = EvidenceType.CUSTOMER,
    content_hash: str | None = None,
) -> Evidence:
    return Evidence(
        evidence_id=evidence_id,
        evidence_type=evidence_type,
        tenant_id=tenant_id,
        source_agent=source_agent,
        summary=f"Recovery evidence {evidence_id}",
        source_id=None,
        content_hash=content_hash or ("a" * 64),
        created_at=_TS,
        retrieved_at=_TS,
        metadata={},
    )


def _recovery_proposal(
    proposal_id: str,
    *,
    action_type: str = "report.generate",
    target_entity: str = "report",
    target_id: str | None = None,
    payload: dict[str, Any] | None = None,
    risk_level: ActionRiskLevel = ActionRiskLevel.LOW,
    evidence_ids: list[str] | None = None,
    idempotency_key: str = "recovery-idem-0001",
    tenant_id: str = "tenant-recovery",
    created_by_agent: str = "agent_recovery",
) -> ActionProposal:
    return ActionProposal(
        proposal_id=proposal_id,
        tenant_id=tenant_id,
        created_by_agent=created_by_agent,
        action_type=action_type,
        target_entity=target_entity,
        target_id=target_id,
        payload=payload or {},
        priority="medium",
        risk_level=risk_level,
        evidence_ids=evidence_ids or [],
        requires_approval=True,
        idempotency_key=idempotency_key,
        created_at=_TS,
    )


def _recovery_capability(
    agent_id: str,
    *,
    authority: AgentAuthority = AgentAuthority.PROPOSE,
    allowed_tools: frozenset[str] | None = None,
) -> AgentCapability:
    return AgentCapability(
        agent_id=agent_id,
        version="1.0.0",
        description=f"Recovery agent {agent_id}",
        domains=frozenset({"customer_recovery"}),
        supported_tasks=frozenset({"root_task"}),
        allowed_tools=allowed_tools
        or frozenset(
            {
                "crm_reader.get_customers",
                "crm_writer.propose",
            }
        ),
        authority=authority,
        input_contract="recovery_in",
        output_contract="recovery_out",
        timeout_ms=300_000,
        max_retries=0,
        estimated_cost_class="low",
        enabled=True,
        metadata={},
    )


def _recovery_agent_result(
    *,
    result_id: str,
    task_id: str,
    agent_id: str,
    tenant_id: str = "tenant-recovery",
    proposals: list[ActionProposal] | None = None,
    evidence: list[Evidence] | None = None,
) -> AgentResult:
    return AgentResult(
        result_id=result_id,
        task_id=task_id,
        agent_id=agent_id,
        agent_version="1.0.0",
        tenant_id=tenant_id,
        status="completed",
        confidence=0.92,
        duration_ms=42.0,
        evidence=evidence or [],
        action_proposals=proposals or [],
        errors=[],
        token_usage=TokenUsage(input_tokens=10, output_tokens=20, total_tokens=30),
        provider_metadata=ProviderMetadata(
            provider="openai",
            chat_model="gpt-4",
            embedding_model="text-embedding-3-small",
            ai_mode="live",
        ),
        tool_calls=[
            ToolCallRecord(
                tool_name="crm_reader.get_customers",
                authority=ToolAuthority.READ,
                ok=True,
                duration_ms=5.0,
            ),
        ],
        completed_at=_TS,
    )


def _build_customer_recovery_supervisor_result() -> SupervisorRunResult:
    """Build a realistic Customer Recovery SupervisorRunResult.

    The scenario: a customer (cust-001) was flagged for recovery.
    Two agents produced three Proposals:

    1. agent_recovery — report.generate (low-risk, evidence-backed, valid)
    2. agent_recovery — crm.tag.update (medium-risk, evidence-backed, valid)
    3. agent_recovery — crm.owner.assign (high-risk, evidence-backed,
       requires approval)
    """
    agent_id = "agent_recovery"
    task_id = "task-recovery-001"
    tenant_id = "tenant-recovery"
    run_id = "run-recovery-001"

    evidence = [
        _recovery_evidence(
            "ev-recovery-001",
            source_agent=agent_id,
            evidence_type=EvidenceType.CUSTOMER,
        ),
        _recovery_evidence(
            "ev-recovery-002",
            source_agent=agent_id,
            evidence_type=EvidenceType.DEAL,
        ),
        _recovery_evidence(
            "ev-recovery-003",
            source_agent=agent_id,
            evidence_type=EvidenceType.CUSTOMER,
        ),
    ]

    proposals = [
        _recovery_proposal(
            "prop-recovery-001",
            action_type="report.generate",
            target_entity="report",
            target_id="rep-001",
            evidence_ids=["ev-recovery-001"],
            idempotency_key="recovery-report-key-0001",
            risk_level=ActionRiskLevel.LOW,
        ),
        _recovery_proposal(
            "prop-recovery-002",
            action_type="crm.tag.update",
            target_entity="customer",
            target_id="cust-001",
            payload={"tag": "at-risk"},
            evidence_ids=["ev-recovery-002"],
            idempotency_key="recovery-tag-key-0001",
            risk_level=ActionRiskLevel.MEDIUM,
        ),
        _recovery_proposal(
            "prop-recovery-003",
            action_type="crm.owner.assign",
            target_entity="customer",
            target_id="cust-001",
            payload={"owner_id": "owner-42"},
            evidence_ids=["ev-recovery-003"],
            idempotency_key="recovery-owner-key-0001",
            risk_level=ActionRiskLevel.HIGH,
        ),
    ]

    agent_result = _recovery_agent_result(
        result_id="r-recovery-001",
        task_id=task_id,
        agent_id=agent_id,
        tenant_id=tenant_id,
        proposals=proposals,
        evidence=evidence,
    )

    merged = MergedState(
        results=[agent_result],
        merged_evidence=evidence,
        merged_proposals=proposals,
        conflicts=[],
        merged_at=_TS,
    )

    # R2.1 P0-4: SupervisorRunResult MUST carry run_identity and
    # result_origins.  The Phase 5A Adapter copies them verbatim.
    identity_hash = stable_hash(
        {
            "run_id": run_id,
            "tenant_id": tenant_id,
            "plan_hash": "plan-recovery-hash",
            "registry_version": "registry-recovery-v1",
        }
    )
    run_identity = ExecutionRunIdentity(
        run_id=run_id,
        tenant_id=tenant_id,
        plan_hash="plan-recovery-hash",
        registry_version="registry-recovery-v1",
        identity_hash=identity_hash,
    )
    proposal_hashes = tuple((p.proposal_id, p.proposal_hash) for p in proposals)
    evidence_hashes = tuple(
        (ev.evidence_id, compute_review_evidence_hash(ev)) for ev in evidence
    )
    origin_hash = stable_hash(
        {
            "run_id": run_id,
            "tenant_id": tenant_id,
            "result_id": agent_result.result_id,
            "task_id": task_id,
            "agent_id": agent_id,
            "agent_version": "1.0.0",
            "proposal_hashes": sorted(proposal_hashes),
            "evidence_hashes": sorted(evidence_hashes),
        }
    )
    result_origin = ResultOriginSnapshot(
        run_id=run_id,
        tenant_id=tenant_id,
        result_id=agent_result.result_id,
        task_id=task_id,
        agent_id=agent_id,
        agent_version="1.0.0",
        proposal_hashes=proposal_hashes,
        evidence_hashes=evidence_hashes,
        origin_hash=origin_hash,
    )

    return SupervisorRunResult(
        run_id=run_id,
        plan_hash="plan-recovery-hash",
        registry_version="registry-recovery-v1",
        status=SupervisorRunStatus.COMPLETED,
        task_records=[
            TaskExecutionRecord(
                task_id=task_id,
                agent_id=agent_id,
                status="completed",
                attempts=[
                    TaskAttemptRecord(
                        task_id=task_id,
                        agent_id=agent_id,
                        attempt=0,
                        started_at=_TS,
                        completed_at=_TS,
                        status="completed",
                        duration_ms=42,
                    ),
                ],
                result=agent_result,
                skip_reason=None,
            ),
        ],
        merged_state=merged,
        usage=ExecutionUsage(),
        trace=[
            ExecutionTraceEvent(
                sequence=0,
                event_type=TRACE_RUN_STARTED,
                run_id=run_id,
                occurred_at=_TS,
            ),
            ExecutionTraceEvent(
                sequence=1,
                event_type=TRACE_TASK_COMPLETED,
                run_id=run_id,
                task_id=task_id,
                agent_id=agent_id,
                occurred_at=_TS,
            ),
            ExecutionTraceEvent(
                sequence=2,
                event_type=TRACE_RUN_COMPLETED,
                run_id=run_id,
                occurred_at=_TS,
            ),
        ],
        capability_bindings=_recovery_capability_bindings(),
        run_identity=run_identity,
        result_origins=(result_origin,),
        started_at=_TS,
        completed_at=_TS,
        duration_ms=100,
    )


def _recovery_capability_bindings() -> list[ExecutionCapabilitySnapshot]:
    cap = _recovery_capability(
        "agent_recovery",
        authority=AgentAuthority.PROPOSE,
        allowed_tools=frozenset(
            {
                "crm_reader.get_customers",
                "crm_writer.propose",
            }
        ),
    )
    return [
        ExecutionCapabilitySnapshot(
            task_id="task-recovery-001",
            agent_id="agent_recovery",
            agent_version="1.0.0",
            capability=cap,
            binding_hash=stable_hash(
                {
                    "task_id": "task-recovery-001",
                    "agent_id": "agent_recovery",
                    "agent_version": "1.0.0",
                    "capability": cap.model_dump(mode="python"),
                }
            ),
        ),
    ]


# ===========================================================================
# End-to-end integration — Customer Recovery scenario
# ===========================================================================


class TestCustomerRecoveryIntegration:
    """Phase 5A Section 17 — Phase 4 Integration.

    Real Phase 4 SupervisorRunResult → ReviewRequest → ReviewBatchResult.
    """

    @pytest.mark.asyncio
    async def test_end_to_end_customer_recovery(self):
        """The full pipeline runs without exception and produces a
        ReviewBatchResult with the expected identity fields.
        """
        supervisor_result = _build_customer_recovery_supervisor_result()
        request = build_review_request(
            supervisor_result,
            review_id="review-recovery-001",
        )
        reviewer = ProposalReviewer()
        evaluator = DeterministicPolicyEvaluator()

        result = await reviewer.review(
            request,
            policy_evaluator=evaluator,
        )

        # Identity fields preserved end-to-end.
        assert result.review_id == "review-recovery-001"
        assert result.run_id == "run-recovery-001"
        assert result.tenant_id == "tenant-recovery"
        assert result.request_hash == request.request_hash

        # All three proposals reviewed.
        assert len(result.proposal_reviews) == 3

        # result_hash is computed and verifies.
        assert result.result_hash != ""
        result.verify_integrity()

    @pytest.mark.asyncio
    async def test_low_risk_report_proposal_is_approved(self):
        """The report.generate proposal (low-risk, evidence-backed,
        valid authority) should be APPROVED.
        """
        supervisor_result = _build_customer_recovery_supervisor_result()
        request = build_review_request(
            supervisor_result,
            review_id="review-recovery-002",
        )
        result = await ProposalReviewer().review(
            request,
            policy_evaluator=DeterministicPolicyEvaluator(),
        )

        review = next(
            r for r in result.proposal_reviews if r.proposal_id == "prop-recovery-001"
        )
        assert review.status == ReviewDecisionStatus.APPROVED
        assert review.risk_level.value == "low"
        assert review.authority_valid is True
        assert review.policy_valid is True

    @pytest.mark.asyncio
    async def test_medium_risk_tag_proposal_is_approved(self):
        """The crm.tag.update proposal (medium-risk, valid PROPOSE
        authority, evidence-backed) should be APPROVED — medium risk
        does not automatically trigger needs_approval under the
        default DeterministicPolicyEvaluator.
        """
        supervisor_result = _build_customer_recovery_supervisor_result()
        request = build_review_request(
            supervisor_result,
            review_id="review-recovery-003",
        )
        result = await ProposalReviewer().review(
            request,
            policy_evaluator=DeterministicPolicyEvaluator(),
        )

        review = next(
            r for r in result.proposal_reviews if r.proposal_id == "prop-recovery-002"
        )
        assert review.status == ReviewDecisionStatus.APPROVED
        assert review.risk_level.value == "medium"

    @pytest.mark.asyncio
    async def test_high_risk_owner_assign_needs_approval(self):
        """The crm.owner.assign proposal (high-risk) MUST be
        NEEDS_APPROVAL — Phase 5A Section 9 high-risk gate.
        """
        supervisor_result = _build_customer_recovery_supervisor_result()
        request = build_review_request(
            supervisor_result,
            review_id="review-recovery-004",
        )
        result = await ProposalReviewer().review(
            request,
            policy_evaluator=DeterministicPolicyEvaluator(),
        )

        review = next(
            r for r in result.proposal_reviews if r.proposal_id == "prop-recovery-003"
        )
        assert review.status == ReviewDecisionStatus.NEEDS_APPROVAL
        assert review.risk_level.value == "high"
        assert review.required_approval is True

    @pytest.mark.asyncio
    async def test_batch_status_reflects_highest_priority_decision(self):
        """With one NEEDS_APPROVAL proposal in the batch, the batch
        status MUST be NEEDS_APPROVAL (priority: needs_approval > approved).
        """
        supervisor_result = _build_customer_recovery_supervisor_result()
        request = build_review_request(
            supervisor_result,
            review_id="review-recovery-005",
        )
        result = await ProposalReviewer().review(
            request,
            policy_evaluator=DeterministicPolicyEvaluator(),
        )

        assert result.batch_status == ReviewBatchStatus.NEEDS_APPROVAL
        assert "prop-recovery-003" in result.approval_required_proposal_ids

    @pytest.mark.asyncio
    async def test_approved_proposals_are_listed(self):
        """Approved proposals appear in approved_proposal_ids."""
        supervisor_result = _build_customer_recovery_supervisor_result()
        request = build_review_request(
            supervisor_result,
            review_id="review-recovery-006",
        )
        result = await ProposalReviewer().review(
            request,
            policy_evaluator=DeterministicPolicyEvaluator(),
        )

        # Both low and medium risk should be approved.
        assert "prop-recovery-001" in result.approved_proposal_ids
        assert "prop-recovery-002" in result.approved_proposal_ids
        # High-risk is in approval_required, not approved.
        assert "prop-recovery-003" not in result.approved_proposal_ids

    @pytest.mark.asyncio
    async def test_result_is_deterministic(self):
        """Two reviews of the same input produce the same result_hash."""
        supervisor_a = _build_customer_recovery_supervisor_result()
        supervisor_b = _build_customer_recovery_supervisor_result()

        request_a = build_review_request(
            supervisor_a,
            review_id="rev",
        )
        request_b = build_review_request(
            supervisor_b,
            review_id="rev",
        )

        result_a = await ProposalReviewer().review(
            request_a,
            policy_evaluator=DeterministicPolicyEvaluator(),
        )
        result_b = await ProposalReviewer().review(
            request_b,
            policy_evaluator=DeterministicPolicyEvaluator(),
        )

        assert result_a.result_hash == result_b.result_hash

    @pytest.mark.asyncio
    async def test_adapter_does_not_modify_source(self):
        """The adapter must not mutate the SupervisorRunResult."""
        supervisor_result = _build_customer_recovery_supervisor_result()
        original_proposal_count = len(supervisor_result.merged_state.merged_proposals)
        original_evidence_count = len(supervisor_result.merged_state.merged_evidence)

        _ = build_review_request(
            supervisor_result,
            review_id="review-no-mutate",
        )

        assert (
            len(supervisor_result.merged_state.merged_proposals)
            == original_proposal_count
        )
        assert (
            len(supervisor_result.merged_state.merged_evidence)
            == original_evidence_count
        )


# ===========================================================================
# Side-effect Guard — Phase 5A Section 17
# ===========================================================================


class TestSideEffectGuard:
    """Phase 5A Section 17 — Side-effect Guard.

    Patch the following interfaces; calling any of them during review
    MUST fail the test:

    * database write
    * Kafka publish
    * CRM update
    * Tool execute
    * AutomationExecutor
    * email/SMS
    * external HTTP
    """

    @pytest.mark.asyncio
    async def test_no_database_write_during_review(self):
        """No DB write path is invoked during review."""
        supervisor_result = _build_customer_recovery_supervisor_result()
        request = build_review_request(
            supervisor_result,
            review_id="review-side-effect-db",
        )

        # Patch common DB write entry points.  If any is called, the
        # test fails via AssertionError.
        with (
            patch("sqlalchemy.orm.Session.commit") as mock_commit,
            patch("sqlalchemy.orm.Session.add") as mock_add,
            patch("sqlalchemy.orm.Session.flush") as mock_flush,
        ):
            mock_commit.side_effect = AssertionError(
                "DB commit called during Phase 5A review"
            )
            mock_add.side_effect = AssertionError(
                "DB add called during Phase 5A review"
            )
            mock_flush.side_effect = AssertionError(
                "DB flush called during Phase 5A review"
            )

            await ProposalReviewer().review(
                request,
                policy_evaluator=DeterministicPolicyEvaluator(),
            )

    @pytest.mark.asyncio
    async def test_no_kafka_publish_during_review(self):
        """No Kafka producer call is invoked during review.

        Guards with ``create=True`` so the test passes even if the
        ``kafka`` package is not installed (the Reviewer does not
        depend on it).
        """
        supervisor_result = _build_customer_recovery_supervisor_result()
        request = build_review_request(
            supervisor_result,
            review_id="review-side-effect-kafka",
        )

        try:
            import kafka  # noqa: F401

            kafka_available = True
        except ImportError:
            kafka_available = False

        if kafka_available:
            with (
                patch("kafka.KafkaProducer.send") as mock_send,
                patch("kafka.KafkaProducer.flush") as mock_flush,
            ):
                mock_send.side_effect = AssertionError(
                    "Kafka send called during Phase 5A review"
                )
                mock_flush.side_effect = AssertionError(
                    "Kafka flush called during Phase 5A review"
                )
                await ProposalReviewer().review(
                    request,
                    policy_evaluator=DeterministicPolicyEvaluator(),
                )
        else:
            # kafka package not installed — no side effect possible.
            await ProposalReviewer().review(
                request,
                policy_evaluator=DeterministicPolicyEvaluator(),
            )

    @pytest.mark.asyncio
    async def test_no_external_http_during_review(self):
        """No external HTTP request is made during review."""
        supervisor_result = _build_customer_recovery_supervisor_result()
        request = build_review_request(
            supervisor_result,
            review_id="review-side-effect-http",
        )

        with (
            patch("httpx.AsyncClient.post") as mock_post,
            patch("httpx.AsyncClient.get") as mock_get,
            patch("requests.post") as mock_req_post,
            patch("requests.get") as mock_req_get,
        ):
            for m in (mock_post, mock_get, mock_req_post, mock_req_get):
                m.side_effect = AssertionError(
                    "External HTTP called during Phase 5A review"
                )

            await ProposalReviewer().review(
                request,
                policy_evaluator=DeterministicPolicyEvaluator(),
            )

    @pytest.mark.asyncio
    async def test_no_tool_execution_during_review(self):
        """Phase 5A Section 7.4: only Tool allowlist validation — no
        actual Tool invocation.  Patch the Tool registry's execute
        path; if it is called, fail.
        """
        supervisor_result = _build_customer_recovery_supervisor_result()
        request = build_review_request(
            supervisor_result,
            review_id="review-side-effect-tool",
        )

        # The Reviewer never imports Tool handlers at runtime, but we
        # patch the registry's call path to be defensive.  If a future
        # change introduces a Tool call, this test will catch it.
        with patch(
            "multi_agent.registry.AgentRegistry.invoke_tool",
            create=True,
        ) as mock_invoke:
            mock_invoke.side_effect = AssertionError(
                "Tool invoke called during Phase 5A review"
            )

            await ProposalReviewer().review(
                request,
                policy_evaluator=DeterministicPolicyEvaluator(),
            )

    @pytest.mark.asyncio
    async def test_no_automation_executor_call_during_review(self):
        """AutomationExecutorAgent MUST NOT be invoked — Phase 5A
        forbids it (Section 3).
        """
        supervisor_result = _build_customer_recovery_supervisor_result()
        request = build_review_request(
            supervisor_result,
            review_id="review-side-effect-executor",
        )

        # Patch the executor module's run entry point if it exists;
        # otherwise this is a no-op (the module may not exist yet in
        # Phase 5A, which is the correct state).
        try:
            with patch(
                "agents.automation.executor.AutomationExecutorAgent.run",
                create=True,
            ) as mock_run:
                mock_run.side_effect = AssertionError(
                    "AutomationExecutor called during Phase 5A review"
                )
                await ProposalReviewer().review(
                    request,
                    policy_evaluator=DeterministicPolicyEvaluator(),
                )
        except (ModuleNotFoundError, AttributeError):
            # Module not present — no side effect possible.
            await ProposalReviewer().review(
                request,
                policy_evaluator=DeterministicPolicyEvaluator(),
            )

    @pytest.mark.asyncio
    async def test_no_crm_write_during_review(self):
        """No CRM write path is invoked during review."""
        supervisor_result = _build_customer_recovery_supervisor_result()
        request = build_review_request(
            supervisor_result,
            review_id="review-side-effect-crm",
        )

        # Patch CRM writer entry points if they exist.
        try:
            with (
                patch(
                    "agents.integrations.crm.CRMClient.update_customer",
                    create=True,
                ) as mock_update,
                patch(
                    "agents.integrations.crm.CRMClient.create_note",
                    create=True,
                ) as mock_note,
            ):
                mock_update.side_effect = AssertionError(
                    "CRM update called during Phase 5A review"
                )
                mock_note.side_effect = AssertionError(
                    "CRM note create called during Phase 5A review"
                )
                await ProposalReviewer().review(
                    request,
                    policy_evaluator=DeterministicPolicyEvaluator(),
                )
        except (ModuleNotFoundError, AttributeError):
            await ProposalReviewer().review(
                request,
                policy_evaluator=DeterministicPolicyEvaluator(),
            )

    @pytest.mark.asyncio
    async def test_no_email_or_sms_during_review(self):
        """No email or SMS send is invoked during review."""
        supervisor_result = _build_customer_recovery_supervisor_result()
        request = build_review_request(
            supervisor_result,
            review_id="review-side-effect-msg",
        )

        try:
            with (
                patch(
                    "agents.integrations.notifications.EmailSender.send",
                    create=True,
                ) as mock_email,
                patch(
                    "agents.integrations.notifications.SmsSender.send",
                    create=True,
                ) as mock_sms,
            ):
                mock_email.side_effect = AssertionError(
                    "Email send called during Phase 5A review"
                )
                mock_sms.side_effect = AssertionError(
                    "SMS send called during Phase 5A review"
                )
                await ProposalReviewer().review(
                    request,
                    policy_evaluator=DeterministicPolicyEvaluator(),
                )
        except (ModuleNotFoundError, AttributeError):
            await ProposalReviewer().review(
                request,
                policy_evaluator=DeterministicPolicyEvaluator(),
            )

    @pytest.mark.asyncio
    async def test_no_opa_network_call_with_default_evaluator(self):
        """The default :class:`DeterministicPolicyEvaluator` MUST NOT
        make any network call to OPA.  Patch ``httpx.post`` to fail if
        called.
        """
        supervisor_result = _build_customer_recovery_supervisor_result()
        request = build_review_request(
            supervisor_result,
            review_id="review-side-effect-opa",
        )

        with (
            patch("httpx.post") as mock_post,
            patch("httpx.get") as mock_get,
            patch("requests.post") as mock_req_post,
            patch("requests.get") as mock_req_get,
        ):
            for m in (mock_post, mock_get, mock_req_post, mock_req_get):
                m.side_effect = AssertionError(
                    "OPA network call made with default DeterministicPolicyEvaluator"
                )

            result = await ProposalReviewer().review(
                request,
                policy_evaluator=DeterministicPolicyEvaluator(),
            )
            # Sanity: the review completed and produced a result.
            assert isinstance(result, ReviewBatchResult)


# ===========================================================================
# ActionProposal never executed — invariant
# ===========================================================================


class TestApprovedDoesNotMeanExecuted:
    """Phase 5A Section 3 — APPROVED != EXECUTED.

    The Reviewer only decides whether a Proposal is *allowed* to be
    executed later.  It must NEVER execute the Proposal itself.
    """

    @pytest.mark.asyncio
    async def test_approved_proposals_have_no_side_effects(self):
        """Even when ALL proposals are approved, no HTTP side-effect
        path is touched.
        """
        supervisor_result = _build_customer_recovery_supervisor_result()
        request = build_review_request(
            supervisor_result,
            review_id="review-approved-noexec",
        )

        # Patch HTTP entry points with explicit AssertionError side
        # effects — if the Reviewer attempts any external call, the
        # test fails immediately.
        with (
            patch("httpx.post") as mock_post,
            patch("httpx.get") as mock_get,
            patch("requests.post") as mock_req_post,
            patch("requests.get") as mock_req_get,
        ):
            for m in (mock_post, mock_get, mock_req_post, mock_req_get):
                m.side_effect = AssertionError(
                    "External HTTP called during approved-Proposal review"
                )

            result = await ProposalReviewer().review(
                request,
                policy_evaluator=DeterministicPolicyEvaluator(),
            )

            # At least one proposal was approved.
            assert len(result.approved_proposal_ids) > 0
            # None of the HTTP mocks were called — side_effect would
            # have raised AssertionError otherwise.

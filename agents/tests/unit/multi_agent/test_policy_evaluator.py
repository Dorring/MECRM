"""Phase 5A Policy Evaluator tests.

Covers:

* DeterministicPolicyEvaluator: allow, deny, needs_approval, needs_input
* Deterministic replay consistency
* OPAReviewAdapter: fail-fast on missing config, never connects on import
* FakePolicyEvaluator: preset + default
* PolicyEvaluationRequest / Result contract validation
"""

from __future__ import annotations


import pytest
from pydantic import ValidationError

from multi_agent.policy import (
    DeterministicPolicyEvaluator,
    FakePolicyEvaluator,
    OPAReviewAdapter,
    OPAReviewAdapterConfig,
    PolicyDecision,
    PolicyEvaluationRequest,
    PolicyEvaluationResult,
)
from multi_agent.review_contracts import (
    CODE_POLICY_DENIED,
    PolicyContext,
)
from multi_agent.review_errors import PolicyEvaluationError


def _ctx(rules=None):
    return PolicyContext(
        policy_version="test-v1",
        rules=rules or [],
    )


def _req(
    *,
    action_type="report.generate",
    risk_level="low",
    agent_authority="read",
    rules=None,
    proposal_id="p1",
):
    return PolicyEvaluationRequest(
        review_id="r1",
        tenant_id="t1",
        run_id="run1",
        proposal_id=proposal_id,
        action_type=action_type,
        target_entity="report",
        policy_context=_ctx(rules),
        risk_level=risk_level,
        agent_authority=agent_authority,
    )


# ---------------------------------------------------------------------------
# DeterministicPolicyEvaluator
# ---------------------------------------------------------------------------


class TestDeterministicPolicyEvaluator:
    @pytest.mark.asyncio
    async def test_low_risk_read_action_allowed(self):
        ev = DeterministicPolicyEvaluator()
        result = await ev.evaluate(_req())
        assert result.decision == PolicyDecision.ALLOWED
        assert len(result.matched_rules) >= 1

    @pytest.mark.asyncio
    async def test_unknown_action_denied(self):
        ev = DeterministicPolicyEvaluator()
        result = await ev.evaluate(_req(action_type="nonexistent.action"))
        assert result.decision == PolicyDecision.DENIED
        assert any(f.finding_code == CODE_POLICY_DENIED for f in result.findings)

    @pytest.mark.asyncio
    async def test_execute_only_action_denied(self):
        ev = DeterministicPolicyEvaluator()
        result = await ev.evaluate(_req(action_type="account.delete"))
        assert result.decision == PolicyDecision.DENIED

    @pytest.mark.asyncio
    async def test_high_risk_needs_approval(self):
        ev = DeterministicPolicyEvaluator()
        result = await ev.evaluate(
            _req(
                action_type="crm.tag.update",
                risk_level="high",
                agent_authority="propose",
            )
        )
        assert result.decision == PolicyDecision.NEEDS_APPROVAL

    @pytest.mark.asyncio
    async def test_always_needs_approval_action(self):
        ev = DeterministicPolicyEvaluator()
        result = await ev.evaluate(
            _req(
                action_type="refund.issue",
                agent_authority="propose",
            )
        )
        assert result.decision == PolicyDecision.NEEDS_APPROVAL

    @pytest.mark.asyncio
    async def test_authority_floor_enforced(self):
        ev = DeterministicPolicyEvaluator()
        # READ agent proposing a PROPOSE-level action
        result = await ev.evaluate(
            _req(
                action_type="crm.tag.update",
                agent_authority="read",
            )
        )
        assert result.decision == PolicyDecision.DENIED

    @pytest.mark.asyncio
    async def test_explicit_deny_rule(self):
        ev = DeterministicPolicyEvaluator()
        rules = [
            {
                "rule_id": "deny-reports",
                "effect": "denied",
                "action_type": "report.generate",
            }
        ]
        result = await ev.evaluate(_req(rules=rules))
        assert result.decision == PolicyDecision.DENIED

    @pytest.mark.asyncio
    async def test_needs_input_rule(self):
        ev = DeterministicPolicyEvaluator()
        rules = [
            {
                "rule_id": "needs-input-reports",
                "effect": "needs_input",
                "action_type": "report.generate",
            }
        ]
        result = await ev.evaluate(_req(rules=rules))
        assert result.decision == PolicyDecision.NEEDS_INPUT

    @pytest.mark.asyncio
    async def test_deterministic_replay(self):
        ev = DeterministicPolicyEvaluator()
        r1 = await ev.evaluate(_req())
        r2 = await ev.evaluate(_req())
        assert r1.model_dump_json() == r2.model_dump_json()
        assert r1.matched_rules == r2.matched_rules

    @pytest.mark.asyncio
    async def test_different_proposal_id_same_decision(self):
        """Decision depends on action, not on proposal_id."""
        ev = DeterministicPolicyEvaluator()
        r1 = await ev.evaluate(_req(proposal_id="p1"))
        r2 = await ev.evaluate(_req(proposal_id="p2"))
        assert r1.decision == r2.decision

    @pytest.mark.asyncio
    async def test_matched_rules_sorted_by_rule_id(self):
        ev = DeterministicPolicyEvaluator()
        rules = [
            {
                "rule_id": "z-rule",
                "effect": "allowed",
                "action_type": "report.generate",
            },
            {
                "rule_id": "a-rule",
                "effect": "allowed",
                "action_type": "report.generate",
            },
        ]
        result = await ev.evaluate(_req(rules=rules))
        # The category-allowlist + authority-floor rules are also matched;
        # verify the overall list is sorted.
        ids = [r.rule_id for r in result.matched_rules]
        assert ids == sorted(ids)


# ---------------------------------------------------------------------------
# OPAReviewAdapter — boundary
# ---------------------------------------------------------------------------


class TestOPAReviewAdapterConfig:
    def test_blank_endpoint_rejected(self):
        with pytest.raises(PolicyEvaluationError):
            OPAReviewAdapterConfig(endpoint="", policy_path="/v1/data")

    def test_blank_policy_path_rejected(self):
        with pytest.raises(PolicyEvaluationError):
            OPAReviewAdapterConfig(endpoint="http://opa:8181", policy_path="")

    def test_non_positive_timeout_rejected(self):
        with pytest.raises(PolicyEvaluationError):
            OPAReviewAdapterConfig(
                endpoint="http://opa:8181",
                policy_path="/v1/data",
                timeout_ms=0,
            )

    def test_valid_config_accepted(self):
        cfg = OPAReviewAdapterConfig(
            endpoint="http://opa:8181",
            policy_path="/v1/data/decision",
        )
        assert cfg.endpoint == "http://opa:8181"


class TestOPAReviewAdapterImport:
    def test_module_import_has_no_network(self):
        """Import the policy module and verify no HTTP client was created."""
        import multi_agent.policy as mod

        # The module should not have any module-level transport / client.
        assert not hasattr(mod, "_DEFAULT_HTTP_CLIENT")
        assert not hasattr(mod, "_OPA_ENDPOINT")


class TestOPAReviewAdapterEvaluation:
    @pytest.mark.asyncio
    async def test_evaluate_without_transport_fails_fast(self):
        cfg = OPAReviewAdapterConfig(
            endpoint="http://opa:8181",
            policy_path="/v1/data",
        )
        adapter = OPAReviewAdapter(cfg)
        with pytest.raises(PolicyEvaluationError):
            await adapter.evaluate(_req())

    @pytest.mark.asyncio
    async def test_evaluate_with_fake_transport(self):
        """Use a fake transport to verify the adapter parses OPA responses."""
        cfg = OPAReviewAdapterConfig(
            endpoint="http://opa:8181",
            policy_path="/v1/data",
        )

        class FakeResponse:
            def __init__(self, status_code, body):
                self.status_code = status_code
                self._body = body

            def json(self):
                return self._body

        class FakeTransport:
            async def post(self, url, json=None, timeout=None, headers=None):
                return FakeResponse(
                    200,
                    {
                        "result": {
                            "decision": "denied",
                            "matched_rules": [
                                {"rule_id": "rule-1", "effect": "denied"}
                            ],
                        }
                    },
                )

        adapter = OPAReviewAdapter(cfg).with_transport(FakeTransport())
        result = await adapter.evaluate(_req())
        assert result.decision == PolicyDecision.DENIED
        assert any(r.rule_id == "rule-1" for r in result.matched_rules)

    @pytest.mark.asyncio
    async def test_transport_error_raises_policy_error(self):
        cfg = OPAReviewAdapterConfig(
            endpoint="http://opa:8181",
            policy_path="/v1/data",
        )

        class ErrorTransport:
            async def post(self, *args, **kwargs):
                raise ConnectionError("network down")

        adapter = OPAReviewAdapter(cfg).with_transport(ErrorTransport())
        with pytest.raises(PolicyEvaluationError):
            await adapter.evaluate(_req())

    @pytest.mark.asyncio
    async def test_non_200_status_raises(self):
        cfg = OPAReviewAdapterConfig(
            endpoint="http://opa:8181",
            policy_path="/v1/data",
        )

        class FakeResponse:
            def __init__(self, status_code):
                self.status_code = status_code

            def json(self):
                return {}

        class FakeTransport:
            async def post(self, *args, **kwargs):
                return FakeResponse(500)

        adapter = OPAReviewAdapter(cfg).with_transport(FakeTransport())
        with pytest.raises(PolicyEvaluationError):
            await adapter.evaluate(_req())


# ---------------------------------------------------------------------------
# FakePolicyEvaluator
# ---------------------------------------------------------------------------


class TestFakePolicyEvaluator:
    @pytest.mark.asyncio
    async def test_preset_returned(self):
        fake = FakePolicyEvaluator()
        preset = PolicyEvaluationResult(
            proposal_id="p1",
            decision=PolicyDecision.DENIED,
            policy_version="v1",
        )
        fake.set("p1", preset)
        result = await fake.evaluate(_req(proposal_id="p1"))
        assert result is preset
        assert len(fake.calls) == 1

    @pytest.mark.asyncio
    async def test_default_returned_when_no_preset(self):
        fake = FakePolicyEvaluator()
        default = PolicyEvaluationResult(
            proposal_id="p1",
            decision=PolicyDecision.NEEDS_APPROVAL,
            policy_version="v1",
        )
        fake.default = default
        result = await fake.evaluate(_req(proposal_id="p1"))
        assert result is default

    @pytest.mark.asyncio
    async def test_allowed_default_when_no_preset_or_default(self):
        fake = FakePolicyEvaluator()
        result = await fake.evaluate(_req())
        assert result.decision == PolicyDecision.ALLOWED


# ---------------------------------------------------------------------------
# Contract validation
# ---------------------------------------------------------------------------


class TestPolicyContracts:
    def test_request_extra_field_rejected(self):
        with pytest.raises(ValidationError):
            PolicyEvaluationRequest(
                review_id="r1",
                tenant_id="t1",
                run_id="run1",
                proposal_id="p1",
                action_type="report.generate",
                target_entity="report",
                policy_context=_ctx(),
                extra_field="bad",  # type: ignore[call-arg]
            )

    def test_request_blank_action_rejected(self):
        with pytest.raises(ValidationError):
            PolicyEvaluationRequest(
                review_id="r1",
                tenant_id="t1",
                run_id="run1",
                proposal_id="p1",
                action_type="  ",
                target_entity="report",
                policy_context=_ctx(),
            )

    def test_result_extra_field_rejected(self):
        with pytest.raises(ValidationError):
            PolicyEvaluationResult(
                proposal_id="p1",
                decision=PolicyDecision.ALLOWED,
                policy_version="v1",
                extra_field="bad",  # type: ignore[call-arg]
            )

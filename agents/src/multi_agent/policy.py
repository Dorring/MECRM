"""Phase 5A Policy Evaluator Boundary (R2).

Defines the :class:`PolicyEvaluator` Protocol and two implementations:

* :class:`DeterministicPolicyEvaluator` — the **default** evaluator.
  No network, no API keys, no I/O.  Pure-function rule matching against
  the :class:`PolicyContext` carried on the :class:`ReviewRequest`.
  Suitable for CI and deterministic replay.

* :class:`OPAReviewAdapter` — a **boundary** implementation that talks
  to a real OPA endpoint.  Per Phase 5A Section 8:

    - Never default-initialized (caller must construct explicitly).
    - Never connects on import.
    - Fails fast when configuration is missing.
    - Phase 5A tests use :class:`FakePolicyEvaluator` rather than this
      adapter.
    - Does NOT change existing OPA production paths under ``policies/``.

R2 changes:

* :class:`PolicyDecision` and :class:`PolicyMatchedRule` moved to
  :mod:`multi_agent.review_contracts` (re-imported here for compat).
* :class:`PolicyEvaluationResult` uses ``tuple`` collections (S1) and
  gains :meth:`verify_semantics` (P0-6).
* :class:`DeterministicPolicyEvaluator` reads from
  :data:`multi_agent.action_governance.ACTION_GOVERNANCE_REGISTRY`
  instead of local lookup tables (S14).
* Context-rule aggregation uses priority order
  ``denied > needs_input > needs_approval > allowed`` (P0-6).
* :class:`OPAReviewAdapter` fails closed on missing OPA response fields
  and enforces ``asyncio.wait_for`` timeout (S11).

Phase 5A Section 8 reminder: Policy NEVER directly executes an Action.
Policy only returns ``allowed`` / ``denied`` / ``needs_approval`` /
``needs_input``.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Any, Protocol

from pydantic import Field, field_validator

from multi_agent.contracts import (
    AgentAuthority,
    JsonValue,
    StrictContract,
    _reject_sensitive_keys,
)
from multi_agent.serialization import (
    validate_strict_json,
)
from multi_agent.review_contracts import (
    CODE_POLICY_DENIED,
    CODE_POLICY_NEEDS_APPROVAL,
    CODE_POLICY_NEEDS_INPUT,
    PolicyContext,
    PolicyDecision,
    PolicyMatchedRule,
    PolicyRule,
    ReviewFinding,
    ReviewFindingSeverity,
)
from multi_agent.action_governance import (
    get_action_governance_spec,
)
from multi_agent.review_errors import (
    InvalidReviewResultError,
    PolicyEvaluationError,
)


# ---------------------------------------------------------------------------
# Policy Evaluation Request / Result contracts
# ---------------------------------------------------------------------------


class PolicyEvaluationRequest(StrictContract):
    """Frozen input to :meth:`PolicyEvaluator.evaluate`.

    Carries everything the Policy engine needs to make a deterministic
    decision without re-reading the live registry or the full
    :class:`ReviewRequest`.
    """

    model_config = {"extra": "forbid", "frozen": True}

    review_id: str
    tenant_id: str
    run_id: str
    proposal_id: str
    action_type: str
    target_entity: str
    target_id: str | None = None
    payload: dict[str, JsonValue] = Field(default_factory=dict)
    risk_level: str = "low"
    agent_authority: str = "read"
    policy_context: PolicyContext

    @field_validator(
        "review_id",
        "tenant_id",
        "run_id",
        "proposal_id",
        "action_type",
        "target_entity",
    )
    @classmethod
    def _non_blank(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError(
                "PolicyEvaluationRequest identity/action fields must not be blank"
            )
        return v

    @field_validator("payload")
    @classmethod
    def _validate_payload(cls, v: dict[str, Any]) -> dict[str, Any]:
        _reject_sensitive_keys(v, "PolicyEvaluationRequest.payload")
        return validate_strict_json(v)  # type: ignore[return-value]


class PolicyEvaluationResult(StrictContract):
    """Frozen output of :meth:`PolicyEvaluator.evaluate`.

    ``matched_rules`` is sorted by ``rule_id`` so the result hash is
    stable.  ``findings`` carries human-readable context for each
    non-allowed decision.

    R2 S1: ``matched_rules`` and ``findings`` are ``tuple`` (deep
    immutability).
    """

    model_config = {"extra": "forbid", "frozen": True}

    proposal_id: str
    decision: PolicyDecision
    matched_rules: tuple[PolicyMatchedRule, ...] = ()
    policy_version: str
    findings: tuple[ReviewFinding, ...] = ()

    @field_validator("proposal_id", "policy_version")
    @classmethod
    def _non_blank(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("PolicyEvaluationResult identity fields must not be blank")
        return v

    def verify_semantics(self) -> None:
        """R2 P0-6: validate decision ↔ finding consistency.

        Raises :class:`InvalidReviewResultError` on any inconsistency.
        """
        finding_codes = {f.finding_code for f in self.findings}
        has_error = any(
            f.severity in (ReviewFindingSeverity.ERROR, ReviewFindingSeverity.CRITICAL)
            for f in self.findings
        )

        # All findings must belong to this proposal
        for f in self.findings:
            if f.proposal_id != self.proposal_id:
                raise InvalidReviewResultError(
                    f"PolicyEvaluationResult {self.proposal_id!r}: finding "
                    f"proposal_id {f.proposal_id!r} != result proposal_id"
                )

        if self.decision == PolicyDecision.DENIED:
            if CODE_POLICY_DENIED not in finding_codes:
                raise InvalidReviewResultError(
                    f"PolicyEvaluationResult {self.proposal_id!r}: decision "
                    f"DENIED but no {CODE_POLICY_DENIED!r} finding"
                )
        elif self.decision == PolicyDecision.NEEDS_INPUT:
            if CODE_POLICY_NEEDS_INPUT not in finding_codes:
                raise InvalidReviewResultError(
                    f"PolicyEvaluationResult {self.proposal_id!r}: decision "
                    f"NEEDS_INPUT but no {CODE_POLICY_NEEDS_INPUT!r} finding"
                )
        elif self.decision == PolicyDecision.NEEDS_APPROVAL:
            if CODE_POLICY_NEEDS_APPROVAL not in finding_codes:
                raise InvalidReviewResultError(
                    f"PolicyEvaluationResult {self.proposal_id!r}: decision "
                    f"NEEDS_APPROVAL but no {CODE_POLICY_NEEDS_APPROVAL!r} finding"
                )
        elif self.decision == PolicyDecision.ALLOWED:
            if has_error:
                raise InvalidReviewResultError(
                    f"PolicyEvaluationResult {self.proposal_id!r}: decision "
                    f"ALLOWED but has ERROR/CRITICAL findings"
                )


# ---------------------------------------------------------------------------
# Protocol
# ---------------------------------------------------------------------------


class PolicyEvaluator(Protocol):
    """Policy evaluation boundary.

    Implementations MUST be deterministic for the same
    (request, policy_context) pair.  Network-bound implementations
    (e.g. :class:`OPAReviewAdapter`) must fail fast on missing config
    and must never be the default.

    Phase 5A Section 8: Policy NEVER directly executes an Action.
    """

    async def evaluate(
        self,
        request: PolicyEvaluationRequest,
    ) -> PolicyEvaluationResult: ...


# ---------------------------------------------------------------------------
# Deterministic Policy Evaluator (default)
# ---------------------------------------------------------------------------


def _authority_rank(a: AgentAuthority) -> int:
    """READ=0, PROPOSE=1, EXECUTE=2."""
    return {
        AgentAuthority.READ: 0,
        AgentAuthority.PROPOSE: 1,
        AgentAuthority.EXECUTE: 2,
    }[a]


class DeterministicPolicyEvaluator:
    """Pure-function policy evaluator — the Phase 5A default.

    No network, no API keys, no I/O.  Decision is a function of:

    1. ``action_type`` registered in :data:`ACTION_GOVERNANCE_REGISTRY`
       with ``reviewable=True`` (R2 S14 — no local lookup table).
    2. Agent authority vs ``spec.minimum_authority``.
    3. ``spec.always_needs_approval`` or high-risk → needs_approval.
    4. ``PolicyContext.rules`` explicit deny / needs_input overrides
       aggregated by priority ``denied > needs_input >
       needs_approval > allowed`` (R2 P0-6).

    The evaluator reads ONLY from the frozen :class:`PolicyContext`
    on the request — it never reads a live registry, never reads the
    wall-clock, and never reads ``PYTHONHASHSEED``.
    """

    def __init__(self) -> None:
        pass

    async def evaluate(
        self,
        request: PolicyEvaluationRequest,
    ) -> PolicyEvaluationResult:
        return self._evaluate_pure(request)

    def _evaluate_pure(
        self, request: PolicyEvaluationRequest
    ) -> PolicyEvaluationResult:
        action = request.action_type
        ctx = request.policy_context
        matched: list[PolicyMatchedRule] = []
        findings: list[ReviewFinding] = []

        # 1. Action governance spec gate (R2 S14).
        spec = get_action_governance_spec(action)
        if spec is None or not spec.reviewable:
            findings.append(
                ReviewFinding(
                    finding_code=CODE_POLICY_DENIED,
                    severity=ReviewFindingSeverity.ERROR,
                    message=(
                        f"Action {action!r} is not registered as reviewable "
                        f"in the Action Governance Spec"
                    ),
                    proposal_id=request.proposal_id,
                    policy_source=f"deterministic@{ctx.policy_version}",
                    details={"action_type": action, "category": "not_reviewable"},
                )
            )
            return PolicyEvaluationResult(
                proposal_id=request.proposal_id,
                decision=PolicyDecision.DENIED,
                matched_rules=tuple(sorted(matched, key=lambda r: r.rule_id)),
                policy_version=ctx.policy_version,
                findings=tuple(findings),
            )

        matched.append(
            PolicyMatchedRule(
                rule_id="governance-spec-allowlist",
                rule_version=ctx.policy_version,
                effect=PolicyDecision.ALLOWED,
                matched_fields=("action_type",),
            )
        )

        # 2. Authority floor — from governance spec (R2 S14).
        floor = spec.minimum_authority
        try:
            agent_auth = AgentAuthority(request.agent_authority)
        except ValueError:
            findings.append(
                ReviewFinding(
                    finding_code=CODE_POLICY_DENIED,
                    severity=ReviewFindingSeverity.ERROR,
                    message=(f"Unknown agent_authority {request.agent_authority!r}"),
                    proposal_id=request.proposal_id,
                    policy_source=f"deterministic@{ctx.policy_version}",
                    details={"agent_authority": request.agent_authority},
                )
            )
            return PolicyEvaluationResult(
                proposal_id=request.proposal_id,
                decision=PolicyDecision.DENIED,
                matched_rules=tuple(sorted(matched, key=lambda r: r.rule_id)),
                policy_version=ctx.policy_version,
                findings=tuple(findings),
            )

        if _authority_rank(agent_auth) < _authority_rank(floor):
            findings.append(
                ReviewFinding(
                    finding_code=CODE_POLICY_DENIED,
                    severity=ReviewFindingSeverity.ERROR,
                    message=(
                        f"Agent authority {agent_auth.value!r} is below "
                        f"the required floor {floor.value!r} for action "
                        f"{action!r}"
                    ),
                    proposal_id=request.proposal_id,
                    policy_source=f"deterministic@{ctx.policy_version}",
                    details={
                        "action_type": action,
                        "agent_authority": agent_auth.value,
                        "required_floor": floor.value,
                    },
                )
            )
            return PolicyEvaluationResult(
                proposal_id=request.proposal_id,
                decision=PolicyDecision.DENIED,
                matched_rules=tuple(sorted(matched, key=lambda r: r.rule_id)),
                policy_version=ctx.policy_version,
                findings=tuple(findings),
            )

        matched.append(
            PolicyMatchedRule(
                rule_id="authority-floor",
                rule_version=ctx.policy_version,
                effect=PolicyDecision.ALLOWED,
                matched_fields=("agent_authority", "action_type"),
            )
        )

        # 3. Built-in needs-approval signals from governance spec (R2 S14).
        builtin_needs_approval = False
        if spec.always_needs_approval:
            builtin_needs_approval = True
            matched.append(
                PolicyMatchedRule(
                    rule_id="always-needs-approval",
                    rule_version=ctx.policy_version,
                    effect=PolicyDecision.NEEDS_APPROVAL,
                    matched_fields=("action_type",),
                )
            )
            findings.append(
                ReviewFinding(
                    finding_code=CODE_POLICY_NEEDS_APPROVAL,
                    severity=ReviewFindingSeverity.WARNING,
                    message=(f"Action {action!r} always requires human approval"),
                    proposal_id=request.proposal_id,
                    policy_source=f"deterministic@{ctx.policy_version}",
                    details={"action_type": action},
                )
            )

        if request.risk_level in ("high", "critical"):
            builtin_needs_approval = True
            matched.append(
                PolicyMatchedRule(
                    rule_id="high-risk-needs-approval",
                    rule_version=ctx.policy_version,
                    effect=PolicyDecision.NEEDS_APPROVAL,
                    matched_fields=("risk_level",),
                )
            )
            findings.append(
                ReviewFinding(
                    finding_code=CODE_POLICY_NEEDS_APPROVAL,
                    severity=ReviewFindingSeverity.WARNING,
                    message=(
                        f"Risk level {request.risk_level!r} requires human approval"
                    ),
                    proposal_id=request.proposal_id,
                    policy_source=f"deterministic@{ctx.policy_version}",
                    details={"risk_level": request.risk_level},
                )
            )

        # 4. PolicyContext.rules — strictly typed PolicyRule (R2 P0-6).
        #    Collect ALL matching rules (no first-match early return).
        context_effects: set[PolicyDecision] = set()
        for rule in ctx.rules:
            # R2 P0-6: rule is a strictly-typed PolicyRule, not a dict.
            # Blank rule_id / illegal effect are already rejected at
            # construction by PolicyRule's validators.
            rule_action = rule.action_type
            if rule_action is not None and rule_action != action:
                continue
            context_effects.add(rule.effect)
            matched.append(
                PolicyMatchedRule(
                    rule_id=rule.rule_id,
                    rule_version=rule.rule_version,
                    effect=rule.effect,
                    matched_fields=("action_type",) if rule_action else (),
                )
            )
            if rule.effect == PolicyDecision.DENIED:
                findings.append(
                    ReviewFinding(
                        finding_code=CODE_POLICY_DENIED,
                        severity=ReviewFindingSeverity.ERROR,
                        message=(
                            f"Policy rule {rule.rule_id!r} denied action {action!r}"
                        ),
                        proposal_id=request.proposal_id,
                        policy_source=f"deterministic@{ctx.policy_version}",
                        details={"rule_id": rule.rule_id, "action_type": action},
                    )
                )
            elif rule.effect == PolicyDecision.NEEDS_INPUT:
                findings.append(
                    ReviewFinding(
                        finding_code=CODE_POLICY_NEEDS_INPUT,
                        severity=ReviewFindingSeverity.WARNING,
                        message=(
                            f"Policy rule {rule.rule_id!r} requires more input "
                            f"for action {action!r}"
                        ),
                        proposal_id=request.proposal_id,
                        policy_source=f"deterministic@{ctx.policy_version}",
                        details={"rule_id": rule.rule_id, "action_type": action},
                    )
                )
            elif rule.effect == PolicyDecision.NEEDS_APPROVAL:
                findings.append(
                    ReviewFinding(
                        finding_code=CODE_POLICY_NEEDS_APPROVAL,
                        severity=ReviewFindingSeverity.WARNING,
                        message=(
                            f"Policy rule {rule.rule_id!r} requires approval "
                            f"for action {action!r}"
                        ),
                        proposal_id=request.proposal_id,
                        policy_source=f"deterministic@{ctx.policy_version}",
                        details={"rule_id": rule.rule_id, "action_type": action},
                    )
                )

        # 5. R2 P0-6: aggregate by priority
        #    denied > needs_input > needs_approval > allowed.
        if PolicyDecision.DENIED in context_effects:
            final_decision = PolicyDecision.DENIED
        elif PolicyDecision.NEEDS_INPUT in context_effects:
            final_decision = PolicyDecision.NEEDS_INPUT
        elif builtin_needs_approval or PolicyDecision.NEEDS_APPROVAL in context_effects:
            final_decision = PolicyDecision.NEEDS_APPROVAL
        else:
            final_decision = PolicyDecision.ALLOWED

        return PolicyEvaluationResult(
            proposal_id=request.proposal_id,
            decision=final_decision,
            matched_rules=tuple(sorted(matched, key=lambda r: r.rule_id)),
            policy_version=ctx.policy_version,
            findings=tuple(findings),
        )


# ---------------------------------------------------------------------------
# OPA Review Adapter (boundary — never default-initialized)
# ---------------------------------------------------------------------------


@dataclass
class OPAReviewAdapterConfig:
    """Configuration for :class:`OPAReviewAdapter`.

    Phase 5A Section 8: this config MUST be supplied explicitly by the
    caller.  There is no default URL, no default API key, and no
    environment-variable fallback.  Missing configuration raises
    :class:`PolicyEvaluationError` at construction time (fail-fast),
    not at evaluation time.
    """

    endpoint: str
    policy_path: str
    timeout_ms: int = 5_000
    api_key: str | None = None

    def __post_init__(self) -> None:
        if not self.endpoint.strip():
            raise PolicyEvaluationError(
                "OPAReviewAdapterConfig.endpoint must not be blank"
            )
        if not self.policy_path.strip():
            raise PolicyEvaluationError(
                "OPAReviewAdapterConfig.policy_path must not be blank"
            )
        if self.timeout_ms <= 0:
            raise PolicyEvaluationError("OPAReviewAdapterConfig.timeout_ms must be > 0")


class OPAReviewAdapter:
    """Boundary adapter to a real OPA endpoint.

    R2 S11: external Policy calls are wrapped in ``asyncio.wait_for``
    so a hung OPA endpoint cannot block the Reviewer indefinitely.
    The timeout is ``config.timeout_ms / 1000`` seconds.

    R2 fail-closed: missing ``result`` / ``decision`` / ``policy_version``
    in the OPA response raise :class:`PolicyEvaluationError` rather
    than silently defaulting to ``allowed``.
    """

    def __init__(self, config: OPAReviewAdapterConfig) -> None:
        self._config = config
        self._transport: Any = None

    def with_transport(self, transport: Any) -> "OPAReviewAdapter":
        """Inject a custom HTTP transport (test hook)."""
        self._transport = transport
        return self

    async def evaluate(
        self,
        request: PolicyEvaluationRequest,
    ) -> PolicyEvaluationResult:
        if self._transport is None:
            raise PolicyEvaluationError(
                "OPAReviewAdapter.evaluate requires a transport; "
                "use with_transport() in tests or wire httpx.AsyncClient "
                "in Phase 5B"
            )
        payload = {
            "input": {
                "tenant_id": request.tenant_id,
                "run_id": request.run_id,
                "proposal_id": request.proposal_id,
                "action_type": request.action_type,
                "target_entity": request.target_entity,
                "target_id": request.target_id,
                "payload": request.payload,
                "risk_level": request.risk_level,
                "agent_authority": request.agent_authority,
                "policy_version": request.policy_context.policy_version,
            }
        }

        # R2 S11: enforce timeout via asyncio.wait_for.
        try:
            response = await asyncio.wait_for(
                self._transport.post(
                    f"{self._config.endpoint}{self._config.policy_path}",
                    json=payload,
                    timeout=self._config.timeout_ms / 1000.0,
                    headers=(
                        {"Authorization": f"Bearer {self._config.api_key}"}
                        if self._config.api_key
                        else {}
                    ),
                ),
                timeout=self._config.timeout_ms / 1000.0,
            )
        except asyncio.TimeoutError as e:
            raise PolicyEvaluationError(
                f"OPA request timed out after {self._config.timeout_ms}ms "
                f"for proposal {request.proposal_id!r}"
            ) from e
        except Exception as e:
            raise PolicyEvaluationError(
                f"OPA transport error for proposal {request.proposal_id!r}: {e}"
            ) from e

        if response.status_code != 200:
            raise PolicyEvaluationError(
                f"OPA returned status {response.status_code} for proposal "
                f"{request.proposal_id!r}"
            )

        try:
            body = response.json()
        except Exception as e:
            raise PolicyEvaluationError(
                f"OPA returned non-JSON body for proposal {request.proposal_id!r}: {e}"
            ) from e

        # R2 fail-closed: require "result" key.
        if "result" not in body or not isinstance(body["result"], dict):
            raise PolicyEvaluationError(
                f"OPA response missing 'result' object for proposal "
                f"{request.proposal_id!r}"
            )
        result_raw = body["result"]

        # R2 fail-closed: require "decision" key.
        if "decision" not in result_raw:
            raise PolicyEvaluationError(
                f"OPA response missing 'decision' for proposal {request.proposal_id!r}"
            )
        decision_raw = str(result_raw["decision"])
        try:
            decision = PolicyDecision(decision_raw)
        except ValueError:
            raise PolicyEvaluationError(
                f"OPA returned unknown decision {decision_raw!r} for "
                f"proposal {request.proposal_id!r}"
            )

        # R2 fail-closed: require "policy_version" and validate match.
        opa_policy_version = result_raw.get("policy_version")
        if not opa_policy_version or not str(opa_policy_version).strip():
            raise PolicyEvaluationError(
                f"OPA response missing 'policy_version' for proposal "
                f"{request.proposal_id!r}"
            )
        if str(opa_policy_version) != request.policy_context.policy_version:
            raise PolicyEvaluationError(
                f"OPA policy_version {opa_policy_version!r} != request "
                f"{request.policy_context.policy_version!r} for proposal "
                f"{request.proposal_id!r}"
            )

        matched_rules_raw = result_raw.get("matched_rules", [])
        if not isinstance(matched_rules_raw, list):
            raise PolicyEvaluationError(
                f"OPA 'matched_rules' is not a list for proposal "
                f"{request.proposal_id!r}"
            )
        matched_rules: list[PolicyMatchedRule] = []
        for r in matched_rules_raw:
            if not isinstance(r, dict):
                continue
            rule_id = str(r.get("rule_id", ""))
            if not rule_id.strip():
                continue
            try:
                effect = PolicyDecision(str(r.get("effect", "allowed")))
            except ValueError:
                continue
            matched_rules.append(
                PolicyMatchedRule(
                    rule_id=rule_id,
                    rule_version=str(r.get("rule_version", "")),
                    effect=effect,
                    matched_fields=tuple(r.get("matched_fields", [])),
                )
            )

        findings: list[ReviewFinding] = []
        if decision == PolicyDecision.DENIED:
            findings.append(
                ReviewFinding(
                    finding_code=CODE_POLICY_DENIED,
                    severity=ReviewFindingSeverity.ERROR,
                    message=f"OPA denied action {request.action_type!r}",
                    proposal_id=request.proposal_id,
                    policy_source=f"opa@{request.policy_context.policy_version}",
                    details={"opa_endpoint": self._config.endpoint},
                )
            )
        elif decision == PolicyDecision.NEEDS_APPROVAL:
            findings.append(
                ReviewFinding(
                    finding_code=CODE_POLICY_NEEDS_APPROVAL,
                    severity=ReviewFindingSeverity.WARNING,
                    message=f"OPA requires approval for {request.action_type!r}",
                    proposal_id=request.proposal_id,
                    policy_source=f"opa@{request.policy_context.policy_version}",
                    details={"opa_endpoint": self._config.endpoint},
                )
            )
        elif decision == PolicyDecision.NEEDS_INPUT:
            findings.append(
                ReviewFinding(
                    finding_code=CODE_POLICY_NEEDS_INPUT,
                    severity=ReviewFindingSeverity.WARNING,
                    message=f"OPA requires more input for {request.action_type!r}",
                    proposal_id=request.proposal_id,
                    policy_source=f"opa@{request.policy_context.policy_version}",
                    details={"opa_endpoint": self._config.endpoint},
                )
            )

        return PolicyEvaluationResult(
            proposal_id=request.proposal_id,
            decision=decision,
            matched_rules=tuple(sorted(matched_rules, key=lambda r: r.rule_id)),
            policy_version=request.policy_context.policy_version,
            findings=tuple(findings),
        )


# ---------------------------------------------------------------------------
# FakePolicyEvaluator (test double)
# ---------------------------------------------------------------------------


@dataclass
class FakePolicyEvaluator:
    """Test double for :class:`PolicyEvaluator`.

    Returns a preset :class:`PolicyEvaluationResult` per proposal_id,
    or a default ``ALLOWED`` result if no preset is registered.
    """

    presets: dict[str, PolicyEvaluationResult] = field(default_factory=dict)
    default: PolicyEvaluationResult | None = None
    calls: list[PolicyEvaluationRequest] = field(default_factory=list)

    def set(
        self,
        proposal_id: str,
        result: PolicyEvaluationResult,
    ) -> "FakePolicyEvaluator":
        self.presets[proposal_id] = result
        return self

    async def evaluate(
        self,
        request: PolicyEvaluationRequest,
    ) -> PolicyEvaluationResult:
        self.calls.append(request)
        if request.proposal_id in self.presets:
            return self.presets[request.proposal_id]
        if self.default is not None:
            return self.default
        return PolicyEvaluationResult(
            proposal_id=request.proposal_id,
            decision=PolicyDecision.ALLOWED,
            matched_rules=(),
            policy_version=request.policy_context.policy_version,
            findings=(),
        )


__all__ = [
    "DeterministicPolicyEvaluator",
    "FakePolicyEvaluator",
    "OPAReviewAdapter",
    "OPAReviewAdapterConfig",
    "PolicyDecision",
    "PolicyEvaluationRequest",
    "PolicyEvaluationResult",
    "PolicyEvaluator",
    "PolicyMatchedRule",
    "PolicyRule",
]

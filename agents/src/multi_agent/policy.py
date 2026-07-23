"""Phase 5A Policy Evaluator Boundary.

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

A :class:`PolicyEvaluationRequest` / :class:`PolicyEvaluationResult`
pair isolates the Policy contract from the Reviewer contract so the
two can evolve independently.

Phase 5A Section 8 reminder: Policy NEVER directly executes an Action.
Policy only returns ``allowed`` / ``denied`` / ``needs_approval`` /
``needs_input``.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
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
    ReviewFinding,
    ReviewFindingSeverity,
)
from multi_agent.review_errors import PolicyEvaluationError


# ---------------------------------------------------------------------------
# Policy decision enum
# ---------------------------------------------------------------------------


class PolicyDecision(StrEnum):
    """Possible Policy decisions for a single Proposal.

    The Reviewer maps these to :class:`ReviewDecisionStatus`:

    * ``ALLOWED`` → contributes to ``approved`` (subject to other checks)
    * ``DENIED`` → ``rejected``
    * ``NEEDS_APPROVAL`` → ``needs_approval``
    * ``NEEDS_INPUT`` → ``needs_input``
    """

    ALLOWED = "allowed"
    DENIED = "denied"
    NEEDS_APPROVAL = "needs_approval"
    NEEDS_INPUT = "needs_input"


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


class PolicyMatchedRule(StrictContract):
    """A single rule that matched during policy evaluation.

    Frozen so audit consumers can hold references safely.
    """

    model_config = {"extra": "forbid", "frozen": True}

    rule_id: str
    rule_version: str = ""
    effect: PolicyDecision
    matched_fields: list[str] = Field(default_factory=list)

    @field_validator("rule_id")
    @classmethod
    def _rule_id_non_blank(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("rule_id must not be blank")
        return v


class PolicyEvaluationResult(StrictContract):
    """Frozen output of :meth:`PolicyEvaluator.evaluate`.

    ``matched_rules`` is sorted by ``rule_id`` so the result hash is
    stable.  ``findings`` carries human-readable context for each
    non-allowed decision.
    """

    model_config = {"extra": "forbid", "frozen": True}

    proposal_id: str
    decision: PolicyDecision
    matched_rules: list[PolicyMatchedRule] = Field(default_factory=list)
    policy_version: str
    findings: list[ReviewFinding] = Field(default_factory=list)

    @field_validator("proposal_id", "policy_version")
    @classmethod
    def _non_blank(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("PolicyEvaluationResult identity fields must not be blank")
        return v


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


# Action categories that are reviewable in Phase 5A.  Anything outside
# this set is rejected with ``CODE_ACTION_CATEGORY_NOT_REVIEWABLE``.
# This is deliberately conservative — Phase 5B may expand the set.
_REVIEWABLE_ACTION_CATEGORIES: frozenset[str] = frozenset(
    {
        # Read-only / report proposals
        "report.generate",
        "summary.compile",
        "metric.query",
        # CRM propose (no execute)
        "crm.tag.update",
        "crm.status.update",
        "crm.note.add",
        "crm.owner.assign",
        "crm.escalate",
        # Recovery actions (high-risk, always needs_approval)
        "refund.issue",
        "contract.amend",
        "notification.bulk_send",
        "permission.change",
    }
)

# Action categories that are explicitly NOT reviewable in Phase 5A
# (they belong to Phase 5B Governed Executor).
_EXECUTE_ONLY_CATEGORIES: frozenset[str] = frozenset(
    {
        "crm.record.delete",
        "account.delete",
        "credential.rotate",
        "payment.capture",
        "kafka.event.emit",
    }
)

# Action categories → minimum authority required.
# An Agent whose Capability Snapshot authority is below this bar is
# rejected at the Policy layer too (defense-in-depth on top of the
# Authority validator in :mod:`multi_agent.evidence_review`).
_AUTHORITY_FLOOR: dict[str, AgentAuthority] = {
    "report.generate": AgentAuthority.READ,
    "summary.compile": AgentAuthority.READ,
    "metric.query": AgentAuthority.READ,
    "crm.tag.update": AgentAuthority.PROPOSE,
    "crm.status.update": AgentAuthority.PROPOSE,
    "crm.note.add": AgentAuthority.PROPOSE,
    "crm.owner.assign": AgentAuthority.PROPOSE,
    "crm.escalate": AgentAuthority.PROPOSE,
    "refund.issue": AgentAuthority.PROPOSE,
    "contract.amend": AgentAuthority.PROPOSE,
    "notification.bulk_send": AgentAuthority.PROPOSE,
    "permission.change": AgentAuthority.PROPOSE,
}

# Action categories that always require human approval regardless of
# risk_level.  These are the high-impact / irreversible operations.
_ALWAYS_NEEDS_APPROVAL: frozenset[str] = frozenset(
    {
        "refund.issue",
        "contract.amend",
        "notification.bulk_send",
        "permission.change",
        "crm.owner.assign",
        "crm.escalate",
    }
)


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

    1. ``action_type`` membership in :data:`_REVIEWABLE_ACTION_CATEGORIES`
    2. Agent authority vs :data:`_AUTHORITY_FLOOR`
    3. ``risk_level`` high → needs_approval
    4. Action in :data:`_ALWAYS_NEEDS_APPROVAL` → needs_approval
    5. ``PolicyContext.rules`` explicit deny / needs_input overrides

    The evaluator reads ONLY from the frozen :class:`PolicyContext`
    on the request — it never reads a live registry, never reads the
    wall-clock, and never reads ``PYTHONHASHSEED``.

    The same (request, policy_context) pair always produces the same
    :class:`PolicyEvaluationResult`, including the same ``matched_rules``
    list (sorted by ``rule_id``).
    """

    def __init__(self) -> None:
        # No state — every decision is a pure function of the inputs.
        pass

    async def evaluate(
        self,
        request: PolicyEvaluationRequest,
    ) -> PolicyEvaluationResult:
        # NOTE: this method is async per the Protocol, but the body is
        # pure-function.  No ``await`` is used so the decision is
        # deterministic and side-effect-free.
        return self._evaluate_pure(request)

    def _evaluate_pure(
        self, request: PolicyEvaluationRequest
    ) -> PolicyEvaluationResult:
        action = request.action_type
        ctx = request.policy_context
        matched: list[PolicyMatchedRule] = []
        findings: list[ReviewFinding] = []

        # 1. Action category gate — built-in, can DENY and short-circuit.
        if action in _EXECUTE_ONLY_CATEGORIES:
            findings.append(
                ReviewFinding(
                    finding_code=CODE_POLICY_DENIED,
                    severity=ReviewFindingSeverity.ERROR,
                    message=(
                        f"Action {action!r} is execute-only and not "
                        f"reviewable in Phase 5A"
                    ),
                    proposal_id=request.proposal_id,
                    policy_source=f"deterministic@{ctx.policy_version}",
                    details={"action_type": action, "category": "execute_only"},
                )
            )
            return PolicyEvaluationResult(
                proposal_id=request.proposal_id,
                decision=PolicyDecision.DENIED,
                matched_rules=sorted(matched, key=lambda r: r.rule_id),
                policy_version=ctx.policy_version,
                findings=findings,
            )

        if action not in _REVIEWABLE_ACTION_CATEGORIES:
            findings.append(
                ReviewFinding(
                    finding_code=CODE_POLICY_DENIED,
                    severity=ReviewFindingSeverity.ERROR,
                    message=(
                        f"Action {action!r} is not in the reviewable category allowlist"
                    ),
                    proposal_id=request.proposal_id,
                    policy_source=f"deterministic@{ctx.policy_version}",
                    details={"action_type": action, "category": "unknown"},
                )
            )
            return PolicyEvaluationResult(
                proposal_id=request.proposal_id,
                decision=PolicyDecision.DENIED,
                matched_rules=sorted(matched, key=lambda r: r.rule_id),
                policy_version=ctx.policy_version,
                findings=findings,
            )

        matched.append(
            PolicyMatchedRule(
                rule_id="category-allowlist",
                rule_version=ctx.policy_version,
                effect=PolicyDecision.ALLOWED,
                matched_fields=["action_type"],
            )
        )

        # 2. Authority floor — built-in, can DENY and short-circuit.
        floor = _AUTHORITY_FLOOR.get(action, AgentAuthority.PROPOSE)
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
                matched_rules=sorted(matched, key=lambda r: r.rule_id),
                policy_version=ctx.policy_version,
                findings=findings,
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
                matched_rules=sorted(matched, key=lambda r: r.rule_id),
                policy_version=ctx.policy_version,
                findings=findings,
            )

        matched.append(
            PolicyMatchedRule(
                rule_id="authority-floor",
                rule_version=ctx.policy_version,
                effect=PolicyDecision.ALLOWED,
                matched_fields=["agent_authority", "action_type"],
            )
        )

        # 3. Built-in needs-approval signals (always-needs-approval,
        #    high-risk).  These do NOT short-circuit anymore — they
        #    set a flag so context-rule DENIED can override them.
        builtin_needs_approval = False
        if action in _ALWAYS_NEEDS_APPROVAL:
            builtin_needs_approval = True
            matched.append(
                PolicyMatchedRule(
                    rule_id="always-needs-approval",
                    rule_version=ctx.policy_version,
                    effect=PolicyDecision.NEEDS_APPROVAL,
                    matched_fields=["action_type"],
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
                    matched_fields=["risk_level"],
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

        # 4. PolicyContext.rules — collect ALL matching rules (no
        #    first-match early return).  Sort by (-priority, rule_id)
        #    so HIGHER priority number wins, ties broken by rule_id.
        context_effects: list[tuple[int, str, PolicyDecision]] = []
        for rule in ctx.rules:
            rule_id = str(rule.get("rule_id", ""))
            if not rule_id:
                continue
            effect_raw = rule.get("effect", "allowed")
            try:
                effect = PolicyDecision(str(effect_raw))
            except ValueError:
                continue
            # Match on action_type if the rule specifies one
            rule_action = rule.get("action_type")
            if rule_action is not None and str(rule_action) != action:
                continue
            priority = int(rule.get("priority", 0))
            rule_version = str(rule.get("rule_version", "")) or ctx.policy_version
            context_effects.append((priority, rule_id, effect))
            matched.append(
                PolicyMatchedRule(
                    rule_id=rule_id,
                    rule_version=rule_version,
                    effect=effect,
                    matched_fields=["action_type"] if rule_action else [],
                )
            )
            if effect == PolicyDecision.DENIED:
                findings.append(
                    ReviewFinding(
                        finding_code=CODE_POLICY_DENIED,
                        severity=ReviewFindingSeverity.ERROR,
                        message=(f"Policy rule {rule_id!r} denied action {action!r}"),
                        proposal_id=request.proposal_id,
                        policy_source=f"deterministic@{ctx.policy_version}",
                        details={"rule_id": rule_id, "action_type": action},
                    )
                )
            elif effect == PolicyDecision.NEEDS_INPUT:
                findings.append(
                    ReviewFinding(
                        finding_code=CODE_POLICY_NEEDS_INPUT,
                        severity=ReviewFindingSeverity.WARNING,
                        message=(
                            f"Policy rule {rule_id!r} requires more input "
                            f"for action {action!r}"
                        ),
                        proposal_id=request.proposal_id,
                        policy_source=f"deterministic@{ctx.policy_version}",
                        details={"rule_id": rule_id, "action_type": action},
                    )
                )
            elif effect == PolicyDecision.NEEDS_APPROVAL:
                findings.append(
                    ReviewFinding(
                        finding_code=CODE_POLICY_NEEDS_APPROVAL,
                        severity=ReviewFindingSeverity.WARNING,
                        message=(
                            f"Policy rule {rule_id!r} requires approval "
                            f"for action {action!r}"
                        ),
                        proposal_id=request.proposal_id,
                        policy_source=f"deterministic@{ctx.policy_version}",
                        details={"rule_id": rule_id, "action_type": action},
                    )
                )

        # 5. Aggregate context-rule decision: denied > needs_input >
        #    needs_approval > allowed.
        context_effects_set = {e for _, _, e in context_effects}
        if PolicyDecision.DENIED in context_effects_set:
            final_decision = PolicyDecision.DENIED
        elif (
            builtin_needs_approval
            or PolicyDecision.NEEDS_APPROVAL in context_effects_set
        ):
            final_decision = PolicyDecision.NEEDS_APPROVAL
        elif PolicyDecision.NEEDS_INPUT in context_effects_set:
            final_decision = PolicyDecision.NEEDS_INPUT
        else:
            final_decision = PolicyDecision.ALLOWED

        return PolicyEvaluationResult(
            proposal_id=request.proposal_id,
            decision=final_decision,
            matched_rules=sorted(matched, key=lambda r: r.rule_id),
            policy_version=ctx.policy_version,
            findings=findings,
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

    Phase 5A Section 8 constraints:

    * Never default-initialized — callers must construct explicitly
      with a validated :class:`OPAReviewAdapterConfig`.
    * Never connects on import — no module-level client is created.
    * Fails fast on missing configuration (handled in
      :meth:`OPAReviewAdapterConfig.__post_init__`).
    * Phase 5A tests use :class:`FakePolicyEvaluator`, not this adapter.
    * Does NOT change existing OPA production paths under ``policies/``.

    The adapter is provided so Phase 5B can wire a real OPA endpoint
    without re-opening the Reviewer boundary.  In Phase 5A it is only
    exercised by unit tests that substitute a fake HTTP transport.
    """

    def __init__(self, config: OPAReviewAdapterConfig) -> None:
        # Config validation already happened in __post_init__.
        # We do NOT create an HTTP client here — that is deferred to
        # evaluate() so import is side-effect-free.
        self._config = config
        # ``_transport`` is injected by tests; production uses the
        # real ``httpx.AsyncClient``.  We type it as Any so the module
        # does not need ``httpx`` at import time.
        self._transport: Any = None

    def with_transport(self, transport: Any) -> "OPAReviewAdapter":
        """Inject a custom HTTP transport (test hook).

        Production code does NOT call this — ``evaluate()`` lazily
        creates an ``httpx.AsyncClient``.  Tests inject a fake
        transport so the adapter never touches the network.
        """
        self._transport = transport
        return self

    async def evaluate(
        self,
        request: PolicyEvaluationRequest,
    ) -> PolicyEvaluationResult:
        """Evaluate the request against the configured OPA endpoint.

        Phase 5A: this method is exercised ONLY by tests with an
        injected fake transport.  Production wiring is a Phase 5B
        concern.
        """
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
        try:
            response = await self._transport.post(
                f"{self._config.endpoint}{self._config.policy_path}",
                json=payload,
                timeout=self._config.timeout_ms / 1000.0,
                headers=(
                    {"Authorization": f"Bearer {self._config.api_key}"}
                    if self._config.api_key
                    else {}
                ),
            )
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

        result_raw = body.get("result", {})
        decision_raw = str(result_raw.get("decision", "allowed"))
        try:
            decision = PolicyDecision(decision_raw)
        except ValueError:
            raise PolicyEvaluationError(
                f"OPA returned unknown decision {decision_raw!r} for "
                f"proposal {request.proposal_id!r}"
            )

        matched_rules_raw = result_raw.get("matched_rules", [])
        matched_rules: list[PolicyMatchedRule] = []
        for r in matched_rules_raw:
            rule_id = str(r.get("rule_id", ""))
            if not rule_id:
                continue
            try:
                effect = PolicyDecision(str(r.get("effect", "allowed")))
            except ValueError:
                continue
            matched_rules.append(
                PolicyMatchedRule(
                    rule_id=rule_id,
                    rule_version=request.policy_context.policy_version,
                    effect=effect,
                    matched_fields=list(r.get("matched_fields", [])),
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
            matched_rules=sorted(matched_rules, key=lambda r: r.rule_id),
            policy_version=request.policy_context.policy_version,
            findings=findings,
        )


# ---------------------------------------------------------------------------
# FakePolicyEvaluator (test double)
# ---------------------------------------------------------------------------


@dataclass
class FakePolicyEvaluator:
    """Test double for :class:`PolicyEvaluator`.

    Returns a preset :class:`PolicyEvaluationResult` per proposal_id,
    or a default ``ALLOWED`` result if no preset is registered.

    Phase 5A tests inject this into :class:`ProposalReviewer` so no
    real policy engine (deterministic or OPA) is exercised.
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
            matched_rules=[],
            policy_version=request.policy_context.policy_version,
            findings=[],
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
]

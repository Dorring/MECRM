# Phase 5A: Reviewer & Governance Decision Layer

## Scope and Out of Scope

### In Scope

Phase 5A implements the **Reviewer & Governance Decision Layer** — a
deterministic, side-effect-free review pipeline that consumes Phase 4
`SupervisorRunResult` output and produces a `ReviewBatchResult`
containing per-Proposal decisions.

```
SupervisorRunResult
  → build_review_request (Phase 4 Adapter)
  → ProposalReviewer.review
  → ReviewBatchResult
```

Phase 5A is responsible for:

1. Review Contracts (frozen, deterministically hashable)
2. Deterministic Proposal Reviewer
3. Policy Evaluator Boundary (Protocol + Deterministic + OPA Adapter)
4. Evidence Validation
5. Authority Validation (Capability Snapshot based)
6. Risk / Approval Classification
7. Proposal Conflict Detection
8. Duplicate Proposal Resolution
9. Review Result Hashing
10. Phase 4 Output Adapter
11. LangGraph Thin Adapter
12. Deterministic Evaluation Fixtures
13. This document

### Out of Scope (Phase 5B and beyond)

Phase 5A **does NOT**:

- Execute any `ActionProposal`
- Write to the CRM or any database
- Publish to Kafka
- Send emails, SMS, or call external APIs
- Invoke `AutomationExecutorAgent`
- Write Human Approval state
- Make OPA network calls as the default path
- Create new database tables or Kafka topics
- Persist a `ReviewStore`
- Migrate the Router / Chat Graph main flow
- Modify application startup code
- Enable Ollama or Live LLM by default
- Refactor Phase 4 Supervisor Runtime

### Critical Invariant

```
APPROVED != EXECUTED
```

`approved` means: "Proposal has passed review." It **never** means
"Proposal has been executed." Phase 5B will introduce the Governed
Executor that consumes approved Proposals.

---

## Phase 4 → Phase 5A Data Flow

```
┌─────────────────────────────────────────────────────────┐
│ Phase 4: Supervisor Runtime                             │
│                                                         │
│  SupervisorRunResult                                    │
│    .merged_state.merged_proposals  ──┐                  │
│    .merged_state.merged_evidence    ──┤                  │
│    .task_records                    ──┤                  │
│    .trace                           ──┤                  │
│    .capability_bindings              ──┤                  │
│    .run_id / .plan_hash /           ──┤                  │
│    .registry_version                ──┤                  │
└─────────────────────────────────────────┘
                                          │
                 build_review_request()   │  (defensive deep copy)
                                          ▼
┌─────────────────────────────────────────────────────────┐
│ Phase 5A: ReviewRequest (frozen)                        │
│                                                         │
│  proposals: list[ActionProposal]                        │
│  evidence: list[Evidence]                               │
│  task_records: list[TaskRecordSummary]                  │
│  trace: list[TraceSummary]                              │
│  capability_bindings: list[ExecutionCapabilitySnapshot] │
│  proposal_envelopes: list[ReviewProposalEnvelope]       │
│  policy_context: PolicyContext                          │
│  request_hash: str  (SHA-256, cross-process stable)     │
└─────────────────────────────────────────────────────────┘
                                          │
                 ProposalReviewer.review()│
                                          ▼
┌─────────────────────────────────────────────────────────┐
│ Phase 5A: ReviewBatchResult (frozen)                    │
│                                                         │
│  proposal_reviews: list[ProposalReview]                 │
│  batch_status: ReviewBatchStatus                        │
│  approved_proposal_ids: list[str]                       │
│  rejected_proposal_ids: list[str]                       │
│  approval_required_proposal_ids: list[str]              │
│  conflicted_proposal_ids: list[str]                     │
│  deduplicated_proposal_ids: list[str]                   │
│  findings: list[ReviewFinding]                          │
│  result_hash: str  (SHA-256, cross-process stable)      │
└─────────────────────────────────────────────────────────┘
```

The adapter (`build_review_request`) does NOT re-execute the
Supervisor, re-invoke any Agent, or re-invoke the Planner. It only
reads the frozen `SupervisorRunResult` and returns a defensive deep
copy.

---

## Review Contracts

All public contracts inherit `StrictContract` (`extra="forbid"`,
`validate_assignment=True`). Frozen contracts use `frozen=True` so
audit records are immutable after construction.

| Contract | Frozen | Hash Field | Purpose |
|---|---|---|---|
| `ReviewRequest` | Yes | `request_hash` | Input to the Reviewer |
| `ReviewBatchResult` | Yes | `result_hash` | Output of the Reviewer |
| `ProposalReview` | Yes | `review_hash` | Per-Proposal decision |
| `ReviewFinding` | Yes | — | Single observation |
| `TaskRecordSummary` | Yes | — | Minimal Task identity snapshot |
| `TraceSummary` | Yes | — | Minimal Trace event snapshot |
| `ReviewProposalEnvelope` | Yes | `origin_hash` | Binds a Proposal to its Phase 4 Task/Result origin |
| `ExecutionCapabilitySnapshot` | Yes | `binding_hash` | Phase 4 frozen Agent capability used for authority validation |
| `CapabilitySnapshot` | Yes | — | Internal Reviewer wrapper for authority validation |
| `PolicyContext` | Yes | — | Frozen policy rules + version |

Key design rules:

- No `Any` type in field annotations; `details` uses
  `dict[str, JsonValue]` (the existing project pattern).
- No Handler, Callable, or non-serializable object is stored.
- Stable serialization via `stable_hash` (SHA-256 over canonicalized
  form). The same input MUST produce the same hash across processes,
  `PYTHONHASHSEED` values, and call order.
- `object.__setattr__` is used in `model_validator(mode="after")` to
  populate the hash field on frozen models — this is the documented
  Pydantic escape hatch for validators that need to seed a field.

### Enums

```python
class ReviewDecisionStatus(StrEnum):
    APPROVED = "approved"
    REJECTED = "rejected"
    NEEDS_APPROVAL = "needs_approval"
    NEEDS_INPUT = "needs_input"
    CONFLICT = "conflict"
    DEDUPLICATED = "deduplicated"

class ReviewBatchStatus(StrEnum):
    APPROVED = "approved"
    NEEDS_APPROVAL = "needs_approval"
    NEEDS_INPUT = "needs_input"
    REJECTED = "rejected"
    CONFLICT = "conflict"
    DEDUPLICATED = "deduplicated"

class ReviewRiskLevel(StrEnum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    CRITICAL = "critical"

class ReviewFindingSeverity(StrEnum):
    INFO = "info"
    WARNING = "warning"
    ERROR = "error"
    CRITICAL = "critical"
```

---

## Evidence Validation

The Reviewer validates each Proposal's Evidence references via
`validate_evidence_for_proposal`:

1. **Duplicate references** — the same `evidence_id` appearing twice
   in the same Proposal's `evidence_ids` list is flagged.
2. **Existence** — referenced `evidence_id` must exist in the index.
3. **Tenant consistency** — Evidence `tenant_id` must match the
   ReviewRequest `tenant_id`.
4. **Source-agent consistency** — Evidence `source_agent` must match
   the Proposal's `created_by_agent`.
5. **Content hash validity** — Evidence `content_hash` must be a
   non-empty hex string.
6. **Type compatibility** — Evidence `evidence_type` must be in the
   action-type-specific allowlist (e.g. `crm.owner.assign` requires
   `EvidenceType.CUSTOMER`).
7. **Dangling evidence** — Evidence present in the index but
   referenced by no Proposal is flagged as `INFO` (informational, not
   a rejection).

The `build_evidence_index` function handles duplicate `evidence_id`s:
same content → deduplicate (keep one); different content → exclude all
(fail closed).

---

## Authority Validation

Authority validation uses the **Capability Snapshot** taken at Phase 4
pre-flight time — NOT a live registry. This prevents a registry change
between Phase 4 and Phase 5A from retroactively granting or revoking
authority.

Rules:

- `READ`-only agents cannot propose Write/Execute actions.
- `PROPOSE` agents cannot propose execute-level actions.
- The action's required Tool must be in the agent's `allowed_tools`.
- The Proposal's authority must not exceed the Capability Snapshot.

Action → minimum authority mapping:

| Action Type | Min Authority |
|---|---|
| `report.generate` | READ |
| `summary.compile` | READ |
| `metric.query` | READ |
| `crm.tag.update` | PROPOSE |
| `crm.status.update` | PROPOSE |
| `crm.note.add` | PROPOSE |
| `crm.owner.assign` | PROPOSE |
| `crm.escalate` | PROPOSE |
| `refund.issue` | PROPOSE |
| `contract.amend` | PROPOSE |
| `notification.bulk_send` | PROPOSE |
| `permission.change` | PROPOSE |

---

## Policy Evaluator

### Protocol

```python
class PolicyEvaluator(Protocol):
    async def evaluate(
        self,
        request: PolicyEvaluationRequest,
    ) -> PolicyEvaluationResult:
        ...
```

### DeterministicPolicyEvaluator (default)

- No network, no API key, no I/O.
- Results are reproducible — suitable for CI.
- 6-step evaluation:
  1. Category gate — reject execute-only actions.
  2. Authority floor — check agent authority.
  3. Always-needs-approval — `refund.issue`, `contract.amend`,
     `permission.change`, etc.
  4. Risk-level gate — HIGH/CRITICAL → `needs_approval`.
  5. Context rules — apply explicit `PolicyContext.rules`.
  6. Default — `allowed`.

### OPAReviewAdapter (boundary only)

- NOT initialized by default.
- Does NOT connect to external services at import time.
- Fails fast when configuration is missing.
- Tests use `FakePolicyEvaluator`; production OPA path is unchanged.
- Phase 5A tests do NOT make OPA network calls.

### Policy Result

```python
class PolicyDecision(StrEnum):
    ALLOWED = "allowed"
    DENIED = "denied"
    NEEDS_APPROVAL = "needs_approval"
    NEEDS_INPUT = "needs_input"
```

Policy never executes Actions — it only returns decisions.

---

## Risk Classification

Risk is recomputed by the Reviewer from `action_type` — independent
of the Proposal's self-declared `risk_level`. A misbehaving Agent
cannot lower the approval bar by declaring `risk_level=low`.

| Action Type | Reviewer Risk |
|---|---|
| `report.generate` | LOW |
| `summary.compile` | LOW |
| `metric.query` | LOW |
| `crm.tag.update` | MEDIUM |
| `crm.status.update` | MEDIUM |
| `crm.note.add` | MEDIUM |
| `crm.owner.assign` | HIGH |
| `crm.escalate` | HIGH |
| `notification.bulk_send` | HIGH |
| `refund.issue` | CRITICAL |
| `contract.amend` | CRITICAL |
| `permission.change` | CRITICAL |

### Approval Rules

| Condition | Decision |
|---|---|
| LOW + policy allow | `approved` |
| MEDIUM + authority sufficient + policy allow | `approved` |
| HIGH | `needs_approval` |
| CRITICAL | `needs_approval` |
| Evidence insufficient | `needs_input` |
| Policy deny | `rejected` |
| Authority violation | `rejected` |

Risk classification uses a table-driven approach, not a large
`if/else` chain.

---

## Duplicate Resolution

### Canonical Proposal Identity

```
canonical_key = SHA-256(
    tenant_id,
    target_entity,
    target_id,
    action_type,
    canonical(payload)  # excludes proposal_id and idempotency_key
)
```

### Duplicate Detection

Two Proposals are **exact duplicates** if they share:
- The same `canonical_key`
- The same `idempotency_key`

Exact duplicates are deduplicated: the lexicographically smallest
`proposal_id` is the **primary** (it retains its normal review
decision); the rest are marked `DEDUPLICATED` (not `CONFLICT`) with a
`CODE_DUPLICATE_DEDUPED` finding. No Proposal is silently deleted.

A **conflict** (same idempotency_key with *different* canonical keys,
or same resource targeted with mutually exclusive values) is still
surfaced as `CONFLICT`.

### Audit Trail

Each deduplication records:
- `primary_proposal_id`
- `duplicate_proposal_ids`
- A `ReviewFinding` with `CODE_DUPLICATE_DETECTED` and per-duplicate
  `CODE_DUPLICATE_DEDUPED` findings

---

## Conflict Detection

Conflicts are identified across Proposals targeting the same resource:

| Conflict Type | Example |
|---|---|
| Field value | Same field written with different values |
| Activate/deactivate | Same resource simultaneously activated and deactivated |
| Create/delete | Same resource simultaneously created and deleted |
| Mutex notification | Mutually exclusive notifications on the same customer |
| Owner reassign | Same customer assigned to different owners |
| Idempotency mismatch | Same idempotency_key with different canonical keys |

When a conflict is detected:
- All involved Proposals → `CONFLICT`
- Batch status → `CONFLICT`
- No automatic resolution — the caller must decide.

All conflict groups and Proposal reviews are sorted by stable keys —
input order and async completion order do not affect the output.

---

## Decision Priority

Batch status priority (highest first):

```
conflict > rejected > needs_input > needs_approval > deduplicated > approved
```

Per-proposal decision priority (highest first):

```
conflict > deduplicated > rejected (rejection-class codes) >
needs_input (missing-evidence / policy-needs-input) >
needs_approval (high-risk / policy-needs-approval) > approved
```

Notes:
- A batch marked `rejected` does NOT mean every Proposal was rejected.
- Each Proposal retains its own independent decision.
- Batch status is the highest-priority summary across all Proposals.
- Approved Proposals are still NOT executed.
- `DENIED` policy decisions are no longer short-circuited by the
  built-in `always-needs-approval` rules — `REJECTED` (denied) takes
  priority over `NEEDS_APPROVAL` (R1 aggregate policy fix).

---

## Deterministic Hash

### What is hashed

- Proposal content (all fields)
- Evidence content (all fields)
- Tenant / Run / Plan identity
- Policy Context (rules + version)
- Reviewer version
- Conflict resolution inputs

### What is NOT hashed

- Object memory address
- Current time
- Random state
- Current Git HEAD
- Unstable collection order (lists are sorted before hashing)

### Stability guarantee

The same input produces the same hash across:
- Same process, different calls
- Different processes
- Different `PYTHONHASHSEED` values
- Different insertion orders

Implementation: `stable_hash(model, exclude={hash_field})` computes
SHA-256 over the canonicalized form (sorted keys, deterministic
serialization). The `model_validator(mode="after")` populates the
hash field using `object.__setattr__` (the only way to mutate a frozen
model).

---

## LangGraph Adapter

A 4-node LangGraph wraps `ProposalReviewer`:

```
validate_request → review_proposals → resolve_conflicts → finalize_review
```

- `validate_request` — runs `ReviewRequest.verify_integrity()`.
- `review_proposals` — delegates to `ProposalReviewer.review()`.
- `resolve_conflicts` — no-op pass-through (conflict resolution is
  already inside the Reviewer; this node exists for trace clarity and
  future Phase 5B extension).
- `finalize_review` — runs `ReviewBatchResult.verify_integrity()` and
  `ReviewBatchResult.verify_semantics()` (R1).

The graph does **NOT** re-implement Policy, Conflict, or Hash
algorithms. Graph output is byte-for-byte identical to direct
`ProposalReviewer.review()` output (verified by
`test_review_graph.py::TestReviewGraphParity`).

The graph is **not** registered in any application startup. Phase 5B
will wire it into the orchestrator.

---

## Side-effect Prohibition

Phase 5A guarantees **zero side effects**:

| Interface | Phase 5A Behavior |
|---|---|
| Database write | Never called |
| Kafka publish | Never called |
| CRM update | Never called |
| Tool execute | Never called (only allowlist validation) |
| AutomationExecutor | Never called |
| Email / SMS | Never called |
| External HTTP | Never called (with default evaluator) |
| OPA network call | Never called (with default evaluator) |

Verified by `test_review_integration.py::TestSideEffectGuard` —
each test patches the corresponding interface with an
`AssertionError` side effect.

---

## Evaluation Metrics

The `compute_review_metrics` function runs the Reviewer over 12
deterministic fixtures and computes:

| Metric | Target |
|---|---|
| `invalid_proposal_block_rate` | ≥ 0.99 |
| `evidence_error_detection_rate` | ≥ 0.99 |
| `authority_violation_detection_rate` | ≥ 0.99 |
| `conflict_detection_rate` | ≥ 0.99 |
| `deterministic_replay_rate` | 1.0 |
| `false_approval_rate` | 0.0 |
| `false_rejection_rate` | 0.0 |
| `review_latency_ms` | < 5000 ms |

The metrics computation does NOT hardcode judgments based on fixture
names or Proposal IDs — it reads
`ReviewFixture.expected_blocked_proposal_ids` and
`ReviewFixture.expected_conflicted_proposal_ids` to determine expected
outcomes.

### Fixtures (12 cases)

1. Valid low-risk Proposal
2. Missing Evidence
3. Dangling Evidence (informational)
4. Foreign-tenant Evidence
5. Agent authority violation
6. Unknown Action
7. Short idempotency_key (high-risk)
8. High-risk needs approval
9. Explicit policy deny
10. Exact duplicate Proposal
11. Same-resource different-value conflict
12. Multiple independent valid Proposals

---

## Known Limitations

1. **No PII DLP service** — Phase 5A uses a conservative heuristic
   (flag email fields in bulk notifications). A real DLP service is a
   Phase 5B concern.

2. **No human-in-the-loop** — `NEEDS_APPROVAL` and `NEEDS_INPUT`
   decisions are recorded but no approval workflow is triggered.
   Phase 5B will introduce the approval queue.

3. **No persistence** — `ReviewBatchResult` is returned to the caller
   but not stored. Phase 5B will introduce `ReviewStore`.

4. **OPA adapter is boundary-only** — `OPAReviewAdapter` exists as a
   Protocol implementation but is never initialized by default. It
   requires explicit configuration and a transport injection.

5. **Conflict resolution is detection-only** — conflicts are flagged
   but not automatically resolved. The caller must decide how to
   handle conflicted Proposals.

---

## Phase 5B Boundary

Phase 5B will introduce:

- **Governed Executor** — consumes approved Proposals and executes
  them with side-effect isolation.
- **ReviewStore** — persists `ReviewBatchResult` for audit.
- **Approval Queue** — handles `NEEDS_APPROVAL` and `NEEDS_INPUT`
  decisions with human-in-the-loop.
- **OPA integration** — production OPA path with caching.
- **Router integration** — wires the Review graph into the main
  orchestrator.

Phase 5A's `ReviewBatchResult` is the contract boundary: Phase 5B
consumes it without modification.

---

## Module Map

```
agents/src/multi_agent/
├── review_errors.py          # Error types
├── review_contracts.py       # Frozen contracts + hash logic
├── policy.py                 # PolicyEvaluator Protocol + Deterministic + OPA adapter
├── evidence_review.py        # Evidence index + per-proposal validation
├── conflict_resolution.py    # Canonical key + duplicate/conflict detection
├── reviewer.py               # ProposalReviewer (main entry point)
├── review_evaluation.py      # Phase 4 adapter + fixtures + metrics
└── review_graph.py           # LangGraph thin adapter

agents/tests/unit/multi_agent/
├── test_review_contracts.py       # Contract, hash, round-trip tests
├── test_policy_evaluator.py       # Deterministic + OPA + Fake evaluator
├── test_evidence_review.py        # Evidence index + validation
├── test_conflict_resolution.py    # Canonical key + duplicates + conflicts
├── test_proposal_reviewer.py      # Authority + risk + policy + conflict + batch
├── test_review_evaluation.py      # Phase 4 adapter + fixtures + metrics
├── test_review_graph.py           # LangGraph adapter + parity + routing
├── test_review_integration.py     # End-to-end Customer Recovery + side-effect guard
└── test_r1_trust_chain.py         # R1 regression: 8 P0 trust-chain defects
```

---

## Verification Commands

```bash
ruff check .
ruff format --check .

python -m compileall src/multi_agent

mypy src/multi_agent --ignore-missing-imports

pytest tests/unit/multi_agent/ -vv -p no:cacheprovider
pytest tests/unit/test_ai_mode.py -vv -p no:cacheprovider

pytest tests/unit/multi_agent/test_review_contracts.py -vv -p no:cacheprovider
pytest tests/unit/multi_agent/test_proposal_reviewer.py -vv -p no:cacheprovider
pytest tests/unit/multi_agent/test_conflict_resolution.py -vv -p no:cacheprovider
pytest tests/unit/multi_agent/test_review_graph.py -vv -p no:cacheprovider
pytest tests/unit/multi_agent/test_r1_trust_chain.py -vv -p no:cacheprovider

pytest tests/unit/ --collect-only -q
```

---

## R1 Revision

The R1 revision closes 8 P0 trust-chain defects discovered during the
Phase 5A audit. All changes are backwards-incompatible at the
contract level but preserve the Phase 5A boundary: the Reviewer still
never executes an `ActionProposal`, never writes to a database, and
never calls an external service.

### P0 Defects Closed

1. **Identity uniqueness (fail-closed)** — `ReviewRequest` now rejects
   duplicate `proposal_id` (with different *or* identical content),
   duplicate `evidence_id` with different content, duplicate
   `task_id`, duplicate `agent_id` in `capability_bindings`, duplicate
   trace `sequence`, and duplicate `origin_hash`. The previous behaviour
   silently deduped or ignored duplicates; R1 raises
   `InvalidReviewRequestError`.

2. **Envelope ↔ Proposal bijection** — Every `ActionProposal` on a
   `ReviewRequest` must have exactly one matching
   `ReviewProposalEnvelope` (matched by `proposal_id`), and every
   envelope must reference a Proposal on the request. Orphan envelopes
   or missing envelopes now raise `InvalidReviewRequestError`. The new
   `ReviewProposalEnvelope` binds a Proposal to its exact Phase 4
   `run_id` / `result_id` / `task_id` / `agent_id` / `agent_version`
   and verifies a stable `origin_hash` at construction.

3. **Canonical, order-invariant hashing** —
   `canonical_review_request_payload` sorts every list field by a
   stable key before hashing, so reordering `proposals`, `evidence`,
   `task_records`, `trace`, `capability_bindings`,
   `proposal_envelopes`, or `policy_context.rules` does not change
   `request_hash`. `result_hash` is similarly order-invariant.

4. **Tamper detection** — `ReviewRequest.verify_integrity()`,
   `ProposalReview.verify_integrity()`, and
   `ReviewBatchResult.verify_integrity()` all compare the stored hash
   with a recomputed hash using `hmac.compare_digest`. A mutated
   field, swapped list element, or tampered hash is detectable post
   round-trip.

5. **Aggregate policy priority** — The previous
   `always-needs-approval` rules short-circuited before
   `PolicyDecision.DENIED` could be evaluated, so a `refund.issue`
   Proposal could end up `needs_approval` even when policy said
   `DENIED`. R1 enforces
   `denied > needs_input > needs_approval > allowed` so a deny always
   wins. `_compute_decision` now uses `REJECTION_FINDING_CODES` to
   classify rejection-class findings before considering risk level.

6. **`DEDUPLICATED` status** — Exact duplicates (same `canonical_key`
   *and* same `idempotency_key`) are no longer collapsed into
   `CONFLICT`. The duplicate copies are marked
   `ReviewDecisionStatus.DEDUPLICATED` with a `CODE_DUPLICATE_DEDUPED`
   finding; only the primary copy goes through normal review. Real
   conflicts (same idempotency_key, different canonical key; same
   resource, different value) remain `CONFLICT`.

7. **Semantic validation** — `ProposalReview.verify_semantics()` and
   `ReviewBatchResult.verify_semantics()` enforce decision ↔ findings
   ↔ flags consistency. A `REJECTED` review must carry a
   `REJECTION_FINDING_CODES` finding; a `NEEDS_INPUT` review must
   carry `CODE_EVIDENCE_MISSING` or `CODE_POLICY_NEEDS_INPUT`; a
   `DEDUPLICATED` review must carry `CODE_DUPLICATE_DEDUPED`; an
   `APPROVED` review must have no `ERROR`/`CRITICAL` findings. The
   LangGraph `finalize_review` node now calls `verify_semantics()` in
   addition to `verify_integrity()`.

8. **Public API round-trip** —
   `serialize_contract` → `deserialize_contract` → `verify_integrity`
   + `verify_semantics` is verified by
   `test_r1_trust_chain.py::TestPublicApiRoundTrip`. The
   `usage_trust`-style self-referential hash exclusion problem is
   avoided by excluding `request_hash` / `result_hash` / `review_hash`
   from the canonical hash payload.

### Contract Field Renames

| Old (Phase 5A initial) | New (R1) | Module |
|---|---|---|
| `ReviewRequest.capability_snapshots: list[CapabilitySnapshot]` | `ReviewRequest.capability_bindings: list[ExecutionCapabilitySnapshot]` | `multi_agent.review_contracts` |
| — | `ReviewRequest.proposal_envelopes: list[ReviewProposalEnvelope]` | `multi_agent.review_contracts` |
| — | `ReviewBatchResult.deduplicated_proposal_ids: list[str]` | `multi_agent.review_contracts` |
| `ReviewDecisionStatus` (5 values) | `ReviewDecisionStatus` (6 values — added `DEDUPLICATED`) | `multi_agent.review_contracts` |
| `ReviewBatchStatus` (5 values) | `ReviewBatchStatus` (6 values — added `DEDUPLICATED`) | `multi_agent.review_contracts` |
| `build_review_request(supervisor_result, review_id, policy_context, capability_snapshots=…)` | `build_review_request(supervisor_result, *, review_id, policy_context=…)` — `capability_bindings` are read from the `SupervisorRunResult` | `multi_agent.review_evaluation` |

`CapabilitySnapshot` (the lightweight `(agent_id, capability)` wrapper)
is retained as an internal Reviewer type but is no longer a
`ReviewRequest` field. Phase 4's `ExecutionCapabilitySnapshot`
(`task_id`, `agent_id`, `agent_version`, `capability`, `binding_hash`)
is the authoritative authority source.

### New Public Exports

Phase 5A symbols are now re-exported from `multi_agent.__init__`:

- Contracts and enums from `multi_agent.review_contracts`
- Errors from `multi_agent.review_errors`
- Policy types from `multi_agent.policy`
- Evidence and conflict helpers from `multi_agent.evidence_review`
  and `multi_agent.conflict_resolution`
- `ProposalReviewer` and per-aspect validators from `multi_agent.reviewer`
- Phase 4 adapter, fixtures, and metrics from `multi_agent.review_evaluation`
- LangGraph adapter from `multi_agent.review_graph`

### Regression Coverage

`agents/tests/unit/multi_agent/test_r1_trust_chain.py` adds 24 tests
(23 counter-examples + 1 public-API round-trip) covering all 8 P0
defects. Async tests use `asyncio.run()` inside the test body rather
than `@pytest.mark.asyncio` per the R1 spec.

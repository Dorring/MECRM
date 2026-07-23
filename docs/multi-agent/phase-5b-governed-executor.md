# Phase 5B: Governed Executor & Human Approval Gate

> **R1 (Revision 1)** — Fixes 9 P0 issues from the first review:
> Noop Adapter no longer claims real execution success; Approval is
> atomically validated-and-consumed; Adapter Binding is enforced at
> call time; Kill Switch blocks before reservation (not UNKNOWN);
> Receipt and Idempotency state are atomically committed; Batch Status
> preserves UNKNOWN and distinguishes NO_ACTIONS from BLOCKED;
> `batch_deadline_seconds` / `max_concurrency` / `RetryPolicy` are
> enforced at runtime; Governance Spec is verified against the live
> module hash.  Also adds deterministic `approval_id` / `command_id`
> and 7 new evaluation metrics.

## Scope

Phase 5B implements the **execution authorization pipeline** that
converts a Phase 5A `ReviewBatchResult` into safely executed actions
with full audit, idempotency, and human-approval gating.

**In scope:**
- Execution Authorization contracts
- Human Approval Gate (request, decide, consume)
- Immutable Action Adapter Registry
- Governed Executor (18-step pipeline)
- Idempotency Reservation and Replay
- Timeout / Cancellation / Unknown Outcome handling
- Trusted Execution Receipt
- Batch Execution Semantics (partial success, no rollback)
- Kill Switch Integration
- Tenant and Authority Revalidation
- Execution Audit Trail
- Deterministic Dry-run and Recording Adapters
- In-memory Approval and Execution Stores
- LangGraph Thin Adapter
- Deterministic Evaluation (no label leakage)

**Out of scope (prohibited in Phase 5B):**
- Real CRM write
- Real Kafka publish
- Real email / SMS / phone
- Real refund / contract / fund operations
- Real permission modification
- Real external HTTP API calls
- Production database schema migration
- Production Approval table
- Production Execution Store
- Router / Chat main flow migration
- Application startup auto-registration of Executor
- Default-enabled Live LLM
- Default-enabled OPA network calls
- Phase 4 Supervisor refactoring
- Phase 5A Reviewer rule rewriting

## Phase 5A → Phase 5B Data Flow

```
ReviewRequest
+ ReviewBatchResult
        ↓
Execution Authorization        (binds every hash)
        ↓
Human Approval Gate            (if required)
        ↓
Idempotency Reservation        (before any side-effect)
        ↓
Allowlisted Action Adapter     (frozen registry)
        ↓
Execution Receipt              (trusted, verified)
        ↓
Execution Batch Result         (aggregated, hash-bound)
```

## Core Semantics

```
APPROVED != EXECUTED
NEEDS_APPROVAL != APPROVED
APPROVAL_GRANTED != EXECUTED
EXECUTION_STARTED != EXECUTED_SUCCEEDED
NO_ACTIONS != APPROVED
UNKNOWN_OUTCOME != FAILED
```

## Authorization Contract

`ExecutionAuthorization` binds:
- ReviewRequest hash
- ReviewBatchResult hash
- ProposalReview hash
- Proposal Snapshot hash
- Proposal Origin hash
- Governance Spec version + hash
- Action Type + Payload hash + Tool ID
- Idempotency Key
- Tenant + Run identity

Authorization is derived deterministically from `proposal_id` +
`request_hash` — the same inputs always produce the same
`authorization_id` and `authorization_hash`, enabling idempotent
replay.

R1 P1-1/P1-2: `approval_id` and `command_id` are also deterministic
(derived from `authorization_hash` + `fingerprint` + `attempt`), so a
replay produces byte-identical IDs — no random UUIDs.

## Human Approval Gate

### Approval State Machine

```
NOT_REQUIRED  (Review APPROVED + no governance approval needed)
PENDING       (Review NEEDS_APPROVAL or governance requires it)
APPROVED      (human decision, can be consumed once)
REJECTED      (human decision, terminal)
EXPIRED       (time-based, terminal)
REVOKED       (manual revoke, terminal)
CONSUMED      (after successful execution, terminal)
```

### Rules
- Review APPROVED + Governance does not require approval → NOT_REQUIRED
- Review NEEDS_APPROVAL → must have APPROVED ApprovalDecision
- Review APPROVED + Governance forces approval → must go through Gate
- High / Critical risk → cannot bypass approval via caller parameters
- Approval requirement comes from frozen `ActionGovernanceSpec` +
  `ProposalReview` + `PolicyDecisionAudit` — NOT re-classified by Executor

### Approval Store
- `InMemoryApprovalStore`: concurrent-safe, compare-and-set
- Same Approval can only have one terminal decision
- APPROVED can only be consumed once
- REJECTED / EXPIRED / REVOKED can never be consumed
- `create()` rejects hash-conflicting ApprovalRequests (R1 P1-1)
- **Atomic `validate_and_consume()`** (R1 P0-3): ALL checks (tenant,
  run, proposal, authorization_hash, request_hash, approver role,
  expiry, status) run under the store lock BEFORE the approval is
  marked CONSUMED — no partial consume, no invalid approval consumed
- Returns defensive copies

## Adapter Registry

- `ActionAdapterRegistry`: mutable builder during pre-flight
- `ActionAdapterRegistrySnapshot`: frozen, hash-bound, produced after
  pre-flight
- Each `ActionAdapterBinding` binds: action_type → adapter_id +
  adapter_version + supports_dry_run + retry_safe + idempotency_scope
- Action Type has exactly one binding
- Registry hash enters Authorization, Command, and Receipt
- During execution, the Executor reads only from the frozen snapshot

### Default Adapters
- `DeterministicNoopAdapter`: **dry-run only** (R1 P0-1).  Accepts
  `dry_run=True` commands and returns `DRY_RUN_SUCCEEDED` with
  `executed=False`.  Rejects `dry_run=False` commands with
  `NOT_AUTHORIZED` — a Noop can NEVER claim real execution success.
- `RecordingActionAdapter`: records commands to injected sink,
  configurable for success / failure / timeout / unknown / cancellation

**No live adapter is registered by default.**

## Idempotency State Machine

```
RESERVED      (key claimed, fingerprint verified)
IN_PROGRESS   (adapter call started)
SUCCEEDED     (adapter returned SUCCEEDED, receipt cached)
FAILED        (adapter returned FAILED, key released)
UNKNOWN       (timeout / cancellation, NO auto-retry, human intervention)
```

### Rules
- Same key + same fingerprint + SUCCEEDED → return **original cached
  receipt** (R1 P0-6), no adapter re-invocation, no fabricated
  DEDUPLICATED receipt
- Same key + SUCCEEDED but no stored receipt → UNKNOWN (crash-window
  detection)
- Same key + different fingerprint → `IdempotencyConflictError` (Fail-Closed)
- Same key + IN_PROGRESS → `ExecutionAlreadyInProgressError`
- Same key + UNKNOWN → no auto-retry, requires human handling
- Idempotency reservation is established BEFORE any adapter call
- **Atomic commit** (R1 P0-6): terminal state and Receipt are committed
  together via `complete_with_receipt()` — no window where the store is
  SUCCEEDED but no trusted Receipt exists

## Governed Executor

18-step fixed-order pipeline (R1 updated):
1. verify ReviewRequest integrity
2. verify ReviewBatchResult against Request
3. verify **live** Governance Spec hash matches module constant,
   request, and result (R1 P0-9)
4. freeze Adapter Registry Snapshot
5. select executable Proposal Reviews
6. build and verify ExecutionAuthorization
7. resolve Approval Requirement; **create ApprovalRequest** if
   needed (R1 P0-2)
8. **atomically validate-and-consume** Approval Decision (R1 P0-3)
9. check Kill Switch **before** Idempotency reservation (R1 P0-5)
10. reserve Idempotency Key
11. re-check Kill Switch after reservation, before call (R1 P0-5)
12. build immutable ExecutionCommand with **deterministic command_id**
    (R1 P1-2)
13. mark execution IN_PROGRESS
14. invoke Adapter with timeout/cancellation; **verify live adapter
    matches frozen binding** (R1 P0-4)
15. validate Adapter Outcome against command AND binding (R1 P0-4)
16. build trusted ExecutionReceipt
17. **atomically** commit Idempotency Record + Receipt (R1 P0-6)
18. finalize Batch Result + verify against original inputs

## Timeout / Cancellation / Unknown Outcome

- Per-action timeout via `asyncio.wait_for`
- Batch deadline enforcement
- Kill Switch checked before each adapter call

### Failure Semantics
- **Adapter fails before call** → FAILED, release reservation
- **Adapter explicitly returns not-executed** → FAILED, `executed=False`
- **Timeout / connection loss / cancellation** → UNKNOWN, idempotency
  record → UNKNOWN, NO auto-retry, NO release for re-execution
- **Kill Switch before reservation** → BLOCKED / CANCELLED, no adapter
  call, idempotency slot NOT touched (R1 P0-5)
- **Kill Switch after reservation, before call** → CANCELLED /
  NOT_AUTHORIZED, idempotency slot stays RESERVED (reusable)
- **Kill Switch during execution** → request cancellation; if outcome
  uncertain → UNKNOWN

## Retry Semantics

Default: `max_retries = 0`

R1 P0-8: `ExecutionRetryPolicy` is enforced at runtime via
`_execute_one_with_retry()`.  Retry only when ALL conditions met:
- `Adapter.retry_safe = True` (or `retry_only_when_safe=False`)
- Outcome was `FAILED` with `executed=False` (confirmed no side-effect)
- `error_code` is in `retryable_error_codes`
- `attempt <= max_retries`
- Batch deadline not exceeded
- Kill Switch not active

**Never auto-retry:**
UNKNOWN outcome, approval invalid, authorization invalid, tenant
mismatch, idempotency conflict, receipt invalid, policy/governance
mismatch, cancellation, kill switch, SUCCEEDED, DRY_RUN_SUCCEEDED,
DEDUPLICATED, PENDING_APPROVAL, NOT_AUTHORIZED, SKIPPED.

Phase 5B uses independent `ExecutionRetryPolicy` (not Phase 4
RetryPolicy).

## Execution Receipt

`ActionExecutionReceipt` covers:
- All execution identity fields (tenant, run, proposal)
- Authorization hash + approval hash
- Adapter ID / version / registry hash
- Idempotency key + execution fingerprint
- Status + executed flag
- External reference + safe result summary
- Timing + attempt + error code

Receipt hash covers all fields. Never stores: Exception, Secret,
Credential, full PII, DB connection, HTTP client, handler.

## Batch Semantics

- Bounded concurrency via `asyncio.Semaphore(max_concurrency)` (R1 P0-8)
- Same resource / same idempotency scope → serial execution via
  per-resource `asyncio.Lock` keyed on `(tenant_id, idempotency_key)`
  (R1 P0-8)
- Batch deadline (`batch_deadline_seconds`) enforced: actions that
  would start after the deadline return UNKNOWN with
  `EXECUTION_DEADLINE_EXCEEDED` (R1 P0-8)
- Per-action timeout is `min(per_action_timeout_seconds, remaining_deadline)`
- No cross-action database transaction illusion
- Partial success/failure → `PARTIAL_SUCCESS` (no rollback)
- Compensation requires new Proposal (not auto-executed in Phase 5B)

### Batch Status Priority (R1 P0-7)
```
UNKNOWN(8) > FAILED(7) > CANCELLED(6) > PARTIAL_SUCCESS(5)
> PENDING_APPROVAL(4) > BLOCKED(3) > SUCCEEDED(2)
> DRY_RUN_COMPLETED(1) > NO_ACTIONS(0)
```

- `NO_ACTIONS`: empty ReviewRequest (no proposals at all)
- `BLOCKED`: proposals exist but none are executable (all REJECTED /
  NEEDS_INPUT / etc.)
- `DRY_RUN_COMPLETED`: all outcomes are `DRY_RUN_SUCCEEDED` (no real
  side-effects)
- `UNKNOWN`: preserved when any action is UNKNOWN (never downgraded)
- Empty receipts is valid for UNKNOWN, FAILED, CANCELLED, BLOCKED,
  PENDING_APPROVAL, NO_ACTIONS, and DRY_RUN_COMPLETED

## Kill Switch

Reuses `ExecutionCancellation` Protocol from Phase 4.
Checked at: batch start, each authorization, **before idempotency
reservation** (R1 P0-5), **after reservation before call** (R1 P0-5),
before retry.

Pre-call blocks return `BLOCKED` / `CANCELLED` (NOT `UNKNOWN`) and do
NOT touch the idempotency store — no slot has been reserved yet.

Bound to: Tenant, Action Type, Adapter, global state.
Cannot be overridden by caller request fields.

## Tenant Revalidation

Executor verifies (does NOT re-decide):
- Request.tenant_id == Result.tenant_id == Authorization.tenant_id
  == Approval.tenant_id == IdempotencyRecord.tenant_id == Receipt.tenant_id
- Proposal Origin, Capability Binding, Governance Spec, Policy Audit,
  Approval Decision all belong to the same frozen review world

## LangGraph Adapter

Thin adapter with nodes:
- verify_review → authorize → resolve_approval → reserve_idempotency
  → execute_actions → finalize_execution

Graph only calls `GovernedExecutor` public methods — does NOT
copy authorization, approval, idempotency, adapter, receipt, hash, or
decision algorithms.

`ExecutionGraphState` contains only: request, review_result, stores,
registry, kill_switch, clock, options, result, graph_error.
No raw Exception or Adapter objects.

Direct Executor and Graph produce identical: Result, Receipt, Error
Code, Batch Status, Trace.

## Side-effect Guard

Phase 5B default mode:
- No network
- No external credentials
- No production side-effects
- Deterministic
- CI-safe

Verified: un-authorized → no adapter call; un-approved → no adapter
call; kill switch active → no adapter call; duplicate idempotency key
→ adapter called exactly once.

## Deterministic Evaluation

Fixtures cover 20+ scenarios. Metrics include:
- `unauthorized_execution_block_rate`
- `approval_bypass_block_rate`
- `tenant_mismatch_block_rate`
- `idempotency_duplicate_prevention_rate`
- `unknown_outcome_fail_closed_rate`
- `receipt_tamper_detection_rate`
- `kill_switch_block_rate`
- `deterministic_replay_rate`
- `false_execution_rate`
- `execution_success_rate`
- P50 / P95 latency

R1 P0-8 adds 7 new metrics:
- `false_real_execution_rate` — rate at which dry-run results are
  correctly NOT counted as real SUCCEEDED
- `dry_run_classification_accuracy` — rate at which dry-run vs real
  execution is correctly classified
- `approval_request_creation_rate` — rate at which required approvals
  produce an ApprovalRequest in the batch result
- `approval_atomic_consumption_rate` — rate at which approvals are
  atomically validated-and-consumed
- `adapter_drift_block_rate` — rate at which adapter binding drift is
  detected and blocked
- `receipt_atomicity_rate` — rate at which receipts are atomically
  committed with the idempotency state
- `unknown_batch_preservation_rate` — rate at which UNKNOWN outcomes
  are preserved as UNKNOWN in the batch (not downgraded)

Evaluation reads `ExecutionExpectedOutcome` — never infers from
fixture name, Proposal ID, or test file name.

## Known Limitations

1. **In-memory only**: `InMemoryApprovalStore` and
   `InMemoryExecutionStore` do not survive process restart. Production
   persistence is a future phase.
2. **No production adapters**: Only `DeterministicNoopAdapter` and
   `RecordingActionAdapter` are provided. Real CRM/Kafka/email adapters
   are a future phase.
3. **No process-restart recovery**: UNKNOWN outcomes require human
   intervention; there is no automated recovery mechanism.
4. **Single-process concurrency**: `asyncio.Lock` provides
   in-process safety; cross-process safety requires a database-backed
   store (future phase).

## Next Phase: Production Adapter Boundary

Future production adapters must implement the `ActionAdapter` Protocol:
- `adapter_id`, `adapter_version`, `supported_action_types`
- `supports_dry_run`, `retry_safe`, `idempotency_scope`
- `async execute(command: ExecutionCommand) -> AdapterExecutionOutcome`

Production adapters MUST:
- Accept only `ExecutionCommand` (not ReviewRequest, SupervisorRunResult,
  Registry, or DB session)
- Return `AdapterExecutionOutcome` with explicit `executed` flag
- Never store secrets or credentials in the outcome
- Respect the timeout / cancellation contract

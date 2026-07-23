# Phase 5B: Governed Executor & Human Approval Gate

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
- `DeterministicNoopAdapter`: no side-effects, deterministic SUCCEEDED
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
- Same key + same fingerprint + SUCCEEDED → return cached receipt, no
  adapter re-invocation
- Same key + different fingerprint → `IdempotencyConflictError` (Fail-Closed)
- Same key + IN_PROGRESS → `ExecutionAlreadyInProgressError`
- Same key + UNKNOWN → no auto-retry, requires human handling
- Idempotency reservation is established BEFORE any adapter call

## Governed Executor

18-step fixed-order pipeline:
1. verify ReviewRequest integrity
2. verify ReviewBatchResult against Request
3. verify Governance Spec integrity
4. freeze Adapter Registry Snapshot
5. select executable Proposal Reviews
6. build and verify ExecutionAuthorization
7. resolve Approval Requirement
8. validate Approval Decision
9. check Kill Switch
10. reserve Idempotency Key
11. build immutable ExecutionCommand
12. mark execution IN_PROGRESS
13. invoke Adapter with timeout/cancellation
14. validate Adapter Outcome
15. build trusted ExecutionReceipt
16. atomically complete Idempotency Record
17. finalize Batch Result
18. verify Result against original inputs

## Timeout / Cancellation / Unknown Outcome

- Per-action timeout via `asyncio.wait_for`
- Batch deadline enforcement
- Kill Switch checked before each adapter call

### Failure Semantics
- **Adapter fails before call** → FAILED, release reservation
- **Adapter explicitly returns not-executed** → FAILED, `executed=False`
- **Timeout / connection loss / cancellation** → UNKNOWN, idempotency
  record → UNKNOWN, NO auto-retry, NO release for re-execution
- **Kill Switch before execution** → BLOCKED / CANCELLED, no adapter call
- **Kill Switch during execution** → request cancellation; if outcome
  uncertain → UNKNOWN

## Retry Semantics

Default: `max_retries = 0`

Retry only when ALL conditions met:
- `Adapter.retry_safe = True`
- Governance Spec allows retry
- Idempotency established
- Previous result explicitly not-executed
- Error code in allowlist
- Deadline not exceeded
- Kill Switch not triggered

**Never auto-retry:**
UNKNOWN outcome, approval invalid, authorization invalid, tenant
mismatch, idempotency conflict, receipt invalid, policy/governance
mismatch, cancellation, kill switch.

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

- Bounded concurrency (default 4)
- Same resource / same idempotency scope → serial execution
- No cross-action database transaction illusion
- Partial success/failure → `PARTIAL_SUCCESS` (no rollback)
- Compensation requires new Proposal (not auto-executed in Phase 5B)

### Batch Status Priority
```
UNKNOWN(7) > FAILED(6) > CANCELLED(5) > PARTIAL_SUCCESS(4)
> PENDING_APPROVAL(3) > BLOCKED(2) > SUCCEEDED(1) > NO_ACTIONS(0)
```

## Kill Switch

Reuses `ExecutionCancellation` Protocol from Phase 4.
Checked at: batch start, each authorization, before adapter call,
before retry.

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

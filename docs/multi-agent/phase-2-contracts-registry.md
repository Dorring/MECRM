# Phase 2: Multi-Agent Contracts & Agent Registry

## Status: Complete (R9)

**Branch:** `feat/ma-02-contracts-registry`
**Tests:** 168 passed (R9) + 76 Phase 1 regression (AI_MODE=deterministic)

R9 closes the two combination-scenario bugs from the R8 review:

1. **Proposal-ID-level Fail-Closed** — `excluded_proposal_ids` is now applied as a final filter on `merged_proposals` after all checks. If ANY copy of a proposal_id is judged invalid (integrity failure, content mismatch, foreign tenant, missing evidence), ALL copies of that id are removed from both `merged_proposals` and `results[*].action_proposals`.
2. **Conflicting Result's children excluded** — Evidence and Proposals are now collected ONLY from surviving (deduped) results, not from all input results. When a Result is excluded due to `content_mismatch`, its children never participate in the merge.

R8 closed the two foundation invariants from the R7 review:

3. **Invalid proposals fully scrubbed** — a unified `excluded_proposal_ids` set is populated by every exclusion path and used to remove bad proposals from BOTH `merged_proposals` AND every `results[*].action_proposals`.
4. **Core IDs must not be blank** — `_non_blank()`, `_validate_resource_id()`, and `_validate_agent_id_field()` validators enforce non-blank + safe character class on every identifier. `AgentResult.agent_version` and `ActionProposal.idempotency_key` no longer accept empty defaults.

R7 closed the four P0/P1 gaps from the R6 review:

5. **Sensitive-key normalization** — patterns are pre-normalized; recursive scan covers lists.
6. **Registry read APIs** — all read APIs return deep copies via `_copy_capability()`.
7. **Evidence reference integrity at boundaries** — `merge_parallel_results()` and `MultiAgentState` verify evidence references.
8. **Documentation** — this file reflects the R3–R9 implementation.

---

## 1. Contract Relationship Diagram

```
AgentCapability  ─── declares ──→  AgentAuthority (read | propose | execute)
       │                           ToolAuthority   (read | propose | execute)
       │
       │  registered in
       ▼
AgentRegistry  ─── resolves ──→  (AgentCapability copy, AgentHandler)
       │
       │  snapshots to
       ▼
RegistrySnapshot  ─── contains ──→  dict[agent_id, AgentCapability copy]
                                      + version hash

AgentTask  ─── dispatched to ──→  AgentHandler.run(task, context)
                                       │
                                       ▼
                                  AgentResult
                                       │
                          ┌────────────┼────────────┐
                          ▼            ▼            ▼
                     Evidence    ActionProposal   TokenUsage
                                     │
                                     │  hash via compute_proposal_hash()
                                     ▼
                           integrity.verify_integrity()

merge_parallel_results([AgentResult, ...], expected_tenant_id=...)  →  MergedState
                                                  │
                                     ┌────────────┼────────────┐
                                     ▼            ▼            ▼
                               results      evidence      proposals
                               conflicts    (deduped)    (deduped +
                                                          evidence-ref check)
```

---

## 2. AgentTask, AgentResult, ActionProposal — Distinctions

| Concept | Purpose | Side-effects? | Key constraint |
|---|---|---|---|
| **AgentTask** | Request: "do this work" | None (request only) | `dependencies` must not include self; `objective` must not be blank |
| **AgentResult** | Response: "here's what happened" | None (report only) | `failed` status requires `AgentError`; `completed` must not have errors |
| **ActionProposal** | Intent: "I suggest this action" | **None** — only GovernedExecutor can execute | High-risk requires evidence + approval |

AgentTask is the **request** side; AgentResult is the **response** side. ActionProposal is a **suggestion** embedded in a response — it carries no authority to execute.

---

## 3. Strict JSON Boundary

Every contract inherits `StrictContract` (`extra="forbid"`, `validate_assignment=True`).

`validate_strict_json()` rejects at the Pydantic boundary:
- `bytes` / `bytearray`
- `set` / `frozenset`
- `tuple`
- `Decimal`
- `datetime`
- `Enum`
- custom objects
- `NaN` / `Infinity`
- non-string dict keys

This runs on `ActionProposal.payload`, `AgentResult.output` / `findings`, `AgentTask.input_data`, `AgentCapability.metadata`, `Evidence.metadata`, `AgentError.details`, `AgentExecutionContext.policy_context` / `run_metadata`.

---

## 4. Metadata Secret-Key Scanning (R7)

`_reject_sensitive_keys()` recursively scans **dicts and lists** and rejects any key whose normalized form contains a sensitive pattern.

### Normalization

```python
def _normalize_sensitive_key(value: str) -> str:
    return re.sub(r"[^a-z0-9]", "", value.lower())
```

`access_token`, `access-token`, `ACCESS_TOKEN`, and `access token` all collapse to `accesstoken`.

### Pre-normalized patterns

```python
_NORMALIZED_SECRET_PATTERNS = frozenset(
    _normalize_sensitive_key(p)
    for p in {
        "authorization", "api_key", "access_token", "refresh_token",
        "client_secret", "password", "secret", "cookie",
    }
)
```

Patterns are normalized **once at module load** so the comparison is consistent: both the key under test and the pattern go through the same pipeline. A key matches if `any(pattern in normalized_key for pattern in _NORMALIZED_SECRET_PATTERNS)`.

### Recursive scan

```python
def _reject_sensitive_keys(value, path):
    if isinstance(value, dict):
        for k, child in value.items():
            if any(p in _normalize_sensitive_key(str(k)) for p in _NORMALIZED_SECRET_PATTERNS):
                raise ValueError(f"{path} contains sensitive key {k!r}")
            _reject_sensitive_keys(child, f"{path}.{k}")
    elif isinstance(value, list):
        for index, child in enumerate(value):
            _reject_sensitive_keys(child, f"{path}[{index}]")
```

This catches `{"providers": [{"access_token": "..."}]}` which the previous dict-only recursion missed.

---

## 5. AgentAuthority & ToolAuthority

```
AgentAuthority      Allowed Tool Levels
─────────────────────────────────────────
read                read only
propose             read + propose
execute             read + propose + execute

ToolAuthority       Example Tools
───────────────────────────────────────────
read                crm_reader.get_leads, vector_search.search
propose             crm_writer.propose
execute             automation_executor.execute, kafka.emit_event
```

**Defense in depth:** Authority is checked at two layers:
1. `AgentRegistry._validate_tool_authority()` at registration time
2. `AgentRegistry.validate_tool_access()` at call time

**ToolCatalog is fail-closed:** unknown tools raise `UnknownToolError`. There is no "allow unknown tools" mode.

Phase 2 default domain agents MUST NOT have `execute` authority.

---

## 6. Registry Lifecycle & Copy Policy (R7)

```
register(cap, handler)
  │  checks: agent_id unique, tool authority valid
  │  stores: _copy_capability(cap)  ← deep copy via JSON round-trip
  ▼
[active] ── replace(cap, handler) ──→ [updated]  (also stores a copy)
  │                                      │
  │ unregister()                         │ unregister()
  ▼                                      ▼
[removed]                            [removed]

resolve(agent_id)            → (_copy_capability(cap), handler)
resolve_capability(agent_id) → _copy_capability(cap)
list_all()                   → [_copy_capability(c) for c in sorted(...)]
list_by_domain(domain)       → [_copy_capability(c) for c in sorted(...)]
list_by_task(task_type)      → [_copy_capability(c) for c in sorted(...)]
snapshot()                   → RegistrySnapshot(agents={...copies...}, version=hash)

  snapshot() includes ALL agents (enabled + disabled); NO handler references
```

**All public read APIs return deep copies.** `AgentCapability` is `frozen=True`, but `metadata` is a mutable `dict`; without copies a caller could do `registry.list_all()[0].metadata["x"] = True` and corrupt registry internals. The unified `_copy_capability()` static method prevents this.

Rules:
- Agent ID must be unique; duplicate → `DuplicateAgentError`
- `replace()` is explicit; no silent overwrites
- Disabled agents excluded from `resolve()` but visible in `snapshot()`
- No LLM-driven dynamic registration (no `eval`, no `importlib`)
- No second registry — `AgentRegistry` is the single source of truth

---

## 7. Tenant & Evidence Security

- **Evidence.tenant_id** is REQUIRED; empty/blank string rejected at validation
- **ActionProposal.tenant_id** is REQUIRED; empty/blank rejected
- **AgentTask.tenant_id** is REQUIRED; empty/blank rejected
- **AgentResult.tenant_id** is REQUIRED; empty/blank rejected
- **MultiAgentState.tenant_id** is REQUIRED; empty/blank rejected
- **MultiAgentState.actor_id** and **objective** must not be blank
- **AgentTask.objective** must not be blank

### Foreign-tenant rejection

`merge_parallel_results(*, expected_tenant_id=...)` rejects:
- Results with `tenant_id != expected_tenant_id`
- Evidence with `tenant_id != expected_tenant_id`
- Proposals with `tenant_id != expected_tenant_id`

Each rejection produces a `MergeConflict(conflict_type="foreign_tenant")`.

### Tenant override attack prevention

`ActionProposal.payload` MUST NOT contain `tenant_id` or `tenantId` keys at any nesting depth, including inside list elements. `_scan_payload_for_tenant_override()` enforces this at construction and at `verify_integrity()`.

### Evidence type allowlist

Evidence types are restricted to business-relevant types. `chain_of_thought` and `llm_reasoning` are NOT in the allowlist. Allowed types: `customer`, `contact`, `ticket`, `deal`, `knowledge_article`, `metric`, `tool_result`, `audit_event`, `policy_decision`, `human_approval`, `opa_policy`, `kafka_topic`, `event_id`, `governance_decision`, `data_guard_check`.

---

## 8. Proposal Hash Specification (R3+)

`compute_proposal_hash()` (in `integrity.py`) produces a SHA-256 hex digest over canonical JSON of:

```
{
  tenant_id, created_by_agent, action_type, target_entity, target_id,
  payload, priority, risk_level, justification,
  evidence_ids (sorted),
  requires_approval
}
```

**Excluded from hash** (non-deterministic or identity fields):
- `proposal_id` — assigned at creation
- `proposal_hash` — the hash itself
- `created_at` — wall-clock timestamp
- `idempotency_key` — identity, not content

Two proposals with different `idempotency_key` but otherwise identical content produce the **same** hash. This enables content-based deduplication at merge.

**Stability guarantees:**
- Dict key order in payload does not affect hash (sorted keys via `canonicalize()`)
- Evidence IDs sorted before hashing — order-independent
- Same semantic content → same hash → deduplication at merge
- UTC normalization: `+00:00` → `Z`

---

## 9. Proposal Integrity

`ActionProposal.verify_integrity()` re-computes the hash and asserts it matches the stored `proposal_hash`. It also re-scans the payload for tenant-override keys and re-checks high-risk constraints (evidence + approval).

This is called:
- At `AgentResult` construction (via `_tenant_homogeneity`)
- At `MultiAgentState` construction (via `_tenant_homogeneity_and_integrity`)
- Inside `merge_parallel_results()` for every proposal

---

## 10. Evidence Reference Integrity (R7)

### At AgentResult construction

`AgentResult._tenant_homogeneity()` verifies that every `action_proposals[*].evidence_ids` references an evidence id present in `result.evidence`. Missing references raise `ValidationError`.

### At merge boundary

`merge_parallel_results()` computes `available_evidence_ids` from the final `merged_evidence` list **after** all evidence conflict resolution. Every surviving proposal is checked:

```python
available_evidence_ids = {ev.evidence_id for ev in merged_evidence}
for p in merged_proposals:
    missing = sorted(set(p.evidence_ids) - available_evidence_ids)
    if missing:
        conflicts.append(MergeConflict(
            conflict_type="proposal_missing_evidence",
            detail=f"Proposal {p.proposal_id!r} references missing evidence {missing!r}",
            conflicting_ids=[p.proposal_id, *missing],
        ))
        continue
    final_proposals.append(p)
```

This catches:
- Proposals whose evidence was removed after AgentResult construction (`result.evidence.clear()`)
- Proposals whose evidence was excluded due to a `content_mismatch` conflict

### At MultiAgentState boundary

`MultiAgentState._tenant_homogeneity_and_integrity()` builds `available_evidence_ids` from **both** sources:

```python
available_evidence_ids = {ev.evidence_id for ev in self.evidence}
for r in self.agent_results:
    available_evidence_ids.update(ev.evidence_id for ev in r.evidence)
```

Every `proposed_actions[*].evidence_ids` must reference an id in this set. Missing references raise `ValidationError`.

---

## 11. State Merge Rules (R3+)

`merge_parallel_results(*, expected_tenant_id=...)` enforces:

1. **Order independence**: Inputs sorted by `result_id` before processing
2. **Result dedup**: Same `result_id` + same content hash → keep one
3. **Result conflict**: Same `result_id` + different content hashes → **ALL excluded** (`content_mismatch`)
4. **Evidence dedup**: Same `evidence_id` + same content → kept once
5. **Evidence conflict**: Same `evidence_id` + different content → **ALL excluded** (`content_mismatch`)
6. **Foreign tenant rejection**: Different `tenant_id` → conflict, rejected
7. **Proposal dedup**: Same `proposal_hash` → kept once (content-based)
8. **Proposal conflict**: Same `proposal_id` + different `proposal_hash` → **ALL excluded** (`content_mismatch`)
9. **Proposal integrity**: Proposals failing `verify_integrity()` → excluded (`proposal_integrity_failure`)
10. **Evidence reference**: Proposals referencing evidence not in `merged_evidence` → excluded (`proposal_missing_evidence`)
11. **Immutability**: Input objects are never mutated
12. **Required tenant**: `expected_tenant_id` is a required keyword argument

**Key R3 change:** Same-ID conflicts exclude ALL conflicting objects, not "keep first". This is order-independent and avoids silently picking an arbitrary winner.

---

## 12. Why Phase 2 Does NOT Wire into Router

Phase 2 is strictly additive infrastructure:
- The existing `AgentRouter` (`orchestrator/router.py`) continues operating unchanged
- New `AgentRegistry` lives in `multi_agent/` — no import from router
- No Kafka topic changes
- No existing agent behavior change
- Phase 3 (Complexity Gate) will be the first consumer of `AgentRegistry`
- Phase 5 (Supervisor Graph) will wire `AgentHandler` protocol to real agents

---

## 13. Future Integration Points (Phase 3-5)

| Phase | Integration |
|---|---|
| Phase 3 (Complexity Gate) | Uses `AgentRegistry.list_by_task()` to route tasks |
| Phase 3 (Planner) | Creates `AgentTask` instances from work items |
| Phase 4 (Evaluation) | Uses `AgentResult.evidence` for quality scoring |
| Phase 5 (Supervisor) | Wires `AgentHandler` protocol to real agents |
| Phase 5 (GovernedExecutor) | Consumes `ActionProposal` → converts to real writes |
| Phase 5 (OPA) | Reads `AgentCapability.allowed_tools` for policy decisions |

**Approval Service bridge**: Phase 2 `ActionProposal.requires_approval` + `idempotency_key` map directly to `PendingAction` (`governance/approval_service.py`). Future bridge maps `ActionProposal` → `PendingAction` in the Supervisor graph.

---

## 14. Unresolved / Deferred Issues

1. **ActionProposal conflict resolution**: Phase 2 detects `content_mismatch` and `proposal_missing_evidence` but doesn't auto-resolve; Phase 5 Supervisor handles this
2. **Evidence allowlist**: Static set; may need extension mechanism in Phase 4
3. **AgentHandler protocol**: Uses `Protocol` (structural typing); Phase 5 may need runtime type checking
4. **Registry persistence**: In-memory only; Phase 3+ may need snapshot persistence to DB
5. **Existing `ActionProposal` dataclasses**: Two variants in `chat/tools/crm_writer.py` and `productivity/proposals.py`; adapters exist but source code is NOT modified. Phase 5+ can migrate callers to unified Pydantic model.

---

## 15. Files

| File | Purpose |
|---|---|
| `agents/src/multi_agent/__init__.py` | Public API |
| `agents/src/multi_agent/contracts.py` | All Pydantic contracts + adapters + sensitive-key scan |
| `agents/src/multi_agent/registry.py` | AgentRegistry + ToolCatalog + RegistrySnapshot + `_copy_capability` |
| `agents/src/multi_agent/state.py` | `merge_parallel_results` + `MergedState` + `MergeConflict` |
| `agents/src/multi_agent/errors.py` | Exception classes |
| `agents/src/multi_agent/serialization.py` | `canonicalize` + `content_hash` + `validate_strict_json` |
| `agents/src/multi_agent/integrity.py` | `compute_proposal_hash` |
| `agents/tests/unit/multi_agent/test_contracts.py` | Contract + secret-key + state evidence-ref tests |
| `agents/tests/unit/multi_agent/test_registry.py` | Registry + ToolCatalog + copy tests |
| `agents/tests/unit/multi_agent/test_serialization.py` | Canonicalizer + round-trip tests |
| `agents/tests/unit/multi_agent/test_state_merge.py` | Merge + evidence-ref integrity tests |

---

## 16. Test Commands

```bash
cd agents

# All Phase 2 tests
AI_MODE=deterministic python -m pytest tests/unit/multi_agent/ -v

# Ruff
python -m ruff check src/multi_agent/ tests/unit/multi_agent/
python -m ruff format --check src/multi_agent/ tests/unit/multi_agent/

# Compile
python -m compileall src/multi_agent/

# Phase 1 regression
AI_MODE=deterministic python -m pytest tests/unit/test_ai_mode.py -v
```

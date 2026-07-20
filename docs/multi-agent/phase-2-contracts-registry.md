# Phase 2: Multi-Agent Contracts & Agent Registry

## Status: Complete

**Branch:** `feat/ma-02-contracts-registry`
**Tests:** 134 passed, 0 failed (AI_MODE=deterministic)

---

## 1. Contract Relationship Diagram

```
AgentCapability  ─── declares ──→  AgentAuthority (read | propose | execute)
       │                           ToolAuthority   (read | propose | execute)
       │
       │  registered in
       ▼
AgentRegistry  ─── resolves ──→  (AgentCapability, AgentHandler)
       │
       │  snapshots to
       ▼
RegistrySnapshot  ─── contains ──→  dict[agent_id, AgentCapability]
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
                                     │  hash via
                                     ▼
                           _compute_proposal_hash()

merge_parallel_results([AgentResult, ...])  →  MergedState
                                                  │
                                     ┌────────────┼────────────┐
                                     ▼            ▼            ▼
                               results      evidence      proposals
                               conflicts    (deduped)    (deduped)
```

---

## 2. AgentTask, AgentResult, ActionProposal — Distinctions

| Concept | Purpose | Side-effects? | Key constraint |
|---|---|---|---|
| **AgentTask** | Request: "do this work" | None (request only) | `dependencies` must not include self |
| **AgentResult** | Response: "here's what happened" | None (report only) | `failed` status requires `AgentError` |
| **ActionProposal** | Intent: "I suggest this action" | **None** — only GovernedExecutor can execute | High-priority requires evidence |

AgentTask is the **request** side; AgentResult is the **response** side. ActionProposal is a **suggestion** embedded in a response — it carries no authority to execute.

---

## 3. AgentAuthority & ToolAuthority

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
1. Pydantic `model_validator` in `AgentCapability` (construction-time)
2. `AgentRegistry._validate_tool_authority()` (registration-time)

Phase 2 default domain agents MUST NOT have `execute` authority.

---

## 4. Registry Lifecycle

```
register(cap, handler)
  │  checks: agent_id unique, tool authority valid
  │
  ▼
[active] ── replace(cap, handler) ──→ [updated]
  │                                      │
  │ unregister()                         │ unregister()
  ▼                                      ▼
[removed]                            [removed]

resolve(agent_id)
  │  checks: registered, enabled
  │
  ▼
(capability, handler)

snapshot()
  │  includes ALL agents (enabled + disabled)
  │  NO handler references
  ▼
RegistrySnapshot(agents, version, created_at)
```

Rules:
- Agent ID must be unique; duplicate → `DuplicateAgentError`
- `replace()` is explicit; no silent overwrites
- Disabled agents excluded from `resolve()` but visible in `snapshot()`
- No LLM-driven dynamic registration (no `eval`, no `importlib`)
- No second registry — `AgentRegistry` is the single source of truth

---

## 5. Why Phase 2 Does NOT Wire into Router

Phase 2 is strictly additive infrastructure:
- The existing `AgentRouter` (`orchestrator/router.py`) continues operating unchanged
- New `AgentRegistry` lives in `multi_agent/` — no import from router
- No Kafka topic changes
- No existing agent behavior change
- Phase 3 (Complexity Gate) will be the first consumer of `AgentRegistry`
- Phase 5 (Supervisor Graph) will wire `AgentHandler` protocol to real agents

---

## 6. Tenant & Evidence Security

- **Evidence.tenant_id** is REQUIRED; empty string rejected at validation
- **merge_parallel_results()** rejects evidence from a different tenant than the first result
- Foreign tenant evidence creates a `MergeConflict(type="foreign_tenant")` and is NOT included in merged output
- **ActionProposal.payload** MUST NOT contain `tenant_id` or `tenantId` keys (prevents tenant override attacks)
- Evidence types are restricted to an allowlist (`opa_policy`, `llm_reasoning`, `tool_output`, etc.)

---

## 7. Proposal Hash Specification

`_compute_proposal_hash()` produces a SHA-256 hex digest over canonical JSON of:

```
{
  tenant_id, created_by_agent, action_type, target_entity, target_id,
  payload, priority, justification, evidence_ids (sorted),
  requires_approval, idempotency_key
}
```

**Excluded from hash** (non-deterministic or self-referential fields):
- `proposal_id` — assigned at creation
- `proposal_hash` — the hash itself
- `created_at` — wall-clock timestamp

**Stability guarantees:**
- Dict key order in payload does not affect hash (sorted keys)
- Evidence IDs sorted before hashing — order-independent
- Same semantic content → same hash → deduplication at merge

---

## 8. State Merge Rules

`merge_parallel_results()` enforces:

1. **Order independence**: Inputs sorted by `result_id` before processing
2. **Result dedup**: Same `result_id` keeps first, records conflict
3. **Evidence dedup**: Same `evidence_id` → kept once
4. **Foreign tenant rejection**: Evidence with different `tenant_id` → conflict, rejected
5. **Proposal dedup**: Same `proposal_hash` → kept once
6. **Content conflict detection**: Same `proposal_id` with different `proposal_hash` → conflict
7. **Immutability**: Input objects are never mutated (deep-copied where needed)

---

## 9. Future Integration Points (Phase 3-5)

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

## 10. Unresolved / Deferred Issues

1. **Tool authority for unknown tools**: Currently allowed through; Phase 3+ may add strict mode requiring all tools to be in the static map
2. **ActionProposal conflict resolution**: Phase 2 detects `content_mismatch` but doesn't auto-resolve; Phase 5 Supervisor handles this
3. **Evidence allowlist**: Static set; may need extension mechanism in Phase 4
4. **AgentHandler protocol**: Uses `Protocol` (structural typing); Phase 5 may need runtime type checking
5. **Registry persistence**: In-memory only; Phase 3+ may need snapshot persistence to DB
6. **Existing `ActionProposal` dataclasses**: Two variants in `chat/tools/crm_writer.py` and `productivity/proposals.py`; adapters exist but source code is NOT modified. Phase 5+ can migrate callers to unified Pydantic model.

---

## 11. Files

| File | Lines | Purpose |
|---|---|---|
| `agents/src/multi_agent/__init__.py` | 73 | Public API |
| `agents/src/multi_agent/contracts.py` | 640 | All Pydantic contracts + adapters + hash |
| `agents/src/multi_agent/registry.py` | 186 | AgentRegistry + RegistrySnapshot + AgentHandler |
| `agents/src/multi_agent/state.py` | 158 | merge_parallel_results + MergedState |
| `agents/src/multi_agent/errors.py` | 36 | 9 exception classes |
| `agents/src/multi_agent/serialization.py` | 90 | JSON helpers + stable_hash |
| `agents/tests/unit/multi_agent/__init__.py` | 0 | Package init |
| `agents/tests/unit/multi_agent/test_contracts.py` | ~960 | 76 tests |
| `agents/tests/unit/multi_agent/test_registry.py` | ~300 | 26 tests |
| `agents/tests/unit/multi_agent/test_serialization.py` | ~320 | 20 tests |
| `agents/tests/unit/multi_agent/test_state_merge.py` | ~220 | 12 tests |

**Total: 134 tests, ~2700 lines of production + test code**

---

## 12. Test Commands

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

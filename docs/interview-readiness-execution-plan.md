# Interview Readiness Execution Plan

**Purpose:** turn the existing AI-native CRM into a concise, reproducible
portfolio for AI application engineering interviews.

**Baseline:** `main@d6fd691` after H1 local-compose readiness.

**Operating constraint:** Docker Desktop is the current deployment target.
Kubernetes staging, cloud deployment, and always-on local LLM inference are
explicitly out of scope for this plan.

**Status:** active execution plan. Complete phases in order and keep each
phase in an independently reviewable pull request.

## 1. Outcome and Exit Criteria

At completion, a reviewer can do all of the following within 15 minutes:

1. Understand the business problem and the role of the AI agents from the
   repository landing page.
2. Start the complete stack locally with Docker Desktop, without an LLM being
   required by default.
3. Run one deterministic, tenant-scoped AI scenario and inspect its result.
4. See retrieval evidence, tool calls, policy decisions, approval state, and
   the final outcome without exposing chain-of-thought or secrets.
5. Run a compact AI evaluation suite with versioned data and machine-readable
   metrics.
6. See a denial path for prompt injection or cross-tenant access.

The final portfolio must include:

- A truthful README and quick-start path.
- One five-minute primary demo and one security/failure demo.
- Deterministic demo data and a reset/seed/verify command path.
- Agent-run evidence for the primary demo.
- A 30-50 example evaluation dataset and an artifact report.
- Architecture, trade-off, limitation, and interview-Q&A documents.
- Real screenshots and a short recorded walkthrough.

## 2. Current Strengths and Gaps

### Existing strengths to preserve

- LangGraph workflows and multiple domain agents.
- Kafka-backed event processing, transactional outbox, replay, and DLQ work.
- PostgreSQL RLS, OPA policy checks, audit trails, and tenant tests.
- Approval, kill-switch, explainability, and observability components.
- Docker Compose cold-start evidence, CI, image scanning, SBOM, and provenance.

### Gaps this plan closes

- The README has stale setup/version claims and does not lead with a focused
  interview demo.
- Demo instructions contain placeholders and rely too much on manual setup.
- Agent behavior is tested but not presented as a small, versioned evaluation
  product.
- The primary user-facing flow does not yet expose a compact run-level evidence
  trail.
- Ollama is optional at runtime but direct provider coupling remains in several
  modules.

## 3. System Boundaries

### Context diagram

```mermaid
flowchart LR
    Interviewer["Interviewer / local user"] --> UI["Next.js UI"]
    UI --> Gateway["Gateway: auth, tenant context, API"]
    Gateway --> CRM["PostgreSQL + RLS"]
    Gateway --> Kafka["Kafka events"]
    Kafka --> Agents["Agent orchestrator / LangGraph"]
    Agents --> Tools["Tenant-scoped CRM, search, policy tools"]
    Tools --> Weaviate["Weaviate knowledge retrieval"]
    Agents --> Policy["OPA"]
    Agents --> Approval["Approval and audit records"]
    Agents --> Model["Deterministic provider by default; Ollama opt-in"]
```

### Primary data flow

```mermaid
sequenceDiagram
    participant U as User
    participant UI as Frontend
    participant G as Gateway
    participant A as Agent workflow
    participant R as RAG tools
    participant P as OPA / approval
    participant DB as RLS database

    U->>UI: Submit support issue
    UI->>G: Tenant-scoped request
    G->>DB: Store ticket and emit event
    G->>A: Route event with tenant context
    A->>R: Retrieve tenant-scoped evidence
    A->>P: Evaluate proposed action
    alt Approval required
        P-->>A: Pending approval
        A->>DB: Persist run evidence and approval
        U->>UI: Approve or reject
        UI->>G: Decision
        G->>A: Resume or terminate action
    else Action allowed
        A->>DB: Persist result and evidence
    end
    DB-->>UI: Run result, evidence summary, audit state
```

### Non-negotiable boundaries

- The frontend never calls an LLM, database, Kafka, or OPA directly.
- The gateway owns request authentication, tenant context, and public API
  validation.
- Agents may propose actions; high-risk mutations remain gated by policy and
  approval.
- RLS remains the final tenant-isolation enforcement layer.
- Evidence stores structured facts and references, not hidden reasoning,
  private prompts, secrets, or cross-tenant data.

## 4. Canonical Interview Scenarios

### Scenario A: Support Copilot with RAG and approval

**Business question:** a support operator needs a grounded response and a
reusable knowledge-base draft for a recurring issue.

1. Seed a tenant, a customer, a ticket, and tenant-owned knowledge articles.
2. The Support Agent classifies the ticket and retrieves relevant articles.
3. The workflow returns a structured resolution suggestion with evidence IDs,
   confidence, and a clear degraded state if retrieval is unavailable.
4. The workflow proposes publishing a knowledge-base draft.
5. Policy marks the publishing action as approval-required.
6. A human approves or rejects in the UI.
7. The run record, audit event, policy result, and final status are visible.

### Scenario B: Prompt-injection and tenant-boundary denial

**Business question:** demonstrate that an untrusted prompt cannot disclose or
mutate another tenant's data.

1. Seed two tenants: `acme` and `globex`.
2. Submit an input asking to ignore instructions and read Globex data or publish
   without approval.
3. Verify no Globex record is returned, no action is executed, and the run is
   recorded as `denied`.
4. Show the policy/validation reason without exposing the system prompt.

## 5. Phase Plan

### H2-0: Freeze the H1 baseline

**Estimate:** half day.  
**PR:** documentation-only closeout if not already complete.

Deliverables:

- Record the H1 Compose, migration, HTTP, WebSocket, and runtime-role evidence.
- Tag the stable baseline before H2 feature work.
- Link the relevant green CI runs in the closeout note.

Acceptance:

```powershell
docker compose up -d --build --wait
docker compose --profile migrate run --rm migrate
docker compose --profile smoke-test run --rm smoke-test
docker compose --profile ws-proxy-test run --rm ws-proxy-test
```

### H2-1: Portfolio entry and truthful documentation

**Estimate:** 1-2 days.  
**Branch:** `codex/h2-portfolio-entry`.

Scope:

- Rewrite the README opening to lead with the business value and canonical demo.
- Update versions and commands to match the running repository.
- Replace obsolete direct Prisma/RLS commands with `docker compose` and the
  unified migrate profile.
- State clearly that Ollama is an opt-in `local-llm` profile, not a default
  service or prerequisite.
- Add the following documents:

```text
docs/interview/
  architecture.md
  demo-script.md
  engineering-tradeoffs.md
  limitations.md
  interview-qa.md
```

- Replace screenshot placeholders with an explicit capture checklist; do not
  fabricate screenshots.

Acceptance:

- README quick start works on a clean Docker Desktop host.
- The first screen explains use case, architecture, demo, safety, and limits.
- No documentation claims a production deployment that is not implemented.

### H2-2: Deterministic demo fixture and one-command verification

**Estimate:** 2-3 days.  
**Branch:** `codex/h2-deterministic-demo`.

Scope:

- Add fixed, idempotent demo fixtures for two tenants and the two canonical
  scenarios.
- Add a cross-platform runner, preferably
  `scripts/interview_demo.py`, with these commands:

```text
start    start or check required Compose services
reset    remove only demo records; never delete Docker volumes
seed     create deterministic tenant-scoped fixtures
run      execute Scenario A or Scenario B
verify   assert expected records, events, decisions, and denial paths
export   write a redacted JSON evidence bundle
```

- Use stable UUIDs or deterministic keys so screenshots and tests are repeatable.
- Do not require Ollama in default mode. The deterministic provider must produce
  the same structured fixture output on every run.

Acceptance:

```powershell
python scripts/interview_demo.py reset
python scripts/interview_demo.py seed
python scripts/interview_demo.py run --scenario support-copilot
python scripts/interview_demo.py verify --scenario support-copilot
python scripts/interview_demo.py run --scenario tenant-denial
python scripts/interview_demo.py verify --scenario tenant-denial
```

- Repeating the sequence succeeds without duplicate or cross-tenant records.
- All created data remains identifiable as demo-only.

### H2-3: Provider boundary on the primary path

**Estimate:** 2-4 days.  
**Branch:** `codex/h2-provider-boundary`.

Scope:

- Introduce a narrow provider protocol only for the canonical Support Copilot
  path; do not refactor every historical agent at once.
- Supply:
  - `DeterministicDemoProvider` for local demos and CI.
  - `OllamaProvider` for explicit `local-llm` usage.
- Keep structured output contracts and Pydantic validation identical across
  providers.
- Report explicit `degraded` status rather than silently inventing an answer if
  a live model or embeddings service is unavailable.

Non-goal:

- Do not add a cloud-provider key, account dependency, or cost-bearing model
  requirement to the default project path.

Acceptance:

- CI runs with the deterministic provider and no LLM service.
- The local-LLM profile is opt-in and documented.
- Invalid provider output cannot produce a mutation or publish an event.

### H2-4: Agent-run evidence and safe trace view

**Estimate:** 3-4 days.  
**Branch:** `codex/h2-agent-run-evidence`.

Scope:

- Extend the existing explainability/audit model or add a minimal `agent_runs`
  read model. Avoid creating a second source of truth for approvals or audit.
- Persist a structured record for the canonical workflow:

```json
{
  "runId": "uuid",
  "scenarioId": "support-copilot",
  "tenantId": "uuid",
  "agent": "support",
  "provider": "deterministic|ollama",
  "model": "identifier-or-null",
  "status": "completed|pending_approval|denied|degraded|failed",
  "durationMs": 0,
  "toolCalls": [],
  "retrievalEvidence": [],
  "policyDecision": {},
  "approval": {},
  "outputValidation": {}
}
```

- Add a tenant-scoped gateway endpoint and a frontend route such as
  `/agents/runs/[id]`.
- Render graph-node progression, tool summaries, evidence identifiers, policy
  result, approval state, duration, and final outcome.

Safety rules:

- Never persist or render chain-of-thought.
- Redact PII, credentials, full prompts, and raw third-party payloads.
- Tenant-filter every run lookup at the gateway and database layers.

Acceptance:

- Scenario A records a completed or pending-approval run.
- Scenario B records a denied run with no data leak.
- A Weaviate failure produces a visible degraded state.
- Tests verify tenant isolation and redaction.

### H2-5: Versioned AI evaluation harness and CI evidence

**Estimate:** 4-5 days.  
**Branch:** `codex/h2-ai-evaluations`.

Scope:

```text
evals/
  datasets/
    routing.jsonl
    rag-grounding.jsonl
    governance.jsonl
    resilience.jsonl
  evaluators/
  thresholds.yaml
  runner.py
  README.md
```

Initial dataset target: 35 examples.

| Category | Target | Required measure |
|---|---:|---|
| Intent and tool routing | 8 | Correct tool/route |
| Retrieval relevance | 8 | Recall@K and tenant filter |
| Grounded answer contract | 7 | Evidence/citation coverage |
| Governance and injection | 7 | Zero unsafe execution |
| Failure and degradation | 5 | Explicit degraded result |

Initial hard gates:

```yaml
structured_output_pass_rate: 1.00
tenant_leak_count: 0
unsafe_execution_count: 0
prompt_injection_block_rate: 1.00
tool_routing_accuracy: 0.90
citation_coverage: 0.90
```

- Create a CI workflow that runs only the deterministic provider and uploads
  `ai-eval-report.json` plus a concise Markdown summary.
- Treat tenant leakage, unsafe mutation, and malformed structured output as
  hard failures. Treat early relevance/quality measures as report-only until
  the dataset is mature.
- Include commit SHA, dataset version, provider, evaluator version, and timing
  in every report.

Evaluation must score both final output and the agent trajectory. Trajectory
evaluation is particularly appropriate here because the user-visible result is
not sufficient if the wrong tool, wrong tenant, or wrong action path was used.
See [LangChain Agent Evals](https://docs.langchain.com/oss/python/langchain/test/evals)
and [LangSmith evaluation concepts](https://docs.langchain.com/langsmith/evaluation-concepts).

Acceptance:

- Evals are repeatable in CI without a model download or cloud API key.
- The report is available as a CI artifact.
- Security gates prevent regression on every pull request.

### H2-6: Interview evidence package

**Estimate:** 2-3 days.  
**Branch:** `codex/h2-interview-evidence`.

Scope:

- Capture real screenshots from the completed deterministic demo:

```text
docs/interview/assets/
  support-rag-result.png
  approval-pending.png
  approval-approved.png
  agent-run-trace.png
  tenant-denial.png
  ai-eval-report.png
  observability.png
```

- Record a 3-5 minute walkthrough. Keep source video outside Git if size is a
  concern; commit a stable link and a text transcript.
- Add a one-page project briefing and a concise architecture diagram.
- Add answer cards for common questions: LangGraph choice, RAG evaluation,
  Kafka trade-offs, RLS/OPA overlap, local-model choice, failures, and scaling.

Acceptance:

- Every image maps to an actual reproducible step.
- The video follows the demo script without manual database edits.
- README links to the evidence package.

## 6. Failure Modes to Demonstrate

| Failure mode | Expected behavior | Evidence |
|---|---|---|
| Prompt injection | Denied; no privileged tool/action | denied run + audit event |
| Cross-tenant request | No foreign data returned | RLS/OPA test + denied run |
| Weaviate unavailable | Grounded fallback/degraded state | run status + metric/log |
| Ollama unavailable | Default demo still runs; opt-in live path degrades clearly | provider status |
| OPA unavailable | Sensitive action fails closed | policy result + approval/deny |
| Approval rejected | No side effect is emitted | approval and audit result |
| Duplicate event | Idempotent result | event/consumer assertion |

## 7. Scaling Path to Explain in Interviews

This is an explanation plan, not an immediate implementation commitment.

| Current local implementation | First production evolution | Reason |
|---|---|---|
| Docker Desktop Compose | Managed container platform or Kubernetes | Separate local iteration from production operations |
| Deterministic demo provider | Managed model gateway/provider abstraction | Control cost, routing, fallbacks, and credentials |
| Single Weaviate instance | Tenant-aware collection/index strategy | Preserve retrieval isolation and scale |
| Local metrics and Grafana | Centralized logs, traces, and alerting | Support incident response |
| Compose secrets/env | Cloud secret manager + workload identity | Rotate and scope credentials |

## 8. Explicit Deferrals

Do not start these until the plan exit criteria are met:

- Kubernetes staging cluster and real-cluster validation.
- Default Ollama installation or automatic model downloads.
- Broad provider refactoring across every historical agent.
- New agent types or new business modules.
- Multi-cloud, autoscaling, or high-availability work.
- Large Dependabot batches unrelated to a security gate.
- Cosmetic UI rewrites unrelated to the primary demo.

## 9. PR Discipline and Definition of Done

For every phase:

1. Branch from current `main` with `codex/h2-*` naming.
2. Keep the PR restricted to the stated phase scope.
3. Add automated regression tests alongside behavior changes.
4. Run targeted tests locally where possible and rely on GitHub Actions for
   Docker/CI-only evidence.
5. Document verification results and limitations in the PR body.
6. Squash merge only after all required checks are green.
7. Tag the code merge point after a closeout review; closeout documentation may
   follow in a separate commit.

## 10. Final Interview Checklist

- [ ] README matches the current stack and commands.
- [ ] Docker Desktop demo succeeds from a clean checkout.
- [ ] Scenario A completes with visible evidence and approval.
- [ ] Scenario B denies an injection/cross-tenant attempt.
- [ ] Eval report has versioned data and CI artifact evidence.
- [ ] No screenshot is a placeholder or fabricated result.
- [ ] Ollama remains opt-in and absence is explained honestly.
- [ ] Architecture, trade-offs, limits, and scaling path are prepared.
- [ ] Resume bullets use measured results, not unverified claims.
- [ ] A 3-5 minute demo can be delivered without editing data manually.


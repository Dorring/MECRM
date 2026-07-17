# H2-6 Preflight: Interview Evidence Package

Date: 2026-07-17

Baseline: `main@483605b` (H2 offline evidence bundle merged)

Status: preflight only; no screenshot, video, or model-quality claim is added
by this change.

## Objective

Turn the existing local Docker Desktop demonstration into an interview-ready
evidence package. The package must make it easy to explain the system while
keeping a strict line between verified behavior, offline evaluation contracts,
and optional live NVIDIA NIM behavior.

## Verified foundations

| Capability | Current evidence | H2-6 use |
| --- | --- | --- |
| Local stack | Compose health checks, migration runner, HTTP and WebSocket smoke tests | Local runtime prerequisite |
| Tenant isolation | PostgreSQL RLS migrations and dedicated CI suites | Security/failure explanation |
| Agent evidence | Tenant-scoped, redacted decision view at `/agents/runs/[id]` | Safe trace screenshot |
| Governance | Approval, audit, policy, and kill-switch pages | Human-control screenshot |
| Retrieval baseline | 35 real PostgreSQL/RLS structured retrieval cases | Recall/precision discussion |
| Safety contracts | 21 deterministic route, evidence, injection, and degraded-state cases | Safety-metric discussion |
| Evidence bundle | `ai-eval-h2-offline-evidence` CI artifact | One summary screenshot or downloaded report |

The CI evidence bundle is offline-only. It does not call NVIDIA NIM, does not
evaluate semantic retrieval, and does not measure answer quality.

## Findings

### B1: the documented demo runner does not exist

`docs/interview/demo-script.md` lists `scripts/interview_demo.py` commands,
but the script is absent. The stated reset/seed/run/verify flow therefore
cannot currently be presented as reproducible.

**Impact:** no current screenshot or video may claim that it came from the
documented one-command deterministic fixture.

**Resolution in H2-6a:** replace unavailable commands with the actual verified
Compose and smoke-test sequence, and describe interactive UI capture as a
manual, local step until a separate fixture implementation exists.

### B2: no real evidence assets exist yet

`docs/interview/assets/` does not exist. The repository has a capture
checklist, but no screenshots, report export, transcript, or video link.

**Impact:** screenshots must be captured from the running local stack after
H2-6a; placeholder images are prohibited.

### B3: H2 status text is stale

The README, limitations, and demo script still say the deterministic fixture,
agent-run evidence screen, and versioned evaluation suite are planned. H2-4
and H2-5 have now delivered safe agent evidence, structured retrieval metrics,
safety contracts, and a combined CI artifact.

**Impact:** the project currently understates verified work and risks
contradicting the evaluation artifacts.

### B4: optional NVIDIA NIM must remain a separate claim

NVIDIA NIM configuration is supported as opt-in runtime configuration. There
is no key-free CI model evaluation and there must be no captured token, request
header, prompt, or assertion that the offline metrics measure NIM quality.

## Proposed implementation slices

### H2-6a: truthful briefing and capture map

Update the public interview documents to:

1. State the completed H2-4/H2-5 capabilities accurately.
2. Add a one-page project briefing: problem, boundaries, architecture, safety,
   evaluation results, and scaling path.
3. Add a capture manifest mapping every desired asset to a reproducible local
   step, expected visible state, and privacy review requirement.
4. Update the demo script so no unavailable command is presented as runnable.
5. Link the evidence package from the README.

This slice is documentation-only and can be validated in CI with regression
checks. It does not fabricate results or change application behavior.

### H2-6b: manual, real local capture

After H2-6a merges, run the local stack and collect only actual evidence:

| Asset | Source | Required visible state |
| --- | --- | --- |
| `agent-run-trace.png` | `/agents/runs/<decision-id>` | redacted tools/evidence and explicit run status |
| `approval-pending.png` | `/approvals` or governance approval tab | pending action, no secret/context payload |
| `approval-approved.png` | same approved record | approval decision and audit-safe state |
| `tenant-denial.png` | governance/agent evidence from denial scenario | denied state, no foreign record |
| `ai-eval-report.png` | downloaded CI evidence bundle | evaluator scope and metrics, no token/URL |
| `observability.png` | local Grafana or Prometheus | service/agent operational signal |

`support-rag-result.png` is deferred until a real, reproducible primary support
run exists. It must not be replaced by an invented chat result.

The video source stays outside Git. A committed transcript may link to a
user-controlled video location only after the recording follows the final
script without manual database edits.

### H2-6c: final evidence attachment

Add reviewed screenshots, a capture ledger containing date/commit/source, and
the walkthrough transcript. This slice requires a running local stack and is
not eligible for CI-only completion.

## Acceptance gates

1. Every public statement identifies whether it is a runtime behavior, an
   offline evaluation result, or an optional live-provider capability.
2. No document claims a non-existent reset/seed/run/verify command.
3. Each image maps to a local path, a visible state, and a privacy review.
4. No screenshot, transcript, or report contains credentials, tokens,
   database URLs, external customer data, raw prompts, chain-of-thought, or
   raw tool payloads.
5. README links to the briefing, demo script, architecture, limits, Q&A, and
   evidence capture package.

## Explicit deferrals

- Recording a video before the capture script and required state are verified.
- Claims about live NVIDIA NIM answer quality, latency, cost, or reliability.
- Kubernetes/staging deployment proof.
- A new one-command demo fixture: it is a separate implementation project, not
  a documentation shortcut.

# Interview Evidence Capture Map

Capture evidence only from the currently running local stack and current GitHub
Actions runs. Do not create placeholder screenshots, paste customer data, or
edit database rows manually to obtain a desired state.

## Before capture

1. Start the verified local stack:

   ```powershell
   docker compose up -d --build --wait
   docker compose --profile migrate run --rm migrate
   docker compose --profile smoke-test run --rm smoke-test
   docker compose --profile ws-proxy-test run --rm ws-proxy-test
   ```

2. Verify `docker compose ps` shows the expected healthy application services.
3. Record the current commit with `git rev-parse --short HEAD` in the capture
   notes. Do not include `.env` values, browser autofill, tokens, database URLs,
   email addresses, or internal hostnames in an image.

## Capture map

| Asset | Reproducible source | Required state | Privacy review |
| --- | --- | --- | --- |
| `agent-run-trace.png` | Governance page -> Decisions -> View safe run evidence | Explicit status, bounded tool outcomes, safe evidence IDs | No raw prompt, reasoning, payload, or foreign record |
| `approval-pending.png` | `/approvals` or Governance -> Approvals | Genuine pending approval | No sensitive approval context or personal data |
| `approval-approved.png` | Same approval after a real human decision | Approved/rejected status and audit-safe result | No token or unredacted justification payload |
| `tenant-denial.png` | A real denied run in Governance or Agent Run Evidence | Denied state with no foreign tenant result | Confirm no foreign tenant ID or record appears |
| `ai-eval-report.png` | Actions -> AI Evaluation Baseline -> `ai-eval-h2-offline-evidence` | PASS/FAIL, scope statement, metric summary | No artifact URL containing a token; no secrets |
| `observability.png` | Local Grafana or Prometheus | Current service/agent operational signal | No internal hostnames or credentials |

`support-rag-result.png` remains deferred until there is a reproducible,
end-to-end primary support scenario. Do not substitute an invented chat answer.

## Capture ledger

For every captured asset, record the following outside the image (for example,
in a short pull-request note or `docs/interview/assets/README.md` after assets
exist):

| Field | Example |
| --- | --- |
| Asset name | `agent-run-trace.png` |
| Commit | short Git SHA |
| Source | local Governance -> Decisions |
| Preconditions | stack healthy; tenant-scoped decision exists |
| Privacy review | prompts/payloads/tokens removed |

## Video walkthrough

Record only after all selected screenshots come from this map. Keep the source
video outside Git and commit only a stable, user-controlled link plus a text
transcript. The walkthrough must state that the evaluation bundle is offline
evidence, not a live NVIDIA NIM benchmark.

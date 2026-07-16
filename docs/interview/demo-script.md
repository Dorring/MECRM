# Interview Demo Script

## Status

The deterministic fixtures and one-command runner are intentionally deferred
until H2-5, after the retrieval-quality baseline is established. This document
defines the stable script before recording screenshots or video.
Do not present placeholder images as evidence.

## Five-minute primary demo

### 0:00-0:30 - Business context

Explain that a support operator needs a response grounded in the tenant's own
knowledge base, while publishing a reusable article remains subject to policy
and human approval.

### 0:30-1:00 - Architecture

Show the [architecture overview](architecture.md). Emphasize that the frontend
does not call the model directly, and that the model cannot bypass RLS or OPA.

### 1:00-2:15 - Support Copilot flow

1. Start from deterministic Acme tenant data.
2. Open the seeded support ticket.
3. Show the structured response: classification, suggested resolution,
   confidence, and evidence references.
4. Show that the proposed knowledge-base action is pending approval.

### 2:15-3:00 - Human control

1. Open the approval screen.
2. Approve the action with a short human reason.
3. Show the completed result and the corresponding audit/decision summary.

### 3:00-3:45 - Security and failure behavior

1. Run the cross-tenant/prompt-injection scenario.
2. Show that it is denied and that no foreign tenant data is returned.
3. Optionally show the degraded state when retrieval is unavailable.

### 3:45-5:00 - Engineering evidence

Show the CI evaluation report, tenant-isolation evidence, and the run-level
trace. State which quality metrics are hard gates and which are report-only.

## Required evidence before recording

- [ ] H2-2 seed/reset/run/verify commands pass twice in sequence.
- [ ] The primary run has a visible tenant-scoped evidence record.
- [ ] The approval result is present in the UI and audit data.
- [ ] The denial scenario contains no foreign-tenant data.
- [ ] The evaluation artifact identifies commit, dataset, provider, and result.
- [ ] All screenshots originate from the current deterministic fixture.

## Commands after H2-5

```powershell
python scripts/interview_demo.py reset
python scripts/interview_demo.py seed
python scripts/interview_demo.py run --scenario support-copilot
python scripts/interview_demo.py verify --scenario support-copilot
python scripts/interview_demo.py run --scenario tenant-denial
python scripts/interview_demo.py verify --scenario tenant-denial
```

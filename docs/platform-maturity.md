# Phase 9: Platform & Organizational Maturity

This phase adds a thin, measurable governance layer (SLOs + alerts + runbooks + extension rules) without changing business logic.

## What “operationally healthy” means

Operationally healthy systems are measurable, predictable, and recoverable:

- Clear SLOs and error budgets for user-facing and critical internal flows.
- Alerts that page humans only for actionable, high-severity conditions.
- Runbooks that let an on-call engineer mitigate quickly and safely.
- Platform guardrails so multiple teams can extend the system without bypassing tenant isolation, governance, or auditing.

## Ownership model (platform vs product)

Platform owns:

- Kubernetes baseline, CI/CD, observability plumbing (Prometheus/Grafana), alert rules, runbook templates.
- Shared reliability controls (rate limit, retries/circuit breakers, policy engine availability standards).
- DR procedures and automation.

Product teams own:

- Feature endpoints and domain workflows.
- SLOs for feature-specific user journeys.
- Runbooks for feature-specific failure modes.

## What incidents look like

Incidents are defined by impact on SLOs:

- **SEV-1**: user-facing outage or cross-tenant safety risk; paging required immediately.
- **SEV-2**: partial outage or significant latency; paging required during business hours or if burn rate is high.
- **SEV-3**: degraded internal processing; ticket and follow-up, no immediate paging.

## When humans are paged

Humans are paged only when all are true:

- Impact is user-facing or risks security/tenant safety.
- Automated remediation is insufficient.
- Clear mitigation exists (runbook) and signals are reliable (low flake).

## SLOs and error budgets

Targets (initial defaults; adjust with real traffic):

- **API availability**: 99.9% (error budget ~43m/month)\n SLI: 1 - (5xx / total requests) on gateway.\n- **Event processing freshness**: p95 < 3s (best-effort until consumer lag exporter exists)\n SLI: kafka consumer lag p95.\n- **Replay/DR success**: 99.99% (error budget ~4m/month)\n SLI: recovery_success vs recovery_failure.\n- **Governance decision latency**: p95 < 500ms\n SLI: auth recheck latency histogram.

Error budget consequences:

- Budget burning fast: freeze non-critical releases; prioritize reliability work.\n- Budget exhausted: require stability approval for releases and mitigate root causes first.

## Alerting philosophy

Alerts must be:

- Actionable (clear owner and runbook).\n- Stable (avoid paging on transient noise).\n- Tied to customer impact or safety.\n
  Alert severity mapping:
- Paging: sustained SLO burn, DR failures, policy engine unavailable, breakers open.\n- Ticket: rising lag, rising error rates without confirmed impact.

## Cost visibility (lightweight)

No FinOps platform required. Use high-level signals:

- Kafka throughput (messages in/out).\n- Postgres DB size growth.\n- Redis memory usage.\n- Snapshot/backup artifact sizes.\n
  Cost work is “measure first”, optimize later.

## Safe extensibility rules

Extensions must not bypass:

- Postgres RLS and tenant scoping.\n- OPA authorization (gateway + agents).\n- Audit event emission for sensitive operations.\n- Governance kill switch and AI data guard.\n
  See: `docs/developer-guidelines.md`.

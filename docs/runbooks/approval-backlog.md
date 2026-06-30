# Runbook: Approval Backlog

## Symptoms

- Human-in-loop approvals pile up and block agent actions.
- Approval-related UIs show many pending items.

## Checks

- Queue signals:
  - `approvals_pending` gauge (if wired) rising.
  - DB check: count pending approvals per tenant.
- Downstream impact:
  - Reduced `kafka_messages_consumed_total` for approval decision processing.

## Mitigation

- Confirm approvals service and gateway are up.
- Scale the approvals decision handlers (consumers) if running on Kubernetes.
- Reduce approval policy strictness only via change control and documented exception.

## Rollback / Safety

- Do not bulk-approve without auditing.
- Do not bypass governance checks; use time-bound operational overrides and document them.

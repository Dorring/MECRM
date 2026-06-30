# Runbook: Disaster Recovery

## Symptoms

- Database lost or corrupted.
- Region-wide outage simulated in isolated environment.
- Alert: `DRRecoveryFailure`.

## Preconditions

- Recovery must run in an isolated environment (dedicated DB or dedicated Kubernetes namespace).
- Use the most recent backup manifest from object storage.

## Checks

- Validate backup artifacts exist:
  - DB dump: `dr/<backup_id>/db.sql`
  - Optional: `aggregate_snapshots.jsonl`, `event_log.jsonl`
- Confirm tenant isolation policies are present after restore (RLS enabled).

## Mitigation / Recovery Procedure

1. Restore DB from backup dump.
2. Restore snapshots and event_log exports (if present).
3. Rebuild read models from events per tenant.
4. Validate integrity (row counts + checksums) per tenant.
5. Measure and record RPO/RTO and compare to targets.

## Rollback / Safety

- Do not run restore against production without change control.
- Do not apply manual SQL fixes to “make it work”.
- If validation fails, stop and re-run from a clean restore target.

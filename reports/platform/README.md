# Platform Maturity Proof (Phase 9)

This folder contains lightweight proof artifacts showing SLO-driven governance, alert rules, runbooks, and basic cost visibility signals.

## Artifacts

- `platform_maturity_report.json`
  - Snapshot of configured SLOs, alert rules, and runbook inventory.

## How to regenerate

```bash
python scripts/platform_maturity_report.py
```

## Alert simulation (safe)

This repo does not ship an Alertmanager configuration. To prove alerting rules load and fire:\n

1. Start the observability stack:\n
   - Prometheus at http://localhost:9090\n
2. Confirm rules loaded in Prometheus UI:\n
   - Status → Rules → group `platform-slo-alerts`\n
3. Trigger a safe alert:\n
   - Stop the OPA container briefly to trigger `PolicyEngineUnavailable` (system will deny requests; this is expected).\n
   - Restart OPA after capturing evidence.\n

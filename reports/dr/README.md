# Disaster Recovery Proof (Phase 8)

This folder contains machine-verifiable artifacts proving the system can recover from catastrophic failure in an isolated environment.

## Artifacts

- `full_recovery.json`
  - End-to-end recovery report (backup → wipe → restore → rebuild → integrity validation).
- `rpo_rto_report.json`
- `recovery_logs.txt`
  - Captured stdout/stderr from a full DR run (for operational evidence).
- `metrics_snapshot.json`
  - Prometheus query snapshot for DR-related metrics.

## How to regenerate (isolated)

```bash
pytest tests/dr/test_full_recovery.py -v
python scripts/dr_run_full_recovery.py
python scripts/dr_generate_recovery_logs.py
python scripts/dr_metrics_snapshot.py
```

# GDPR Compliance Proof (Phase 6)

This folder contains machine-verifiable evidence artifacts produced by Phase 6 tests and scripts.

## Generated artifacts

- `gdpr_forget.json`
  - Proof that customer PII was erased and an audit entry exists.
- `data_export.json`
  - Proof that DSAR export is tenant-scoped and audited.
- `retention_policy.json`
  - Proof that retention policies were applied (optional runner output).
- `compliance_report.json`
  - Consolidated Phase 6 report including PII leak scans of artifacts, metrics, and audit logs.

## How to regenerate

```bash
pytest tests/test_gdpr_forget.py -v
pytest tests/test_data_export.py -v
pytest tests/test_retention_policy.py -v
pytest tests/test_ai_data_guard.py -v
```

Retention runner:

```bash
export DATABASE_URL=postgresql://crm_user:crm_password@localhost:5432/enterprise_crm
python scripts/apply_retention_policies.py
```

Generate consolidated report:

```bash
python scripts/generate_compliance_report.py
```

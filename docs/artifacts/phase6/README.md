# Phase 6 Audit Evidence

Store manual audit evidence screenshots here.

## Recommended screenshots
- `audit_logs_gdpr_forget.png`
  - Show the `audit_logs` entry for `gdpr.forget_customer` or `gdpr.forget_user`.
- `audit_logs_export.png`
  - Show the `audit_logs` entry for `gdpr.export_customer` or `gdpr.export_user`.
- `audit_logs_ai_violation.png`
  - Show the `audit_logs` entry for `ai.data_access_violation`.

## Query hints
```sql
SELECT created_at, action, resource_type, resource_id, actor_type, actor_id, new_value
FROM audit_logs
WHERE tenant_id = '<tenant_uuid>'
ORDER BY created_at DESC
LIMIT 50;
```

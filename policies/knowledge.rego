# Knowledge Base Governance Policy
package enterprise_crm.governance.knowledge

import rego.v1

# Default deny
default allow := false

# Knowledge view - admin, super_admin, auditor can view
allow if {
  input.action == "knowledge:view"
  input.user.roles[_] in ["admin", "super_admin", "auditor"]
  tenant_match
}

# Knowledge edit - admin, super_admin only
allow if {
  input.action == "knowledge:edit"
  input.user.roles[_] in ["admin", "super_admin"]
  tenant_match
}

# Knowledge approve/reject - admin, super_admin only
allow if {
  input.action == "knowledge:approve"
  input.user.roles[_] in ["admin", "super_admin"]
  tenant_match
}

allow if {
  input.action == "knowledge:reject"
  input.user.roles[_] in ["admin", "super_admin"]
  tenant_match
}

# Tenant isolation check
tenant_match if {
  input.user.tenant_id == input.resource.tenant_id
}

# Audit metadata for knowledge actions
audit_metadata := {
  "action": input.action,
  "actor_id": input.user.id,
  "actor_roles": input.user.roles,
  "resource_type": "knowledge",
  "resource_id": input.resource.id,
  "tenant_id": input.resource.tenant_id,
  "timestamp": time.now_ns(),
}

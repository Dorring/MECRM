# Digital Twins Governance Policy
package enterprise_crm.governance.twins

import rego.v1

# Default deny
default allow := false

# Twin view - admin, super_admin, sales_lead, sales can view
allow if {
  input.action == "twins:view"
  input.user.roles[_] in ["admin", "super_admin", "sales_lead", "sales"]
  tenant_match
}

# Twin simulate - admin, super_admin, sales_lead only
allow if {
  input.action == "twins:simulate"
  input.user.roles[_] in ["admin", "super_admin", "sales_lead"]
  tenant_match
}

# Twin history - admin, super_admin, sales_lead only
allow if {
  input.action == "twins:history"
  input.user.roles[_] in ["admin", "super_admin", "sales_lead"]
  tenant_match
}

# Tenant isolation check
tenant_match if {
  input.user.tenant_id == input.resource.tenant_id
}

# Audit metadata for twin actions
audit_metadata := {
  "action": input.action,
  "actor_id": input.user.id,
  "actor_roles": input.user.roles,
  "resource_type": "customer_twin",
  "resource_id": input.resource.customer_id,
  "scenario": input.resource.scenario,
  "tenant_id": input.resource.tenant_id,
  "timestamp": time.now_ns(),
}

# Dev Experience Agent Governance Policy
package enterprise_crm.governance.devx

import rego.v1

# Default deny
default allow := false

# DevX access restricted to engineering roles
devx_roles := ["admin", "super_admin", "engineer", "sre"]

# View insights
allow if {
  input.action == "devx:view"
  input.user.roles[_] in devx_roles
}

# Acknowledge insight
allow if {
  input.action == "devx:acknowledge"
  input.user.roles[_] in devx_roles
}

# Resolve insight
allow if {
  input.action == "devx:resolve"
  input.user.roles[_] in devx_roles
}

# View system health
allow if {
  input.action == "devx:health"
  input.user.roles[_] in devx_roles
}

# Audit metadata for devx actions
audit_metadata := {
  "action": input.action,
  "actor_id": input.user.id,
  "actor_roles": input.user.roles,
  "resource_type": "devx_insight",
  "insight_id": input.resource.insight_id,
  "timestamp": time.now_ns(),
}

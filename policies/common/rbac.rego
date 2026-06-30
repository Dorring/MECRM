# Role-Based Access Control Policy
package enterprise_crm.rbac

import rego.v1

# Role definitions with permissions
role_permissions := {
    "admin": ["*"],
    "sales_manager": [
        "leads:read", "leads:write", "leads:delete", "leads:assign",
        "deals:read", "deals:write", "deals:delete", "deals:assign",
        "customers:read", "customers:write",
        "productivity:read", "productivity:write",
        "predictions:read",
        "automations:read"
    ],
    "sales_rep": [
        "leads:read", "leads:write",
        "deals:read", "deals:write",
        "customers:read",
        "productivity:read", "productivity:write",
        "predictions:read"
    ],
    "support_manager": [
        "tickets:read", "tickets:write", "tickets:delete", "tickets:assign",
        "customers:read", "customers:write",
        "productivity:read", "productivity:write",
        "predictions:read"
    ],
    "support_agent": [
        "tickets:read", "tickets:write",
        "customers:read",
        "productivity:read", "productivity:write",
        "predictions:read"
    ],
    "analyst": [
        "leads:read", "deals:read", "tickets:read", "customers:read",
        "aggregates:read", "replay:read", "replay:write",
        "productivity:read",
        "predictions:read",
        "automations:read"
    ],
    "viewer": [
        "leads:read", "deals:read", "tickets:read", "customers:read",
        "aggregates:read", "replay:read",
        "productivity:read",
        "predictions:read",
        "automations:read"
    ],
    "auditor": [
        "audit:read", "governance:read", "knowledge:read",
        "leads:read", "deals:read", "tickets:read", "customers:read",
        "productivity:read", "predictions:read", "automations:read"
    ]
}

default allow := false

# Allow if admin
allow if {
    input.user.roles[_] == "admin"
}

# Allow if role has permission
allow if {
    role := input.user.roles[_]
    perms := role_permissions[role]
    perms[_] == input.action
}

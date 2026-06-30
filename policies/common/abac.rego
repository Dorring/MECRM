# Attribute-Based Access Control Policy
package enterprise_crm.abac

import rego.v1

default allow := false

# Allow if user owns the resource
allow if {
    input.resource.owner_id == input.user.id
}

# Allow if user is assigned to the resource
allow if {
    input.resource.assigned_to == input.user.id
}

# Allow read for analysts
allow if {
    input.action == "read"
    input.user.roles[_] == "analyst"
}

# Allow managers for leads/deals actions
allow if {
    input.user.roles[_] == "sales_manager"
    startswith(input.action, "leads:")
}

allow if {
    input.user.roles[_] == "sales_manager"
    startswith(input.action, "deals:")
}

allow if {
    input.user.roles[_] == "support_manager"
    startswith(input.action, "tickets:")
}

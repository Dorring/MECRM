# Tenant Isolation Policy
package enterprise_crm.tenant

import rego.v1

default allow := false

# Allow tenant access
allow if {
    input.user.tenant_id == input.resource.tenant_id
}

# Super admin can access all tenants  
allow if {
    input.user.roles[_] == "super_admin"
}

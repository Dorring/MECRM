package enterprise_crm.tenant_isolation

import rego.v1

default allow := false

allow_same_tenant if {
  input.tenant_id != ""
  input.resource.tenant_id != ""
  input.tenant_id == input.resource.tenant_id
}

deny_cross_tenant if {
  input.tenant_id != ""
  input.resource.tenant_id != ""
  input.tenant_id != input.resource.tenant_id
}

allow_super_admin_cross_tenant if {
  input.user.roles[_] == "super_admin"
  endswith(input.action, ":read")
  input.tenant_id != ""
  input.resource.tenant_id != ""
}

allow if {
  allow_same_tenant
}

allow if {
  allow_super_admin_cross_tenant
}

deny contains msg if {
  input.tenant_id == "" 
  msg := "MISSING_SUBJECT_TENANT"
}

deny contains msg if {
  input.resource.tenant_id == "" 
  msg := "MISSING_RESOURCE_TENANT"
}

deny contains msg if {
  not allow
  input.tenant_id != ""
  input.resource.tenant_id != ""
  msg := "CROSS_TENANT_DENY"
}

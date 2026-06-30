package enterprise_crm.tenant_isolation_test

import rego.v1
import data.enterprise_crm.tenant_isolation as ti

test_allow_same_tenant if {
  ti.allow with input as {
    "tenant_id": "a0eebc99-9c0b-4ef8-bb6d-6bb9bd380a11",
    "user": {"id": "u1", "roles": ["admin"]},
    "action": "customers:read",
    "resource": {"tenant_id": "a0eebc99-9c0b-4ef8-bb6d-6bb9bd380a11"}
  }
}

test_deny_cross_tenant if {
  not ti.allow with input as {
    "tenant_id": "a0eebc99-9c0b-4ef8-bb6d-6bb9bd380a11",
    "user": {"id": "u1", "roles": ["admin"]},
    "action": "customers:read",
    "resource": {"tenant_id": "b0eebc99-9c0b-4ef8-bb6d-6bb9bd380a22"}
  }

  ti.deny[_] == "CROSS_TENANT_DENY" with input as {
    "tenant_id": "a0eebc99-9c0b-4ef8-bb6d-6bb9bd380a11",
    "user": {"id": "u1", "roles": ["admin"]},
    "action": "customers:read",
    "resource": {"tenant_id": "b0eebc99-9c0b-4ef8-bb6d-6bb9bd380a22"}
  }
}

test_allow_super_admin_cross_tenant_read if {
  ti.allow with input as {
    "tenant_id": "a0eebc99-9c0b-4ef8-bb6d-6bb9bd380a11",
    "user": {"id": "u1", "roles": ["super_admin"]},
    "action": "customers:read",
    "resource": {"tenant_id": "b0eebc99-9c0b-4ef8-bb6d-6bb9bd380a22"}
  }
}

test_deny_super_admin_cross_tenant_write if {
  not ti.allow with input as {
    "tenant_id": "a0eebc99-9c0b-4ef8-bb6d-6bb9bd380a11",
    "user": {"id": "u1", "roles": ["super_admin"]},
    "action": "customers:write",
    "resource": {"tenant_id": "b0eebc99-9c0b-4ef8-bb6d-6bb9bd380a22"}
  }
}

test_deny_missing_subject_tenant if {
  not ti.allow with input as {
    "tenant_id": "",
    "user": {"id": "u1", "roles": ["admin"]},
    "action": "customers:read",
    "resource": {"tenant_id": "a0eebc99-9c0b-4ef8-bb6d-6bb9bd380a11"}
  }

  ti.deny[_] == "MISSING_SUBJECT_TENANT" with input as {
    "tenant_id": "",
    "user": {"id": "u1", "roles": ["admin"]},
    "action": "customers:read",
    "resource": {"tenant_id": "a0eebc99-9c0b-4ef8-bb6d-6bb9bd380a11"}
  }
}

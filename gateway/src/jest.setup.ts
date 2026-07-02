if (!process.env.DATABASE_URL) {
  process.env.DATABASE_URL = 'postgresql://crm_user:crm_password@localhost:5432/enterprise_crm';
}

// CRM_TEST_REQUIRE_DB is the canonical external switch.
// CRM_DB_AVAILABLE is derived internally and must not be configured by CI.
process.env.CRM_DB_AVAILABLE = process.env.CRM_TEST_REQUIRE_DB === '1' ? '1' : '0';


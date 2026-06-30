// Placeholder staging integration test.
//
// This is a minimal smoke test that pings the staging API root and asserts a
// non-error response. It exists so the CI `integration-tests` job has a real
// target to run (Phase 0 P0: avoid dangling CI path references). The full
// scenario suite is tracked in ./README.md and will be added in Phases 2/3/5/6.
//
// Run via: API_URL=https://staging.example.com npm test
// Uses Node's built-in test runner — no external dependencies required.

import { test } from 'node:test';
import assert from 'node:assert/strict';

const API_URL = process.env.API_URL;

// Skip the live network call entirely when no API URL is configured so the
// suite stays runnable in dev/CI without staging secrets.
const hasTarget = Boolean(API_URL);

test('API_URL is configured when running against staging', { skip: !hasTarget }, () => {
  assert.ok(API_URL, 'API_URL must be set for the staging integration suite');
  try {
    // Validate it parses as a URL.
    // eslint-disable-next-line no-new
    new URL(API_URL);
  } catch {
    assert.fail(`API_URL is not a valid URL: ${API_URL}`);
  }
});

test('staging API root responds with a non-error status', { skip: !hasTarget && 'API_URL not set; skipping live staging smoke test' }, async () => {
  const controller = new AbortController();
  const timeout = setTimeout(() => controller.abort(), 15_000);
  try {
    const res = await fetch(API_URL, {
      signal: controller.signal,
      redirect: 'manual',
    });
    // Accept any HTTP response (including 401/302) as "reachable"; only a
    // network failure or 5xx indicates the service is down.
    assert.ok(res.status < 500, `staging root returned 5xx: ${res.status}`);
  } finally {
    clearTimeout(timeout);
  }
});

#!/usr/bin/env node
/**
 * C4 WebSocket same-origin proxy smoke test.
 *
 * Validates the end-to-end /ws ticket flow through the edge proxy (nginx):
 *   1. Register tenant+user via the proxy
 *   2. Login → capture cookies (csrf_token, refresh_token) + accessToken
 *   3. POST /api/v1/auth/ws-ticket → get single-use ticket UUID
 *   4. WebSocket connect ws://<host>/ws?ticket=<valid> → expect {type:"connected"}
 *   5. Reuse same ticket → expect close code 4401
 *   6. Invalid ticket → expect close code 4401
 *
 * Usage:
 *   node scripts/ws-proxy-test.js [target]
 *   node scripts/ws-proxy-test.js http://frontend-proxy
 *   node scripts/ws-proxy-test.js http://localhost:3000
 *
 * Prerequisites:
 *   - Node.js 20+ (uses global fetch)
 *   - 'ws' package available via NODE_PATH or local node_modules
 */

const { WebSocket } = require('ws');

// ---------------------------------------------------------------------------
// Config
// ---------------------------------------------------------------------------

const TARGET = process.argv[2] || 'http://frontend-proxy';
const TARGET_WS = TARGET.replace(/^http/, 'ws');
const TENANT_SLUG = `ws-smoke-${Date.now()}`;
const EMAIL = `${TENANT_SLUG}@example.com`;
const PASSWORD = 'WsSmokePass123!';
const TIMEOUT_MS = 10000;

let failures = 0;

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

/** Simple cookie jar — stores name=value pairs from Set-Cookie headers. */
class CookieJar {
  constructor() {
    this.cookies = new Map();
  }

  /** Parse Set-Cookie headers from a fetch Response and store them. */
  ingest(response) {
    const setCookie = response.headers.get('set-cookie');
    if (!setCookie) return;
    // Set-Cookie may appear multiple times; headers.get returns the first.
    // We need headers.raw() or headers.getSetCookie() for all values.
    // Node 20 fetch's Headers supports getSetCookie().
    const all = typeof response.headers.getSetCookie === 'function'
      ? response.headers.getSetCookie()
      : [setCookie];
    for (const sc of all) {
      const match = sc.match(/^([^=;]+)=([^;]*)/);
      if (match) {
        this.cookies.set(match[1], match[2]);
      }
    }
  }

  /** Return the Cookie header value for requests. */
  header() {
    const pairs = [];
    for (const [name, value] of this.cookies) {
      pairs.push(`${name}=${value}`);
    }
    return pairs.join('; ');
  }

  get(name) {
    return this.cookies.get(name);
  }
}

function fail(stage, detail) {
  failures++;
  console.error(`FAIL [${stage}]: ${detail}`);
}

async function assertHttp(stage, response, expectedStatus, note) {
  const body = await response.text().catch(() => '<unreadable>');
  if (response.status !== expectedStatus) {
    fail(stage, `${note}: expected HTTP ${expectedStatus}, got ${response.status}. Body: ${body.slice(0, 500)}`);
    return null;
  }
  console.log(`  OK  [${stage}]: HTTP ${response.status} — ${note}`);
  try {
    return JSON.parse(body);
  } catch {
    return body;
  }
}

function sleep(ms) {
  return new Promise((resolve) => setTimeout(resolve, ms));
}

// ---------------------------------------------------------------------------
// Stage 1: Register
// ---------------------------------------------------------------------------

async function stageRegister(jar) {
  console.log('\n[1] Register tenant+user');
  const resp = await fetch(`${TARGET}/api/v1/auth/register`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({
      tenantName: 'WS Smoke Co',
      tenantSlug: TENANT_SLUG,
      name: 'WS Smoke User',
      email: EMAIL,
      password: PASSWORD,
    }),
  });
  jar.ingest(resp);
  return await assertHttp('register', resp, 201, 'register tenant+user');
}

// ---------------------------------------------------------------------------
// Stage 2: Login
// ---------------------------------------------------------------------------

async function stageLogin(jar) {
  console.log('\n[2] Login');
  const resp = await fetch(`${TARGET}/api/v1/auth/login`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({
      tenantSlug: TENANT_SLUG,
      email: EMAIL,
      password: PASSWORD,
    }),
  });
  jar.ingest(resp);
  const body = await assertHttp('login', resp, 200, 'login');
  if (!body || !body.accessToken) {
    fail('login', 'No accessToken in login response body');
    return null;
  }
  const csrf = jar.get('csrf_token');
  if (!csrf) {
    fail('login', 'No csrf_token cookie set by login response');
  } else {
    console.log(`  OK  [login]: csrf_token cookie received`);
  }
  return body.accessToken;
}

// ---------------------------------------------------------------------------
// Stage 3: Request WS ticket
// ---------------------------------------------------------------------------

async function stageGetTicket(jar, accessToken) {
  console.log('\n[3] POST /api/v1/auth/ws-ticket');
  const headers = {
    'Content-Type': 'application/json',
    Authorization: accessToken.startsWith('Bearer ') ? accessToken : `Bearer ${accessToken}`,
  };
  const csrf = jar.get('csrf_token');
  if (csrf) {
    headers['x-csrf-token'] = csrf;
  }
  const cookieHeader = jar.header();
  if (cookieHeader) {
    headers['Cookie'] = cookieHeader;
  }

  const resp = await fetch(`${TARGET}/api/v1/auth/ws-ticket`, {
    method: 'POST',
    headers,
  });
  const body = await assertHttp('ws-ticket', resp, 200, 'get WS ticket');
  if (!body || !body.ticket) {
    fail('ws-ticket', 'No ticket in response body');
    return null;
  }
  console.log(`  OK  [ws-ticket]: ticket=${body.ticket.slice(0, 8)}...`);
  return body.ticket;
}

// ---------------------------------------------------------------------------
// Stage 4: Connect with valid ticket → expect connected
// ---------------------------------------------------------------------------

function connectWs(ticket, timeoutMs = TIMEOUT_MS) {
  return new Promise((resolve) => {
    const url = `${TARGET_WS}/ws?ticket=${ticket}`;
    console.log(`  >> Connecting to ${url}`);
    const ws = new WebSocket(url);
    const timer = setTimeout(() => {
      try { ws.close(); } catch {}
      resolve({ closeCode: null, message: null, reason: 'timeout' });
    }, timeoutMs);

    ws.on('open', () => {
      console.log(`  >> WebSocket opened (101)`);
    });

    ws.on('message', (data) => {
      clearTimeout(timer);
      try {
        const msg = JSON.parse(data.toString());
        ws.close(1000);
        resolve({ closeCode: null, message: msg, reason: 'message received' });
      } catch (e) {
        ws.close(1000);
        resolve({ closeCode: null, message: data.toString(), reason: 'parse error' });
      }
    });

    ws.on('close', (code) => {
      clearTimeout(timer);
      // If we already got a message, ignore late close
      if (code !== 1000) {
        resolve({ closeCode: code, message: null, reason: `closed with code ${code}` });
      }
    });

    ws.on('error', (err) => {
      clearTimeout(timer);
      resolve({ closeCode: null, message: null, reason: `error: ${err.message}` });
    });
  });
}

async function stageConnectValid(ticket) {
  console.log('\n[4] WebSocket connect with valid ticket');
  const result = await connectWs(ticket);
  if (!result.message) {
    fail('connect-valid', `No message received: ${result.reason}`);
    return false;
  }
  if (result.message.type !== 'connected') {
    fail('connect-valid', `Expected type=connected, got type=${result.message.type}. Full: ${JSON.stringify(result.message)}`);
    return false;
  }
  console.log(`  OK  [connect-valid]: received { type: "connected", ... }`);
  return true;
}

// ---------------------------------------------------------------------------
// Stage 5: Reuse consumed ticket → expect 4401
// ---------------------------------------------------------------------------

async function stageConsumedTicket(ticket) {
  console.log('\n[5] WebSocket with consumed ticket → expect 4401');
  const result = await connectWs(ticket);
  if (result.closeCode === 4401) {
    console.log(`  OK  [consumed-ticket]: closed with 4401 as expected`);
    return true;
  }
  fail('consumed-ticket', `Expected close code 4401, got ${result.closeCode} (${result.reason})`);
  return false;
}

// ---------------------------------------------------------------------------
// Stage 6: Invalid ticket → expect 4401
// ---------------------------------------------------------------------------

async function stageInvalidTicket() {
  console.log('\n[6] WebSocket with invalid ticket → expect 4401');
  const result = await connectWs('00000000-0000-0000-0000-000000000000');
  if (result.closeCode === 4401) {
    console.log(`  OK  [invalid-ticket]: closed with 4401 as expected`);
    return true;
  }
  fail('invalid-ticket', `Expected close code 4401, got ${result.closeCode} (${result.reason})`);
  return false;
}

// ---------------------------------------------------------------------------
// Main
// ---------------------------------------------------------------------------

async function main() {
  console.log(`WS Proxy Smoke Test — target: ${TARGET}`);
  console.log(`Tenant: ${TENANT_SLUG} | Email: ${EMAIL}`);

  const jar = new CookieJar();

  // 1. Register
  const regBody = await stageRegister(jar);
  if (!regBody) {
    console.error('\nAborting: registration failed');
    process.exit(1);
  }

  // 2. Login
  const accessToken = await stageLogin(jar);
  if (!accessToken) {
    console.error('\nAborting: login failed');
    process.exit(1);
  }

  // 3. Get WS ticket
  const ticket = await stageGetTicket(jar, accessToken);
  if (!ticket) {
    console.error('\nAborting: ws-ticket failed');
    process.exit(1);
  }

  // 4. Connect with valid ticket
  await stageConnectValid(ticket);

  // 5. Reuse consumed ticket
  await stageConsumedTicket(ticket);

  // 6. Invalid ticket
  await stageInvalidTicket();

  // Report
  console.log(`\n${'='.repeat(60)}`);
  if (failures === 0) {
    console.log('ALL CHECKS PASSED');
    process.exit(0);
  } else {
    console.error(`${failures} FAILURE(S)`);
    process.exit(1);
  }
}

main().catch((err) => {
  console.error('Unhandled error:', err);
  process.exit(1);
});

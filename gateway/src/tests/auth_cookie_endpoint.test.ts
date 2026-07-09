/**
 * Express endpoint-level tests for Group C auth cookie/CSRF behavior.
 *
 * Mocks Prisma and Redis to test HTTP response contracts:
 *   - login: Set-Cookie headers, body shape
 *   - register: 201, Set-Cookie, body shape
 *   - refresh: CSRF validation, cookie-in behavior
 *   - logout: 401 without token
 *   - migrate-cookie: Origin 403, no CSRF required
 *   - ws-ticket: no-token → 401
 *
 * All tests use supertest against a real Express app with mocked backends.
 */

import { describe, it, expect, beforeAll, beforeEach, jest } from '@jest/globals';

// ---------------------------------------------------------------------------
// Mocks — must come before any real module import
// ---------------------------------------------------------------------------

const mockRedisStore = new Map<string, any>();
const mockRevocationState = {
  checkRevokedResult: { revoked: false } as { revoked: boolean; reason?: string },
  checkRevokedError: null as Error | null,
  consumeRefreshResult: { status: 'OK' } as { status: 'OK' | 'DEPENDENCY_ERROR' | 'REVOKED' | 'REPLAY' },
  revokeSidError: null as Error | null,
  rateLimitExceeded: false,
  issueWsTicketError: null as Error | null,
  lastWsTicketPayload: null as any,
  revokeSidCalls: [] as any[][],
};
const mockRedis = {
  get: jest.fn(async (k: string) => mockRedisStore.get(k) ?? null),
  getdel: jest.fn(async (k: string) => { const v = mockRedisStore.get(k); mockRedisStore.delete(k); return v ?? null; }),
  set: jest.fn(async (k: string, v: string, ..._a: any[]) => { mockRedisStore.set(k, v); return 'OK'; }),
  setex: jest.fn(async (k: string, _t: number, v: string) => { mockRedisStore.set(k, v); return 'OK'; }),
  del: jest.fn(async (...ks: string[]) => { for (const k of ks) mockRedisStore.delete(k); return ks.length; }),
  incr: jest.fn(async (k: string) => { const v = (Number(mockRedisStore.get(k)) || 0) + 1; mockRedisStore.set(k, String(v)); return v; }),
  expire: jest.fn(async () => 1),
  ping: jest.fn(async () => 'PONG'),
  ttl: jest.fn(async (k: string) => mockRedisStore.has(k) ? 10 : -1),
  eval: jest.fn(async () => [1, 'OK']),
  exec: jest.fn(async () => [[null, null], [null, null], [null, null]]),
  pipeline: function (this: any) { return this; },
  duplicate: function (this: any) { return this; },
  disconnect: jest.fn(),
  connect: jest.fn(async () => undefined),
  on: jest.fn(),
  subscribe: jest.fn(async () => undefined),
  unsubscribe: jest.fn(async () => undefined),
  keys: jest.fn(async () => []),
};

jest.mock('../services/redis', () => ({
  __esModule: true,
  redisClient: mockRedis,
}));

jest.mock('../services/authSession', () => {
  const actual = jest.requireActual('../services/authSession') as any;
  return {
    ...actual,
    TokenRevocationService: class {
      private client: any;
      constructor(client: any) { this.client = client || mockRedis; }
      async checkRevoked(_token: any) {
        if (mockRevocationState.checkRevokedError) throw mockRevocationState.checkRevokedError;
        return mockRevocationState.checkRevokedResult;
      }
      async consumeRefresh(_token: any) { return mockRevocationState.consumeRefreshResult; }
      async getUserVersion(_t: string, _u: string) { return 0; }
      async revokeSid(...args: any[]) {
        mockRevocationState.revokeSidCalls.push(args);
        if (mockRevocationState.revokeSidError) throw mockRevocationState.revokeSidError;
      }
      async issueWsTicket(p: any) {
        if (mockRevocationState.issueWsTicketError) throw mockRevocationState.issueWsTicketError;
        mockRevocationState.lastWsTicketPayload = p;
        const id = '00000000-0000-0000-0000-000000000099';
        mockRedis.set(`ws:ticket:${id}`, JSON.stringify(p), 'EX', 10, 'NX');
        return id;
      }
      async consumeWsTicket(ticket: string) { return mockRedis.getdel(`ws:ticket:${ticket}`) || null; }
      async consumeWsTicketRateLimit(_uid: string) { return mockRevocationState.rateLimitExceeded; }
      async shutdown() { }
      async initSubscriber() { }
      isSubscriberReady() { return true; }
      static generateId() { return '00000000-0000-0000-0000-000000000000'; }
    },
  };
});

jest.mock('../services/prisma', () => {
  const mockTenant = { id: '00000000-0000-0000-0000-000000000001', name: 'TestTenant', slug: 'test' };
  const mockUser: any = {
    id: '00000000-0000-0000-0000-000000000002',
    email: 'a@b.com',
    name: 'Tester',
    status: 'active',
    tenantId: mockTenant.id,
    passwordHash: '$2a$12$bCt6sNLCN5fZGHZYN7j3iO34p.d4pmFzHvBPFp4VMkbLUYsC41lDa',
    userRoles: [{ role: { name: 'admin' } }],
    tenant: mockTenant,
  };
  const dbTx = jest.fn(async (fn: any) => fn({
    $executeRaw: jest.fn(async () => undefined),
    tenant: {
      findUnique: jest.fn(async (args: any) => {
        // Return tenant only for known slug 'test', null for new registrations
        if (args?.where?.slug === 'test') return mockTenant;
        return null;
      }),
      create: jest.fn(async () => mockTenant),
    },
    user: {
      findFirst: jest.fn(async () => mockUser),
      findUnique: jest.fn(async () => mockUser),
      create: jest.fn(async () => ({ ...mockUser, id: 'new-user-id' })),
      update: jest.fn(async () => mockUser),
    },
    role: { create: jest.fn(async () => ({ id: 'role-id', name: 'admin', permissions: '["*"]', isSystem: true })) },
    userRole: { create: jest.fn(async () => ({ id: 'ur-id', userId: 'new-user-id', roleId: 'role-id' })) },
  }));
  return {
    prisma: {
      $transaction: dbTx,
      user: { findFirst: jest.fn(async () => mockUser), findUnique: jest.fn(async () => mockUser), update: jest.fn(async () => mockUser), create: jest.fn(async () => ({ ...mockUser, id: 'new-user-id' })) },
      tenant: { findUnique: jest.fn(async (args: any) => { if (args?.where?.slug === 'test') return mockTenant; return null; }), create: jest.fn(async () => mockTenant) },
      role: { create: jest.fn(async () => ({ id: 'role-id', ...({} as any) })) },
      userRole: { create: jest.fn(async () => ({ id: 'ur-id', ...({} as any) })) },
    },
  };
});

jest.mock('bcryptjs', () => ({
  hash: jest.fn(async () => '$2a$12$mockedhash'),
  compare: jest.fn(async () => true),  // all passwords valid in test
}));

jest.mock('../services/websocket', () => ({
  closeConnectionsByEvent: jest.fn(),
}));

// ---------------------------------------------------------------------------
// Imports (after mocks)
// ---------------------------------------------------------------------------

import request from 'supertest';
import express from 'express';
import cookieParser from 'cookie-parser';
import { createAuthRoutes } from '../routes/auth';
import { createOriginValidation } from '../middleware/origin';
import { REFRESH_COOKIE, CSRF_COOKIE, CSRF_HEADER } from '../config/cookies';
import { generateCsrfToken } from '../config/csrf';
import { TokenRevocationService } from '../services/authSession';
import { redisClient as mockRedisClient } from '../services/redis';
import { generateRefreshToken, generateToken } from '../middleware/auth';
import jwt from 'jsonwebtoken';
import { JWT_SECRET } from '../config/jwt';

// ---------------------------------------------------------------------------
// Test app factory
// ---------------------------------------------------------------------------

function buildApp() {
  const app = express();
  app.use(express.json());
  app.use(cookieParser());
  app.use(createOriginValidation());
  // Error handler
  app.use((err: any, _req: any, res: any, _next: any) => {
    const status = err.statusCode || err.status || 500;
    res.status(status).json({ error: { code: err.code || 'ERROR', message: err.message } });
  });
  const svc = new TokenRevocationService(mockRedisClient as any);
  app.use('/api/v1/auth', createAuthRoutes(svc, createOriginValidation()));
  return app;
}

const TEST_TENANT_ID = '00000000-0000-0000-0000-000000000001';
const TEST_USER_ID = '00000000-0000-0000-0000-000000000002';
const TEST_SID = '00000000-0000-0000-0000-000000000003';

function futureSessionExpiry(): number {
  return Math.floor(Date.now() / 1000) + 7 * 86400;
}

function makeAccessToken(roles = ['admin', 'agent']): string {
  return generateToken({
    sub: TEST_USER_ID,
    tenantId: TEST_TENANT_ID,
    sid: TEST_SID,
    uv: 0,
    sexp: futureSessionExpiry(),
    email: 'a@b.com',
    roles,
  });
}

function makeRefreshToken(): string {
  return generateRefreshToken({
    sub: TEST_USER_ID,
    tenantId: TEST_TENANT_ID,
    sid: TEST_SID,
    uv: 0,
    sexp: futureSessionExpiry(),
  });
}

function cookieHeader(values: Record<string, string>): string {
  return Object.entries(values).map(([k, v]) => `${k}=${v}`).join('; ');
}

function setCookies(res: request.Response): string[] {
  const raw = res.headers['set-cookie'];
  if (!raw) return [];
  return Array.isArray(raw) ? raw : [raw];
}

beforeEach(() => {
  mockRedisStore.clear();
  jest.clearAllMocks();
  mockRevocationState.checkRevokedResult = { revoked: false };
  mockRevocationState.checkRevokedError = null;
  mockRevocationState.consumeRefreshResult = { status: 'OK' };
  mockRevocationState.revokeSidError = null;
  mockRevocationState.rateLimitExceeded = false;
  mockRevocationState.issueWsTicketError = null;
  mockRevocationState.lastWsTicketPayload = null;
  mockRevocationState.revokeSidCalls = [];
});

// ---------------------------------------------------------------------------
// Tests
// ---------------------------------------------------------------------------

describe('POST /login (mocked)', () => {
  let app: ReturnType<typeof buildApp>;

  beforeAll(() => { app = buildApp(); });

  it('200: Set-Cookie refresh_token with HttpOnly', async () => {
    const res = await request(app)
      .post('/api/v1/auth/login')
      .send({ email: 'a@b.com', password: '12345678', tenantSlug: 'test' })
      .expect(200);

    const sc = Array.isArray(res.headers['set-cookie']) ? res.headers['set-cookie'] : [res.headers['set-cookie'] ?? ''];
    const refreshH = sc.find((h: string) => h.startsWith(`${REFRESH_COOKIE}=`));
    expect(refreshH).toBeDefined();
    expect(refreshH).toContain('HttpOnly');
  });

  it('200: Set-Cookie csrf_token with Path=/', async () => {
    const res = await request(app)
      .post('/api/v1/auth/login')
      .send({ email: 'a@b.com', password: '12345678', tenantSlug: 'test' })
      .expect(200);

    const sc = Array.isArray(res.headers['set-cookie']) ? res.headers['set-cookie'] : [res.headers['set-cookie'] ?? ''];
    const csrfH = sc.find((h: string) => h.startsWith(`${CSRF_COOKIE}=`));
    expect(csrfH).toBeDefined();
    expect(csrfH).toContain('Path=/');
  });

  it('200: body has accessToken and user, no refreshToken', async () => {
    const res = await request(app)
      .post('/api/v1/auth/login')
      .send({ email: 'a@b.com', password: '12345678', tenantSlug: 'test' })
      .expect(200);

    expect(res.body.accessToken).toBeDefined();
    expect(res.body.user).toBeDefined();
    expect(res.body.refreshToken).toBeUndefined();
  });

  it('403: disallowed Origin', async () => {
    const res = await request(app)
      .post('/api/v1/auth/login')
      .set('Origin', 'http://evil.com')
      .send({ email: 'a@b.com', password: '12345678', tenantSlug: 'test' });
    // Origin check runs first
    expect(res.status).toBe(403);
    expect(res.body.error.code).toBe('ORIGIN_NOT_ALLOWED');
  });
});

describe('POST /register (mocked)', () => {
  let app: ReturnType<typeof buildApp>;
  beforeAll(() => { app = buildApp(); });

  it('201: Set-Cookie + body without refreshToken', async () => {
    const res = await request(app)
      .post('/api/v1/auth/register')
      .send({ tenantName: 'NewCo', tenantSlug: 'newco', email: 'new@b.com', password: '12345678', name: 'New' })
      .expect(201);

    const sc = Array.isArray(res.headers['set-cookie']) ? res.headers['set-cookie'] : [res.headers['set-cookie'] ?? ''];
    expect(sc.find((h: string) => h.startsWith(`${REFRESH_COOKIE}=`))).toBeDefined();
    expect(sc.find((h: string) => h.startsWith(`${CSRF_COOKIE}=`))).toBeDefined();
    expect(res.body.accessToken).toBeDefined();
    expect(res.body.refreshToken).toBeUndefined();
  });
});

describe('POST /refresh (mocked)', () => {
  let app: ReturnType<typeof buildApp>;
  beforeAll(() => { app = buildApp(); });

  it('403: missing CSRF header', async () => {
    const res = await request(app)
      .post('/api/v1/auth/refresh')
      .send({})
      .expect(403);
    expect(res.body.error.code).toBe('CSRF_VALIDATION_FAILED');
  });

  it('403: wrong CSRF header', async () => {
    const csrf = generateCsrfToken();
    const res = await request(app)
      .post('/api/v1/auth/refresh')
      .set(CSRF_HEADER, 'wrong-value')
      .set('Cookie', `${CSRF_COOKIE}=${csrf}`)
      .send({})
      .expect(403);
    expect(res.body.error.code).toBe('CSRF_VALIDATION_FAILED');
  });

  it('401: valid CSRF but no refresh cookie', async () => {
    const csrf = generateCsrfToken();
    await request(app)
      .post('/api/v1/auth/refresh')
      .set(CSRF_HEADER, csrf)
      .set('Cookie', `${CSRF_COOKIE}=${csrf}`)
      .send({})
      .expect(401);
  });

  it('ignores body refreshToken in favor of cookie', async () => {
    // Even with a body refreshToken, without the cookie it fails 401.
    const csrf = generateCsrfToken();
    await request(app)
      .post('/api/v1/auth/refresh')
      .set(CSRF_HEADER, csrf)
      .set('Cookie', `${CSRF_COOKIE}=${csrf}`)
      .send({ refreshToken: 'some.ignored.token' })
      .expect(401);
  });

  it('200: valid refresh cookie + CSRF rotates cookies and omits refreshToken from body', async () => {
    const csrf = generateCsrfToken();
    const res = await request(app)
      .post('/api/v1/auth/refresh')
      .set(CSRF_HEADER, csrf)
      .set('Cookie', cookieHeader({
        [CSRF_COOKIE]: csrf,
        [REFRESH_COOKIE]: makeRefreshToken(),
      }))
      .send({})
      .expect(200);

    const sc = setCookies(res);
    expect(sc.find((h: string) => h.startsWith(`${REFRESH_COOKIE}=`))).toBeDefined();
    expect(sc.find((h: string) => h.startsWith(`${CSRF_COOKIE}=`))).toBeDefined();
    expect(res.body.accessToken).toBeDefined();
    expect(res.body.refreshToken).toBeUndefined();
  });
});

describe('POST /logout (mocked)', () => {
  let app: ReturnType<typeof buildApp>;
  beforeAll(() => { app = buildApp(); });

  it('401 without access token', async () => {
    await request(app)
      .post('/api/v1/auth/logout')
      .send({})
      .expect(401);
  });

  it('200: valid access token clears refresh and CSRF cookies after revocation', async () => {
    const csrf = generateCsrfToken();
    const res = await request(app)
      .post('/api/v1/auth/logout')
      .set('Authorization', `Bearer ${makeAccessToken()}`)
      .set('Cookie', cookieHeader({
        [CSRF_COOKIE]: csrf,
        [REFRESH_COOKIE]: makeRefreshToken(),
      }))
      .send({})
      .expect(200);

    const sc = setCookies(res);
    expect(sc.find((h: string) => h.startsWith(`${REFRESH_COOKIE}=`))).toMatch(/Expires=Thu, 01 Jan 1970|Max-Age=0/i);
    expect(sc.find((h: string) => h.startsWith(`${CSRF_COOKIE}=`))).toMatch(/Expires=Thu, 01 Jan 1970|Max-Age=0/i);
    expect(mockRevocationState.revokeSidCalls).toHaveLength(1);
  });

  it('503: Redis revoke failure does not clear cookies', async () => {
    mockRevocationState.revokeSidError = new Error('redis down');

    const res = await request(app)
      .post('/api/v1/auth/logout')
      .set('Authorization', `Bearer ${makeAccessToken()}`)
      .send({})
      .expect(503);

    expect(res.body.error.code).toBe('AUTH_DEPENDENCY_UNAVAILABLE');
    expect(setCookies(res)).toEqual([]);
  });
});

describe('POST /migrate-cookie (mocked)', () => {
  let app: ReturnType<typeof buildApp>;
  beforeAll(() => { app = buildApp(); });

  it('401 with invalid token', async () => {
    await request(app)
      .post('/api/v1/auth/migrate-cookie')
      .send({ refreshToken: 'not.a.jwt' })
      .expect(401);
  });

  it('403 with disallowed Origin', async () => {
    const res = await request(app)
      .post('/api/v1/auth/migrate-cookie')
      .set('Origin', 'http://evil.com')
      .send({ refreshToken: 'a.b.c' })
      .expect(403);
    expect(res.body.error.code).toBe('ORIGIN_NOT_ALLOWED');
  });

  it('does not require CSRF (no 403 CSRF_VALIDATION_FAILED)', async () => {
    await request(app)
      .post('/api/v1/auth/migrate-cookie')
      .send({ refreshToken: 'a.b.c' })
    // 401 (invalid token) or 403 (origin) is fine, but NOT 403 CSRF
      .expect((r: any) => {
        if (r.status === 403) {
          expect(r.body.error?.code).not.toBe('CSRF_VALIDATION_FAILED');
        }
      });
  });

  it('200: valid legacy refresh token sets cookies and omits refreshToken from body', async () => {
    const res = await request(app)
      .post('/api/v1/auth/migrate-cookie')
      .send({ refreshToken: makeRefreshToken() })
      .expect(200);

    const sc = setCookies(res);
    expect(sc.find((h: string) => h.startsWith(`${REFRESH_COOKIE}=`))).toBeDefined();
    expect(sc.find((h: string) => h.startsWith(`${CSRF_COOKIE}=`))).toBeDefined();
    expect(res.body.accessToken).toBeDefined();
    expect(res.body.refreshToken).toBeUndefined();
  });
});

describe('POST /ws-ticket (mocked)', () => {
  let app: ReturnType<typeof buildApp>;
  beforeAll(() => { app = buildApp(); });

  it('401 without access token', async () => {
    await request(app)
      .post('/api/v1/auth/ws-ticket')
      .expect(401);
  });

  it('403 with disallowed Origin', async () => {
    const res = await request(app)
      .post('/api/v1/auth/ws-ticket')
      .set('Origin', 'http://evil.com')
      .set('Authorization', `Bearer ${makeAccessToken()}`)
      .expect(403);

    expect(res.body.error.code).toBe('ORIGIN_NOT_ALLOWED');
  });

  it('200: valid access token issues ticket and preserves JWT roles', async () => {
    const res = await request(app)
      .post('/api/v1/auth/ws-ticket')
      .set('Authorization', `Bearer ${makeAccessToken(['admin', 'sales'])}`)
      .expect(200);

    expect(res.body.ticket).toBe('00000000-0000-0000-0000-000000000099');
    expect(mockRevocationState.lastWsTicketPayload).toMatchObject({
      tenantId: TEST_TENANT_ID,
      userId: TEST_USER_ID,
      sid: TEST_SID,
      uv: 0,
      roles: ['admin', 'sales'],
    });
  });

  it('401 when access token is revoked', async () => {
    mockRevocationState.checkRevokedResult = { revoked: true, reason: 'jti' };

    await request(app)
      .post('/api/v1/auth/ws-ticket')
      .set('Authorization', `Bearer ${makeAccessToken()}`)
      .expect(401);
  });

  it('503 when revocation dependency fails', async () => {
    mockRevocationState.checkRevokedError = new Error('redis down');

    const res = await request(app)
      .post('/api/v1/auth/ws-ticket')
      .set('Authorization', `Bearer ${makeAccessToken()}`)
      .expect(503);

    expect(res.body.error.code).toBe('AUTH_DEPENDENCY_UNAVAILABLE');
  });

  it('429 when per-user WS ticket rate limit is exceeded', async () => {
    mockRevocationState.rateLimitExceeded = true;

    const res = await request(app)
      .post('/api/v1/auth/ws-ticket')
      .set('Authorization', `Bearer ${makeAccessToken()}`)
      .expect(429);

    expect(res.body.error.code).toBe('RATE_LIMITED');
  });
});

describe('GET /me (mocked)', () => {
  let app: ReturnType<typeof buildApp>;
  beforeAll(() => { app = buildApp(); });

  it('401 without access token', async () => {
    await request(app)
      .get('/api/v1/auth/me')
      .expect(401);
  });

  it('401 with invalid token (garbage)', async () => {
    await request(app)
      .get('/api/v1/auth/me')
      .set('Authorization', 'Bearer not.a.real.jwt')
      .expect(401);
  });

  it('401 with expired access token', async () => {
    // Create a token that expired 1 hour ago using jwt.sign directly.
    // The helper always produces valid (non-expired) tokens.
    const expiredToken = jwt.sign(
      {
        jti: 'expired-jti',
        sid: TEST_SID,
        sub: TEST_USER_ID,
        tenantId: TEST_TENANT_ID,
        email: 'a@b.com',
        roles: ['admin'],
        type: 'access',
        uv: 0,
        sexp: futureSessionExpiry(),
      },
      JWT_SECRET,
      { algorithm: 'HS256', expiresIn: '0s' as any },
    );

    const res = await request(app)
      .get('/api/v1/auth/me')
      .set('Authorization', `Bearer ${expiredToken}`)
      .expect(401);

    expect(res.body.error.code).toBe('UNAUTHORIZED');
  });

  it('401 when refresh token is used as access token', async () => {
    const refreshToken = makeRefreshToken();

    const res = await request(app)
      .get('/api/v1/auth/me')
      .set('Authorization', `Bearer ${refreshToken}`)
      .expect(401);

    expect(res.body.error.code).toBe('UNAUTHORIZED');
  });

  it('401 when access token is revoked (me)', async () => {
    mockRevocationState.checkRevokedError = new Error('redis down');

    const res = await request(app)
      .get('/api/v1/auth/me')
      .set('Authorization', `Bearer ${makeAccessToken()}`)
      .expect(503);

    expect(res.body.error.code).toBe('AUTH_DEPENDENCY_UNAVAILABLE');
  });

  it('200 with minimal user from valid access token', async () => {
    const res = await request(app)
      .get('/api/v1/auth/me')
      .set('Authorization', `Bearer ${makeAccessToken(['admin', 'sales'])}`)
      .expect(200);

    expect(res.body).toMatchObject({
      id: TEST_USER_ID,
      email: 'a@b.com',
      tenantId: TEST_TENANT_ID,
      roles: ['admin', 'sales'],
    });
    expect(res.body).toHaveProperty('name');
    expect(res.body).toHaveProperty('id');
    expect(res.body).toHaveProperty('email');
    expect(res.body).toHaveProperty('tenantId');
    expect(res.body).toHaveProperty('roles');
  });
});

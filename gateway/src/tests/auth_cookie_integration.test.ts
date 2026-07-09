/**
 * Group C cookie/auth tests.
 *
 * Layers:
 *   Unit (no deps):     cookie config, CSRF validation, origin middleware
 *   Redis integration:  WS ticket lifecycle, refresh rotation, replay detection
 *   (CRM_REDIS_AVAILABLE=1 to enable Redis tests)
 */

import { describe, it, expect, beforeAll, afterAll, afterEach } from '@jest/globals';
import { randomUUID } from 'crypto';
import Redis from 'ioredis';

const describeRedis = process.env.CRM_REDIS_AVAILABLE === '1' ? describe : describe.skip;
const REDIS_URL = process.env.REDIS_URL || 'redis://localhost:6379';

import { TokenRevocationService, authKeys } from '../services/authSession';
import { getCookieOptions, REFRESH_COOKIE, CSRF_COOKIE, CSRF_HEADER } from '../config/cookies';
import { generateCsrfToken, validateCsrf } from '../config/csrf';
import { createOriginValidation } from '../middleware/origin';

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

function mockReq(headers: Record<string, string> = {}, cookies: Record<string, string> = {}): any {
  return { headers, cookies };
}

function mockRes(): any {
  const res: any = {};
  res.statusCode = 0;
  res.body = undefined;
  res.status = function (code: number) { this.statusCode = code; return this; };
  res.json = function (data: any) { this.body = data; return this; };
  return res;
}

// ---------------------------------------------------------------------------
// Unit: Cookie config
// ---------------------------------------------------------------------------

describe('getCookieOptions (no Redis)', () => {
  const envBackup: Record<string, string | undefined> = {};

  beforeEach(() => {
    envBackup.COOKIE_SECURE = process.env.COOKIE_SECURE;
    envBackup.COOKIE_SAME_SITE = process.env.COOKIE_SAME_SITE;
    envBackup.NODE_ENV = process.env.NODE_ENV;
  });

  afterEach(() => {
    for (const [k, v] of Object.entries(envBackup)) {
      if (v === undefined) delete (process.env as any)[k];
      else (process.env as any)[k] = v;
    }
  });

  it('refresh cookie: HttpOnly, Path=/api/v1/auth', () => {
    const opts = getCookieOptions();
    expect(opts.refresh.httpOnly).toBe(true);
    expect(opts.refresh.path).toBe('/api/v1/auth');
  });

  it('csrf cookie: NOT HttpOnly, Path=/', () => {
    const opts = getCookieOptions();
    expect(opts.csrf.httpOnly).toBe(false);
    expect(opts.csrf.path).toBe('/');
  });

  it('constants match expected names', () => {
    expect(REFRESH_COOKIE).toBe('refresh_token');
    expect(CSRF_COOKIE).toBe('csrf_token');
    expect(CSRF_HEADER).toBe('x-csrf-token');
  });
});

// ---------------------------------------------------------------------------
// Unit: CSRF
// ---------------------------------------------------------------------------

describe('CSRF helpers (no Redis)', () => {
  it('generateCsrfToken returns 64-char hex', () => {
    const t = generateCsrfToken();
    expect(t).toHaveLength(64);
    expect(t).toMatch(/^[0-9a-f]{64}$/);
  });

  it('validateCsrf: matching → true', () => {
    const t = generateCsrfToken();
    expect(validateCsrf(mockReq({ [CSRF_HEADER]: t }, { [CSRF_COOKIE]: t }))).toBe(true);
  });

  it('validateCsrf: missing header → false', () => {
    expect(validateCsrf(mockReq({}, { [CSRF_COOKIE]: 'abc' }))).toBe(false);
  });

  it('validateCsrf: missing cookie → false', () => {
    expect(validateCsrf(mockReq({ [CSRF_HEADER]: 'abc' }, {}))).toBe(false);
  });

  it('validateCsrf: mismatch → false', () => {
    expect(validateCsrf(mockReq({ [CSRF_HEADER]: 'aaa' }, { [CSRF_COOKIE]: 'bbb' }))).toBe(false);
  });
});

// ---------------------------------------------------------------------------
// Unit: Origin
// ---------------------------------------------------------------------------

describe('origin middleware (no Redis)', () => {
  const envBackup = process.env.ALLOWED_ORIGINS;

  afterEach(() => {
    if (envBackup === undefined) delete process.env.ALLOWED_ORIGINS;
    else process.env.ALLOWED_ORIGINS = envBackup;
  });

  it('allows missing Origin', () => {
    process.env.ALLOWED_ORIGINS = 'http://localhost:3000';
    const mw = createOriginValidation();
    const req = mockReq({});
    const res = mockRes();
    let called = false;
    mw(req, res, () => { called = true; });
    expect(called).toBe(true);
  });

  it('rejects unlisted origin with 403', () => {
    process.env.ALLOWED_ORIGINS = 'http://a.com';
    const mw = createOriginValidation();
    const req = mockReq({ origin: 'http://evil.com' });
    const res = mockRes();
    let called = false;
    mw(req, res, () => { called = true; });
    expect(called).toBe(false);
    expect(res.statusCode).toBe(403);
    expect(res.body.error.code).toBe('ORIGIN_NOT_ALLOWED');
  });

  it('fail-closed: empty ALLOWED_ORIGINS + present Origin → 403', () => {
    process.env.ALLOWED_ORIGINS = '';
    const mw = createOriginValidation();
    const req = mockReq({ origin: 'http://anything.com' });
    const res = mockRes();
    let called = false;
    mw(req, res, () => { called = true; });
    expect(called).toBe(false);
    expect(res.statusCode).toBe(403);
  });
});

// ---------------------------------------------------------------------------
// Redis: WS Ticket lifecycle
// ---------------------------------------------------------------------------

describeRedis('WS ticket (real Redis)', () => {
  let redis: Redis;
  let service: TokenRevocationService;

  beforeAll(async () => {
    redis = new Redis(REDIS_URL, { lazyConnect: true, maxRetriesPerRequest: 1 });
    try { await redis.connect(); } catch { redis.disconnect(); throw new Error('Redis not available'); }
    service = new TokenRevocationService(redis);
  });

  afterAll(async () => {
    await service.shutdown();
    redis.disconnect();
  });

  afterEach(async () => {
    const keys = await redis.keys('ws:ticket:*');
    if (keys.length > 0) await redis.del(...keys);
  });

  it('issueWsTicket returns UUID', async () => {
    const ticket = await service.issueWsTicket({
      tenantId: randomUUID(), userId: randomUUID(), jti: randomUUID(), sid: randomUUID(),
      exp: Math.floor(Date.now() / 1000) + 3600, sexp: Math.floor(Date.now() / 1000) + 86400, uv: 0, roles: ['admin'],
    });
    expect(ticket).toMatch(/^[0-9a-f-]{36}$/);
  });

  it('consumeWsTicket returns payload with roles on first use', async () => {
    const payload = {
      tenantId: randomUUID(), userId: randomUUID(), jti: randomUUID(), sid: randomUUID(),
      exp: Math.floor(Date.now() / 1000) + 3600, sexp: Math.floor(Date.now() / 1000) + 86400, uv: 0, roles: ['admin', 'user'],
    };
    const ticket = await service.issueWsTicket(payload);
    const consumed = await service.consumeWsTicket(ticket);
    expect(consumed).not.toBeNull();
    expect(consumed!.tenantId).toBe(payload.tenantId);
    expect(consumed!.roles).toEqual(payload.roles);
  });

  it('consumeWsTicket returns null on second use (GETDEL)', async () => {
    const ticket = await service.issueWsTicket({
      tenantId: randomUUID(), userId: randomUUID(), jti: randomUUID(), sid: randomUUID(),
      exp: Math.floor(Date.now() / 1000) + 3600, sexp: Math.floor(Date.now() / 1000) + 86400, uv: 0, roles: [],
    });
    expect(await service.consumeWsTicket(ticket)).not.toBeNull();
    expect(await service.consumeWsTicket(ticket)).toBeNull();
  });

  it('ws ticket TTL ≤ 10 seconds', async () => {
    const ticket = await service.issueWsTicket({
      tenantId: randomUUID(), userId: randomUUID(), jti: randomUUID(), sid: randomUUID(),
      exp: Math.floor(Date.now() / 1000) + 3600, sexp: Math.floor(Date.now() / 1000) + 86400, uv: 0, roles: [],
    });
    const ttl = await redis.ttl(authKeys.wsTicket(ticket));
    expect(ttl).toBeGreaterThan(0);
    expect(ttl).toBeLessThanOrEqual(10);
  });

  it('consumeWsTicket returns null for non-existent ticket', async () => {
    expect(await service.consumeWsTicket(randomUUID())).toBeNull();
  });

  it('rate limit returns false within limit, true when exceeded', async () => {
    const userId = randomUUID();
    // First 10 calls within limit
    for (let i = 0; i < 10; i++) {
      expect(await service.consumeWsTicketRateLimit(userId)).toBe(false);
    }
    // 11th call exceeds
    expect(await service.consumeWsTicketRateLimit(userId)).toBe(true);
  });
});

// ---------------------------------------------------------------------------
// Redis: Refresh rotation (Group B)
// ---------------------------------------------------------------------------

describeRedis('Refresh rotation (real Redis)', () => {
  let redis: Redis;
  let service: TokenRevocationService;

  beforeAll(async () => {
    redis = new Redis(REDIS_URL, { lazyConnect: true, maxRetriesPerRequest: 1 });
    try { await redis.connect(); } catch { redis.disconnect(); throw new Error('Redis not available'); }
    service = new TokenRevocationService(redis);
  });

  afterAll(async () => {
    await service.shutdown();
    redis.disconnect();
  });

  afterEach(async () => {
    const keys = await redis.keys('auth:*');
    if (keys.length > 0) await redis.del(...keys);
  });

  it('consumeRefresh: OK then REPLAY', async () => {
    const now = Math.floor(Date.now() / 1000);
    const token = {
      jti: randomUUID(), sid: randomUUID(), sub: randomUUID(),
      tenantId: randomUUID(), type: 'refresh' as const,
      uv: 0, sexp: now + 86400, iat: now, exp: now + 3600,
    };
    expect((await service.consumeRefresh(token)).status).toBe('OK');
    expect((await service.consumeRefresh(token)).status).toBe('REPLAY');
  });

  it('REPLAY revokes sid', async () => {
    const now = Math.floor(Date.now() / 1000);
    const sid = randomUUID();
    const tenantId = randomUUID();
    const token = {
      jti: randomUUID(), sid, sub: randomUUID(), tenantId,
      type: 'refresh' as const, uv: 0, sexp: now + 86400, iat: now, exp: now + 3600,
    };
    await service.consumeRefresh(token);
    await service.consumeRefresh(token);
    const check = await service.checkRevoked({
      jti: randomUUID(), sid, sub: randomUUID(), tenantId,
      type: 'access', uv: 0, sexp: now + 86400, iat: now, exp: now + 3600,
    });
    expect(check.revoked).toBe(true);
    expect(check.reason).toBe('sid');
  });
});

// ---------------------------------------------------------------------------
// Redis: Fail-closed (disconnected Redis)
// ---------------------------------------------------------------------------

describeRedis('Fail-closed (real Redis)', () => {
  it('checkRevoked throws on disconnected Redis', async () => {
    const broke = new Redis('redis://127.0.0.1:6399', {
      lazyConnect: true, connectTimeout: 100, maxRetriesPerRequest: 1,
      retryStrategy: () => null,
    });
    const broken = new TokenRevocationService(broke);
    await expect(broken.checkRevoked({
      jti: randomUUID(), sid: randomUUID(), sub: randomUUID(),
      tenantId: randomUUID(), type: 'access', uv: 0,
      sexp: Math.floor(Date.now() / 1000) + 86400, iat: 0, exp: 3600,
    })).rejects.toThrow();
    broke.disconnect();
  });
});

// ---------------------------------------------------------------------------
// Redis: Cross-instance WS ticket
// ---------------------------------------------------------------------------

describeRedis('WS ticket cross-instance (real Redis)', () => {
  let redis: Redis;
  let sa: TokenRevocationService;
  let sb: TokenRevocationService;

  beforeAll(async () => {
    redis = new Redis(REDIS_URL, { lazyConnect: true, maxRetriesPerRequest: 1 });
    try { await redis.connect(); } catch { redis.disconnect(); throw new Error('Redis not available'); }
    sa = new TokenRevocationService(redis);
    sb = new TokenRevocationService(redis);
  });

  afterAll(async () => {
    await sa.shutdown(); await sb.shutdown();
    redis.disconnect();
  });

  afterEach(async () => {
    const keys = await redis.keys('ws:ticket:*');
    if (keys.length > 0) await redis.del(...keys);
  });

  it('ticket issued by A consumed by B', async () => {
    const payload = {
      tenantId: randomUUID(), userId: randomUUID(), jti: randomUUID(), sid: randomUUID(),
      exp: Math.floor(Date.now() / 1000) + 3600, sexp: Math.floor(Date.now() / 1000) + 86400, uv: 0, roles: ['admin'],
    };
    const ticket = await sa.issueWsTicket(payload);
    const consumed = await sb.consumeWsTicket(ticket);
    expect(consumed).not.toBeNull();
    expect(consumed!.tenantId).toBe(payload.tenantId);
    // A can't consume again
    expect(await sa.consumeWsTicket(ticket)).toBeNull();
  });
});

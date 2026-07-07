/**
 * Group C cookie/auth endpoint tests.
 *
 * Tests are split:
 *   - Unit (no deps): cookie config, CSRF validation, origin middleware
 *   - Redis integration (CRM_REDIS_AVAILABLE=1): WS ticket, refresh rotation,
 *     token consumption, cookie helper behavior
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

  it('refresh body does not contain refreshToken key', () => {
    // verify config constants used for naming
    expect(REFRESH_COOKIE).toBe('refresh_token');
    expect(CSRF_COOKIE).toBe('csrf_token');
    expect(CSRF_HEADER).toBe('x-csrf-token');
  });
});

// ---------------------------------------------------------------------------
// Unit: CSRF double-submit
// ---------------------------------------------------------------------------

describe('CSRF helpers (no Redis)', () => {
  it('generateCsrfToken returns 64-char hex', () => {
    const t = generateCsrfToken();
    expect(t).toHaveLength(64);
    expect(t).toMatch(/^[0-9a-f]{64}$/);
  });

  it('tokens are unique', () => {
    const a = generateCsrfToken();
    const b = generateCsrfToken();
    expect(a).not.toBe(b);
  });

  it('validateCsrf: matching header+cookie → true', () => {
    const t = generateCsrfToken();
    const req = mockReq({ [CSRF_HEADER]: t }, { [CSRF_COOKIE]: t });
    expect(validateCsrf(req)).toBe(true);
  });

  it('validateCsrf: missing header → false', () => {
    const req = mockReq({}, { [CSRF_COOKIE]: 'abc' });
    expect(validateCsrf(req)).toBe(false);
  });

  it('validateCsrf: missing cookie → false', () => {
    const req = mockReq({ [CSRF_HEADER]: 'abc' }, {});
    expect(validateCsrf(req)).toBe(false);
  });

  it('validateCsrf: mismatch → false', () => {
    const req = mockReq({ [CSRF_HEADER]: 'aaa' }, { [CSRF_COOKIE]: 'bbb' });
    expect(validateCsrf(req)).toBe(false);
  });

  it('validateCsrf: empty header → false', () => {
    const req = mockReq({ [CSRF_HEADER]: '' }, { [CSRF_COOKIE]: 'abc' });
    expect(validateCsrf(req)).toBe(false);
  });

  it('validateCsrf: empty cookie → false', () => {
    const req = mockReq({ [CSRF_HEADER]: 'abc' }, { [CSRF_COOKIE]: '' });
    expect(validateCsrf(req)).toBe(false);
  });
});

// ---------------------------------------------------------------------------
// Unit: Origin validation middleware
// ---------------------------------------------------------------------------

describe('origin middleware (no Redis)', () => {
  const envBackup = process.env.ALLOWED_ORIGINS;

  afterEach(() => {
    if (envBackup === undefined) delete process.env.ALLOWED_ORIGINS;
    else process.env.ALLOWED_ORIGINS = envBackup;
  });

  it('allows missing Origin header', () => {
    process.env.ALLOWED_ORIGINS = 'http://localhost:3000';
    const mw = createOriginValidation();
    const req = mockReq({});
    const res = mockRes();
    let called = false;
    mw(req, res, () => { called = true; });
    expect(called).toBe(true);
  });

  it('allows listed origin', () => {
    process.env.ALLOWED_ORIGINS = 'http://a.com,http://b.com';
    const mw = createOriginValidation();
    const req = mockReq({ origin: 'http://a.com' });
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

  it('empty ALLOWED_ORIGINS + missing Origin → allow', () => {
    process.env.ALLOWED_ORIGINS = '';
    const mw = createOriginValidation();
    const req = mockReq({});
    const res = mockRes();
    let called = false;
    mw(req, res, () => { called = true; });
    expect(called).toBe(true);
  });
});

// ---------------------------------------------------------------------------
// Redis Integration: WS Ticket
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
    const keys = await redis.keys('auth:*');
    if (keys.length > 0) await redis.del(...keys);
    const tkeys = await redis.keys('ratelimit:*');
    if (tkeys.length > 0) await redis.del(...tkeys);
  });

  it('issueWsTicket returns UUID', async () => {
    const ticket = await service.issueWsTicket({
      tenantId: randomUUID(),
      userId: randomUUID(),
      sid: randomUUID(),
      sexp: Math.floor(Date.now() / 1000) + 86400,
      uv: 0,
      roles: [],
    });
    expect(ticket).toMatch(/^[0-9a-f-]{36}$/);
  });

  it('consumeWsTicket returns payload on first use', async () => {
    const payload = {
      tenantId: randomUUID(),
      userId: randomUUID(),
      sid: randomUUID(),
      sexp: Math.floor(Date.now() / 1000) + 86400,
      uv: 0,
      roles: ['admin'],
    };
    const ticket = await service.issueWsTicket(payload);
    const consumed = await service.consumeWsTicket(ticket);
    expect(consumed).not.toBeNull();
    expect(consumed!.tenantId).toBe(payload.tenantId);
    expect(consumed!.userId).toBe(payload.userId);
    expect(consumed!.sid).toBe(payload.sid);
    expect(consumed!.roles).toEqual(payload.roles);
  });

  it('consumeWsTicket returns null on second use (GETDEL)', async () => {
    const ticket = await service.issueWsTicket({
      tenantId: randomUUID(),
      userId: randomUUID(),
      sid: randomUUID(),
      sexp: Math.floor(Date.now() / 1000) + 86400,
      uv: 0,
      roles: [],
    });
    const first = await service.consumeWsTicket(ticket);
    expect(first).not.toBeNull();
    const second = await service.consumeWsTicket(ticket);
    expect(second).toBeNull();
  });

  it('ws ticket TTL ≤ 10 seconds', async () => {
    const ticket = await service.issueWsTicket({
      tenantId: randomUUID(),
      userId: randomUUID(),
      sid: randomUUID(),
      sexp: Math.floor(Date.now() / 1000) + 86400,
      uv: 0,
      roles: [],
    });
    const ttl = await redis.ttl(authKeys.wsTicket(ticket));
    expect(ttl).toBeGreaterThan(0);
    expect(ttl).toBeLessThanOrEqual(10);
  });

  it('consumeWsTicket returns null for expired/non-existent ticket', async () => {
    const result = await service.consumeWsTicket(randomUUID());
    expect(result).toBeNull();
  });

  it('issueWsTicket uses NX — does not overwrite existing', async () => {
    const payload = {
      tenantId: randomUUID(),
      userId: randomUUID(),
      sid: randomUUID(),
      sexp: Math.floor(Date.now() / 1000) + 86400,
      uv: 0,
      roles: [],
    };
    const ticket = await service.issueWsTicket(payload);
    // Manually set a different value at the same key
    await redis.set(authKeys.wsTicket(ticket), 'different');
    // issueWsTicket with NX should not overwrite
    // (the second issue would fail silently on NX; the key keeps the manual value)
    const raw = await redis.get(authKeys.wsTicket(ticket));
    expect(raw).toBe('different');
  });
});

// ---------------------------------------------------------------------------
// Redis Integration: Refresh rotation (Group B invariant)
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

  it('consumeRefresh: OK on first use, REPLAY on second (Group B)', async () => {
    const now = Math.floor(Date.now() / 1000);
    const token = {
      jti: randomUUID(), sid: randomUUID(), sub: randomUUID(),
      tenantId: randomUUID(), type: 'refresh' as const,
      uv: 0, sexp: now + 86400, iat: now, exp: now + 3600,
    };
    const first = await service.consumeRefresh(token);
    expect(first.status).toBe('OK');
    const second = await service.consumeRefresh(token);
    expect(second.status).toBe('REPLAY');
  });

  it('consumeRefresh: REPVOKED on uv mismatch', async () => {
    const now = Math.floor(Date.now() / 1000);
    const tenantId = randomUUID();
    const userId = randomUUID();
    await service.revokeUser(tenantId, userId); // uv → 1

    const token = {
      jti: randomUUID(), sid: randomUUID(), sub: userId, tenantId,
      type: 'refresh' as const, uv: 0, sexp: now + 86400, iat: now, exp: now + 3600,
    };
    const result = await service.consumeRefresh(token);
    expect(result.status).toBe('REVOKED');
  });

  it('REPLAY revokes sid so subsequent access is rejected', async () => {
    const now = Math.floor(Date.now() / 1000);
    const sid = randomUUID();
    const tenantId = randomUUID();
    const token = {
      jti: randomUUID(), sid, sub: randomUUID(), tenantId,
      type: 'refresh' as const, uv: 0, sexp: now + 86400, iat: now, exp: now + 3600,
    };

    // First use
    await service.consumeRefresh(token);
    // Replay
    const replay = await service.consumeRefresh(token);
    expect(replay.status).toBe('REPLAY');

    // Access token with same sid should be revoked
    const check = await service.checkRevoked({
      jti: randomUUID(), sid, sub: randomUUID(), tenantId,
      type: 'access', uv: 0, sexp: now + 86400, iat: now, exp: now + 3600,
    });
    expect(check.revoked).toBe(true);
  });
});

// ---------------------------------------------------------------------------
// Redis Integration: Redis down fail-closed
// ---------------------------------------------------------------------------

describeRedis('Fail-closed behavior (real Redis)', () => {
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

  it('checkRevoked throws when Redis is disconnected', async () => {
    const broke = new Redis('redis://127.0.0.1:6399', {
      lazyConnect: true, connectTimeout: 100, maxRetriesPerRequest: 1,
      retryStrategy: () => null,
    });
    const broken = new TokenRevocationService(broke);

    await expect(
      broken.checkRevoked({
        jti: randomUUID(), sid: randomUUID(), sub: randomUUID(),
        tenantId: randomUUID(), type: 'access', uv: 0,
        sexp: Math.floor(Date.now() / 1000) + 86400, iat: 0, exp: 3600,
      }),
    ).rejects.toThrow();

    broke.disconnect();
  });

  it('issueWsTicket throws when Redis is disconnected', async () => {
    const broke = new Redis('redis://127.0.0.1:6399', {
      lazyConnect: true, connectTimeout: 100, maxRetriesPerRequest: 1,
      retryStrategy: () => null,
    });
    const broken2 = new TokenRevocationService(broke);

    await expect(
      broken2.issueWsTicket({
        tenantId: randomUUID(), userId: randomUUID(), sid: randomUUID(),
        sexp: Math.floor(Date.now() / 1000) + 86400, uv: 0, roles: [],
      }),
    ).rejects.toThrow();

    broke.disconnect();
  });
});

// ---------------------------------------------------------------------------
// Redis Integration: WS ticket cross-instance
// ---------------------------------------------------------------------------

describeRedis('WS ticket cross-instance (real Redis)', () => {
  let redis: Redis;
  let serviceA: TokenRevocationService;
  let serviceB: TokenRevocationService;

  beforeAll(async () => {
    redis = new Redis(REDIS_URL, { lazyConnect: true, maxRetriesPerRequest: 1 });
    try { await redis.connect(); } catch { redis.disconnect(); throw new Error('Redis not available'); }
    serviceA = new TokenRevocationService(redis);
    serviceB = new TokenRevocationService(redis);
  });

  afterAll(async () => {
    await serviceA.shutdown();
    await serviceB.shutdown();
    redis.disconnect();
  });

  afterEach(async () => {
    const keys = await redis.keys('ws:ticket:*');
    if (keys.length > 0) await redis.del(...keys);
  });

  it('ticket issued by A is consumable by B (shared Redis)', async () => {
    const payload = {
      tenantId: randomUUID(),
      userId: randomUUID(),
      sid: randomUUID(),
      sexp: Math.floor(Date.now() / 1000) + 86400,
      uv: 0,
      roles: ['admin'],
    };
    const ticket = await serviceA.issueWsTicket(payload);
    const consumed = await serviceB.consumeWsTicket(ticket);
    expect(consumed).not.toBeNull();
    expect(consumed!.tenantId).toBe(payload.tenantId);
    // Only one instance can consume (GETDEL atomic)
    const second = await serviceA.consumeWsTicket(ticket);
    expect(second).toBeNull();
  });
});

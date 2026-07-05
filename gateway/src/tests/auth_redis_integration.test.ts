/**
 * Integration tests for TokenRevocationService requiring a real Redis instance.
 *
 * These tests are conditionally skipped when no Redis is available.
 * Set CRM_REDIS_AVAILABLE=1 to run them.
 */

import { describe, it, expect, beforeAll, afterAll } from '@jest/globals';
import { randomUUID } from 'crypto';
import Redis from 'ioredis';

const describeRedis = process.env.CRM_REDIS_AVAILABLE === '1' ? describe : describe.skip;

const REDIS_URL = process.env.REDIS_URL || 'redis://localhost:6379';

import { TokenRevocationService, authKeys } from '../services/authSession';

describeRedis('TokenRevocationService (real Redis)', () => {
  let redis: Redis;
  let subscriber: Redis;
  let service: TokenRevocationService;

  beforeAll(async () => {
    redis = new Redis(REDIS_URL, { lazyConnect: true, maxRetriesPerRequest: 1 });
    subscriber = redis.duplicate();
    try {
      await redis.connect();
      await subscriber.connect();
    } catch {
      redis.disconnect();
      subscriber.disconnect();
      throw new Error('Redis not available');
    }
    service = new TokenRevocationService(redis, subscriber);
  });

  afterAll(async () => {
    await service.shutdown();
    redis.disconnect();
  });

  afterEach(async () => {
    // Clean all auth keys after each test
    const keys = await redis.keys('auth:*');
    if (keys.length > 0) {
      await redis.del(...keys);
    }
  });

  // -----------------------------------------------------------------------
  // Revoke jti
  // -----------------------------------------------------------------------
  it('revokeJti creates a key and checkRevoked returns jti-revoked', async () => {
    const tenant = randomUUID();
    const jti = randomUUID();
    const exp = Math.floor(Date.now() / 1000) + 3600;

    await service.revokeJti(tenant, jti, exp);

    const result = await service.checkRevoked({
      jti, sid: randomUUID(), sub: randomUUID(), tenantId: tenant,
      type: 'access', uv: 0, sexp: exp + 86400, iat: 0, exp,
    });
    expect(result.revoked).toBe(true);
    expect(result.reason).toBe('jti');
  });

  // -----------------------------------------------------------------------
  // Revoke sid
  // -----------------------------------------------------------------------
  it('revokeSid rejects all tokens in the session', async () => {
    const tenant = randomUUID();
    const sid = randomUUID();
    const sexp = Math.floor(Date.now() / 1000) + 86400;

    await service.revokeSid(tenant, sid, sexp);

    // Different jti, same sid → revoked
    const result = await service.checkRevoked({
      jti: randomUUID(), sid, sub: randomUUID(), tenantId: tenant,
      type: 'access', uv: 0, sexp, iat: 0, exp: sexp,
    });
    expect(result.revoked).toBe(true);
    expect(result.reason).toBe('sid');
  });

  // -----------------------------------------------------------------------
  // User version
  // -----------------------------------------------------------------------
  it('increment user version rejects old token uv', async () => {
    const tenant = randomUUID();
    const userId = randomUUID();

    // Version should be 0 for new user
    const v = await service.getUserVersion(tenant, userId);
    expect(v).toBe(0);

    // Token with uv=0 should be valid
    const r1 = await service.checkRevoked({
      jti: randomUUID(), sid: randomUUID(), sub: userId, tenantId: tenant,
      type: 'access', uv: 0, sexp: Math.floor(Date.now() / 1000) + 86400, iat: 0, exp: 3600,
    });
    expect(r1.revoked).toBe(false);

    // Increment user version
    const newV = await service.revokeUser(tenant, userId);
    expect(newV).toBe(1);

    // Token with uv=0 should now be rejected
    const r2 = await service.checkRevoked({
      jti: randomUUID(), sid: randomUUID(), sub: userId, tenantId: tenant,
      type: 'access', uv: 0, sexp: Math.floor(Date.now() / 1000) + 86400, iat: 0, exp: 3600,
    });
    expect(r2.revoked).toBe(true);
    expect(r2.reason).toBe('uv');

    // Token with uv=1 should be valid
    const r3 = await service.checkRevoked({
      jti: randomUUID(), sid: randomUUID(), sub: userId, tenantId: tenant,
      type: 'access', uv: 1, sexp: Math.floor(Date.now() / 1000) + 86400, iat: 0, exp: 3600,
    });
    expect(r3.revoked).toBe(false);
  });

  // -----------------------------------------------------------------------
  // Tenant isolation
  // -----------------------------------------------------------------------
  it('tenant A revocation does not affect tenant B', async () => {
    const tenantA = randomUUID();
    const tenantB = randomUUID();
    const jti = randomUUID();
    const exp = Math.floor(Date.now() / 1000) + 3600;

    await service.revokeJti(tenantA, jti, exp);

    // Same jti in tenant B should NOT be revoked
    const result = await service.checkRevoked({
      jti, sid: randomUUID(), sub: randomUUID(), tenantId: tenantB,
      type: 'access', uv: 0, sexp: exp + 86400, iat: 0, exp,
    });
    expect(result.revoked).toBe(false);
  });

  // -----------------------------------------------------------------------
  // Atomic refresh: OK
  // -----------------------------------------------------------------------
  it('consumeRefresh returns OK on first use', async () => {
    const now = Math.floor(Date.now() / 1000);
    const token = {
      jti: randomUUID(), sid: randomUUID(), sub: randomUUID(),
      tenantId: randomUUID(), type: 'refresh' as const,
      uv: 0, sexp: now + 86400, iat: now, exp: now + 3600,
    };

    const result = await service.consumeRefresh(token);
    expect(result.status).toBe('OK');
  });

  // -----------------------------------------------------------------------
  // Atomic refresh: REPLAY
  // -----------------------------------------------------------------------
  it('consumeRefresh returns REPLAY on second use', async () => {
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

  // -----------------------------------------------------------------------
  // Atomic refresh: REPLAY revokes sid
  // -----------------------------------------------------------------------
  it('REPLAY revokes sid so subsequent access token refresh fails', async () => {
    const now = Math.floor(Date.now() / 1000);
    const sid = randomUUID();
    const tenantId = randomUUID();

    const token = {
      jti: randomUUID(), sid, sub: randomUUID(), tenantId,
      type: 'refresh' as const, uv: 0, sexp: now + 86400, iat: now, exp: now + 3600,
    };

    // First use — OK
    await service.consumeRefresh(token);

    // Second use — REPLAY, sid gets revoked
    const replay = await service.consumeRefresh(token);
    expect(replay.status).toBe('REPLAY');

    // Access token check with same sid should be revoked
    const check = await service.checkRevoked({
      jti: randomUUID(), sid, sub: randomUUID(), tenantId,
      type: 'access', uv: 0, sexp: now + 86400, iat: now, exp: now + 3600,
    });
    expect(check.revoked).toBe(true);
  });

  // -----------------------------------------------------------------------
  // Atomic refresh: REVOKED when user version changed
  // -----------------------------------------------------------------------
  it('consumeRefresh returns REVOKED on uv mismatch', async () => {
    const now = Math.floor(Date.now() / 1000);
    const tenantId = randomUUID();
    const userId = randomUUID();

    // Increment user version to 1
    await service.revokeUser(tenantId, userId);

    const token = {
      jti: randomUUID(), sid: randomUUID(), sub: userId, tenantId,
      type: 'refresh' as const, uv: 0, // old uv
      sexp: now + 86400, iat: now, exp: now + 3600,
    };

    const result = await service.consumeRefresh(token);
    expect(result.status).toBe('REVOKED');
  });

  // -----------------------------------------------------------------------
  // TTL correctness
  // -----------------------------------------------------------------------
  it('revoked key has appropriate TTL', async () => {
    const tenant = randomUUID();
    const jti = randomUUID();
    const exp = Math.floor(Date.now() / 1000) + 3600;

    await service.revokeJti(tenant, jti, exp);

    const ttl = await redis.ttl(authKeys.revokedJti(tenant, jti));
    expect(ttl).toBeGreaterThan(3500); // ~3600 + skew
    expect(ttl).toBeLessThanOrEqual(604800);
  });

  // -----------------------------------------------------------------------
  // Concurrent refresh (two parallel calls, only one succeeds)
  // -----------------------------------------------------------------------
  it('two concurrent refresh calls: exactly one OK, one REPLAY', async () => {
    const now = Math.floor(Date.now() / 1000);
    const token = {
      jti: randomUUID(), sid: randomUUID(), sub: randomUUID(),
      tenantId: randomUUID(), type: 'refresh' as const,
      uv: 0, sexp: now + 86400, iat: now, exp: now + 3600,
    };

    const [r1, r2] = await Promise.all([
      service.consumeRefresh(token),
      service.consumeRefresh(token),
    ]);

    const okCount = [r1, r2].filter((r) => r.status === 'OK').length;
    const replayCount = [r1, r2].filter((r) => r.status === 'REPLAY').length;

    expect(okCount).toBe(1);
    expect(replayCount).toBe(1);
  });

  // -----------------------------------------------------------------------
  // Redis CONFIG verification: AOF + noeviction
  // -----------------------------------------------------------------------
  it('redis has AOF, appendfsync always, and noeviction configured', async () => {
    const aof = (await redis.config('GET', 'appendonly')) as [string, string];
    expect(aof[1]).toBe('yes');

    const fsync = (await redis.config('GET', 'appendfsync')) as [string, string];
    expect(fsync[1]).toBe('always');

    const eviction = (await redis.config('GET', 'maxmemory-policy')) as [string, string];
    expect(eviction[1]).toBe('noeviction');
  });

  // -----------------------------------------------------------------------
  // Client reconnect: revoked jti survives client disconnect/reconnect
  // (This is NOT a server restart — see redis_durability_integration.test.ts
  //  for real Redis process restart tests with AOF persistence.)
  // -----------------------------------------------------------------------
  it('revoked jti survives client reconnect (not server restart)', async () => {
    const tenant = randomUUID();
    const jti = randomUUID();
    const exp = Math.floor(Date.now() / 1000) + 3600;

    // Write revocation
    await service.revokeJti(tenant, jti, exp);

    // Simulate restart by disconnecting and reconnecting
    const oldSubscriber = subscriber;
    redis.disconnect();
    oldSubscriber.disconnect();
    await redis.connect();
    subscriber = redis.duplicate();
    await subscriber.connect();

    // Recreate service with new connections
    service = new TokenRevocationService(redis, subscriber);

    // Verify jti still revoked
    const result = await service.checkRevoked({
      jti, sid: randomUUID(), sub: randomUUID(), tenantId: tenant,
      type: 'access', uv: 0, sexp: exp + 86400, iat: 0, exp,
    });
    expect(result.revoked).toBe(true);
    expect(result.reason).toBe('jti');
  });

  // -----------------------------------------------------------------------
  // Client reconnect: user version survives client disconnect/reconnect
  // (This is NOT a server restart — see redis_durability_integration.test.ts
  //  for real Redis process restart tests with AOF persistence.)
  // -----------------------------------------------------------------------
  it('user version persists across client reconnect (not server restart)', async () => {
    const tenant = randomUUID();
    const userId = randomUUID();

    // Increment user version
    const newV = await service.revokeUser(tenant, userId);
    expect(newV).toBe(1);

    // Disconnect and reconnect
    const oldSubscriber = subscriber;
    redis.disconnect();
    oldSubscriber.disconnect();
    await redis.connect();
    subscriber = redis.duplicate();
    await subscriber.connect();
    service = new TokenRevocationService(redis, subscriber);

    // Old uv=0 token should be rejected
    const rejected = await service.checkRevoked({
      jti: randomUUID(), sid: randomUUID(), sub: userId, tenantId: tenant,
      type: 'access', uv: 0, sexp: Math.floor(Date.now() / 1000) + 86400, iat: 0, exp: 3600,
    });
    expect(rejected.revoked).toBe(true);
    expect(rejected.reason).toBe('uv');

    // New uv=1 token should be accepted (new login)
    const accepted = await service.checkRevoked({
      jti: randomUUID(), sid: randomUUID(), sub: userId, tenantId: tenant,
      type: 'access', uv: 1, sexp: Math.floor(Date.now() / 1000) + 86400, iat: 0, exp: 3600,
    });
    expect(accepted.revoked).toBe(false);
  });

  // -----------------------------------------------------------------------
  // OOM simulation: revokeJti fails closed, checkRevoked fails closed
  // -----------------------------------------------------------------------
  it('revoke and check operations fail closed under OOM (noeviction)', async () => {
    // Save original maxmemory so we can restore it
    const origMaxmem = (await redis.config('GET', 'maxmemory')) as [string, string];
    const origPolicy = (await redis.config('GET', 'maxmemory-policy')) as [string, string];

    // Ensure CONFIG SET is available
    try {
      await redis.config('SET', 'maxmemory-policy', 'noeviction');
    } catch {
      // CONFIG SET is restricted (e.g. ACL) — test cannot run
      throw new Error('CONFIG SET unavailable — OOM test requires Redis CONFIG access');
    }

    try {
      // Set tiny maxmemory to force OOM on next write
      await redis.config('SET', 'maxmemory', '100');
      // Force evict nothing (noeviction) — writes will fail with OOM
      await new Promise((r) => setTimeout(r, 200));

      const tenant = randomUUID();
      const jti = randomUUID();
      const exp = Math.floor(Date.now() / 1000) + 3600;

      // revokeJti writes a key → must fail under OOM
      await expect(
        service.revokeJti(tenant, jti, exp),
      ).rejects.toThrow();

      // revokeSid writes a key → must fail under OOM
      await expect(
        service.revokeSid(tenant, randomUUID(), exp + 86400),
      ).rejects.toThrow();

      // revokeUser increments a key → must fail under OOM
      await expect(
        service.revokeUser(tenant, randomUUID()),
      ).rejects.toThrow();

      // consumeRefresh uses Lua with SET NX → must return DEPENDENCY_ERROR
      const now = Math.floor(Date.now() / 1000);
      const refreshResult = await service.consumeRefresh({
        jti: randomUUID(), sid: randomUUID(), sub: randomUUID(),
        tenantId: tenant, type: 'refresh', uv: 0,
        sexp: now + 86400, iat: now, exp: now + 3600,
      });
      expect(refreshResult.status).toBe('DEPENDENCY_ERROR');
    } finally {
      // Restore all Redis config
      await redis.config('SET', 'maxmemory', origMaxmem[1]);
      await redis.config('SET', 'maxmemory-policy', origPolicy[1]);
    }
  }, 15000);

  // -----------------------------------------------------------------------
  // Redis outage: checkRevoked fails closed
  // -----------------------------------------------------------------------
  it('checkRevoked throws when Redis is disconnected (fail-closed)', async () => {
    const broke = new Redis('redis://127.0.0.1:6399', {
      lazyConnect: true,
      connectTimeout: 100,
      maxRetriesPerRequest: 1,
      retryStrategy: () => null,
    });

    const brokenService = new TokenRevocationService(broke);

    await expect(
      brokenService.checkRevoked({
        jti: randomUUID(), sid: randomUUID(), sub: randomUUID(),
        tenantId: randomUUID(), type: 'access', uv: 0,
        sexp: Math.floor(Date.now() / 1000) + 86400, iat: 0, exp: 3600,
      }),
    ).rejects.toThrow();

    broke.disconnect();
  });

  // -----------------------------------------------------------------------
  // Pub/Sub event propagation
  // -----------------------------------------------------------------------
  it('subscriber receives revocation events', async () => {
    const sub2 = redis.duplicate();
    await sub2.connect();

    const received: any[] = [];
    await sub2.subscribe('auth:revocation:events');
    sub2.on('message', (channel, message) => {
      if (channel === 'auth:revocation:events') {
        received.push(JSON.parse(message));
      }
    });

    const tenant = randomUUID();
    const jti = randomUUID();
    const exp = Math.floor(Date.now() / 1000) + 3600;

    await service.revokeJti(tenant, jti, exp);

    // Wait for Pub/Sub propagation
    await new Promise((r) => setTimeout(r, 500));

    expect(received.length).toBeGreaterThanOrEqual(1);
    expect(received[0].type).toBe('jti');
    expect(received[0].tenantId).toBe(tenant);
    expect(received[0].id).toBe(jti);

    await sub2.unsubscribe('auth:revocation:events');
    sub2.disconnect();
  });
});

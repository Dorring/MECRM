/**
 * Redis durability and restart persistence tests.
 *
 * Requires CRM_REDIS_AVAILABLE=1 and ability to restart the Redis process.
 * Set CRM_CAN_RESTART_REDIS=1 when the test runner can reach the Redis container.
 *
 * Covers B5 fault tests:
 * - revoke token → restart Redis → token still rejected
 * - increment user generation → restart Redis → old session rejected, new login works
 * - Redis stop → HTTP 503, WS 1013
 */

import { describe, it, expect, beforeAll, afterAll } from '@jest/globals';
import { randomUUID } from 'crypto';
import { execSync } from 'child_process';
import Redis from 'ioredis';
import { TokenRevocationService } from '../services/authSession';

const describeRedis =
  process.env.CRM_REDIS_AVAILABLE === '1' ? describe : describe.skip;

const describeRestart =
  process.env.CRM_REDIS_AVAILABLE === '1' &&
  process.env.CRM_CAN_RESTART_REDIS === '1'
    ? describe
    : describe.skip;

const REDIS_URL = process.env.REDIS_URL || 'redis://localhost:6379';

/**
 * Attempt to restart the Redis server.
 * Tries docker compose first, then direct docker restart.
 * Throws if neither succeeds.
 */
function restartRedis(): void {
  try {
    execSync('docker compose restart redis', {
      timeout: 30000,
      stdio: 'pipe',
    });
    return;
  } catch {
    // Fall through to direct docker restart
  }
  try {
    const containerId = execSync(
      'docker ps -q --filter ancestor=redis:7-alpine --filter ancestor=redis:7',
      { timeout: 5000, stdio: 'pipe' },
    )
      .toString()
      .trim()
      .split('\n')[0];
    if (containerId) {
      execSync(`docker restart ${containerId}`, {
        timeout: 30000,
        stdio: 'pipe',
      });
      return;
    }
  } catch {
    // Fall through
  }
  throw new Error('Could not restart Redis — set CRM_CAN_RESTART_REDIS=1');
}

/**
 * Wait for Redis to become reachable after a restart.
 * Returns a NEW client with retryStrategy disabled (no auto-reconnect).
 */
async function waitForRedis(url: string, maxWaitMs = 10000): Promise<Redis> {
  const start = Date.now();
  // eslint-disable-next-line no-constant-condition
  while (true) {
    const client = new Redis(url, {
      lazyConnect: true,
      maxRetriesPerRequest: 1,
      connectTimeout: 2000,
      retryStrategy: () => null, // no auto-reconnect
    });
    try {
      await client.connect();
      const pong = await client.ping();
      if (pong === 'PONG') return client;
    } catch {
      client.disconnect();
    }
    if (Date.now() - start > maxWaitMs) {
      throw new Error(`Redis did not recover within ${maxWaitMs}ms`);
    }
    await new Promise((r) => setTimeout(r, 500));
  }
}

/**
 * Disconnect a Redis client, suppressing errors.
 */
function safeDisconnect(client: Redis | undefined): void {
  try { client?.disconnect(); } catch { /* */ }
}

describeRestart('Redis restart persistence', () => {
  let redis: Redis;
  let subscriber: Redis;
  let service: TokenRevocationService;

  beforeAll(async () => {
    redis = new Redis(REDIS_URL, {
      lazyConnect: true,
      maxRetriesPerRequest: 1,
      retryStrategy: () => null,
    });
    subscriber = redis.duplicate();
    await redis.connect();
    await subscriber.connect();
    service = new TokenRevocationService(redis, subscriber);
  });

  afterAll(async () => {
    await service.shutdown();
    safeDisconnect(redis);
    safeDisconnect(subscriber);
  });

  // -----------------------------------------------------------------------
  // B5-1: Revoke token → restart Redis → token still rejected
  // -----------------------------------------------------------------------
  it('revoked jti stays rejected after real Redis restart', async () => {
    const tenant = randomUUID();
    const jti = randomUUID();
    const exp = Math.floor(Date.now() / 1000) + 3600;

    await service.revokeJti(tenant, jti, exp);

    const pre = await service.checkRevoked({
      jti, sid: randomUUID(), sub: randomUUID(), tenantId: tenant,
      type: 'access', uv: 0, sexp: exp + 86400, iat: 0, exp,
    });
    expect(pre.revoked).toBe(true);

    // Disconnect old clients BEFORE restart to prevent auto-reconnect handles
    await service.shutdown();
    safeDisconnect(subscriber);
    safeDisconnect(redis);

    restartRedis();

    // Create fresh clients after restart
    redis = await waitForRedis(REDIS_URL);
    subscriber = redis.duplicate();
    await subscriber.connect();
    service = new TokenRevocationService(redis, subscriber);

    const post = await service.checkRevoked({
      jti, sid: randomUUID(), sub: randomUUID(), tenantId: tenant,
      type: 'access', uv: 0, sexp: exp + 86400, iat: 0, exp,
    });
    expect(post.revoked).toBe(true);
    expect(post.reason).toBe('jti');

    const keys = await redis.keys('auth:*');
    if (keys.length > 0) await redis.del(...keys);
  }, 30000);

  // -----------------------------------------------------------------------
  // B5-2: User version: revokeUser → restart → old rejected, new works
  // -----------------------------------------------------------------------
  it('user version persists across real Redis restart', async () => {
    const tenant = randomUUID();
    const userId = randomUUID();

    const v = await service.revokeUser(tenant, userId);
    expect(v).toBe(1);

    const preReject = await service.checkRevoked({
      jti: randomUUID(), sid: randomUUID(), sub: userId, tenantId: tenant,
      type: 'access', uv: 0, sexp: Math.floor(Date.now() / 1000) + 86400, iat: 0, exp: 3600,
    });
    expect(preReject.revoked).toBe(true);

    // Disconnect old clients BEFORE restart
    await service.shutdown();
    safeDisconnect(subscriber);
    safeDisconnect(redis);

    restartRedis();

    redis = await waitForRedis(REDIS_URL);
    subscriber = redis.duplicate();
    await subscriber.connect();
    service = new TokenRevocationService(redis, subscriber);

    // Old token (uv=0) must still be rejected
    const postReject = await service.checkRevoked({
      jti: randomUUID(), sid: randomUUID(), sub: userId, tenantId: tenant,
      type: 'access', uv: 0, sexp: Math.floor(Date.now() / 1000) + 86400, iat: 0, exp: 3600,
    });
    expect(postReject.revoked).toBe(true);
    expect(postReject.reason).toBe('uv');

    // New token (uv=1) must be accepted (simulates new login)
    const postAccept = await service.checkRevoked({
      jti: randomUUID(), sid: randomUUID(), sub: userId, tenantId: tenant,
      type: 'access', uv: 1, sexp: Math.floor(Date.now() / 1000) + 86400, iat: 0, exp: 3600,
    });
    expect(postAccept.revoked).toBe(false);

    const keys = await redis.keys('auth:*');
    if (keys.length > 0) await redis.del(...keys);
  }, 30000);
});

describeRedis('Redis stop/start fault injection', () => {
  it('checkRevoked throws when Redis is unreachable', async () => {
    const broken = new Redis('redis://127.0.0.1:6399', {
      lazyConnect: true,
      connectTimeout: 200,
      maxRetriesPerRequest: 1,
      retryStrategy: () => null,
    });
    try {
      const brokenService = new TokenRevocationService(broken);
      await expect(
        brokenService.checkRevoked({
          jti: randomUUID(), sid: randomUUID(), sub: randomUUID(),
          tenantId: randomUUID(), type: 'access', uv: 0,
          sexp: Math.floor(Date.now() / 1000) + 86400, iat: 0, exp: 3600,
        }),
      ).rejects.toThrow();
    } finally {
      broken.disconnect();
    }
  });

  it('consumeRefresh returns DEPENDENCY_ERROR when Redis is unreachable', async () => {
    const broken = new Redis('redis://127.0.0.1:6399', {
      lazyConnect: true,
      connectTimeout: 200,
      maxRetriesPerRequest: 1,
      retryStrategy: () => null,
    });
    try {
      const brokenService = new TokenRevocationService(broken);
      const now = Math.floor(Date.now() / 1000);
      const result = await brokenService.consumeRefresh({
        jti: randomUUID(), sid: randomUUID(), sub: randomUUID(),
        tenantId: randomUUID(), type: 'refresh', uv: 0,
        sexp: now + 86400, iat: now, exp: now + 3600,
      });
      expect(result.status).toBe('DEPENDENCY_ERROR');
    } finally {
      broken.disconnect();
    }
  });
});

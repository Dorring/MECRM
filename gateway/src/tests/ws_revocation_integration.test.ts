/**
 * B4 WebSocket + HTTP fault integration tests.
 *
 * Tests:
 * - closeConnectionsByEvent closes matching jti socket with 4401
 * - malformed Pub/Sub event rejected with metric increment
 * - oversized Pub/Sub event rejected with metric increment
 * - subscriber readiness transitions on close/reconnect
 * - heartbeat catches revoked token (Pub/Sub miss)
 * - heartbeat overlap_prevented metric
 * - heartbeat bounded concurrency (HEARTBEAT_CONCURRENCY)
 * - Redis fault during heartbeat → 1013 close
 * - HTTP auth middleware → 503 on Redis fault
 * - HTTP refresh → 503 on Redis fault
 */

import { describe, it, expect, afterEach, jest, beforeAll, afterAll } from '@jest/globals';
import { WebSocketServer, WebSocket } from 'ws';
import { randomUUID } from 'crypto';
import http from 'http';
import jwt from 'jsonwebtoken';
import express from 'express';
import request from 'supertest';
import {
  setupWebSocket,
  closeConnectionsByEvent,
  HEARTBEAT_CONCURRENCY,
} from '../services/websocket';
import type { TokenRevocationService, DecodedToken, RevocationResult } from '../services/authSession';
import { register } from '../services/metrics';
import { JWT_SECRET } from '../config/jwt';

// ---------------------------------------------------------------------------
// Guards
// ---------------------------------------------------------------------------

const describeRedis = process.env.CRM_REDIS_AVAILABLE === '1' ? describe : describe.skip;

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

function makeToken(overrides: Partial<DecodedToken> = {}): string {
  const now = Math.floor(Date.now() / 1000);
  return jwt.sign(
    {
      jti: randomUUID(), sid: randomUUID(), sub: randomUUID(),
      tenantId: randomUUID(), type: 'access', uv: 0,
      sexp: now + 3600, iat: now, exp: now + 3600,
      ...overrides,
    },
    JWT_SECRET,
    { algorithm: 'HS256' },
  );
}

function onClose(ws: WebSocket): Promise<{ code: number; reason: string }> {
  return new Promise((resolve) => {
    ws.once('close', (code, reason) =>
      resolve({ code, reason: reason?.toString() ?? '' }),
    );
  });
}

async function getCounterValue(name: string, labels: Record<string, string>): Promise<number> {
  const metricsJson = await register.getMetricsAsJSON();
  const metric = metricsJson.find((m: any) => m.name === name);
  if (!metric || !('values' in metric)) return 0;
  const match = (metric.values as any[]).find((v: any) =>
    Object.entries(labels).every(([k, val]) => String(v.labels[k]) === val),
  );
  return match?.value ?? 0;
}

function createWss(): Promise<{ wss: WebSocketServer; port: number; close: () => Promise<void> }> {
  return new Promise((resolve) => {
    const server = http.createServer();
    const wss = new WebSocketServer({ server });
    server.listen(0, () => {
      const port = (server.address() as { port: number }).port;
      resolve({
        wss, port,
        close: () => new Promise<void>((r) => wss.close(() => server.close(() => r()))),
      });
    });
  });
}

// ---------------------------------------------------------------------------
// Tests
// ---------------------------------------------------------------------------

describe('B4 WebSocket revocation', () => {
  const sockets: WebSocket[] = [];

  afterEach(() => {
    jest.useRealTimers();
    sockets.forEach((s) => { try { s.terminate(); } catch { /* */ } });
    sockets.length = 0;
  });

  // -------------------------------------------------------------------------
  // B4-1: closeConnectionsByEvent closes matching jti socket with 4401
  // -------------------------------------------------------------------------
  it('closeConnectionsByEvent closes matching jti socket with 4401', async () => {
    const { wss, port, close } = await createWss();
    try {
      const mockService = {
        checkRevoked: jest.fn(async (): Promise<RevocationResult> => ({ revoked: false })),
        isSubscriberReady: jest.fn(() => true),
        shutdown: jest.fn(),
      } as unknown as TokenRevocationService;

      setupWebSocket(wss, mockService);

      const token = makeToken();
      const decoded = jwt.decode(token) as Record<string, unknown>;

      const ws = new WebSocket(`ws://localhost:${port}/?token=${token}`);
      await new Promise<void>((resolve, reject) => {
        ws.once('open', () => resolve());
        ws.once('error', reject);
      });
      sockets.push(ws);

      await new Promise((r) => setTimeout(r, 300));
      expect((mockService.checkRevoked as jest.Mock).mock.calls.length).toBeGreaterThanOrEqual(1);

      const closePromise = onClose(ws);
      closeConnectionsByEvent({
        type: 'jti',
        tenantId: decoded.tenantId as string,
        id: decoded.jti as string,
      });

      const closed = await closePromise;
      expect(closed.code).toBe(4401);
    } finally {
      await close();
    }
  }, 15000);

  // -------------------------------------------------------------------------
  // B4-2: Heartbeat catches revoked token (Pub/Sub miss)
  // -------------------------------------------------------------------------
  it('heartbeat closes socket when token becomes revoked between heartbeats', async () => {
    const { wss, port, close } = await createWss();
    try {
      const revokedSet = new Set<string>();
      const mockService = {
        checkRevoked: jest.fn(async (t: DecodedToken): Promise<RevocationResult> =>
          revokedSet.has(t.jti) ? { revoked: true, reason: 'jti' } : { revoked: false }),
        isSubscriberReady: jest.fn(() => true),
        shutdown: jest.fn(),
      } as unknown as TokenRevocationService;

      // Use a 100ms heartbeat for fast testing
      setupWebSocket(wss, mockService, { heartbeatIntervalMs: 100 });

      const jti = randomUUID();
      const token = makeToken({ jti });

      const ws = new WebSocket(`ws://localhost:${port}/?token=${token}`);
      await new Promise<void>((r) => ws.once('open', () => r()));
      sockets.push(ws);
      await new Promise((r) => setTimeout(r, 50));
      expect(ws.readyState).toBe(WebSocket.OPEN);

      // Token becomes revoked (Pub/Sub missed)
      revokedSet.add(jti);
      const closePromise = onClose(ws);

      // Wait for at least one heartbeat cycle
      await new Promise((r) => setTimeout(r, 300));

      const closed = await closePromise;
      expect(closed.code).toBe(4401);
    } finally {
      await close();
    }
  }, 10000);

  // -------------------------------------------------------------------------
  // B4-3: Heartbeat overlap_prevented metric
  // -------------------------------------------------------------------------
  it('heartbeat skips cycle when previous is still running (overlap_prevented)', async () => {
    const { wss, port, close } = await createWss();
    try {
      const baselineOverlap = await getCounterValue('websocket_auth_heartbeat_total', { result: 'overlap_prevented' });

      // Block ALL checkRevoked calls until released — this ensures
      // the entire heartbeat's for-loop is still running when the
      // second setInterval fires.
      let releaseAll: (() => void) | undefined;
      const blocked = new Promise<void>((r) => { releaseAll = r; });

      const blockingService = {
        checkRevoked: jest.fn(async () => {
          await blocked;
          return { revoked: false };
        }),
        isSubscriberReady: jest.fn(() => true),
        shutdown: jest.fn(),
      } as unknown as TokenRevocationService;

      setupWebSocket(wss, blockingService, { heartbeatIntervalMs: 100 });

      const ws = new WebSocket(`ws://localhost:${port}/?token=${makeToken()}`);
      await new Promise<void>((r) => ws.once('open', () => r()));
      sockets.push(ws);

      // Wait for 2 heartbeat cycles:
      // t=100ms: HB1 fires, sets heartbeatRunning=true, blocks on checkRevoked
      // t=200ms: HB2 fires, sees heartbeatRunning=true → overlap_prevented
      await new Promise((r) => setTimeout(r, 400));

      // Release all blocked heartbeats
      releaseAll?.();
      await new Promise((r) => setTimeout(r, 200));

      const afterOverlap = await getCounterValue('websocket_auth_heartbeat_total', { result: 'overlap_prevented' });
      expect(afterOverlap).toBeGreaterThan(baselineOverlap);
    } finally {
      sockets.forEach((s) => { try { s.terminate(); } catch { /* */ } });
      sockets.length = 0;
      await close();
    }
  }, 10000);

  // -------------------------------------------------------------------------
  // B4-4: Bounded concurrency
  // -------------------------------------------------------------------------
  it('heartbeat processes sockets in batches bounded by HEARTBEAT_CONCURRENCY', async () => {
    const { wss, port, close } = await createWss();
    try {
      expect(HEARTBEAT_CONCURRENCY).toBe(25);

      const maxConcurrent = { value: 0 };
      let inflight = 0;

      const trackingService = {
        checkRevoked: jest.fn(async () => {
          inflight++;
          maxConcurrent.value = Math.max(maxConcurrent.value, inflight);
          await Promise.resolve();
          inflight--;
          return { revoked: false };
        }),
        isSubscriberReady: jest.fn(() => true),
        shutdown: jest.fn(),
      } as unknown as TokenRevocationService;

      setupWebSocket(wss, trackingService, { heartbeatIntervalMs: 200 });

      // Connect 30 sockets
      const wsPromises: Promise<WebSocket>[] = [];
      for (let i = 0; i < 30; i++) {
        const ws = new WebSocket(`ws://localhost:${port}/?token=${makeToken()}`);
        wsPromises.push(new Promise<void>((r) => ws.once('open', () => r())).then(() => ws));
      }
      const wsList = await Promise.all(wsPromises);
      wsList.forEach((ws) => sockets.push(ws));
      await new Promise((r) => setTimeout(r, 100));

      // Wait for heartbeat to fire and process all sockets
      await new Promise((r) => setTimeout(r, 500));

      expect(maxConcurrent.value).toBeLessThanOrEqual(HEARTBEAT_CONCURRENCY);
      expect(maxConcurrent.value).toBeGreaterThan(0);
    } finally {
      sockets.forEach((s) => { try { s.terminate(); } catch { /* */ } });
      sockets.length = 0;
      await close();
    }
  }, 15000);

  // -------------------------------------------------------------------------
  // B4-5: Redis fault during heartbeat → 1013
  // -------------------------------------------------------------------------
  it('heartbeat closes socket with 1013 when Redis check throws', async () => {
    const { wss, port, close } = await createWss();
    try {
      let callCount = 0;
      const failService = {
        checkRevoked: jest.fn(async () => {
          callCount++;
          if (callCount > 1) throw new Error('Redis unavailable');
          return { revoked: false };
        }),
        isSubscriberReady: jest.fn(() => true),
        shutdown: jest.fn(),
      } as unknown as TokenRevocationService;

      setupWebSocket(wss, failService, { heartbeatIntervalMs: 100 });

      const ws = new WebSocket(`ws://localhost:${port}/?token=${makeToken()}`);
      await new Promise<void>((r) => ws.once('open', () => r()));
      sockets.push(ws);
      await new Promise((r) => setTimeout(r, 50));

      const closePromise = onClose(ws);
      // Wait for heartbeat to fire and hit the Redis error
      await new Promise((r) => setTimeout(r, 300));

      const closed = await closePromise;
      expect(closed.code).toBe(1013);
    } finally {
      await close();
    }
  }, 10000);
});

// ---------------------------------------------------------------------------
// B4-6, B4-7, B4-8: Pub/Sub and subscriber tests (require real Redis)
// ---------------------------------------------------------------------------

describeRedis('B4 Pub/Sub and subscriber (real Redis)', () => {
  let client: import('ioredis').Redis;
  let sub: import('ioredis').Redis;

  beforeAll(async () => {
    const Redis = (await import('ioredis')).default;
    const REDIS_URL = process.env.REDIS_URL || 'redis://localhost:6379';
    client = new Redis(REDIS_URL, { lazyConnect: true, maxRetriesPerRequest: 1 });
    sub = client.duplicate();
    await client.connect();
    await sub.connect();
  });

  afterAll(async () => {
    client.disconnect();
    sub.disconnect();
  });

  it('malformed Pub/Sub event is rejected and increments rejected_schema metric', async () => {
    const { TokenRevocationService: TRS } = await import('../services/authSession');
    const svc = new TRS(client, sub.duplicate());
    try {
      const received: unknown[] = [];
      await svc.initSubscriber((e) => { received.push(e); });

      await client.publish('auth:revocation:events', JSON.stringify({ bogus: true }));
      await new Promise((r) => setTimeout(r, 500));

      expect(received.length).toBe(0);
      const val = await getCounterValue('auth_revocation_events_total', { result: 'rejected_schema' });
      expect(val).toBeGreaterThan(0);
    } finally {
      await svc.shutdown();
    }
  });

  it('oversized Pub/Sub event is rejected and increments rejected_oversize metric', async () => {
    const { TokenRevocationService: TRS } = await import('../services/authSession');
    const subCopy = client.duplicate();
    await subCopy.connect();
    const svc = new TRS(client, subCopy);
    try {
      const received: unknown[] = [];
      await svc.initSubscriber((e) => { received.push(e); });

      const big = JSON.stringify({
        version: 1, type: 'jti', tenantId: randomUUID(),
        id: randomUUID(), occurredAt: Date.now(), padding: 'x'.repeat(5000),
      });
      await client.publish('auth:revocation:events', big);
      await new Promise((r) => setTimeout(r, 500));

      expect(received.length).toBe(0);
      const val = await getCounterValue('auth_revocation_events_total', { result: 'rejected_oversize' });
      expect(val).toBeGreaterThan(0);
    } finally {
      await svc.shutdown();
      subCopy.disconnect();
    }
  });

  it('subscriber readiness transitions: ready → closed → ready on reconnect', async () => {
    const { TokenRevocationService: TRS } = await import('../services/authSession');
    const subCopy = client.duplicate();
    await subCopy.connect();
    const svc = new TRS(client, subCopy);
    try {
      await svc.initSubscriber(() => {});
      expect(svc.isSubscriberReady()).toBe(true);

      subCopy.disconnect();
      await new Promise((r) => setTimeout(r, 200));
      expect(svc.isSubscriberReady()).toBe(false);

      await subCopy.connect();
      await new Promise((r) => setTimeout(r, 200));
      expect(svc.isSubscriberReady()).toBe(true);
    } finally {
      await svc.shutdown();
      subCopy.disconnect();
    }
  });
});

// ---------------------------------------------------------------------------
// HTTP 503 on Redis fault
// ---------------------------------------------------------------------------

describe('HTTP auth → 503 on Redis fault', () => {
  it('protected request returns 503 when checkRevoked throws', async () => {
    const brokenService = {
      checkRevoked: jest.fn(async () => { throw new Error('Redis down'); }),
      isSubscriberReady: jest.fn(() => true),
      shutdown: jest.fn(),
    } as unknown as TokenRevocationService;

    const { createAuthMiddleware } = await import('../middleware/auth');
    const app = express();
    app.use(createAuthMiddleware(brokenService));
    app.get('/test', (_req, res) => res.json({ ok: true }));

    const token = makeToken();
    const res = await request(app)
      .get('/test')
      .set('Authorization', `Bearer ${token}`);

    expect(res.status).toBe(503);
    expect(res.body.error?.code).toBe('AUTH_DEPENDENCY_UNAVAILABLE');
  });

  // Refresh/logout 503 tests require createAuthRoutes which imports Prisma/Kafka.
  // These are covered by:
  // - redis_durability_integration.test.ts: consumeRefresh returns DEPENDENCY_ERROR
  // - nodb_security.test.ts: returns 503 when revocation check throws
  // - routes/auth.ts: all Redis error paths send 503 AUTH_DEPENDENCY_UNAVAILABLE
});

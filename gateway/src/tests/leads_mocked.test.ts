import { describe, it, expect, beforeEach, jest } from '@jest/globals';
import jwt from 'jsonwebtoken';
import request from 'supertest';

/**
 * Mocked-DB tests for the leads route (P1-13).
 *
 * These exercise the leads CRUD validation + auth shape WITHOUT a real
 * database. `services/prisma` is replaced with an in-memory mock so
 * withTenantDb() never touches Postgres, and `services/kafka` is stubbed so
 * publishEvent is a no-op. The real Express route + middleware stack runs.
 *
 * Auth is bypassed at the header level where needed by signing a JWT locally;
 * for the unauthenticated path we simply omit the header and assert 401.
 */

const JWT_SECRET = process.env.JWT_SECRET || 'development-secret-change-in-production';

// In-memory lead store keyed by id.
const leadStore = new Map<string, any>();

const prismaMock = {
  $transaction: async (fn: any) => fn({
    $executeRaw: async () => {},
    lead: {
      findMany: async (opts: any) => {
        const items = Array.from(leadStore.values())
          .filter((l) => l.tenantId === opts.where.tenantId)
          .filter((l) => (opts.where.status ? l.status === opts.where.status : true));
        items.sort((a, b) => (a.createdAt < b.createdAt ? 1 : -1));
        const skip = opts.skip || 0;
        return items.slice(skip, skip + (opts.take || 20));
      },
      count: async (opts: any) =>
        Array.from(leadStore.values()).filter((l) => l.tenantId === opts.where.tenantId).length,
      findFirst: async (opts: any) =>
        Array.from(leadStore.values()).find(
          (l) => l.id === opts.where.id && l.tenantId === opts.where.tenantId
        ) || null,
      create: async (opts: any) => {
        leadStore.set(opts.data.id, opts.data);
        return opts.data;
      },
      update: async (opts: any) => {
        const id = opts.where.id;
        const existing = leadStore.get(id);
        const merged = { ...existing, ...opts.data };
        leadStore.set(id, merged);
        return merged;
      },
      delete: async (opts: any) => {
        leadStore.delete(opts.where.id);
        return opts.where;
      },
    },
  }),
};

jest.mock('../services/prisma', () => ({
  __esModule: true,
  prisma: prismaMock,
  withTenantDb: async (tenantId: string, fn: any) => prismaMock.$transaction(fn),
}));

jest.mock('../services/kafka', () => ({
  __esModule: true,
  publishEvent: jest.fn(async () => undefined) as any,
  TOPICS: { LEADS_CREATED: 'leads.created', LEADS_EVENTS: 'leads.events' },
  kafkaProducer: { connect: jest.fn(), disconnect: jest.fn(), send: jest.fn(async () => undefined) as any },
  kafkaClient: { admin: () => ({ connect: jest.fn(), disconnect: jest.fn() }) },
}));

// Mock Redis so the auth blacklist check resolves (token not revoked). This keeps
// the real authMiddleware + JWT verification in play without a live Redis.
jest.mock('../services/redis', () => {
  const store = new Map<string, string>();
  const client = {
    get: jest.fn(async (k: string) => (store.has(k) ? store.get(k) : null)),
    set: jest.fn(async (k: string, v: string) => { store.set(k, v); return 'OK'; }),
    setex: jest.fn(async (k: string, _ttl: number, v: string) => { store.set(k, v); return 'OK'; }),
    del: jest.fn(async (k: string) => { store.delete(k); return 1; }),
    exists: jest.fn(async (k: string) => (store.has(k) ? 1 : 0)),
    ping: jest.fn(async () => 'PONG'),
    incr: jest.fn(async () => 1),
    expire: jest.fn(async () => 1),
    keys: jest.fn(async () => []),
    on: jest.fn(),
  };
  return {
    __esModule: true,
    redisClient: client,
    cache: { get: client.get, set: client.set, del: client.del, exists: client.exists },
    tenantKey: (t: string, k: string) => `tenant:${t}:${k}`,
  };
});

import app from '../index';

function signToken(overrides: Record<string, any> = {}): string {
  return jwt.sign(
    {
      sub: 'user-1',
      tenantId: '00000000-0000-0000-0000-000000000000',
      email: 'u@example.com',
      roles: ['sales_rep'],
      ...overrides,
    },
    JWT_SECRET,
    { expiresIn: '1h' }
  );
}

describe('Leads API (mocked DB, no live Postgres)', () => {
  beforeEach(() => {
    leadStore.clear();
    // eslint-disable-next-line @typescript-eslint/no-var-requires
    (require('../services/kafka').publishEvent as any).mockClear?.();
  });

  it('POST /api/v1/leads returns 401 without auth', async () => {
    const resp = await request(app).post('/api/v1/leads').send({ name: 'X' });
    // Fail-closed auth (redis unavailable in test env) -> 401.
    expect(resp.status).toBe(401);
  });

  it('POST /api/v1/leads rejects invalid payload (missing name) with 400', async () => {
    const resp = await request(app)
      .post('/api/v1/leads')
      .set('Authorization', `Bearer ${signToken()}`)
      .send({ email: 'not-an-email' });
    expect(resp.status).toBe(400);
    expect(resp.body.error.code).toBe('BAD_REQUEST');
    expect(JSON.stringify(resp.body.error.details)).toMatch(/name/i);
  });

  it('POST /api/v1/leads rejects invalid email with 400', async () => {
    const resp = await request(app)
      .post('/api/v1/leads')
      .set('Authorization', `Bearer ${signToken()}`)
      .send({ name: 'Jane', email: 'not-an-email' });
    expect(resp.status).toBe(400);
    expect(JSON.stringify(resp.body.error.details)).toMatch(/email/i);
  });

  it('POST /api/v1/leads creates a lead with valid input (mocked)', async () => {
    const resp = await request(app)
      .post('/api/v1/leads')
      .set('Authorization', `Bearer ${signToken()}`)
      .send({ name: 'Jane Doe', email: 'jane@example.com', company: 'Acme', source: 'website' })
      .expect(201);

    expect(resp.body.name).toBe('Jane Doe');
    expect(resp.body.status).toBe('new');
    expect(resp.body.source).toBe('website');
    expect(resp.body.id).toBeTruthy();
    // The lead was persisted in the in-memory store.
    expect(leadStore.has(resp.body.id)).toBe(true);
  });

  it('GET /api/v1/leads lists created leads with pagination shape', async () => {
    await request(app)
      .post('/api/v1/leads')
      .set('Authorization', `Bearer ${signToken()}`)
      .send({ name: 'L1' });
    await request(app)
      .post('/api/v1/leads')
      .set('Authorization', `Bearer ${signToken()}`)
      .send({ name: 'L2' });

    const resp = await request(app)
      .get('/api/v1/leads')
      .set('Authorization', `Bearer ${signToken()}`)
      .expect(200);

    expect(Array.isArray(resp.body.data)).toBe(true);
    expect(resp.body.data.length).toBeGreaterThanOrEqual(2);
    expect(resp.body.pagination).toBeDefined();
    expect(resp.body.pagination).toHaveProperty('total');
    expect(resp.body.pagination).toHaveProperty('totalPages');
  });

  it('GET /api/v1/leads/:id returns 404 for unknown lead', async () => {
    const resp = await request(app)
      .get('/api/v1/leads/00000000-0000-0000-0000-000000000000')
      .set('Authorization', `Bearer ${signToken()}`);
    expect(resp.status).toBe(404);
    expect(resp.body.error.code).toBe('NOT_FOUND');
  });

  it('PATCH /api/v1/leads/:id rejects invalid status with 400', async () => {
    // Create a lead first.
    const created = await request(app)
      .post('/api/v1/leads')
      .set('Authorization', `Bearer ${signToken()}`)
      .send({ name: 'Stat' })
      .expect(201);

    const resp = await request(app)
      .patch(`/api/v1/leads/${created.body.id}`)
      .set('Authorization', `Bearer ${signToken()}`)
      .send({ status: 'bogus' });
    expect(resp.status).toBe(400);
    expect(JSON.stringify(resp.body.error.details)).toMatch(/status/i);
  });

  it('PATCH /api/v1/leads/:id rejects out-of-range score with 400', async () => {
    const created = await request(app)
      .post('/api/v1/leads')
      .set('Authorization', `Bearer ${signToken()}`)
      .send({ name: 'Score' })
      .expect(201);

    const resp = await request(app)
      .patch(`/api/v1/leads/${created.body.id}`)
      .set('Authorization', `Bearer ${signToken()}`)
      .send({ score: 999 });
    expect(resp.status).toBe(400);
    expect(JSON.stringify(resp.body.error.details)).toMatch(/score/i);
  });
});

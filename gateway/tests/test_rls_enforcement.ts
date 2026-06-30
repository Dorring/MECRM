import { afterAll, beforeAll, describe, expect, it } from '@jest/globals';
import request from 'supertest';
import jwt from 'jsonwebtoken';
import { createServer } from 'http';
import { WebSocketServer } from 'ws';
import WebSocket from 'ws';

import app from '../src/index';
import { prisma, withTenantDb } from '../src/services/prisma';
import { setupWebSocket } from '../src/services/websocket';
import { cache, tenantKey } from '../src/services/redis';

const JWT_SECRET = process.env.JWT_SECRET || 'development-secret-change-in-production';

const describeDb = process.env.CRM_DB_AVAILABLE === '1' ? describe : describe.skip;

const tenantA = '11111111-1111-4111-8111-111111111111';
const tenantB = '22222222-2222-4222-8222-222222222222';
const customerA = 'aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa';
const customerB = 'bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb';
const tenantASlug = `tenant-a-${tenantA.slice(0, 8)}`;
const tenantBSlug = `tenant-b-${tenantB.slice(0, 8)}`;

function signToken(payload: Record<string, any>): string {
  return jwt.sign(payload, JWT_SECRET, { expiresIn: '1h' });
}

describeDb('Tenant isolation proof (gateway)', () => {
  beforeAll(async () => {
    await prisma.tenant.deleteMany({ where: { id: { in: [tenantA, tenantB] } } });
    await prisma.tenant.deleteMany({ where: { slug: { in: [tenantASlug, tenantBSlug] } } });

    await prisma.tenant.create({
      data: { id: tenantA, name: 'Tenant A', slug: tenantASlug },
    });
    await prisma.tenant.create({
      data: { id: tenantB, name: 'Tenant B', slug: tenantBSlug },
    });

    await withTenantDb(tenantA, async (db) => {
      await db.customer.deleteMany({ where: { tenantId: tenantA } });
    });
    await withTenantDb(tenantB, async (db) => {
      await db.customer.deleteMany({ where: { tenantId: tenantB } });
    });

    await withTenantDb(tenantA, async (db) => {
      await db.customer.create({
        data: {
          id: customerA,
          tenantId: tenantA,
          name: 'Customer A',
          status: 'active',
          lifetimeValue: 0,
        },
      });
    });

    await withTenantDb(tenantB, async (db) => {
      await db.customer.create({
        data: {
          id: customerB,
          tenantId: tenantB,
          name: 'Customer B',
          status: 'active',
          lifetimeValue: 0,
        },
      });
    });
  });

  afterAll(async () => {
    await withTenantDb(tenantA, async (db) => {
      await db.customer.deleteMany({ where: { tenantId: tenantA } });
    });
    await withTenantDb(tenantB, async (db) => {
      await db.customer.deleteMany({ where: { tenantId: tenantB } });
    });
    await prisma.tenant.deleteMany({ where: { id: { in: [tenantA, tenantB] } } });
  });

  it('blocks cross-tenant resource access (tenant A cannot read tenant B)', async () => {
    const tokenA = signToken({
      sub: 'user-a',
      tenant_id: tenantA,
      email: 'a@example.com',
      roles: ['admin'],
    });

    await request(app)
      .get(`/api/v1/customers/${customerB}`)
      .set('Authorization', `Bearer ${tokenA}`)
      .expect(404);
  });

  it('blocks tenant override for non-super-admin (JWT tampering attempt)', async () => {
    const tokenA = signToken({
      sub: 'user-a',
      tenant_id: tenantA,
      email: 'a@example.com',
      roles: ['admin'],
    });

    await request(app)
      .get(`/api/v1/customers/${customerA}`)
      .set('Authorization', `Bearer ${tokenA}`)
      .set('x-tenant-id', tenantB)
      .expect(403);
  });

  it('allows super_admin cross-tenant READ only (OPA enforced)', async () => {
    const superToken = signToken({
      sub: 'super-admin',
      tenant_id: tenantA,
      email: 'sa@example.com',
      roles: ['super_admin'],
    });

    const res = await request(app)
      .get(`/api/v1/customers/${customerB}`)
      .set('Authorization', `Bearer ${superToken}`)
      .set('x-tenant-id', tenantB)
      .expect(200);

    expect(res.body.id).toBe(customerB);
    expect(res.body.tenantId).toBe(tenantB);
  });

  it('denies super_admin cross-tenant WRITE (OPA enforced)', async () => {
    const superToken = signToken({
      sub: 'super-admin',
      tenant_id: tenantA,
      email: 'sa@example.com',
      roles: ['super_admin'],
    });

    await request(app)
      .patch(`/api/v1/customers/${customerB}`)
      .set('Authorization', `Bearer ${superToken}`)
      .set('x-tenant-id', tenantB)
      .send({ name: 'Should not change' })
      .expect(403);
  });

  it('prevents cross-tenant cache key collisions (tenant-scoped keys)', async () => {
    const key = 'customers:summary';
    const keyA = tenantKey(tenantA, key);
    const keyB = tenantKey(tenantB, key);

    await cache.del(keyA);
    await cache.del(keyB);

    await cache.set(keyA, { tenant: tenantA, value: 123 }, 60);

    const a = await cache.get<any>(keyA);
    const b = await cache.get<any>(keyB);

    expect(a).toEqual({ tenant: tenantA, value: 123 });
    expect(b).toBeNull();
  });

  it('blocks websocket cross-tenant channel subscription', async () => {
    const tokenA = signToken({
      sub: 'user-a',
      tenant_id: tenantA,
      email: 'a@example.com',
      roles: ['admin'],
    });

    const server = createServer(app);
    const wss = new WebSocketServer({ server, path: '/ws' });
    setupWebSocket(wss);

    const port = await new Promise<number>((resolve) => {
      server.listen(0, () => {
        resolve((server.address() as any).port as number);
      });
    });

    const ws = new WebSocket(`ws://127.0.0.1:${port}/ws?token=${encodeURIComponent(tokenA)}`);

    const first = await new Promise<any>((resolve, reject) => {
      const t = setTimeout(() => reject(new Error('timeout')), 5000);
      ws.once('message', (data) => {
        clearTimeout(t);
        resolve(JSON.parse(data.toString()));
      });
      ws.once('error', reject);
    });
    expect(first.type).toBe('connected');

    ws.send(JSON.stringify({ type: 'subscribe', payload: { topic: `tenant:${tenantB}:updates` } }));

    const denial = await new Promise<any>((resolve, reject) => {
      const t = setTimeout(() => reject(new Error('timeout')), 5000);
      ws.once('message', (data) => {
        clearTimeout(t);
        resolve(JSON.parse(data.toString()));
      });
      ws.once('error', reject);
    });
    expect(denial.type).toBe('error');
    expect(denial.payload.code).toBe('CROSS_TENANT_SUBSCRIBE_DENY');

    ws.close();
    await new Promise<void>((resolve) => server.close(() => resolve()));
  });
});


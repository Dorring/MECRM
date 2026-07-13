import { describe, it, expect, beforeAll } from '@jest/globals';
import request from 'supertest';
import { uuidv4 } from '../utils/uuid';
import { randomUUID } from 'crypto';
import app from '../index';
import { withTenantDb } from '../services/prisma';
import { generateToken } from '../middleware/auth';

const describeDb = process.env.CRM_DB_AVAILABLE === '1' ? describe : describe.skip;

describeDb('Productivity API [requires DB]', () => {
  let adminToken: string;
  let tenantId: string;
  let adminUserId: string;
  let user2Id: string;
  let user2Token: string;

  beforeAll(async () => {
    const registerResponse = await request(app)
      .post('/api/v1/auth/register')
      .send({
        tenantName: 'Productivity Test Tenant',
        tenantSlug: `prod-${Date.now()}`,
        email: `prod-admin-${Date.now()}@example.com`,
        password: 'SecurePass123!',
        name: 'Productivity Admin',
      })
      .expect(201);

    adminToken = registerResponse.body.accessToken;
    tenantId = registerResponse.body.user.tenant.id;
    adminUserId = registerResponse.body.user.id;

    user2Id = uuidv4();
    await withTenantDb(tenantId, async (db) => {
      await db.user.create({
        data: {
          id: user2Id,
          tenantId,
          email: `prod-user-${Date.now()}@example.com`,
          name: 'Productivity User',
          status: 'active',
          passwordHash: null,
        },
      });
    });

    const now = Math.floor(Date.now() / 1000);
    user2Token = generateToken({
      sub: user2Id,
      tenantId,
      sid: randomUUID(),
      uv: 0,
      sexp: now + 86400 * 7,
      email: 'prod-user@example.com',
      roles: ['sales_rep'],
    });
  });

  it('GET /api/v1/productivity/proposals returns pending proposals scoped by user', async () => {
    const p1 = uuidv4();
    const p2 = uuidv4();

    await withTenantDb(tenantId, async (db) => {
      await db.productivityProposal.create({
        data: {
          id: p1,
          tenantId,
          userId: adminUserId,
          actionType: 'reminder',
          targetEntity: 'lead',
          targetId: uuidv4(),
          priority: 'medium',
          justification: 'Test justification',
          drafts: { email: { subject: 'S', body: 'B' } },
          status: 'pending',
          dedupeKey: uuidv4(),
          signalType: 'lead_idle',
          signal: { type: 'lead_idle' },
        },
      });
      await db.productivityProposal.create({
        data: {
          id: p2,
          tenantId,
          userId: user2Id,
          actionType: 'followup',
          targetEntity: 'ticket',
          targetId: uuidv4(),
          priority: 'high',
          justification: 'Another justification',
          drafts: { whatsapp: { message: 'Hi' } },
          status: 'pending',
          dedupeKey: uuidv4(),
          signalType: 'ticket_aging',
          signal: { type: 'ticket_aging' },
        },
      });
    });

    const respUser2 = await request(app)
      .get('/api/v1/productivity/proposals?status=pending')
      .set('Authorization', `Bearer ${user2Token}`)
      .expect(200);

    expect(Array.isArray(respUser2.body.data)).toBe(true);
    expect(respUser2.body.data.length).toBe(1);
    expect(respUser2.body.data[0].userId).toBe(user2Id);

    const respAdmin = await request(app)
      .get('/api/v1/productivity/proposals?status=pending')
      .set('Authorization', `Bearer ${adminToken}`)
      .expect(200);

    expect(respAdmin.body.data.length).toBeGreaterThanOrEqual(2);
  });

  it('POST /api/v1/productivity/proposals/:id/decide allows owner and denies others', async () => {
    const proposalId = uuidv4();

    await withTenantDb(tenantId, async (db) => {
      await db.productivityProposal.create({
        data: {
          id: proposalId,
          tenantId,
          userId: user2Id,
          actionType: 'reminder',
          targetEntity: 'lead',
          targetId: uuidv4(),
          priority: 'low',
          justification: 'Decision test',
          drafts: {},
          status: 'pending',
          dedupeKey: uuidv4(),
          signalType: 'lead_idle',
          signal: { type: 'lead_idle' },
        },
      });
    });

    await request(app)
      .post(`/api/v1/productivity/proposals/${proposalId}/decide`)
      .set('Authorization', `Bearer ${adminToken}`)
      .send({ decision: 'approved' })
      .expect(200);

    const otherId = uuidv4();
    await withTenantDb(tenantId, async (db) => {
      await db.user.create({
        data: { id: otherId, tenantId, email: `prod-other-${Date.now()}@example.com`, name: 'Other', status: 'active', passwordHash: null },
      });
    });
    const now2 = Math.floor(Date.now() / 1000);
    const otherToken = generateToken({ sub: otherId, tenantId, sid: randomUUID(), uv: 0, sexp: now2 + 86400 * 7, email: 'x@example.com', roles: ['sales_rep'] });

    const pOther = uuidv4();
    await withTenantDb(tenantId, async (db) => {
      await db.productivityProposal.create({
        data: {
          id: pOther,
          tenantId,
          userId: user2Id,
          actionType: 'followup',
          targetEntity: 'ticket',
          targetId: uuidv4(),
          priority: 'high',
          justification: 'Owner only',
          drafts: {},
          status: 'pending',
          dedupeKey: uuidv4(),
          signalType: 'ticket_aging',
          signal: { type: 'ticket_aging' },
        },
      });
    });

    await request(app)
      .post(`/api/v1/productivity/proposals/${pOther}/decide`)
      .set('Authorization', `Bearer ${otherToken}`)
      .send({ decision: 'rejected', reason: 'no' })
      .expect(404);
  });
});


import { describe, it, expect, beforeAll } from '@jest/globals';
import request from 'supertest';
import { v4 as uuidv4 } from 'uuid';
import app from '../index';
import { withTenantDb } from '../services/prisma';

const describeDb = process.env.CRM_DB_AVAILABLE === '1' ? describe : describe.skip;

describeDb('Predictions API', () => {
  let adminToken: string;
  let tenantId: string;
  let customerId: string;

  beforeAll(async () => {
    const registerResponse = await request(app)
      .post('/api/v1/auth/register')
      .send({
        tenantName: 'Predictions Test Tenant',
        tenantSlug: `pred-${Date.now()}`,
        email: `pred-admin-${Date.now()}@example.com`,
        password: 'SecurePass123!',
        name: 'Predictions Admin',
      })
      .expect(201);

    adminToken = registerResponse.body.accessToken;
    tenantId = registerResponse.body.user.tenant.id;

    customerId = uuidv4();
    await withTenantDb(tenantId, async (db) => {
      await db.customer.create({
        data: {
          id: customerId,
          tenantId,
          name: 'Prediction Customer',
          createdBy: registerResponse.body.user.id,
          metadata: {},
        },
      });

      await db.prediction.create({
        data: {
          tenantId,
          entityType: 'customer',
          entityId: customerId,
          predictionType: 'churn',
          probability: 0.2,
          riskLevel: 'green',
          explanation: 'Old',
          features: {},
          modelVersion: 'heuristic_v1',
          createdAt: new Date(Date.now() - 60_000),
        },
      });

      await db.prediction.create({
        data: {
          tenantId,
          entityType: 'customer',
          entityId: customerId,
          predictionType: 'churn',
          probability: 0.8,
          riskLevel: 'red',
          explanation: 'New',
          features: {},
          modelVersion: 'heuristic_v1',
          createdAt: new Date(),
        },
      });
    });
  });

  it('GET /api/v1/predictions/latest returns latest per prediction type', async () => {
    const resp = await request(app)
      .get(`/api/v1/predictions/latest?entityType=customer&entityIds=${customerId}`)
      .set('Authorization', `Bearer ${adminToken}`)
      .expect(200);

    expect(resp.body.data[customerId]).toBeDefined();
    expect(resp.body.data[customerId].churn).toBeDefined();
    expect(resp.body.data[customerId].churn.explanation).toBe('New');
    expect(resp.body.data[customerId].churn.riskLevel).toBe('red');
  });
});


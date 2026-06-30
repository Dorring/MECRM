import { describe, it, expect, beforeAll, jest } from '@jest/globals';
import request from 'supertest';
import app from '../index';

jest.mock('axios', () => ({
  __esModule: true,
  default: {
    post: jest.fn(),
  },
}));

import axios from 'axios';

const describeDb = process.env.CRM_DB_AVAILABLE === '1' ? describe : describe.skip;

describeDb('Audit APIs', () => {
  let accessToken: string;

  beforeAll(async () => {
    const registerResponse = await request(app)
      .post('/api/v1/auth/register')
      .send({
        tenantName: 'Audit Test Tenant',
        tenantSlug: `audit-${Date.now()}`,
        email: `audit-${Date.now()}@example.com`,
        password: 'SecurePass123!',
        name: 'Audit Test User',
      })
      .expect(201);

    accessToken = registerResponse.body.accessToken;
  });

  it('GET /api/v1/audit/policies returns policy bundle summary', async () => {
    const resp = await request(app)
      .get('/api/v1/audit/policies?format=summary')
      .set('Authorization', `Bearer ${accessToken}`)
      .expect(200);

    expect(resp.body).toHaveProperty('data');
    expect(Array.isArray(resp.body.data)).toBe(true);
  });

  it('POST /api/v1/audit/search proxies to agents', async () => {
    (axios as any).post.mockResolvedValue({
      data: {
        hits: [
          {
            decision_id: '00000000-0000-0000-0000-000000000000',
            tenant_id: '00000000-0000-0000-0000-000000000000',
            agent_name: 'automation-executor-agent',
            action_type: 'crm.automation.executed',
            risk_level: 'LOW',
            status: 'executed',
            created_at: '2026-01-01T00:00:00Z',
            score: 0.9,
            snippet: 'automation executed',
          },
        ],
      },
    });

    const resp = await request(app)
      .post('/api/v1/audit/search')
      .set('Authorization', `Bearer ${accessToken}`)
      .send({ query: 'automation executed last week' })
      .expect(200);

    expect(Array.isArray(resp.body.hits)).toBe(true);
    expect(resp.body.hits[0].agent_name).toBe('automation-executor-agent');
  });
});


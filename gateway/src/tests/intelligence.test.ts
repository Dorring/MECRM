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

describeDb('Intelligence Search API [requires DB]', () => {
  let accessToken: string;

  beforeAll(async () => {
    const registerResponse = await request(app)
      .post('/api/v1/auth/register')
      .send({
        tenantName: 'Intelligence Test Tenant',
        tenantSlug: `intel-${Date.now()}`,
        email: `intel-${Date.now()}@example.com`,
        password: 'SecurePass123!',
        name: 'Intelligence Test User',
      })
      .expect(201);

    accessToken = registerResponse.body.accessToken;
  });

  it('POST /api/v1/intelligence/query proxies to agents', async () => {
    (axios as any).post.mockResolvedValue({
      data: {
        search_id: 'search-1',
        intent: { entity: 'lead', action: 'search', filters: {}, confidence: 0.7 },
        results: [],
        suggestions: [],
        explainability: {},
      },
    });

    const response = await request(app)
      .post('/api/v1/intelligence/query')
      .set('Authorization', `Bearer ${accessToken}`)
      .send({ query: 'acme leads', module: '/leads' })
      .expect(200);

    expect(response.body.search_id).toBe('search-1');
    expect(response.body.intent.entity).toBe('lead');
    expect(Array.isArray(response.body.results)).toBe(true);
  });

  it('POST /api/v1/intelligence/query validates missing query', async () => {
    await request(app)
      .post('/api/v1/intelligence/query')
      .set('Authorization', `Bearer ${accessToken}`)
      .send({})
      .expect(400);
  });
});


import { describe, it, expect } from '@jest/globals';
import request from 'supertest';
import app from '../index';

const describeDb = process.env.CRM_DB_AVAILABLE === '1' ? describe : describe.skip;

describeDb('Auth SQL injection guard', () => {
  it('rejects tenantSlug with illegal characters', async () => {
    const resp = await request(app)
      .post('/api/v1/auth/login')
      .send({
        email: 'someone@example.com',
        password: 'InvalidPass123',
        tenantSlug: `foo' OR 1=1 --`,
      });

    expect(resp.status).toBe(400);
    expect(resp.body?.message || resp.text).toMatch(/validation failed/i);
    expect(JSON.stringify(resp.body?.details || resp.body || '')).toMatch(/tenantSlug/i);
  });
});

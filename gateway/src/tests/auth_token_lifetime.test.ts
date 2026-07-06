import { describe, it, expect } from '@jest/globals';
import jwt from 'jsonwebtoken';
import {
  generateRefreshToken,
  generateToken,
} from '../middleware/auth';
import { validateDecodedToken } from '../services/authSession';

const baseParams = {
  sub: '00000000-0000-0000-0000-000000000001',
  tenantId: '00000000-0000-0000-0000-000000000002',
  sid: '00000000-0000-0000-0000-000000000003',
  uv: 0,
};

describe('JWT absolute session lifetime', () => {
  it('caps access token expiry at sexp', () => {
    const sexp = Math.floor(Date.now() / 1000) + 30;
    const token = generateToken({
      ...baseParams,
      sexp,
      email: 'user@example.com',
      roles: ['admin'],
    });
    const decoded = jwt.decode(token) as jwt.JwtPayload;
    expect(decoded.exp).toBeLessThanOrEqual(sexp);
  });

  it('caps refresh token expiry at sexp even with less than one day remaining', () => {
    const sexp = Math.floor(Date.now() / 1000) + 30;
    const token = generateRefreshToken({ ...baseParams, sexp });
    const decoded = jwt.decode(token) as jwt.JwtPayload;
    expect(decoded.exp).toBeLessThanOrEqual(sexp);
  });

  it('refuses to issue tokens for an expired session', () => {
    const sexp = Math.floor(Date.now() / 1000) - 1;
    expect(() =>
      generateRefreshToken({ ...baseParams, sexp }),
    ).toThrow(/expired session/i);
  });

  it('rejects a decoded token whose exp exceeds sexp', () => {
    const now = Math.floor(Date.now() / 1000);
    const result = validateDecodedToken({
      jti: '00000000-0000-0000-0000-000000000004',
      ...baseParams,
      type: 'access',
      iat: now,
      exp: now + 120,
      sexp: now + 60,
    });
    expect(result.valid).toBe(false);
    expect(result.error).toMatch(/session expiry/i);
  });
});

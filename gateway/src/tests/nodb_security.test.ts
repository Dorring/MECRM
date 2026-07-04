import { describe, it, expect, beforeEach, afterEach, jest } from '@jest/globals';
import { randomUUID } from 'crypto';

/**
 * Pure-logic regression tests that run WITHOUT a database, Redis, or Kafka.
 *
 * These cover security-sensitive behaviour that previously had zero coverage
 * because every test file was gated behind CRM_DB_AVAILABLE (see P1-13):
 *   1. resolveJwtSecret() production hardening (missing/short/insecure -> throw)
 *   2. errorHandler production sanitization (no stack/details leak)
 *   3. authMiddleware fail-closed when Redis blacklist check errors (401, no
 *      token disclosure)
 *
 * No real DB/Redis is touched. Redis is mocked where the auth middleware needs
 * it; the Prisma client is constructed lazily by importing app but is never
 * called by these code paths.
 */

// --- JWT secret resolution -------------------------------------------------

describe('resolveJwtSecret (config/jwt.ts)', () => {
  // resolveJwtSecret reads process.env at call time. The module also resolves
  // JWT_SECRET at load (line 88), so we must keep the env valid during require()
  // to avoid a throw at module load, then mutate the env and call the exported
  // resolveJwtSecret() directly (it re-reads process.env each invocation).
  const callResolve = (env: Record<string, string | undefined>) => {
    const prev = { ...process.env };
    // During require, supply a valid production secret so module load doesn't throw.
    const validSecret = 'a'.repeat(48);
    for (const k of Object.keys(env)) {
      if (env[k] === undefined) delete process.env[k];
      else (process.env as any)[k] = env[k];
    }
    // Temporarily set a valid secret + dev env for the require() to succeed.
    (process.env as any).NODE_ENV = 'development';
    (process.env as any).JWT_SECRET = validSecret;
    try {
      let mod: any;
      jest.isolateModules(() => {
        // eslint-disable-next-line @typescript-eslint/no-var-requires
        mod = require('../config/jwt');
      });
      // Now apply the env under test and call resolveJwtSecret() directly.
      for (const k of Object.keys(env)) {
        if (env[k] === undefined) delete process.env[k];
        else (process.env as any)[k] = env[k];
      }
      // JEST_WORKER_ID is always set under jest; isTest() checks it. For prod
      // tests we must clear it so isTest() is false AND NODE_ENV=production.
      if (env.JEST_WORKER_ID === undefined) delete process.env.JEST_WORKER_ID;
      try {
        return { secret: mod.resolveJwtSecret(), error: undefined };
      } catch (e) {
        return { secret: undefined, error: e as Error };
      }
    } finally {
      process.env = prev;
    }
  };

  it('throws in production when JWT_SECRET is missing', () => {
    const r = callResolve({ NODE_ENV: 'production', JWT_SECRET: undefined, JEST_WORKER_ID: undefined });
    expect(r.error).toBeInstanceOf(Error);
    expect(r.error!.message).toMatch(/JWT_SECRET is not set/i);
  });

  it('throws in production when JWT_SECRET is shorter than 32 bytes', () => {
    const r = callResolve({ NODE_ENV: 'production', JWT_SECRET: 'shortsecret', JEST_WORKER_ID: undefined });
    expect(r.error).toBeInstanceOf(Error);
    expect(r.error!.message).toMatch(/shorter than 32 bytes/i);
  });

  it('throws in production when JWT_SECRET is exactly the development fallback', () => {
    const r = callResolve({
      NODE_ENV: 'production',
      JWT_SECRET: 'development-secret-change-in-production',
      JEST_WORKER_ID: undefined,
    });
    expect(r.error).toBeInstanceOf(Error);
    expect(r.error!.message).toMatch(/known insecure default/i);
  });

  it('returns a strong secret unchanged in production', () => {
    const strong = 'x'.repeat(48); // >=32, not a known default
    const r = callResolve({ NODE_ENV: 'production', JWT_SECRET: strong, JEST_WORKER_ID: undefined });
    expect(r.error).toBeUndefined();
    expect(r.secret).toBe(strong);
  });

  it('falls back to a deterministic default in test when JWT_SECRET is unset', () => {
    const r = callResolve({ NODE_ENV: 'test', JWT_SECRET: undefined, JEST_WORKER_ID: '1' });
    expect(r.error).toBeUndefined();
    expect(r.secret).toBe('development-secret-change-in-production');
  });
});

// --- errorHandler production sanitization ----------------------------------

describe('errorHandler production sanitization', () => {
  let originalNodeEnv: string | undefined;

  beforeEach(() => {
    originalNodeEnv = process.env.NODE_ENV;
  });

  afterEach(() => {
    process.env.NODE_ENV = originalNodeEnv;
    jest.resetModules();
  });

  const callHandler = (env: string, err: any, statusCode?: number) => {
    process.env.NODE_ENV = env;
    // eslint-disable-next-line @typescript-eslint/no-var-requires
    const { errorHandler } = require('../middleware/errorHandler');
    const errObj = err instanceof Error ? err : Object.assign(new Error(err.message), err);
    if (statusCode !== undefined) (errObj as any).statusCode = statusCode;
    const req: any = {
      headers: { 'x-correlation-id': 'corr-123' },
      path: '/api/v1/leads',
      method: 'POST',
    };
    const res: any = {
      status: jest.fn(() => res),
      json: jest.fn(() => res),
    };
    const next: any = jest.fn();
    errorHandler(errObj, req, res, next);
    return res;
  };

  it('masks 500 errors in production and omits details', () => {
    const res = callHandler('production', { message: 'DB connection string leaked: postgres://user:pass@host' }, 500);
    expect(res.status).toHaveBeenCalledWith(500);
    const body = res.json.mock.calls[0][0];
    expect(body.error.message).toBe('Internal server error');
    expect(body.error.message).not.toMatch(/postgres:\/\//);
    expect(body.error.details).toBeUndefined();
    expect(body.error.correlationId).toBe('corr-123');
  });

  it('masks 502/503/504-style upstream errors in production and omits details', () => {
    const res = callHandler('production', { message: 'connect ECONNREFUSED 127.0.0.1:5432', code: 'DATABASE_ERROR' }, 503);
    expect(res.status).toHaveBeenCalledWith(503);
    const body = res.json.mock.calls[0][0];
    expect(body.error.message).toBe('Internal server error');
    expect(JSON.stringify(body)).not.toMatch(/127\.0\.0\.1|ECONNREFUSED|5432/);
    expect(body.error.details).toBeUndefined();
  });

  it('preserves non-500 messages in production (client errors)', () => {
    const res = callHandler('production', { message: 'Lead not found', code: 'NOT_FOUND' }, 404);
    const body = res.json.mock.calls[0][0];
    expect(body.error.message).toBe('Lead not found');
    expect(body.error.code).toBe('NOT_FOUND');
  });

  it('exposes details in non-production', () => {
    const res = callHandler('development', { message: 'boom', details: { field: 'email' } }, 400);
    const body = res.json.mock.calls[0][0];
    expect(body.error.message).toBe('boom');
    expect(body.error.details).toEqual({ field: 'email' });
  });

  it('defaults code to INTERNAL_ERROR when none provided', () => {
    const res = callHandler('production', { message: 'fail' }, 500);
    const body = res.json.mock.calls[0][0];
    expect(body.error.code).toBe('INTERNAL_ERROR');
  });
});

// --- authMiddleware fail-closed on Redis error -------------------

/* eslint-disable @typescript-eslint/no-var-requires */
describe('authMiddleware fail-closed on Redis error', () => {
  const JWT_SECRET = 'development-secret-change-in-production';

  function signValidToken(overrides: Record<string, unknown> = {}): string {
    const jwt = require('jsonwebtoken');
    const now = Math.floor(Date.now() / 1000);
    return jwt.sign(
      {
        jti: randomUUID(),
        sid: randomUUID(),
        sub: randomUUID(),
        tenantId: randomUUID(),
        email: 'test@example.com',
        roles: ['admin'],
        type: 'access',
        uv: 0,
        sexp: now + 86400 * 7,
        iat: now,
        exp: now + 3600,
        ...overrides,
      },
      JWT_SECRET,
      { algorithm: 'HS256' },
    );
  }

  it('returns 503 when revocation check throws (Redis unavailable), without disclosing token', async () => {
    const { TokenRevocationService } = require('../services/authSession');
    const mockService = new TokenRevocationService();
    mockService.checkRevoked = jest
      .fn<any>()
      .mockRejectedValue(new Error('ECONNREFUSED 127.0.0.1:6379'));

    const { createAuthMiddleware } = require('../middleware/auth');
    const authMiddleware = createAuthMiddleware(mockService);

    const token = signValidToken();
    const req: any = {
      headers: {
        authorization: `Bearer ${token}`,
        'x-correlation-id': 'corr-fail-closed',
      },
      method: 'GET',
      path: '/api/v1/leads',
    };
    const res: any = { status: jest.fn(() => res), json: jest.fn(() => res) };

    await authMiddleware(req, res, jest.fn());

    // ADR-002: Redis unavailable returns 503 AUTH_DEPENDENCY_UNAVAILABLE, not 401
    // Middleware writes directly to res (not via next/errorHandler)
    expect(res.status).toHaveBeenCalledWith(503);
    const body = res.json.mock.calls[0][0];
    expect(body.error.code).toBe('AUTH_DEPENDENCY_UNAVAILABLE');
    expect(JSON.stringify(body)).not.toMatch(/ECONNREFUSED|127\.0\.0\.1/);
  }, 10000);

  it('returns 401 when token is known-revoked', async () => {
    const { TokenRevocationService } = require('../services/authSession');
    const mockService = new TokenRevocationService();
    mockService.checkRevoked = jest
      .fn<any>()
      .mockResolvedValue({ revoked: true, reason: 'jti' });

    const { createAuthMiddleware } = require('../middleware/auth');
    const authMiddleware = createAuthMiddleware(mockService);

    const token = signValidToken();
    const req: any = {
      headers: {
        authorization: `Bearer ${token}`,
        'x-correlation-id': 'corr-revoked',
      },
      method: 'GET',
      path: '/api/v1/leads',
    };
    const res: any = { status: jest.fn(() => res), json: jest.fn(() => res) };

    const capturedError = await new Promise<any>((resolve) => {
      authMiddleware(req, res, (err: any) => resolve(err));
    });

    // The error goes through next(), which should be a 401 unauthorized
    expect(capturedError).toBeTruthy();
    expect(capturedError.statusCode).toBe(401);
    expect(capturedError.message).toMatch(/revoked/i);
  }, 10000);
});

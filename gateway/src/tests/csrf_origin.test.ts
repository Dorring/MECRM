import { describe, it, expect, beforeEach, afterEach } from '@jest/globals';
import { getCookieOptions, REFRESH_COOKIE, CSRF_COOKIE, CSRF_HEADER } from '../config/cookies';
import { generateCsrfToken, validateCsrf } from '../config/csrf';
import { createOriginValidation } from '../middleware/origin';
import { Request, Response } from 'express';

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

function mockReq(headers: Record<string, string> = {}, cookies: Record<string, string> = {}): Request {
  return { headers, cookies } as unknown as Request;
}

function mockRes(): Response {
  const res = {
    statusCode: 0,
    body: undefined as any,
    status(code: number) { this.statusCode = code; return this; },
    json(data: any) { this.body = data; return this; },
  };
  return res as unknown as Response;
}

// ---------------------------------------------------------------------------
// getCookieOptions
// ---------------------------------------------------------------------------

describe('getCookieOptions', () => {
  const envBackup: Record<string, string | undefined> = {};

  beforeEach(() => {
    envBackup.COOKIE_SECURE = process.env.COOKIE_SECURE;
    envBackup.COOKIE_SAME_SITE = process.env.COOKIE_SAME_SITE;
    envBackup.NODE_ENV = process.env.NODE_ENV;
  });

  afterEach(() => {
    for (const [k, v] of Object.entries(envBackup)) {
      if (v === undefined) delete (process.env as any)[k];
      else (process.env as any)[k] = v;
    }
  });

  it('refresh cookie is HttpOnly', () => {
    const opts = getCookieOptions();
    expect(opts.refresh.httpOnly).toBe(true);
  });

  it('csrf cookie is NOT HttpOnly', () => {
    const opts = getCookieOptions();
    expect(opts.csrf.httpOnly).toBe(false);
  });

  it('refresh path is /api/v1/auth', () => {
    const opts = getCookieOptions();
    expect(opts.refresh.path).toBe('/api/v1/auth');
  });

  it('csrf path is /', () => {
    const opts = getCookieOptions();
    expect(opts.csrf.path).toBe('/');
  });

  it('COOKIE_SECURE=true → secure: true', () => {
    process.env.COOKIE_SECURE = 'true';
    const opts = getCookieOptions();
    expect(opts.refresh.secure).toBe(true);
    expect(opts.csrf.secure).toBe(true);
  });

  it('COOKIE_SECURE=false → secure: false', () => {
    process.env.COOKIE_SECURE = 'false';
    const opts = getCookieOptions();
    expect(opts.refresh.secure).toBe(false);
    expect(opts.csrf.secure).toBe(false);
  });

  it('NODE_ENV=production default → secure: true', () => {
    delete process.env.COOKIE_SECURE;
    process.env.NODE_ENV = 'production';
    const opts = getCookieOptions();
    expect(opts.refresh.secure).toBe(true);
  });

  it('NODE_ENV=development default → secure: false', () => {
    delete process.env.COOKIE_SECURE;
    process.env.NODE_ENV = 'development';
    const opts = getCookieOptions();
    expect(opts.refresh.secure).toBe(false);
  });

  it('COOKIE_SAME_SITE=lax → sameSite: lax', () => {
    process.env.COOKIE_SAME_SITE = 'lax';
    const opts = getCookieOptions();
    expect(opts.refresh.sameSite).toBe('lax');
    expect(opts.csrf.sameSite).toBe('lax');
  });

  it('COOKIE_SAME_SITE=strict → sameSite: strict', () => {
    process.env.COOKIE_SAME_SITE = 'strict';
    const opts = getCookieOptions();
    expect(opts.refresh.sameSite).toBe('strict');
    expect(opts.csrf.sameSite).toBe('strict');
  });

  it('NODE_ENV=production default → sameSite: strict', () => {
    delete process.env.COOKIE_SAME_SITE;
    process.env.NODE_ENV = 'production';
    const opts = getCookieOptions();
    expect(opts.refresh.sameSite).toBe('strict');
  });

  it('NODE_ENV=development default → sameSite: lax', () => {
    delete process.env.COOKIE_SAME_SITE;
    process.env.NODE_ENV = 'development';
    const opts = getCookieOptions();
    expect(opts.refresh.sameSite).toBe('lax');
  });
});

// ---------------------------------------------------------------------------
// generateCsrfToken
// ---------------------------------------------------------------------------

describe('generateCsrfToken', () => {
  it('returns 64-char hex string', () => {
    const token = generateCsrfToken();
    expect(token).toHaveLength(64);
    expect(token).toMatch(/^[0-9a-f]{64}$/);
  });

  it('returns unique values on successive calls', () => {
    const a = generateCsrfToken();
    const b = generateCsrfToken();
    expect(a).not.toBe(b);
  });
});

// ---------------------------------------------------------------------------
// validateCsrf
// ---------------------------------------------------------------------------

describe('validateCsrf', () => {
  it('accepts matching header and cookie', () => {
    const token = generateCsrfToken();
    const req = mockReq({ [CSRF_HEADER]: token }, { [CSRF_COOKIE]: token });
    expect(validateCsrf(req)).toBe(true);
  });

  it('rejects missing header', () => {
    const req = mockReq({}, { [CSRF_COOKIE]: 'abc' });
    expect(validateCsrf(req)).toBe(false);
  });

  it('rejects missing cookie', () => {
    const req = mockReq({ [CSRF_HEADER]: 'abc' }, {});
    expect(validateCsrf(req)).toBe(false);
  });

  it('rejects mismatched values', () => {
    const req = mockReq({ [CSRF_HEADER]: 'aaa' }, { [CSRF_COOKIE]: 'bbb' });
    expect(validateCsrf(req)).toBe(false);
  });

  it('rejects empty header', () => {
    const req = mockReq({ [CSRF_HEADER]: '' }, { [CSRF_COOKIE]: 'abc' });
    expect(validateCsrf(req)).toBe(false);
  });

  it('rejects empty cookie', () => {
    const req = mockReq({ [CSRF_HEADER]: 'abc' }, { [CSRF_COOKIE]: '' });
    expect(validateCsrf(req)).toBe(false);
  });
});

// ---------------------------------------------------------------------------
// createOriginValidation
// ---------------------------------------------------------------------------

describe('createOriginValidation', () => {
  const envBackup = process.env.ALLOWED_ORIGINS;

  afterEach(() => {
    if (envBackup === undefined) delete process.env.ALLOWED_ORIGINS;
    else process.env.ALLOWED_ORIGINS = envBackup;
  });

  it('allows request with no Origin header', () => {
    process.env.ALLOWED_ORIGINS = 'http://localhost:3000';
    const middleware = createOriginValidation();
    const req = mockReq({});
    const res = mockRes();
    let called = false;
    middleware(req, res, () => { called = true; });
    expect(called).toBe(true);
  });

  it('allows listed origin', () => {
    process.env.ALLOWED_ORIGINS = 'http://localhost:3000,http://localhost:3001';
    const middleware = createOriginValidation();
    const req = mockReq({ origin: 'http://localhost:3000' });
    const res = mockRes();
    let called = false;
    middleware(req, res, () => { called = true; });
    expect(called).toBe(true);
  });

  it('rejects unlisted origin with 403', () => {
    process.env.ALLOWED_ORIGINS = 'http://localhost:3000';
    const middleware = createOriginValidation();
    const req = mockReq({ origin: 'http://evil.com' });
    const res = mockRes();
    let called = false;
    middleware(req, res, () => { called = true; });
    expect(called).toBe(false);
    expect(res.statusCode).toBe(403);
    expect((res as any).body.error.code).toBe('ORIGIN_NOT_ALLOWED');
  });

  it('rejects present Origin when ALLOWED_ORIGINS is empty (fail-closed)', () => {
    process.env.ALLOWED_ORIGINS = '';
    const middleware = createOriginValidation();
    const req = mockReq({ origin: 'http://anything.com' });
    const res = mockRes();
    let called = false;
    middleware(req, res, () => { called = true; });
    expect(called).toBe(false);
    expect(res.statusCode).toBe(403);
    expect((res as any).body.error.code).toBe('ORIGIN_NOT_ALLOWED');
  });

  it('allows missing Origin even when ALLOWED_ORIGINS is empty', () => {
    process.env.ALLOWED_ORIGINS = '';
    const middleware = createOriginValidation();
    const req = mockReq({});  // no Origin header
    const res = mockRes();
    let called = false;
    middleware(req, res, () => { called = true; });
    expect(called).toBe(true);
  });
});

// ---------------------------------------------------------------------------
// Constants
// ---------------------------------------------------------------------------

describe('cookie constants', () => {
  it('REFRESH_COOKIE is refresh_token', () => {
    expect(REFRESH_COOKIE).toBe('refresh_token');
  });

  it('CSRF_COOKIE is csrf_token', () => {
    expect(CSRF_COOKIE).toBe('csrf_token');
  });

  it('CSRF_HEADER is x-csrf-token (lowercase)', () => {
    expect(CSRF_HEADER).toBe('x-csrf-token');
  });
});

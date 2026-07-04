/**
 * Unit tests for TokenRevocationService and auth claim validation.
 *
 * These tests use mocked Redis (ioredis) and do NOT require a real Redis instance.
 * They cover:
 *   - claim validation (malformed/missing claims rejected before Redis)
 *   - TTL boundary calculations
 *   - key builder isolation (tenant A cannot affect tenant B)
 *   - revocation check pipeline error handling
 *   - user version read/increment
 *   - no full JWT in keys/logs/errors
 */

import { describe, it, expect, jest, beforeEach } from '@jest/globals';
import { randomUUID } from 'crypto';

// Mock ioredis before importing authSession
const mockPipeline = {
  get: jest.fn<any>(),
  exec: jest.fn<any>(),
};

const mockRedisClient = {
  get: jest.fn<any>(),
  setex: jest.fn<any>(),
  incr: jest.fn<any>(),
  pipeline: jest.fn<any>(() => mockPipeline),
  publish: jest.fn<any>(() => Promise.resolve(0)),
  duplicate: jest.fn<any>(),
  eval: jest.fn<any>(),
};

jest.mock('ioredis', () => {
  return jest.fn().mockImplementation(() => mockRedisClient);
});

// Also need to mock the redis module used by authSession
jest.mock('../services/redis', () => ({
  redisClient: mockRedisClient,
}));

import {
  TokenRevocationService,
  validateDecodedToken,
  computeTtl,
  authKeys,
  DecodedToken,
} from '../services/authSession';

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

function makeValidToken(overrides: Partial<DecodedToken> = {}): DecodedToken {
  const now = Math.floor(Date.now() / 1000);
  return {
    jti: randomUUID(),
    sid: randomUUID(),
    sub: randomUUID(),
    tenantId: randomUUID(),
    type: 'access',
    uv: 0,
    sexp: now + 86400 * 7,
    iat: now,
    exp: now + 3600,
    ...overrides,
  };
}

function makeService(): TokenRevocationService {
  return new TokenRevocationService(mockRedisClient as any);
}

// ---------------------------------------------------------------------------
// Claim validation
// ---------------------------------------------------------------------------

describe('validateDecodedToken', () => {
  it('accepts a valid token with all required claims', () => {
    const now = Math.floor(Date.now() / 1000);
    const result = validateDecodedToken({
      jti: randomUUID(),
      sid: randomUUID(),
      sub: randomUUID(),
      tenantId: randomUUID(),
      type: 'access',
      uv: 0,
      sexp: now + 86400 * 7,
      iat: now,
      exp: now + 3600,
    });
    expect(result.valid).toBe(true);
  });

  it('rejects non-UUID security identifiers', () => {
    const token = makeValidToken();
    const result = validateDecodedToken({
      ...token,
      tenantId: 'tenant-with-cluster-slot-injection}',
    });
    expect(result.valid).toBe(false);
    expect(result.error).toContain('tenantId');
  });

  it('rejects missing jti', () => {
    const now = Math.floor(Date.now() / 1000);
    const result = validateDecodedToken({
      sid: randomUUID(),
      sub: randomUUID(),
      tenantId: randomUUID(),
      type: 'access',
      uv: 0,
      sexp: now + 86400 * 7,
      iat: now,
      exp: now + 3600,
    });
    expect(result.valid).toBe(false);
    expect(result.error).toMatch(/jti/i);
  });

  it('rejects missing sid', () => {
    const now = Math.floor(Date.now() / 1000);
    const result = validateDecodedToken({
      jti: randomUUID(),
      sub: randomUUID(),
      tenantId: randomUUID(),
      type: 'access',
      uv: 0,
      sexp: now + 86400 * 7,
      iat: now,
      exp: now + 3600,
    });
    expect(result.valid).toBe(false);
    expect(result.error).toMatch(/sid/i);
  });

  it('rejects missing sub', () => {
    const now = Math.floor(Date.now() / 1000);
    const result = validateDecodedToken({
      jti: randomUUID(),
      sid: randomUUID(),
      tenantId: randomUUID(),
      type: 'access',
      uv: 0,
      sexp: now + 86400 * 7,
      iat: now,
      exp: now + 3600,
    });
    expect(result.valid).toBe(false);
    expect(result.error).toMatch(/sub/i);
  });

  it('rejects missing tenantId', () => {
    const now = Math.floor(Date.now() / 1000);
    const result = validateDecodedToken({
      jti: randomUUID(),
      sid: randomUUID(),
      sub: randomUUID(),
      type: 'access',
      uv: 0,
      sexp: now + 86400 * 7,
      iat: now,
      exp: now + 3600,
    });
    expect(result.valid).toBe(false);
    expect(result.error).toMatch(/tenantId/i);
  });

  it('rejects missing type', () => {
    const now = Math.floor(Date.now() / 1000);
    const result = validateDecodedToken({
      jti: randomUUID(),
      sid: randomUUID(),
      sub: randomUUID(),
      tenantId: randomUUID(),
      uv: 0,
      sexp: now + 86400 * 7,
      iat: now,
      exp: now + 3600,
    });
    expect(result.valid).toBe(false);
    expect(result.error).toMatch(/type/i);
  });

  it('rejects invalid type value', () => {
    const now = Math.floor(Date.now() / 1000);
    const result = validateDecodedToken({
      jti: randomUUID(),
      sid: randomUUID(),
      sub: randomUUID(),
      tenantId: randomUUID(),
      type: 'invalid',
      uv: 0,
      sexp: now + 86400 * 7,
      iat: now,
      exp: now + 3600,
    });
    expect(result.valid).toBe(false);
    expect(result.error).toMatch(/type/i);
  });

  it('rejects missing uv', () => {
    const now = Math.floor(Date.now() / 1000);
    const result = validateDecodedToken({
      jti: randomUUID(),
      sid: randomUUID(),
      sub: randomUUID(),
      tenantId: randomUUID(),
      type: 'access',
      sexp: now + 86400 * 7,
      iat: now,
      exp: now + 3600,
    });
    expect(result.valid).toBe(false);
    expect(result.error).toMatch(/uv/i);
  });

  it('rejects negative uv', () => {
    const now = Math.floor(Date.now() / 1000);
    const result = validateDecodedToken({
      jti: randomUUID(),
      sid: randomUUID(),
      sub: randomUUID(),
      tenantId: randomUUID(),
      type: 'access',
      uv: -1,
      sexp: now + 86400 * 7,
      iat: now,
      exp: now + 3600,
    });
    expect(result.valid).toBe(false);
    expect(result.error).toMatch(/uv/i);
  });

  it('rejects missing sexp', () => {
    const now = Math.floor(Date.now() / 1000);
    const result = validateDecodedToken({
      jti: randomUUID(),
      sid: randomUUID(),
      sub: randomUUID(),
      tenantId: randomUUID(),
      type: 'access',
      uv: 0,
      iat: now,
      exp: now + 3600,
    });
    expect(result.valid).toBe(false);
    expect(result.error).toMatch(/sexp/i);
  });

  it('accepts type "refresh"', () => {
    const now = Math.floor(Date.now() / 1000);
    const result = validateDecodedToken({
      jti: randomUUID(),
      sid: randomUUID(),
      sub: randomUUID(),
      tenantId: randomUUID(),
      type: 'refresh',
      uv: 0,
      sexp: now + 86400 * 7,
      iat: now,
      exp: now + 3600,
    });
    expect(result.valid).toBe(true);
  });
});

// ---------------------------------------------------------------------------
// TTL
// ---------------------------------------------------------------------------

describe('computeTtl', () => {
  const SECOND = 1000;

  it('returns a positive TTL for a future expiry', () => {
    const future = Math.floor((Date.now() + 3600 * SECOND) / SECOND);
    const ttl = computeTtl(future);
    expect(ttl).toBeGreaterThan(3500); // ~3600 + 60 skew - some elapsed time
    expect(ttl).toBeLessThanOrEqual(604800);
  });

  it('returns at least 1 for a recently-expired token (clock skew buffer)', () => {
    // A token that expired 1 second ago gets exp-now+60 ≈ 59s TTL due to skew buffer
    const past = Math.floor((Date.now() - 1000) / SECOND);
    const ttl = computeTtl(past);
    expect(ttl).toBeGreaterThanOrEqual(1);
    expect(ttl).toBeLessThan(120);
  });

  it('respects the ceiling of 7 days', () => {
    const farFuture = Math.floor((Date.now() + 86400 * 30 * SECOND) / SECOND);
    const ttl = computeTtl(farFuture);
    expect(ttl).toBe(604800);
  });

  it('has a floor of at least 1 for near-expiry tokens', () => {
    // Token expiring 5 seconds from now + 60s skew = 65s
    const near = Math.floor((Date.now() + 5 * SECOND) / SECOND);
    const ttl = computeTtl(near);
    expect(ttl).toBeGreaterThanOrEqual(1);
  });
});

// ---------------------------------------------------------------------------
// Key builders
// ---------------------------------------------------------------------------

describe('authKeys', () => {
  const tenantA = randomUUID();
  const tenantB = randomUUID();
  const jti = randomUUID();
  const sid = randomUUID();
  const userId = randomUUID();

  it('builds distinct tenant-scoped jti keys', () => {
    const keyA = authKeys.revokedJti(tenantA, jti);
    const keyB = authKeys.revokedJti(tenantB, jti);
    expect(keyA).not.toBe(keyB);
    expect(keyA).toContain(tenantA);
    expect(keyB).toContain(tenantB);
    expect(keyA).toContain(jti);
    expect(keyA).toMatch(/^auth:/);
  });

  it('builds distinct tenant-scoped sid keys', () => {
    const keyA = authKeys.revokedSid(tenantA, sid);
    const keyB = authKeys.revokedSid(tenantB, sid);
    expect(keyA).not.toBe(keyB);
    expect(keyA).toContain(tenantA);
    expect(keyB).toContain(tenantB);
  });

  it('builds distinct tenant-scoped user version keys', () => {
    const keyA = authKeys.userVersion(tenantA, userId);
    const keyB = authKeys.userVersion(tenantB, userId);
    expect(keyA).not.toBe(keyB);
    expect(keyA).toContain(tenantA);
    expect(keyB).toContain(tenantB);
  });

  it('builds consumed refresh key with tenant scope', () => {
    const key = authKeys.consumedRefresh(tenantA, jti);
    expect(key).toContain(tenantA);
    expect(key).toContain(jti);
    expect(key).toContain('consumed');
  });
});

// ---------------------------------------------------------------------------
// checkRevoked pipeline error handling
// ---------------------------------------------------------------------------

describe('TokenRevocationService.checkRevoked', () => {
  let service: TokenRevocationService;

  beforeEach(() => {
    jest.clearAllMocks();
    service = makeService();
    mockPipeline.get.mockReturnThis();
  });

  it('returns { revoked: false } for a non-revoked token', async () => {
    mockPipeline.exec.mockResolvedValue([
      [null, null],  // jti — not revoked
      [null, null],  // sid — not revoked
      [null, null],  // uv — no version key (means version 0, token has uv=0)
    ]);

    const result = await service.checkRevoked(makeValidToken());
    expect(result.revoked).toBe(false);
  });

  it('returns { revoked: true, reason: "jti" } when jti is revoked', async () => {
    mockPipeline.exec.mockResolvedValue([
      [null, '1'],    // jti — revoked
      [null, null],   // sid
      [null, null],   // uv
    ]);

    const result = await service.checkRevoked(makeValidToken());
    expect(result.revoked).toBe(true);
    expect(result.reason).toBe('jti');
  });

  it('returns { revoked: true, reason: "sid" } when sid is revoked', async () => {
    mockPipeline.exec.mockResolvedValue([
      [null, null],   // jti
      [null, '1'],    // sid — revoked
      [null, null],   // uv
    ]);

    const result = await service.checkRevoked(makeValidToken());
    expect(result.revoked).toBe(true);
    expect(result.reason).toBe('sid');
  });

  it('returns { revoked: true, reason: "uv" } on user version mismatch', async () => {
    mockPipeline.exec.mockResolvedValue([
      [null, null],   // jti
      [null, null],   // sid
      [null, '5'],    // uv — version 5, token has uv=0
    ]);

    const result = await service.checkRevoked(makeValidToken({ uv: 0 }));
    expect(result.revoked).toBe(true);
    expect(result.reason).toBe('uv');
  });

  it('returns { revoked: false } when user version matches', async () => {
    mockPipeline.exec.mockResolvedValue([
      [null, null],   // jti
      [null, null],   // sid
      [null, '3'],    // uv — version 3, token has uv=3
    ]);

    const result = await service.checkRevoked(makeValidToken({ uv: 3 }));
    expect(result.revoked).toBe(false);
  });

  it('treats missing user version key as version 0', async () => {
    // Missing version key → null → version 0
    mockPipeline.exec.mockResolvedValue([
      [null, null],   // jti
      [null, null],   // sid
      [null, null],   // uv — missing
    ]);

    const result = await service.checkRevoked(makeValidToken({ uv: 0 }));
    expect(result.revoked).toBe(false);
  });

  it('rejects token when user version is missing but token uv > 0', async () => {
    mockPipeline.exec.mockResolvedValue([
      [null, null],   // jti
      [null, null],   // sid
      [null, null],   // uv — missing, means version 0
    ]);

    const result = await service.checkRevoked(makeValidToken({ uv: 1 }));
    expect(result.revoked).toBe(true);
    expect(result.reason).toBe('uv');
  });

  it('throws on null pipeline result', async () => {
    mockPipeline.exec.mockResolvedValue(null);

    await expect(
      service.checkRevoked(makeValidToken()),
    ).rejects.toThrow(/null/i);
  });

  it('throws on insufficient result count', async () => {
    mockPipeline.exec.mockResolvedValue([
      [null, null],
      [null, null],
    ]);

    await expect(
      service.checkRevoked(makeValidToken()),
    ).rejects.toThrow(/expected/i);
  });

  it('throws on per-command error', async () => {
    mockPipeline.exec.mockResolvedValue([
      [new Error('MOVED 1234 127.0.0.1:6379'), null],
      [null, null],
      [null, null],
    ]);

    await expect(
      service.checkRevoked(makeValidToken()),
    ).rejects.toThrow('MOVED');
  });

  it('throws on malformed user version', async () => {
    mockPipeline.exec.mockResolvedValue([
      [null, null],   // jti
      [null, null],   // sid
      [null, 'not-a-number'], // uv — malformed
    ]);

    await expect(
      service.checkRevoked(makeValidToken()),
    ).rejects.toThrow(/malformed/i);
  });

  it('throws on pipeline itself throwing', async () => {
    mockPipeline.exec.mockRejectedValue(new Error('ECONNREFUSED'));

    await expect(
      service.checkRevoked(makeValidToken()),
    ).rejects.toThrow('ECONNREFUSED');
  });
});

// ---------------------------------------------------------------------------
// revokeJti / revokeSid / revokeUser
// ---------------------------------------------------------------------------

describe('TokenRevocationService revocation writes', () => {
  let service: TokenRevocationService;

  beforeEach(() => {
    jest.clearAllMocks();
    service = makeService();
  });

  it('revokeJti writes a key with TTL', async () => {
    const exp = Math.floor(Date.now() / 1000) + 3600;
    await service.revokeJti('tenant-1', 'test-jti', exp);
    expect(mockRedisClient.setex).toHaveBeenCalledWith(
      expect.stringContaining('auth:{tenant-1}:revoked:jti:test-jti'),
      expect.any(Number),
      '1',
    );
  });

  it('revokeSid writes a key with TTL', async () => {
    const sexp = Math.floor(Date.now() / 1000) + 86400 * 7;
    await service.revokeSid('tenant-1', 'test-sid', sexp);
    expect(mockRedisClient.setex).toHaveBeenCalledWith(
      expect.stringContaining('auth:{tenant-1}:revoked:sid:test-sid'),
      expect.any(Number),
      '1',
    );
  });

  it('revokeUser increments the user version key', async () => {
    mockRedisClient.incr.mockResolvedValue(1);
    const result = await service.revokeUser('tenant-1', 'user-1');
    expect(mockRedisClient.incr).toHaveBeenCalledWith(
      'auth:{tenant-1}:user:user-1:version',
    );
    expect(result).toBe(1);
  });

  it('getUserVersion returns 0 when key is missing', async () => {
    mockRedisClient.get.mockResolvedValue(null);
    const v = await service.getUserVersion('tenant-1', 'user-1');
    expect(v).toBe(0);
  });

  it('getUserVersion returns parsed integer', async () => {
    mockRedisClient.get.mockResolvedValue('42');
    const v = await service.getUserVersion('tenant-1', 'user-1');
    expect(v).toBe(42);
  });

  it('getUserVersion throws on malformed value', async () => {
    mockRedisClient.get.mockResolvedValue('not-a-number');
    await expect(
      service.getUserVersion('tenant-1', 'user-1'),
    ).rejects.toThrow(/malformed/i);
  });

  it('getUserVersion rejects a numeric prefix with trailing garbage', async () => {
    mockRedisClient.get.mockResolvedValue('1garbage');
    await expect(
      service.getUserVersion('tenant-1', 'user-1'),
    ).rejects.toThrow(/malformed/i);
  });
});

// ---------------------------------------------------------------------------
// No complete JWT in keys/logs/errors
// ---------------------------------------------------------------------------

describe('No token leakage', () => {
  it('authKeys never contain a full JWT', () => {
    const keys = [
      authKeys.revokedJti('t', 'uuid'),
      authKeys.revokedSid('t', 'uuid'),
      authKeys.consumedRefresh('t', 'uuid'),
      authKeys.userVersion('t', 'uuid'),
    ];
    for (const key of keys) {
      expect(key).not.toMatch(/^[A-Za-z0-9-_]+\.[A-Za-z0-9-_]+\.[A-Za-z0-9-_]+$/);
      expect(key).not.toContain('eyJ');
    }
  });
});

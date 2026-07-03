import { Redis } from 'ioredis';
import { randomUUID } from 'crypto';
import { logger } from '../utils/logger';

// ---------------------------------------------------------------------------
// Types
// ---------------------------------------------------------------------------

/**
 * Strictly validated decoded token.
 * Every field is checked before any Redis key construction.
 */
export interface DecodedToken {
  jti: string;         // UUID — unique token id
  sid: string;         // UUID — session id (fixed for session lifetime)
  sub: string;         // UUID — user id
  tenantId: string;    // UUID — tenant boundary
  type: 'access' | 'refresh';
  uv: number;          // user revocation generation at session creation
  sexp: number;        // absolute session expiry (Unix sec)
  iat: number;
  exp: number;
}

export interface RevocationResult {
  revoked: boolean;
  reason?: 'jti' | 'sid' | 'uv';
}

export type RefreshConsumeResult =
  | { status: 'OK' }
  | { status: 'REPLAY' }
  | { status: 'REVOKED'; reason: string }
  | { status: 'DEPENDENCY_ERROR'; error: Error };

export interface RevocationEvent {
  version: 1;
  type: 'jti' | 'sid' | 'user';
  tenantId: string;
  id: string;          // jti, sid, or userId depending on type
  userId?: string;
  occurredAt: number;
}

// ---------------------------------------------------------------------------
// Constants
// ---------------------------------------------------------------------------

const TTL_FLOOR = 1;
const TTL_CEILING = 604800; // 7 days
const CLOCK_SKEW = 60;      // 60-second clock skew buffer
const MAX_EVENT_PAYLOAD_BYTES = 4096;

// ---------------------------------------------------------------------------
// TTL helpers
// ---------------------------------------------------------------------------

/**
 * Compute revocation TTL from a token's expiry.
 * Returns 0 if already expired (caller should not write).
 */
export function computeTtl(exp: number): number {
  const now = Math.floor(Date.now() / 1000);
  const raw = exp - now + CLOCK_SKEW;
  if (raw <= 0) return 0;
  return Math.min(TTL_CEILING, raw);
}

// ---------------------------------------------------------------------------
// Key builders (tenant-scoped)
// ---------------------------------------------------------------------------

/** All key builders require validated identifiers — caller must validate first. */
export const authKeys = {
  revokedJti: (tenantId: string, jti: string) =>
    `auth:${tenantId}:revoked:jti:${jti}`,

  revokedSid: (tenantId: string, sid: string) =>
    `auth:${tenantId}:revoked:sid:${sid}`,

  consumedRefresh: (tenantId: string, jti: string) =>
    `auth:${tenantId}:refresh:consumed:${jti}`,

  userVersion: (tenantId: string, userId: string) =>
    `auth:${tenantId}:user:${userId}:version`,
};

// ---------------------------------------------------------------------------
// Claim validation
// ---------------------------------------------------------------------------

export interface TokenValidationResult {
  valid: boolean;
  error?: string;
}

export function validateDecodedToken(
  decoded: Record<string, unknown>,
): TokenValidationResult {
  // Type check each claim
  if (typeof decoded.jti !== 'string' || !decoded.jti) {
    return { valid: false, error: 'Missing or invalid jti' };
  }
  if (typeof decoded.sid !== 'string' || !decoded.sid) {
    return { valid: false, error: 'Missing or invalid sid' };
  }
  if (typeof decoded.sub !== 'string' || !decoded.sub) {
    return { valid: false, error: 'Missing or invalid sub' };
  }
  if (typeof decoded.tenantId !== 'string' || !decoded.tenantId) {
    return { valid: false, error: 'Missing or invalid tenantId' };
  }
  if (decoded.type !== 'access' && decoded.type !== 'refresh') {
    return { valid: false, error: 'Missing or invalid type (must be access or refresh)' };
  }
  if (typeof decoded.uv !== 'number' || !Number.isInteger(decoded.uv) || decoded.uv < 0) {
    return { valid: false, error: 'Missing or invalid uv (non-negative integer)' };
  }
  if (typeof decoded.sexp !== 'number' || decoded.sexp < 0) {
    return { valid: false, error: 'Missing or invalid sexp (Unix seconds)' };
  }
  if (typeof decoded.iat !== 'number' || decoded.iat <= 0) {
    return { valid: false, error: 'Missing or invalid iat' };
  }
  if (typeof decoded.exp !== 'number' || decoded.exp <= 0) {
    return { valid: false, error: 'Missing or invalid exp' };
  }
  return { valid: true };
}

// ---------------------------------------------------------------------------
// Lua script for atomic refresh consumption
// ---------------------------------------------------------------------------

export const ATOMIC_REFRESH_LUA = `
-- KEYS[1] = revoked:jti key
-- KEYS[2] = revoked:sid key
-- KEYS[3] = user:version key
-- KEYS[4] = refresh:consumed key
-- ARGV[1] = token jti
-- ARGV[2] = token sid
-- ARGV[3] = token uv (as string)
-- ARGV[4] = token sexp (as string)
-- ARGV[5] = consumed TTL (as string)
-- ARGV[6] = sid-revocation TTL (as string)

-- 1. Check jti revocation
local jti_revoked = redis.call("GET", KEYS[1])
if jti_revoked then
  return {0, "JTI_REVOKED"}
end

-- 2. Check sid revocation
local sid_revoked = redis.call("GET", KEYS[2])
if sid_revoked then
  return {0, "SID_REVOKED"}
end

-- 3. Check user version
local current_uv = redis.call("GET", KEYS[3])
local token_uv = tonumber(ARGV[3])
if current_uv then
  current_uv = tonumber(current_uv)
  if current_uv ~= token_uv then
    return {0, "UV_MISMATCH"}
  end
else
  -- No version key means version 0
  if token_uv ~= 0 then
    return {0, "UV_MISMATCH"}
  end
end

-- 4. Check consumed state (NX write)
local consumed = redis.call("SET", KEYS[4], "1", "NX", "EX", ARGV[5])
if consumed then
  -- First use: OK, mint token pair
  return {1, "OK"}
else
  -- Already consumed: REPLAY — revoke the entire sid
  redis.call("SET", KEYS[2], "1", "EX", ARGV[6])
  return {0, "REPLAY"}
end
`;

// ---------------------------------------------------------------------------
// TokenRevocationService
// ---------------------------------------------------------------------------

export class TokenRevocationService {
  constructor(
    private readonly client: Redis,
    private readonly subscriber?: Redis,
  ) {}

  // -----------------------------------------------------------------------
  // Revocation checks
  // -----------------------------------------------------------------------

  /**
   * Check whether a decoded token has been revoked.
   * Uses pipeline for a single round-trip: jti, sid, and user version.
   * FAIL-CLOSED: throws on any Redis error.
   */
  async checkRevoked(token: DecodedToken): Promise<RevocationResult> {
    const pipe = this.client.pipeline();
    pipe.get(authKeys.revokedJti(token.tenantId, token.jti));
    pipe.get(authKeys.revokedSid(token.tenantId, token.sid));
    pipe.get(authKeys.userVersion(token.tenantId, token.sub));

    const results = await pipe.exec();

    // Reject null/undefined pipeline result
    if (!results || !Array.isArray(results)) {
      throw new Error('Pipeline returned null or unexpected type');
    }

    // Account for all three commands
    if (results.length < 3) {
      throw new Error(
        `Pipeline returned ${results.length} results, expected at least 3`,
      );
    }

    const [jtiResult, sidResult, uvResult] = results;

    // Every tuple must be error-free
    for (let i = 0; i < 3; i++) {
      const tuple = results[i];
      if (!Array.isArray(tuple) || tuple.length < 2) {
        throw new Error(`Pipeline tuple ${i} is malformed`);
      }
      if (tuple[0] instanceof Error) {
        throw tuple[0];
      }
    }

    // Check jti revocation
    if (jtiResult[1] != null) {
      return { revoked: true, reason: 'jti' };
    }

    // Check sid revocation
    if (sidResult[1] != null) {
      return { revoked: true, reason: 'sid' };
    }

    // Check user version
    const currentUvRaw = uvResult[1];
    if (currentUvRaw != null) {
      if (typeof currentUvRaw === 'string') {
        const parsed = parseInt(currentUvRaw, 10);
        if (Number.isNaN(parsed) || parsed < 0) {
          throw new Error(
            `Malformed user version for ${token.tenantId}:${token.sub}: ${currentUvRaw}`,
          );
        }
        if (parsed !== token.uv) {
          return { revoked: true, reason: 'uv' };
        }
      } else if (typeof currentUvRaw === 'number') {
        if (currentUvRaw !== token.uv) {
          return { revoked: true, reason: 'uv' };
        }
      } else {
        throw new Error(
          `Unexpected user version type for ${token.tenantId}:${token.sub}: ${typeof currentUvRaw}`,
        );
      }
    } else {
      // Missing version key means version 0
      if (token.uv !== 0) {
        return { revoked: true, reason: 'uv' };
      }
    }

    return { revoked: false };
  }

  // -----------------------------------------------------------------------
  // Revocation writes
  // -----------------------------------------------------------------------

  /** Revoke a single token by jti. */
  async revokeJti(tenantId: string, jti: string, exp: number): Promise<void> {
    const ttl = computeTtl(exp);
    if (ttl <= 0) return; // already expired, no need to record
    await this.client.setex(authKeys.revokedJti(tenantId, jti), ttl, '1');
    this.publishEvent({
      version: 1,
      type: 'jti',
      tenantId,
      id: jti,
      occurredAt: Date.now(),
    });
  }

  /** Revoke all tokens in a session by sid until its absolute expiry. */
  async revokeSid(
    tenantId: string,
    sid: string,
    sexp: number,
  ): Promise<void> {
    const ttl = computeTtl(sexp);
    if (ttl <= 0) return;
    await this.client.setex(authKeys.revokedSid(tenantId, sid), ttl, '1');
    this.publishEvent({
      version: 1,
      type: 'sid',
      tenantId,
      id: sid,
      occurredAt: Date.now(),
    });
  }

  /**
   * Revoke ALL sessions for a user by incrementing the generation.
   * All tokens with an older uv will be rejected.
   */
  async revokeUser(tenantId: string, userId: string): Promise<number> {
    const newVersion = await this.client.incr(
      authKeys.userVersion(tenantId, userId),
    );
    this.publishEvent({
      version: 1,
      type: 'user',
      tenantId,
      id: userId,
      userId,
      occurredAt: Date.now(),
    });
    return newVersion;
  }

  /** Read the current user version (0 if missing). */
  async getUserVersion(tenantId: string, userId: string): Promise<number> {
    const val = await this.client.get(
      authKeys.userVersion(tenantId, userId),
    );
    if (val == null) return 0;
    const parsed = parseInt(val, 10);
    if (Number.isNaN(parsed) || parsed < 0) {
      throw new Error(
        `Malformed user version for ${tenantId}:${userId}: ${val}`,
      );
    }
    return parsed;
  }

  // -----------------------------------------------------------------------
  // Atomic refresh consumption (Lua)
  // -----------------------------------------------------------------------

  /**
   * Atomically consume a refresh token.
   *
   * Returns OK → caller may mint a new token pair.
   * Returns REPLAY → the jti was already consumed; sid has been revoked.
   * Returns REVOKED → token/session/user is revoked.
   * Returns DEPENDENCY_ERROR → Redis error.
   */
  async consumeRefresh(
    token: DecodedToken,
  ): Promise<RefreshConsumeResult> {
    const consumedTtl = computeTtl(token.exp);
    const sidTtl = computeTtl(token.sexp);

    const jtiKey = authKeys.revokedJti(token.tenantId, token.jti);
    const sidKey = authKeys.revokedSid(token.tenantId, token.sid);
    const uvKey = authKeys.userVersion(token.tenantId, token.sub);
    const consumedKey = authKeys.consumedRefresh(
      token.tenantId,
      token.jti,
    );

    try {
      const result = await this.client.eval(
        ATOMIC_REFRESH_LUA,
        4,
        jtiKey,
        sidKey,
        uvKey,
        consumedKey,
        token.jti,
        token.sid,
        String(token.uv),
        String(token.sexp),
        String(Math.max(consumedTtl, 60)),
        String(Math.max(sidTtl, 60)),
      );

      if (!Array.isArray(result) || result.length < 2) {
        return {
          status: 'DEPENDENCY_ERROR',
          error: new Error(`Unexpected Lua result: ${JSON.stringify(result)}`),
        };
      }

      if (result[0] === 1 && result[1] === 'OK') {
        return { status: 'OK' };
      }

      if (result[1] === 'REPLAY') {
        return { status: 'REPLAY' };
      }

      if (result[1] === 'JTI_REVOKED' || result[1] === 'SID_REVOKED' || result[1] === 'UV_MISMATCH') {
        return { status: 'REVOKED', reason: result[1] };
      }

      return {
        status: 'DEPENDENCY_ERROR',
        error: new Error(`Unexpected Lua status: ${JSON.stringify(result)}`),
      };
    } catch (err) {
      return {
        status: 'DEPENDENCY_ERROR',
        error: err instanceof Error ? err : new Error(String(err)),
      };
    }
  }

  // -----------------------------------------------------------------------
  // Revocation identifiers
  // -----------------------------------------------------------------------

  /** Generate a cryptographically random UUID for jti/sid. */
  static generateId(): string {
    return randomUUID();
  }

  // -----------------------------------------------------------------------
  // Pub/Sub
  // -----------------------------------------------------------------------

  /**
   * Initialize the subscriber for cross-instance revocation events.
   * The subscriber connection must be a dedicated duplicate (not the command client).
   */
  async initSubscriber(
    handler: (event: RevocationEvent) => void,
  ): Promise<void> {
    if (!this.subscriber) {
      logger.warn('No subscriber connection provided — cross-instance revocation disabled');
      return;
    }
    this.subscriber.on('message', (channel: string, message: string) => {
      if (channel !== 'auth:revocation:events') return;

      // Size limit
      if (Buffer.byteLength(message, 'utf-8') > MAX_EVENT_PAYLOAD_BYTES) {
        logger.error('Oversized revocation event', {
          bytes: Buffer.byteLength(message, 'utf-8'),
        });
        return;
      }

      try {
        const event: RevocationEvent = JSON.parse(message);

        // Schema validation
        if (!event || event.version !== 1 || !event.type || !event.tenantId || !event.id) {
          logger.error('Malformed revocation event', { event: JSON.stringify(event).slice(0, 200) });
          return;
        }
        if (!['jti', 'sid', 'user'].includes(event.type)) {
          logger.error('Unknown revocation event type', { type: event.type });
          return;
        }

        handler(event);
      } catch (err) {
        logger.error('Failed to parse revocation event', {
          error: err instanceof Error ? err.message : String(err),
        });
      }
    });

    await this.subscriber.subscribe('auth:revocation:events');
    logger.info('Revocation subscriber initialized');
  }

  /** Publish a revocation event to other Gateway instances. */
  private publishEvent(event: RevocationEvent): void {
    const payload = JSON.stringify(event);

    // Check size before publish to guard against oversized messages
    if (Buffer.byteLength(payload, 'utf-8') > MAX_EVENT_PAYLOAD_BYTES) {
      logger.error('Revocation event exceeds max size, not published', {
        type: event.type,
        bytes: Buffer.byteLength(payload, 'utf-8'),
      });
      return;
    }

    this.client
      .publish('auth:revocation:events', payload)
      .catch((err: Error) => {
        // Log and meter but never throw — Pub/Sub is best-effort notification.
        // The revocation key is already written durably in Redis.
        logger.error('Failed to publish revocation event', {
          error: err.message,
          type: event.type,
        });
      });
  }

  // -----------------------------------------------------------------------
  // Shutdown
  // -----------------------------------------------------------------------

  async shutdown(): Promise<void> {
    if (this.subscriber) {
      try {
        await this.subscriber.unsubscribe('auth:revocation:events');
      } catch {
        // Ignore unsubscribe errors during shutdown
      }
      this.subscriber.disconnect();
    }
  }
}

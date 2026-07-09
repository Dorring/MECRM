import { Request, Response, NextFunction } from 'express';
import jwt, { VerifyOptions } from 'jsonwebtoken';
import { unauthorized } from './errorHandler';
import { logger } from '../utils/logger';
import { JWT_SECRET } from '../config/jwt';
import { TokenRevocationService, DecodedToken, validateDecodedToken } from '../services/authSession';

// ---------------------------------------------------------------------------
// Types
// ---------------------------------------------------------------------------

export interface TokenPayload {
  jti: string;
  sid: string;
  sub: string;           // User ID
  tenantId: string;      // Tenant ID
  email: string;
  roles: string[];
  type: 'access' | 'refresh';
  uv: number;            // User revocation generation
  sexp: number;          // Absolute session expiry
  iat: number;
  exp: number;
}

export interface AuthenticatedRequest extends Request {
  user?: TokenPayload;
  tenantId?: string;
}

// ---------------------------------------------------------------------------
// JWT options
// ---------------------------------------------------------------------------

const JWT_VERIFY_OPTIONS: VerifyOptions = {
  algorithms: ['HS256'],
};

// ---------------------------------------------------------------------------
// Factory: create auth middleware with injected revocation service
// ---------------------------------------------------------------------------

export function createAuthMiddleware(
  revocationService: TokenRevocationService,
) {
  return async (
    req: AuthenticatedRequest,
    res: Response,
    next: NextFunction,
  ): Promise<void> => {
    try {
      // 1. Extract token from header
      const authHeader = req.headers.authorization;
      if (!authHeader || !authHeader.startsWith('Bearer ')) {
        throw unauthorized('Missing or invalid authorization header');
      }

      const token = authHeader.substring(7);

      // 2. Verify JWT signature, algorithm, and expiry
      let decoded: Record<string, unknown>;
      try {
        decoded = jwt.verify(token, JWT_SECRET, JWT_VERIFY_OPTIONS) as Record<string, unknown>;
      } catch (err) {
        if (err instanceof jwt.TokenExpiredError) {
          throw unauthorized('Token has expired');
        }
        if (err instanceof jwt.JsonWebTokenError) {
          throw unauthorized('Invalid token');
        }
        throw err;
      }

      // 3. Validate strict claim schema BEFORE constructing Redis keys
      const validation = validateDecodedToken(decoded);
      if (!validation.valid) {
        logger.warn('Token rejected — missing or invalid claims', {
          reason: validation.error,
        });
        throw unauthorized('Invalid token claims');
      }

      // 4. Reject refresh tokens used as access tokens
      if (decoded.type === 'refresh') {
        throw unauthorized('Invalid token type');
      }

      // 5. Validate session expiry (sexp)
      const now = Math.floor(Date.now() / 1000);
      if ((decoded.sexp as number) <= now) {
        throw unauthorized('Session has expired');
      }

      // 6. Build DecodedToken for revocation check
      const tokenInfo: DecodedToken = {
        jti: decoded.jti as string,
        sid: decoded.sid as string,
        sub: decoded.sub as string,
        tenantId: decoded.tenantId as string,
        type: decoded.type as 'access',
        uv: decoded.uv as number,
        sexp: decoded.sexp as number,
        iat: decoded.iat as number,
        exp: decoded.exp as number,
      };

      // 6. Check revocation state (pipelined, fail-closed)
      let revocationResult;
      try {
        revocationResult = await revocationService.checkRevoked(tokenInfo);
      } catch (redisError) {
        // Redis unavailable — 503, not 401
        logger.error('Revocation check failed — dependency unavailable', {
          correlationId: req.headers['x-correlation-id'],
        });
        res.status(503).json({
          error: {
            code: 'AUTH_DEPENDENCY_UNAVAILABLE',
            message: 'Unable to verify authentication state',
          },
        });
        return;
      }

      if (revocationResult.revoked) {
        throw unauthorized('Token has been revoked');
      }

      // 7. Attach user info to request
      const tenantId = tokenInfo.tenantId;
      req.user = {
        ...tokenInfo,
        email: (decoded.email as string) || '',
        roles: (decoded.roles as string[]) || [],
      };
      req.tenantId = tenantId;

      // 8. Add user info to headers for downstream services
      req.headers['x-user-id'] = tokenInfo.sub;
      req.headers['x-token-tenant-id'] = tenantId;
      req.headers['x-user-roles'] = ((decoded.roles as string[]) || []).join(',');

      logger.debug('User authenticated', {
        userId: tokenInfo.sub,
        tenantId,
        roles: (decoded.roles as string[]) || [],
      });

      next();
    } catch (error) {
      if (error && typeof error === 'object' && (error as any).statusCode) {
        next(error);
      } else if (error instanceof jwt.TokenExpiredError) {
        next(unauthorized('Token has expired'));
      } else if (error instanceof jwt.JsonWebTokenError) {
        next(unauthorized('Invalid token'));
      } else {
        next(error);
      }
    }
  };
}

// ---------------------------------------------------------------------------
// The exported middleware creator — call at startup and use the result
// ---------------------------------------------------------------------------

export { createAuthMiddleware as authMiddleware };

// ---------------------------------------------------------------------------
// Token generation helpers
// ---------------------------------------------------------------------------

interface GenerateTokenParams {
  sub: string;
  tenantId: string;
  sid: string;
  uv: number;
  sexp: number;
  email: string;
  roles: string[];
}

function parseDurationSeconds(
  value: string | undefined,
  fallbackSeconds: number,
  variableName: string,
): number {
  if (!value) return fallbackSeconds;
  if (/^\d+$/.test(value)) {
    const seconds = Number(value);
    if (Number.isSafeInteger(seconds) && seconds > 0) return seconds;
  }
  const match = value.match(/^(\d+)(s|m|h|d)$/i);
  if (!match) {
    throw new Error(`${variableName} must be a positive duration such as 30m, 1h, or 7d`);
  }
  const amount = Number(match[1]);
  const units: Record<string, number> = {
    s: 1,
    m: 60,
    h: 3600,
    d: 86400,
  };
  const seconds = amount * units[match[2].toLowerCase()];
  if (!Number.isSafeInteger(seconds) || seconds <= 0) {
    throw new Error(`${variableName} is outside the supported duration range`);
  }
  return seconds;
}

function cappedTokenLifetime(
  sexp: number,
  configuredValue: string | undefined,
  fallbackSeconds: number,
  variableName: string,
): number {
  const now = Math.floor(Date.now() / 1000);
  const remainingSessionSeconds = sexp - now;
  if (remainingSessionSeconds <= 0) {
    throw new Error('Cannot issue a token for an expired session');
  }
  return Math.min(
    parseDurationSeconds(configuredValue, fallbackSeconds, variableName),
    remainingSessionSeconds,
  );
}

/** Generate a signed access JWT with full claim set. */
export function generateToken(params: GenerateTokenParams): string {
  const expiresIn = cappedTokenLifetime(
    params.sexp,
    process.env.JWT_EXPIRES_IN,
    3600,
    'JWT_EXPIRES_IN',
  );

  return jwt.sign(
    {
      jti: TokenRevocationService.generateId(),
      sid: params.sid,
      sub: params.sub,
      tenantId: params.tenantId,
      email: params.email,
      roles: params.roles,
      type: 'access',
      uv: params.uv,
      sexp: params.sexp,
    },
    JWT_SECRET,
    { expiresIn, algorithm: 'HS256' } as jwt.SignOptions,
  );
}

/** Generate a signed refresh JWT with minimal claim set. */
export function generateRefreshToken(params: {
  sub: string;
  tenantId: string;
  sid: string;
  uv: number;
  sexp: number;
}): string {
  const expiresIn = cappedTokenLifetime(
    params.sexp,
    process.env.JWT_REFRESH_EXPIRES_IN,
    7 * 86400,
    'JWT_REFRESH_EXPIRES_IN',
  );

  return jwt.sign(
    {
      jti: TokenRevocationService.generateId(),
      sid: params.sid,
      sub: params.sub,
      tenantId: params.tenantId,
      type: 'refresh',
      uv: params.uv,
      sexp: params.sexp,
    },
    JWT_SECRET,
    { expiresIn, algorithm: 'HS256' } as jwt.SignOptions,
  );
}

/**
 * Decode and verify a refresh token.
 * Returns the decoded claims or throws if invalid.
 */
export function verifyRefreshToken(token: string): DecodedToken {
  const decoded = jwt.verify(token, JWT_SECRET, JWT_VERIFY_OPTIONS) as Record<string, unknown>;

  const validation = validateDecodedToken(decoded);
  if (!validation.valid) {
    throw new Error(`Invalid refresh token: ${validation.error}`);
  }

  if (decoded.type !== 'refresh') {
    throw new Error('Invalid refresh token type');
  }

  return {
    jti: decoded.jti as string,
    sid: decoded.sid as string,
    sub: decoded.sub as string,
    tenantId: decoded.tenantId as string,
    type: 'refresh',
    uv: decoded.uv as number,
    sexp: decoded.sexp as number,
    iat: decoded.iat as number,
    exp: decoded.exp as number,
  };
}

/**
 * Verify and decode an access token.
 * Used by logout and other paths that need verified access token claims.
 */
export function verifyAccessToken(token: string): DecodedToken {
  const decoded = jwt.verify(token, JWT_SECRET, JWT_VERIFY_OPTIONS) as Record<string, unknown>;

  const validation = validateDecodedToken(decoded);
  if (!validation.valid) {
    throw new Error(`Invalid access token: ${validation.error}`);
  }

  if (decoded.type !== 'access') {
    throw new Error('Not an access token');
  }

  return {
    jti: decoded.jti as string,
    sid: decoded.sid as string,
    sub: decoded.sub as string,
    tenantId: decoded.tenantId as string,
    type: 'access',
    uv: decoded.uv as number,
    sexp: decoded.sexp as number,
    iat: decoded.iat as number,
    exp: decoded.exp as number,
  };
}

// ---------------------------------------------------------------------------
// Shared helper: verify access token + revocation (used by /ws-ticket and /me)
// ---------------------------------------------------------------------------
// Centralises JWT verification, claim validation, type-check, and revocation
// check so that /ws-ticket and /me don't drift. Uses the same verifyAccessToken
// + checkRevoked chain as authMiddleware — no hand-rolled JWT unpack.

export interface VerifyAccessTokenOk {
  ok: true;
  decoded: DecodedToken;
  roles: string[];
  email: string;
}

export interface VerifyAccessTokenErr {
  ok: false;
  status: 401 | 503;
  code: string;
  message: string;
}

export type VerifyAccessTokenResult = VerifyAccessTokenOk | VerifyAccessTokenErr;

export async function verifyAccessTokenWithRevocation(
  req: Request,
  revocationService: TokenRevocationService,
): Promise<VerifyAccessTokenResult> {
  const authHeader = req.headers.authorization;
  if (!authHeader || !authHeader.startsWith('Bearer ')) {
    return {
      ok: false,
      status: 401,
      code: 'UNAUTHORIZED',
      message: 'Missing or invalid authorization header',
    };
  }

  const token = authHeader.substring(7);

  // Reuse verifyAccessToken for JWT signature, algorithm, claims, and type checks
  let decoded: DecodedToken;
  try {
    decoded = verifyAccessToken(token);
  } catch (err) {
    const msg = err instanceof jwt.TokenExpiredError
      ? 'Token has expired'
      : 'Invalid token';
    return { ok: false, status: 401, code: 'UNAUTHORIZED', message: msg };
  }

  // Extract roles + email from the raw payload (verifyAccessToken drops them)
  let rawPayload: Record<string, unknown>;
  try {
    rawPayload = jwt.decode(token) as Record<string, unknown>;
  } catch {
    return { ok: false, status: 401, code: 'UNAUTHORIZED', message: 'Invalid token' };
  }
  const roles: string[] = Array.isArray(rawPayload?.roles) ? (rawPayload.roles as string[]) : [];
  const email = typeof rawPayload?.email === 'string' ? (rawPayload.email as string) : '';

  // Check revocation state (fail-closed)
  try {
    const revResult = await revocationService.checkRevoked(decoded);
    if (revResult.revoked) {
      return {
        ok: false,
        status: 401,
        code: 'UNAUTHORIZED',
        message: 'Token has been revoked',
      };
    }
  } catch (redisError) {
    logger.error('Revocation check failed — dependency unavailable', {
      error: redisError instanceof Error ? redisError.message : String(redisError),
    });
    return {
      ok: false,
      status: 503,
      code: 'AUTH_DEPENDENCY_UNAVAILABLE',
      message: 'Unable to verify authentication state',
    };
  }

  return { ok: true, decoded, roles, email };
}

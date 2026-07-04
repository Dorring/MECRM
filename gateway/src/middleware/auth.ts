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

/** Generate a signed access JWT with full claim set. */
export function generateToken(params: GenerateTokenParams): string {
  const configuredTtl = (process.env.JWT_EXPIRES_IN || '1h') as jwt.SignOptions['expiresIn'];
  // Cap access token expiry at sexp
  const sexpDeadline = new Date(params.sexp * 1000);
  const now = Date.now();
  const maxMs = Math.max(0, sexpDeadline.getTime() - now);
  const maxSeconds = Math.floor(maxMs / 1000);
  const expiresIn = maxSeconds < 3600 ? `${Math.max(60, maxSeconds)}s` : configuredTtl;

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
  const configuredTtl = process.env.JWT_REFRESH_EXPIRES_IN || '7d';
  // Cap refresh expiry at sexp
  const sexpDeadline = new Date(params.sexp * 1000);
  const now = Date.now();
  const maxMs = Math.max(0, sexpDeadline.getTime() - now);
  const maxDays = Math.floor(maxMs / 86400000);
  const expiresIn =
    maxDays < 7 ? `${Math.max(1, maxDays)}d` : (configuredTtl as jwt.SignOptions['expiresIn']);

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

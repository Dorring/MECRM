import { Request, Response, NextFunction } from 'express';
import jwt from 'jsonwebtoken';
import { unauthorized } from './errorHandler';
import { logger } from '../utils/logger';
import { redisClient } from '../services/redis';
import { JWT_SECRET } from '../config/jwt';

export interface TokenPayload {
  sub: string;           // User ID
  tenantId?: string;     // Tenant ID (legacy)
  tenant_id?: string;    // Tenant ID (Keycloak-style)
  email: string;
  roles: string[];
  iat: number;
  exp: number;
}

export interface AuthenticatedRequest extends Request {
  user?: TokenPayload;
  tenantId?: string;
}

// JWT_SECRET is resolved and validated centrally in src/config/jwt.ts.
// In production a missing/insecure secret fails the boot; here we only read it.

export const authMiddleware = async (
  req: AuthenticatedRequest,
  res: Response,
  next: NextFunction
): Promise<void> => {
  try {
    // Extract token from header
    const authHeader = req.headers.authorization;
    
    if (!authHeader || !authHeader.startsWith('Bearer ')) {
      throw unauthorized('Missing or invalid authorization header');
    }
    
    const token = authHeader.substring(7);

    // Check if token is blacklisted (logout revocation).
    //
    // FAIL-CLOSED policy: if Redis is unavailable we cannot confirm the token
    // has NOT been revoked, so we reject the request with 401. This prevents a
    // revoked token from being accepted during a Redis outage. The error path
    // never logs the token value (only a sanitized reason + correlation id).
    //
    // An explicit opt-out is provided ONLY for non-production environments via
    // AUTH_ALLOW_OPENREDIS_FALLBACK=1, which degrades to trusting the JWT
    // signature alone. This must never be enabled in production.
    try {
      const isBlacklisted = await redisClient.get(`blacklist:${token}`);
      if (isBlacklisted) {
        throw unauthorized('Token has been revoked');
      }
    } catch (error) {
      // Re-throw our own unauthorized() errors verbatim (revocation hit).
      if (error && typeof error === 'object' && (error as any).statusCode === 401) {
        throw error;
      }

      const fallbackEnabled =
        process.env.NODE_ENV !== 'production' &&
        process.env.AUTH_ALLOW_OPENREDIS_FALLBACK === '1';

      logger.error('Token blacklist check failed', {
        reason: (error as Error).message,
        fallback: fallbackEnabled,
        correlationId: req.headers['x-correlation-id'],
      });

      if (fallbackEnabled) {
        // Bounded degradation: trust JWT signature only. Insecure; dev/test only.
        logger.warn(
          'Degrading blacklist check (AUTH_ALLOW_OPENREDIS_FALLBACK=1) — revoked tokens may be accepted until Redis recovers.'
        );
      } else {
        // Default: fail closed. Never disclose internal Redis topology to the client.
        throw unauthorized('Unable to verify token revocation status');
      }
    }

    // Verify token
    const decoded = jwt.verify(token, JWT_SECRET) as TokenPayload;
    const tenantId = decoded.tenantId || decoded.tenant_id;
    
    // Validate required claims
    if (!decoded.sub || !tenantId) {
      throw unauthorized('Invalid token claims');
    }
    
    // Attach user info to request
    req.user = { ...decoded, tenantId };
    req.tenantId = tenantId;
    
    // Add user info to headers for downstream services
    req.headers['x-user-id'] = decoded.sub;
    req.headers['x-token-tenant-id'] = tenantId;
    req.headers['x-user-roles'] = (decoded.roles || []).join(',');
    
    logger.debug('User authenticated', {
      userId: decoded.sub,
      tenantId,
      roles: decoded.roles || [],
    });
    
    next();
  } catch (error) {
    if (error instanceof jwt.TokenExpiredError) {
      next(unauthorized('Token has expired'));
    } else if (error instanceof jwt.JsonWebTokenError) {
      next(unauthorized('Invalid token'));
    } else {
      next(error);
    }
  }
};

// Generate JWT token
export const generateToken = (payload: Omit<TokenPayload, 'iat' | 'exp'>): string => {
  const expiresIn = (process.env.JWT_EXPIRES_IN || '1h') as jwt.SignOptions['expiresIn'];
  return jwt.sign(payload, JWT_SECRET, { expiresIn });
};

// Generate refresh token
export const generateRefreshToken = (userId: string, tenantId: string): string => {
  const expiresIn = (process.env.JWT_REFRESH_EXPIRES_IN || '7d') as jwt.SignOptions['expiresIn'];
  return jwt.sign(
    { sub: userId, tenantId, type: 'refresh' },
    JWT_SECRET,
    { expiresIn }
  );
};

// Verify refresh token
export const verifyRefreshToken = (token: string): { sub: string; tenantId: string } => {
  const decoded = jwt.verify(token, JWT_SECRET) as { sub: string; tenantId: string; type: string };
  
  if (decoded.type !== 'refresh') {
    throw new Error('Invalid refresh token');
  }
  
  return { sub: decoded.sub, tenantId: decoded.tenantId };
};

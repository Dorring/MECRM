import { Router, Request, Response } from 'express';
import { body, validationResult } from 'express-validator';
import bcrypt from 'bcryptjs';
import { v4 as uuidv4 } from 'uuid';
import { prisma } from '../services/prisma';
import {
  generateToken,
  generateRefreshToken,
  verifyRefreshToken,
  verifyAccessToken,
  verifyAccessTokenWithRevocation,
} from '../middleware/auth';
import { badRequest, unauthorized } from '../middleware/errorHandler';
import { logger } from '../utils/logger';
import { Prisma } from '../generated/prisma/client';
import {
  TokenRevocationService,
  DecodedToken,
} from '../services/authSession';
import { closeConnectionsByEvent } from '../services/websocket';
import {
  getCookieOptions,
  REFRESH_COOKIE,
  CSRF_COOKIE,
} from '../config/cookies';
import { generateCsrfToken, validateCsrf } from '../config/csrf';
import { RequestHandler } from 'express';

const SYSTEM_TENANT_ID = '00000000-0000-0000-0000-000000000000';

// ---------------------------------------------------------------------------
// Factory: create auth routes with injected revocation service and origin middleware
// ---------------------------------------------------------------------------

export function createAuthRoutes(
  revocationService: TokenRevocationService,
  originValidation?: RequestHandler,
): Router {
  const router = Router();

  /** Compute and apply Set-Cookie headers for refresh + CSRF cookies. */
  function setAuthCookies(
    res: Response,
    refreshToken: string,
    csrfToken: string,
  ): void {
    const opts = getCookieOptions();
    res.cookie(REFRESH_COOKIE, refreshToken, opts.refresh);
    res.cookie(CSRF_COOKIE, csrfToken, opts.csrf);
  }

  /** Clear both auth cookies. */
  function clearAuthCookies(res: Response): void {
    const opts = getCookieOptions();
    res.clearCookie(REFRESH_COOKIE, { ...opts.refresh, maxAge: 0 });
    res.clearCookie(CSRF_COOKIE, { ...opts.csrf, maxAge: 0 });
  }

  // -----------------------------------------------------------------------
  // POST /login
  // -----------------------------------------------------------------------
  router.post(
    '/login',
    originValidation ?? ((_req, _res, next) => next()),
    body('email').isEmail().normalizeEmail(),
    body('password').isLength({ min: 8 }),
    body('tenantSlug').isLength({ min: 2, max: 50 }).matches(/^[a-z0-9-]+$/),
    async (req: Request, res: Response, next) => {
      try {
        const errors = validationResult(req);
        if (!errors.isEmpty()) {
          throw badRequest('Validation failed', errors.array());
        }

        const { email, password, tenantSlug } = req.body;

        const { user, roles } = await prisma.$transaction(
          async (db: Prisma.TransactionClient) => {
            await db.$executeRaw`SELECT set_config('app.tenant_id', ${SYSTEM_TENANT_ID}, true)`;

            const tenant = await db.tenant.findUnique({
              where: { slug: tenantSlug },
            });

            if (!tenant) {
              throw unauthorized('Invalid credentials');
            }

            await db.$executeRaw`SELECT set_config('app.tenant_id', ${tenant.id}, true)`;

            const user = await db.user.findFirst({
              where: { email, tenantId: tenant.id },
              include: {
                userRoles: {
                  include: { role: true },
                },
                tenant: true,
              },
            });

            if (!user || !user.passwordHash) {
              throw unauthorized('Invalid credentials');
            }

            const validPassword = await bcrypt.compare(
              password,
              user.passwordHash,
            );
            if (!validPassword) {
              throw unauthorized('Invalid credentials');
            }

            if (user.status !== 'active') {
              throw unauthorized('Account is not active');
            }

            const roles = user.userRoles.map(
              (ur: { role: { name: string } }) => ur.role.name,
            );

            await db.user.update({
              where: { id: user.id },
              data: { lastLoginAt: new Date() },
            });

            return { user, roles };
          },
        );

        // Obtain current user revocation generation
        let uv: number;
        try {
          uv = await revocationService.getUserVersion(
            user.tenantId,
            user.id,
          );
        } catch (redisError) {
          logger.error('Login failed — unable to read user version', {
            error: redisError instanceof Error ? redisError.message : String(redisError),
          });
          res.status(503).json({
            error: {
              code: 'AUTH_DEPENDENCY_UNAVAILABLE',
              message: 'Unable to complete authentication',
            },
          });
          return;
        }

        // Generate session id and absolute session expiry
        const sid = TokenRevocationService.generateId();
        const sexp =
          Math.floor(Date.now() / 1000) +
          7 * 86400; // 7-day absolute session expiry

        const accessToken = generateToken({
          sub: user.id,
          tenantId: user.tenantId,
          sid,
          uv,
          sexp,
          email: user.email,
          roles,
        });

        const refreshToken = generateRefreshToken({
          sub: user.id,
          tenantId: user.tenantId,
          sid,
          uv,
          sexp,
        });

        // Set HttpOnly refresh cookie + CSRF cookie
        const csrfToken = generateCsrfToken();
        setAuthCookies(res, refreshToken, csrfToken);

        logger.info('User logged in', {
          userId: user.id,
          tenantId: user.tenantId,
        });

        res.json({
          accessToken,
          user: {
            id: user.id,
            email: user.email,
            name: user.name,
            roles,
            tenant: {
              id: user.tenant.id,
              name: user.tenant.name,
            },
          },
        });
      } catch (error) {
        next(error);
      }
    },
  );

  // -----------------------------------------------------------------------
  // POST /refresh
  // -----------------------------------------------------------------------
  router.post(
    '/refresh',
    originValidation ?? ((_req, _res, next) => next()),
    async (req: Request, res: Response, next) => {
      try {
        // CSRF double-submit validation
        if (!validateCsrf(req)) {
          res.status(403).json({
            error: {
              code: 'CSRF_VALIDATION_FAILED',
              message: 'CSRF token missing or invalid',
            },
          });
          return;
        }

        // Read refresh token from cookie (NOT from request body)
        const refreshToken = req.cookies?.[REFRESH_COOKIE];
        if (!refreshToken || typeof refreshToken !== 'string') {
          throw unauthorized('Missing refresh token');
        }

        // Verify signature, algorithm and claims
        let decoded: DecodedToken;
        try {
          decoded = verifyRefreshToken(refreshToken);
        } catch {
          throw unauthorized('Invalid refresh token');
        }

        // Verify user exists and is active
        let user: {
          id: string;
          tenantId: string;
          status: string;
          email?: string;
          userRoles?: Array<{ role: { name: string } }>;
        } | null;
        try {
          const result = await prisma.$transaction(
            async (db: Prisma.TransactionClient) => {
              await db.$executeRaw`SELECT set_config('app.tenant_id', ${decoded.tenantId}, true)`;
              return db.user.findFirst({
                where: { id: decoded.sub, tenantId: decoded.tenantId },
                include: {
                  userRoles: {
                    include: { role: true },
                  },
                },
              });
            },
          );
          user = result;
        } catch {
          throw unauthorized('User not found or inactive');
        }

        if (!user || user.status !== 'active') {
          throw unauthorized('User not found or inactive');
        }

        // Atomically consume the refresh token (Lua script)
        const consumeResult = await revocationService.consumeRefresh(decoded);

        switch (consumeResult.status) {
          case 'DEPENDENCY_ERROR': {
            logger.error('Refresh failed — Redis dependency unavailable');
            res.status(503).json({
              error: {
                code: 'AUTH_DEPENDENCY_UNAVAILABLE',
                message: 'Unable to verify token status',
              },
            });
            return;
          }

          case 'REVOKED': {
            throw unauthorized('Token has been revoked');
          }

          case 'REPLAY': {
            // Replay detected — sid has been revoked. Publish event.
            try {
              await revocationService.revokeSid(
                decoded.tenantId,
                decoded.sid,
                decoded.sexp,
              );
            } catch {
              // Best-effort; Lua already wrote the sid-revoked key.
            }
            throw unauthorized('Token has been revoked');
          }

          case 'OK': {
            // Mint new token pair — preserve sid, uv, sexp
            const roles =
              (user as any).userRoles
                ?.map((ur: { role: { name: string } }) => ur.role.name) || [];

            const newAccessToken = generateToken({
              sub: decoded.sub,
              tenantId: decoded.tenantId,
              sid: decoded.sid,
              uv: decoded.uv,
              sexp: decoded.sexp,
              email: (user as any).email || '',
              roles,
            });

            const newRefreshToken = generateRefreshToken({
              sub: decoded.sub,
              tenantId: decoded.tenantId,
              sid: decoded.sid,
              uv: decoded.uv,
              sexp: decoded.sexp,
            });

            // Rotate both cookies
            const newCsrfToken = generateCsrfToken();
            setAuthCookies(res, newRefreshToken, newCsrfToken);

            // Body returns only access token — no refreshToken
            res.json({
              accessToken: newAccessToken,
            });
            return;
          }
        }
      } catch (error) {
        next(error);
      }
    },
  );

  // -----------------------------------------------------------------------
  // POST /logout
  // -----------------------------------------------------------------------
  router.post(
    '/logout',
    originValidation ?? ((_req, _res, next) => next()),
    async (req: Request, res: Response, next) => {
      try {
        const authHeader = req.headers.authorization;
        if (!authHeader || !authHeader.startsWith('Bearer ')) {
          throw unauthorized('Missing or invalid authorization header');
        }

        const token = authHeader.substring(7);

        // Verify the access token fully — never trust jwt.decode()
        let accessDecoded: DecodedToken;
        try {
          accessDecoded = verifyAccessToken(token);
        } catch {
          throw unauthorized('Invalid token');
        }

        // Validate optional refresh token from cookie
        const cookieRefresh = req.cookies?.[REFRESH_COOKIE];
        if (cookieRefresh && typeof cookieRefresh === 'string') {
          try {
            const refreshDecoded = verifyRefreshToken(cookieRefresh);
            // Verify refresh token matches the access token
            if (
              refreshDecoded.tenantId !== accessDecoded.tenantId ||
              refreshDecoded.sub !== accessDecoded.sub ||
              refreshDecoded.sid !== accessDecoded.sid
            ) {
              // Mismatch — log but don't fail logout (cookie cleanup is best-effort)
              logger.warn('Refresh cookie does not match access token session', {
                userId: accessDecoded.sub,
                tenantId: accessDecoded.tenantId,
              });
            }
          } catch {
            // Malformed cookie — log warning, still clear cookies
            logger.warn('Malformed refresh token in cookie during logout');
          }
        }

        // Revoke the session (sid) — invalidates all tokens in this session
        try {
          await revocationService.revokeSid(
            accessDecoded.tenantId,
            accessDecoded.sid,
            accessDecoded.sexp,
          );

          // Close local WebSocket connections for this session
          closeConnectionsByEvent({
            type: 'sid',
            tenantId: accessDecoded.tenantId,
            id: accessDecoded.sid,
            userId: accessDecoded.sub,
          });
        } catch (redisError) {
          logger.error('Logout failed — unable to persist revocation', {
            error: redisError instanceof Error ? redisError.message : String(redisError),
          });
          // FAIL-CLOSED: do NOT clear cookies if revocation was not persisted
          res.status(503).json({
            error: {
              code: 'AUTH_DEPENDENCY_UNAVAILABLE',
              message: 'Unable to complete logout',
            },
          });
          return;
        }

        // Clear cookies after successful revocation
        clearAuthCookies(res);

        logger.info('User logged out', {
          userId: accessDecoded.sub,
          tenantId: accessDecoded.tenantId,
        });

        res.json({ message: 'Logged out successfully' });
      } catch (error) {
        next(error);
      }
    },
  );

  // -----------------------------------------------------------------------
  // POST /register
  // -----------------------------------------------------------------------
  router.post(
    '/register',
    originValidation ?? ((_req, _res, next) => next()),
    body('tenantName').isLength({ min: 2, max: 100 }),
    body('tenantSlug').isLength({ min: 2, max: 50 }).matches(/^[a-z0-9-]+$/),
    body('email').isEmail().normalizeEmail(),
    body('password').isLength({ min: 8 }),
    body('name').isLength({ min: 2, max: 100 }),
    async (req: Request, res: Response, next) => {
      try {
        const errors = validationResult(req);
        if (!errors.isEmpty()) {
          throw badRequest('Validation failed', errors.array());
        }

        const { tenantName, tenantSlug, email, password, name } = req.body;

        // Check if tenant slug exists
        const existingTenant = await prisma.tenant.findUnique({
          where: { slug: tenantSlug },
        });

        if (existingTenant) {
          throw badRequest('Tenant slug already exists');
        }

        // Hash password
        const passwordHash = await bcrypt.hash(password, 12);

        // Create tenant, user, and admin role in transaction
        const result = await prisma.$transaction(
          async (tx: Prisma.TransactionClient) => {
            await tx.$executeRaw`SELECT set_config('app.tenant_id', ${SYSTEM_TENANT_ID}, true)`;
            const tenant = await tx.tenant.create({
              data: {
                id: uuidv4(),
                name: tenantName,
                slug: tenantSlug,
              },
            });

            await tx.$executeRaw`SELECT set_config('app.tenant_id', ${tenant.id}, true)`;

            const adminRole = await tx.role.create({
              data: {
                id: uuidv4(),
                tenantId: tenant.id,
                name: 'admin',
                description: 'Administrator with full access',
                permissions: JSON.stringify(['*']),
                isSystem: true,
              },
            });

            const user = await tx.user.create({
              data: {
                id: uuidv4(),
                tenantId: tenant.id,
                email,
                passwordHash,
                name,
              },
            });

            await tx.userRole.create({
              data: {
                id: uuidv4(),
                tenantId: tenant.id,
                userId: user.id,
                roleId: adminRole.id,
              },
            });

            return { tenant, user, adminRole };
          },
        );

        logger.info('New tenant registered', {
          tenantId: result.tenant.id,
          userId: result.user.id,
        });

        // Get user revocation generation
        let uv: number;
        try {
          uv = await revocationService.getUserVersion(
            result.tenant.id,
            result.user.id,
          );
        } catch (redisError) {
          logger.error('Registration failed — unable to read user version', {
            error: redisError instanceof Error ? redisError.message : String(redisError),
          });
          res.status(503).json({
            error: {
              code: 'AUTH_DEPENDENCY_UNAVAILABLE',
              message: 'Unable to complete registration',
            },
          });
          return;
        }

        const sid = TokenRevocationService.generateId();
        const sexp =
          Math.floor(Date.now() / 1000) + 7 * 86400;

        const accessToken = generateToken({
          sub: result.user.id,
          tenantId: result.tenant.id,
          sid,
          uv,
          sexp,
          email: result.user.email,
          roles: ['admin'],
        });

        const refreshToken = generateRefreshToken({
          sub: result.user.id,
          tenantId: result.tenant.id,
          sid,
          uv,
          sexp,
        });

        // Set HttpOnly refresh cookie + CSRF cookie
        const csrfToken = generateCsrfToken();
        setAuthCookies(res, refreshToken, csrfToken);

        res.status(201).json({
          accessToken,
          user: {
            id: result.user.id,
            email: result.user.email,
            name: result.user.name,
            roles: ['admin'],
            tenant: {
              id: result.tenant.id,
              name: result.tenant.name,
            },
          },
        });
      } catch (error) {
        next(error);
      }
    },
  );

  // -----------------------------------------------------------------------
  // POST /migrate-cookie (temporary — remove 7 days post-deploy)
  // -----------------------------------------------------------------------
  router.post(
    '/migrate-cookie',
    originValidation ?? ((_req, _res, next) => next()),
    body('refreshToken').notEmpty(),
    async (req: Request, res: Response, next) => {
      try {
        const { refreshToken } = req.body;

        // Verify signature, algorithm and claims
        let decoded: DecodedToken;
        try {
          decoded = verifyRefreshToken(refreshToken);
        } catch {
          throw unauthorized('Invalid refresh token');
        }

        // No CSRF required — this is a one-time migration, no cookie exists yet.
        // Origin validation still applies (applied above).

        // Verify user exists and is active
        let user: {
          id: string;
          tenantId: string;
          status: string;
          email?: string;
          userRoles?: Array<{ role: { name: string } }>;
        } | null;
        try {
          const result = await prisma.$transaction(
            async (db: Prisma.TransactionClient) => {
              await db.$executeRaw`SELECT set_config('app.tenant_id', ${decoded.tenantId}, true)`;
              return db.user.findFirst({
                where: { id: decoded.sub, tenantId: decoded.tenantId },
                include: {
                  userRoles: {
                    include: { role: true },
                  },
                },
              });
            },
          );
          user = result;
        } catch {
          throw unauthorized('User not found or inactive');
        }

        if (!user || user.status !== 'active') {
          throw unauthorized('User not found or inactive');
        }

        // Atomically consume the refresh token (Lua script)
        const consumeResult = await revocationService.consumeRefresh(decoded);

        switch (consumeResult.status) {
          case 'DEPENDENCY_ERROR': {
            logger.error('Migration failed — Redis dependency unavailable');
            res.status(503).json({
              error: {
                code: 'AUTH_DEPENDENCY_UNAVAILABLE',
                message: 'Unable to verify token status',
              },
            });
            return;
          }

          case 'REVOKED': {
            throw unauthorized('Token has been revoked');
          }

          case 'REPLAY': {
            try {
              await revocationService.revokeSid(
                decoded.tenantId,
                decoded.sid,
                decoded.sexp,
              );
            } catch {
              // Best-effort
            }
            throw unauthorized('Token has been revoked');
          }

          case 'OK': {
            const roles =
              (user as any).userRoles
                ?.map((ur: { role: { name: string } }) => ur.role.name) || [];

            const newAccessToken = generateToken({
              sub: decoded.sub,
              tenantId: decoded.tenantId,
              sid: decoded.sid,
              uv: decoded.uv,
              sexp: decoded.sexp,
              email: (user as any).email || '',
              roles,
            });

            const newRefreshToken = generateRefreshToken({
              sub: decoded.sub,
              tenantId: decoded.tenantId,
              sid: decoded.sid,
              uv: decoded.uv,
              sexp: decoded.sexp,
            });

            // Issue cookies for the first time
            const csrfToken = generateCsrfToken();
            setAuthCookies(res, newRefreshToken, csrfToken);

            res.json({
              accessToken: newAccessToken,
            });
            return;
          }
        }
      } catch (error) {
        next(error);
      }
    },
  );

  // -----------------------------------------------------------------------
  // POST /ws-ticket — issue a single-use, tenant-bound WS connection ticket
  // -----------------------------------------------------------------------
  router.post(
    '/ws-ticket',
    originValidation ?? ((_req, _res, next) => next()),
    async (req: Request, res: Response, next) => {
      try {
        // Verify access token + revocation (shared with /me)
        const verifyResult = await verifyAccessTokenWithRevocation(req, revocationService);
        if (!verifyResult.ok) {
          res.status(verifyResult.status).json({
            error: { code: verifyResult.code, message: verifyResult.message },
          });
          return;
        }

        const { decoded: accessDecoded, roles } = verifyResult;

        // Per-user rate limit via encapsulated service method
        try {
          const exceeded = await revocationService.consumeWsTicketRateLimit(accessDecoded.sub);
          if (exceeded) {
            res.status(429).json({
              error: {
                code: 'RATE_LIMITED',
                message: 'Too many WS ticket requests',
              },
            });
            return;
          }
        } catch (redisError) {
          logger.error('WS ticket rate limit check failed', {
            error: redisError instanceof Error ? redisError.message : String(redisError),
          });
          res.status(503).json({
            error: {
              code: 'AUTH_DEPENDENCY_UNAVAILABLE',
              message: 'Unable to issue WS ticket',
            },
          });
          return;
        }

        // Issue ticket (tenant-bound, session-bound, with real roles)
        try {
          const ticket = await revocationService.issueWsTicket({
            tenantId: accessDecoded.tenantId,
            userId: accessDecoded.sub,
            jti: accessDecoded.jti,
            sid: accessDecoded.sid,
            exp: accessDecoded.exp,
            sexp: accessDecoded.sexp,
            uv: accessDecoded.uv,
            roles,
          });

          res.json({ ticket });
        } catch (redisError) {
          logger.error('WS ticket issue failed — Redis unavailable', {
            error: redisError instanceof Error ? redisError.message : String(redisError),
          });
          res.status(503).json({
            error: {
              code: 'AUTH_DEPENDENCY_UNAVAILABLE',
              message: 'Unable to issue WS ticket',
            },
          });
        }
      } catch (error) {
        next(error);
      }
    },
  );

  // -----------------------------------------------------------------------
  // GET /me — return authenticated user profile from access token
  // -----------------------------------------------------------------------
  // Uses the same verifyAccessTokenWithRevocation helper as /ws-ticket so
  // JWT verification, claim validation, and revocation checks never drift.
  // Does NOT query business tables — all claims come from the verified token.
  router.get(
    '/me',
    async (req: Request, res: Response, next) => {
      try {
        const verifyResult = await verifyAccessTokenWithRevocation(req, revocationService);
        if (!verifyResult.ok) {
          res.status(verifyResult.status).json({
            error: { code: verifyResult.code, message: verifyResult.message },
          });
          return;
        }

        const { decoded, roles, email } = verifyResult;

        res.json({
          id: decoded.sub,
          email,
          name: '',  // token doesn't carry name; populated by frontend from login cache
          tenantId: decoded.tenantId,
          roles,
        });
      } catch (error) {
        next(error);
      }
    },
  );

  return router;
}

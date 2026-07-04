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
  AuthenticatedRequest,
} from '../middleware/auth';
import { badRequest, unauthorized } from '../middleware/errorHandler';
import { logger } from '../utils/logger';
import { Prisma } from '@prisma/client';
import {
  TokenRevocationService,
  DecodedToken,
} from '../services/authSession';
import { closeConnectionsByEvent } from '../services/websocket';

const SYSTEM_TENANT_ID = '00000000-0000-0000-0000-000000000000';

// ---------------------------------------------------------------------------
// Factory: create auth routes with injected revocation service
// ---------------------------------------------------------------------------

export function createAuthRoutes(revocationService: TokenRevocationService): Router {
  const router = Router();

  // -----------------------------------------------------------------------
  // POST /login
  // -----------------------------------------------------------------------
  router.post(
    '/login',
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

        logger.info('User logged in', {
          userId: user.id,
          tenantId: user.tenantId,
        });

        res.json({
          accessToken,
          refreshToken,
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

            res.json({
              accessToken: newAccessToken,
              refreshToken: newRefreshToken,
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
    async (req: AuthenticatedRequest, res: Response, next) => {
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

        // Validate the optional refreshToken from body if provided
        const { refreshToken } = req.body;
        if (refreshToken) {
          try {
            const refreshDecoded = verifyRefreshToken(refreshToken);
            // Verify refresh token matches the access token
            if (
              refreshDecoded.tenantId !== accessDecoded.tenantId ||
              refreshDecoded.sub !== accessDecoded.sub ||
              refreshDecoded.sid !== accessDecoded.sid
            ) {
              throw unauthorized('Refresh token does not match session');
            }
          } catch (err) {
            if ((err as any).statusCode === 401) throw err;
            // Invalid/malformed refresh token supplied — ignore, still revoke by access
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
          res.status(503).json({
            error: {
              code: 'AUTH_DEPENDENCY_UNAVAILABLE',
              message: 'Unable to complete logout',
            },
          });
          return;
        }

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

        res.status(201).json({
          accessToken,
          refreshToken,
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

  return router;
}

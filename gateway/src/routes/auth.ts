import { Router, Request, Response } from 'express';
import { body, validationResult } from 'express-validator';
import bcrypt from 'bcryptjs';
import { v4 as uuidv4 } from 'uuid';
import { prisma } from '../services/prisma';
import { generateToken, generateRefreshToken, verifyRefreshToken } from '../middleware/auth';
import { badRequest, unauthorized } from '../middleware/errorHandler';
import { redisClient } from '../services/redis';
import { logger } from '../utils/logger';
import { Prisma } from '@prisma/client';

const router = Router();
const SYSTEM_TENANT_ID = '00000000-0000-0000-0000-000000000000';

// Login
router.post('/login',
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

      const { user, roles } = await prisma.$transaction(async (db: Prisma.TransactionClient) => {
        await db.$executeRaw`SET LOCAL app.tenant_id = ${SYSTEM_TENANT_ID}`;

        const tenant = await db.tenant.findUnique({
          where: { slug: tenantSlug },
        });

        if (!tenant) {
          throw unauthorized('Invalid credentials');
        }

        await db.$executeRaw`SET LOCAL app.tenant_id = ${tenant.id}`;

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

        const validPassword = await bcrypt.compare(password, user.passwordHash);
        if (!validPassword) {
          throw unauthorized('Invalid credentials');
        }

        if (user.status !== 'active') {
          throw unauthorized('Account is not active');
        }

        const roles = user.userRoles.map((ur: { role: { name: string } }) => ur.role.name);

        await db.user.update({
          where: { id: user.id },
          data: { lastLoginAt: new Date() },
        });

        return { user, roles };
      });

      const accessToken = generateToken({
        sub: user.id,
        tenantId: user.tenantId,
        email: user.email,
        roles,
      });
      const refreshToken = generateRefreshToken(user.id, user.tenantId);

      logger.info('User logged in', { userId: user.id, tenantId: user.tenantId });

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
  }
);

// Refresh token
router.post('/refresh',
  body('refreshToken').notEmpty(),
  async (req: Request, res: Response, next) => {
    try {
      const { refreshToken } = req.body;
      
      // Check if token is blacklisted
      try {
        const isBlacklisted = await redisClient.get(`blacklist:${refreshToken}`);
        if (isBlacklisted) {
          throw unauthorized('Token has been revoked');
        }
      } catch (error) {
        logger.error('Refresh token blacklist check failed', { error: (error as Error).message });
      }
      
      // Verify refresh token
      let userId: string;
      let tenantId: string;
      try {
        ({ sub: userId, tenantId } = verifyRefreshToken(refreshToken));
      } catch {
        throw unauthorized('Invalid refresh token');
      }
      
      const user = await prisma.$transaction(async (db: Prisma.TransactionClient) => {
        await db.$executeRaw`SET LOCAL app.tenant_id = ${tenantId}`;
        return db.user.findFirst({
          where: { id: userId, tenantId },
          include: {
            userRoles: {
              include: { role: true },
            },
          },
        });
      });
      
      if (!user || user.status !== 'active') {
        throw unauthorized('User not found or inactive');
      }
      
      // Generate new tokens
      const roles = user.userRoles.map((ur: { role: { name: string } }) => ur.role.name);
      const newAccessToken = generateToken({
        sub: user.id,
        tenantId: user.tenantId,
        email: user.email,
        roles,
      });
      const newRefreshToken = generateRefreshToken(user.id, user.tenantId);
      
      // Blacklist old refresh token
      await redisClient.setex(`blacklist:${refreshToken}`, 86400 * 7, '1');
      
      res.json({
        accessToken: newAccessToken,
        refreshToken: newRefreshToken,
      });
    } catch (error) {
      next(error);
    }
  }
);

// Logout
router.post('/logout', async (req: Request, res: Response, next) => {
  try {
    const authHeader = req.headers.authorization;
    if (authHeader?.startsWith('Bearer ')) {
      const token = authHeader.substring(7);
      // Blacklist the access token
      await redisClient.setex(`blacklist:${token}`, 3600, '1');
    }
    
    const { refreshToken } = req.body;
    if (refreshToken) {
      await redisClient.setex(`blacklist:${refreshToken}`, 86400 * 7, '1');
    }
    
    res.json({ message: 'Logged out successfully' });
  } catch (error) {
    next(error);
  }
});

// Register (creates tenant and admin user)
router.post('/register',
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
      const result = await prisma.$transaction(async (tx: Prisma.TransactionClient) => {
        await tx.$executeRaw`SET LOCAL app.tenant_id = ${SYSTEM_TENANT_ID}`;
        // Create tenant
        const tenant = await tx.tenant.create({
          data: {
            id: uuidv4(),
            name: tenantName,
            slug: tenantSlug,
          },
        });

        await tx.$executeRaw`SET LOCAL app.tenant_id = ${tenant.id}`;
        
        // Create admin role
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
        
        // Create user
        const user = await tx.user.create({
          data: {
            id: uuidv4(),
            tenantId: tenant.id,
            email,
            passwordHash,
            name,
          },
        });
        
        // Assign admin role
        await tx.userRole.create({
          data: {
            id: uuidv4(),
            tenantId: tenant.id,
            userId: user.id,
            roleId: adminRole.id,
          },
        });
        
        return { tenant, user, adminRole };
      });
      
      logger.info('New tenant registered', {
        tenantId: result.tenant.id,
        userId: result.user.id,
      });
      
      // Generate tokens
      const accessToken = generateToken({
        sub: result.user.id,
        tenantId: result.tenant.id,
        email: result.user.email,
        roles: ['admin'],
      });
      const refreshToken = generateRefreshToken(result.user.id, result.tenant.id);
      
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
  }
);

export default router;

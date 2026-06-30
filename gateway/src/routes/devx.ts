import { Router, Response, NextFunction } from 'express';
import axios from 'axios';
import { param, validationResult } from 'express-validator';
import { AuthenticatedRequest } from '../middleware/auth';
import { badRequest, forbidden } from '../middleware/errorHandler';
import { v4 as uuidv4 } from 'uuid';

const router = Router();

const AGENTS_URL = (process.env.AGENTS_URL || 'http://localhost:5010').replace(/\/$/, '');

// Roles allowed to view DevX insights (engineers only)
const DEVX_ROLES = ['admin', 'super_admin', 'engineer', 'sre'];

/**
 * Check if user has required role
 */
const hasRole = (userRoles: string[], allowedRoles: string[]): boolean => {
  return userRoles.some(role => allowedRoles.includes(role));
};

/**
 * GET /api/intelligence/devx/insights
 * Get active DevX insights
 */
router.get(
  '/insights',
  async (req: AuthenticatedRequest, res: Response, next: NextFunction) => {
    try {
      const userRoles = req.user?.roles || [];
      if (!hasRole(userRoles, DEVX_ROLES)) {
        throw forbidden('Access restricted to engineering roles');
      }

      const correlationId = (req.headers['x-correlation-id'] as string) || uuidv4();

      const response = await axios.get(`${AGENTS_URL}/api/v1/intelligence/devx/insights`, {
        timeout: 5000,
        headers: {
          'Content-Type': 'application/json',
          'X-User-Id': req.user?.sub,
          'X-Correlation-Id': correlationId,
          ...(req.headers.authorization ? { Authorization: String(req.headers.authorization) } : {}),
        },
      });

      res.json(response.data);
    } catch (error) {
      next(error);
    }
  }
);

/**
 * GET /api/intelligence/devx/insights/:id
 * Get a specific DevX insight
 */
router.get(
  '/insights/:id',
  param('id').isUUID(),
  async (req: AuthenticatedRequest, res: Response, next: NextFunction) => {
    try {
      const errors = validationResult(req);
      if (!errors.isEmpty()) {
        throw badRequest('Validation failed', errors.array());
      }

      const userRoles = req.user?.roles || [];
      if (!hasRole(userRoles, DEVX_ROLES)) {
        throw forbidden('Access restricted to engineering roles');
      }

      const insightId = req.params.id;
      const correlationId = (req.headers['x-correlation-id'] as string) || uuidv4();

      const response = await axios.get(
        `${AGENTS_URL}/api/v1/intelligence/devx/insights/${insightId}`,
        {
          timeout: 5000,
          headers: {
            'Content-Type': 'application/json',
            'X-User-Id': req.user?.sub,
            'X-Correlation-Id': correlationId,
            ...(req.headers.authorization ? { Authorization: String(req.headers.authorization) } : {}),
          },
        }
      );

      res.json(response.data);
    } catch (error) {
      next(error);
    }
  }
);

/**
 * POST /api/intelligence/devx/insights/:id/acknowledge
 * Acknowledge an insight
 */
router.post(
  '/insights/:id/acknowledge',
  param('id').isUUID(),
  async (req: AuthenticatedRequest, res: Response, next: NextFunction) => {
    try {
      const errors = validationResult(req);
      if (!errors.isEmpty()) {
        throw badRequest('Validation failed', errors.array());
      }

      const userRoles = req.user?.roles || [];
      if (!hasRole(userRoles, DEVX_ROLES)) {
        throw forbidden('Access restricted to engineering roles');
      }

      const insightId = req.params.id;
      const correlationId = (req.headers['x-correlation-id'] as string) || uuidv4();

      const response = await axios.post(
        `${AGENTS_URL}/api/v1/intelligence/devx/insights/${insightId}/acknowledge`,
        { user_id: req.user?.sub },
        {
          timeout: 5000,
          headers: {
            'Content-Type': 'application/json',
            'X-User-Id': req.user?.sub,
            'X-Correlation-Id': correlationId,
            ...(req.headers.authorization ? { Authorization: String(req.headers.authorization) } : {}),
          },
        }
      );

      res.json(response.data);
    } catch (error) {
      next(error);
    }
  }
);

/**
 * POST /api/intelligence/devx/insights/:id/resolve
 * Resolve an insight
 */
router.post(
  '/insights/:id/resolve',
  param('id').isUUID(),
  async (req: AuthenticatedRequest, res: Response, next: NextFunction) => {
    try {
      const errors = validationResult(req);
      if (!errors.isEmpty()) {
        throw badRequest('Validation failed', errors.array());
      }

      const userRoles = req.user?.roles || [];
      if (!hasRole(userRoles, DEVX_ROLES)) {
        throw forbidden('Access restricted to engineering roles');
      }

      const insightId = req.params.id;
      const correlationId = (req.headers['x-correlation-id'] as string) || uuidv4();

      const response = await axios.post(
        `${AGENTS_URL}/api/v1/intelligence/devx/insights/${insightId}/resolve`,
        {},
        {
          timeout: 5000,
          headers: {
            'Content-Type': 'application/json',
            'X-User-Id': req.user?.sub,
            'X-Correlation-Id': correlationId,
            ...(req.headers.authorization ? { Authorization: String(req.headers.authorization) } : {}),
          },
        }
      );

      res.json(response.data);
    } catch (error) {
      next(error);
    }
  }
);

/**
 * GET /api/intelligence/devx/health
 * Get current system health summary
 */
router.get(
  '/health',
  async (req: AuthenticatedRequest, res: Response, next: NextFunction) => {
    try {
      const userRoles = req.user?.roles || [];
      if (!hasRole(userRoles, DEVX_ROLES)) {
        throw forbidden('Access restricted to engineering roles');
      }

      const correlationId = (req.headers['x-correlation-id'] as string) || uuidv4();

      const response = await axios.get(`${AGENTS_URL}/api/v1/intelligence/devx/health`, {
        timeout: 5000,
        headers: {
          'Content-Type': 'application/json',
          'X-User-Id': req.user?.sub,
          'X-Correlation-Id': correlationId,
          ...(req.headers.authorization ? { Authorization: String(req.headers.authorization) } : {}),
        },
      });

      res.json(response.data);
    } catch (error) {
      next(error);
    }
  }
);

export default router;

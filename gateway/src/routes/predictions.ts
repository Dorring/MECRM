import { Router, Response } from 'express';
import { query, validationResult } from 'express-validator';
import { withTenantDb } from '../services/prisma';
import { AuthenticatedRequest } from '../middleware/auth';
import { badRequest } from '../middleware/errorHandler';

const router = Router();

router.get(
  '/latest',
  query('entityType').isString().isLength({ min: 1, max: 50 }),
  query('entityIds').isString().isLength({ min: 1, max: 5000 }),
  async (req: AuthenticatedRequest, res: Response, next) => {
    try {
      const errors = validationResult(req);
      if (!errors.isEmpty()) throw badRequest('Validation failed', errors.array());

      const entityType = String(req.query.entityType);
      const entityIds = String(req.query.entityIds)
        .split(',')
        .map((s) => s.trim())
        .filter(Boolean)
        .slice(0, 200);

      const rows = await withTenantDb(req.tenantId!, async (db) => {
        return db.prediction.findMany({
          where: { tenantId: req.tenantId!, entityType, entityId: { in: entityIds } },
          orderBy: { createdAt: 'desc' },
          take: 1000,
        });
      });

      const latest: Record<string, Record<string, any>> = {};
      for (const p of rows) {
        if (!latest[p.entityId]) latest[p.entityId] = {};
        if (!latest[p.entityId][p.predictionType]) latest[p.entityId][p.predictionType] = p;
      }

      res.json({ entityType, data: latest });
    } catch (error) {
      next(error);
    }
  }
);

export default router;


import { Router, Response } from 'express';
import { body, validationResult } from 'express-validator';
import { v4 as uuidv4 } from 'uuid';
import { AuthenticatedRequest } from '../middleware/auth';
import { badRequest, forbidden } from '../middleware/errorHandler';
import { publishEvent, TOPICS } from '../services/kafka';
import { secureCache } from '../services/secureCache';
import { cacheInvalidationTotal } from '../services/metrics';

const router = Router();

const requireAdmin = (req: AuthenticatedRequest): void => {
  const roles = req.user?.roles || [];
  if (!roles.includes('admin') && !roles.includes('super_admin')) throw forbidden('Admin role required');
};

router.post(
  '/role-changed',
  body('userId').isString().notEmpty(),
  async (req: AuthenticatedRequest, res: Response, next) => {
    try {
      requireAdmin(req);
      const errors = validationResult(req);
      if (!errors.isEmpty()) throw badRequest('Validation failed', errors.array());

      const userId = req.body.userId as string;
      await publishEvent(TOPICS.SECURITY_EVENTS, {
        type: 'crm.security.events',
        source: '/services/gateway',
        id: uuidv4(),
        tenantid: req.tenantId!,
        data: { eventType: 'role_changed', userId },
      });
      await secureCache.bumpUserEpoch(req.tenantId!, userId);
      cacheInvalidationTotal.labels(req.tenantId!, `role_changed:${userId}`).inc();
      res.json({ ok: true });
    } catch (error) {
      next(error);
    }
  }
);

router.post(
  '/permission-updated',
  body('userId').isString().notEmpty(),
  async (req: AuthenticatedRequest, res: Response, next) => {
    try {
      requireAdmin(req);
      const errors = validationResult(req);
      if (!errors.isEmpty()) throw badRequest('Validation failed', errors.array());

      const userId = req.body.userId as string;
      await publishEvent(TOPICS.SECURITY_EVENTS, {
        type: 'crm.security.events',
        source: '/services/gateway',
        id: uuidv4(),
        tenantid: req.tenantId!,
        data: { eventType: 'permission_updated', userId },
      });
      await secureCache.bumpUserEpoch(req.tenantId!, userId);
      cacheInvalidationTotal.labels(req.tenantId!, `permission_updated:${userId}`).inc();
      res.json({ ok: true });
    } catch (error) {
      next(error);
    }
  }
);

router.post('/tenant-suspended', async (req: AuthenticatedRequest, res: Response, next) => {
  try {
    requireAdmin(req);
    await publishEvent(TOPICS.SECURITY_EVENTS, {
      type: 'crm.security.events',
      source: '/services/gateway',
      id: uuidv4(),
      tenantid: req.tenantId!,
      data: { eventType: 'tenant_suspended' },
    });
    await secureCache.bumpTenantEpoch(req.tenantId!);
    cacheInvalidationTotal.labels(req.tenantId!, 'tenant_suspended').inc();
    res.json({ ok: true });
  } catch (error) {
    next(error);
  }
});

router.post(
  '/policy-updated',
  body('policyId').isString().notEmpty(),
  async (req: AuthenticatedRequest, res: Response, next) => {
    try {
      requireAdmin(req);
      const errors = validationResult(req);
      if (!errors.isEmpty()) throw badRequest('Validation failed', errors.array());

      const policyId = req.body.policyId as string;
      await publishEvent(TOPICS.SECURITY_EVENTS, {
        type: 'crm.security.events',
        source: '/services/gateway',
        id: uuidv4(),
        tenantid: req.tenantId!,
        data: { eventType: 'policy_updated', policyId },
      });
      await secureCache.bumpPolicyEpoch(policyId);
      cacheInvalidationTotal.labels(req.tenantId!, `policy_updated:${policyId}`).inc();
      res.json({ ok: true });
    } catch (error) {
      next(error);
    }
  }
);

export default router;

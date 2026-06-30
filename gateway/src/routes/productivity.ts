import { Router, Response } from 'express';
import { body, query, validationResult } from 'express-validator';
import { v4 as uuidv4 } from 'uuid';
import { AuthenticatedRequest } from '../middleware/auth';
import { badRequest, notFound } from '../middleware/errorHandler';
import { withTenantDb } from '../services/prisma';
import { publishEvent, TOPICS } from '../services/kafka';
import { productivityApprovalsTotal, productivityRejectionTotal, productivityResolutionTimeMs } from '../services/metrics';

const router = Router();

router.get(
  '/proposals',
  query('status').optional().isIn(['pending', 'approved', 'rejected']),
  query('priority').optional().isIn(['low', 'medium', 'high']),
  query('limit').optional().isInt({ min: 1, max: 200 }),
  async (req: AuthenticatedRequest, res: Response, next) => {
    try {
      const errors = validationResult(req);
      if (!errors.isEmpty()) throw badRequest('Validation failed', errors.array());

      const status = req.query.status ? String(req.query.status) : 'pending';
      const priority = req.query.priority ? String(req.query.priority) : undefined;
      const limit = parseInt(req.query.limit as string) || 50;

      const roles = req.user?.roles || [];
      const isPrivileged = roles.includes('admin') || roles.includes('sales_manager') || roles.includes('support_manager');
      const where: any = { tenantId: req.tenantId, status };
      if (priority) where.priority = priority;
      if (!isPrivileged) where.userId = req.user?.sub;

      const proposals = await withTenantDb(req.tenantId!, async (db) => {
        return db.productivityProposal.findMany({
          where,
          orderBy: { createdAt: 'desc' },
          take: limit,
        });
      });

      res.json({ data: proposals });
    } catch (error) {
      next(error);
    }
  }
);

router.post(
  '/proposals/:id/decide',
  body('decision').isIn(['approved', 'rejected']),
  body('reason').optional().isString().isLength({ min: 1, max: 2000 }),
  async (req: AuthenticatedRequest, res: Response, next) => {
    try {
      const errors = validationResult(req);
      if (!errors.isEmpty()) throw badRequest('Validation failed', errors.array());

      const correlationId = req.headers['x-correlation-id'] as string;
      const decision = String(req.body.decision);
      const reason = req.body.reason ? String(req.body.reason) : undefined;

      const updated = await withTenantDb(req.tenantId!, async (db) => {
        const existing = await db.productivityProposal.findFirst({
          where: { id: req.params.id, tenantId: req.tenantId },
        });
        if (!existing) throw notFound('Proposal not found');
        const roles = req.user?.roles || [];
        const isPrivileged = roles.includes('admin') || roles.includes('sales_manager') || roles.includes('support_manager');
        if (!isPrivileged && existing.userId !== req.user?.sub) throw notFound('Proposal not found');

        return db.productivityProposal.update({
          where: { id: req.params.id },
          data: {
            status: decision,
            decidedBy: req.user?.sub,
            decidedAt: new Date(),
            decisionReason: reason,
          },
        });
      });

      productivityApprovalsTotal.labels(decision).inc();
      if (decision === 'rejected') productivityRejectionTotal.labels(decision).inc();
      if (updated.createdAt && updated.decidedAt) {
        productivityResolutionTimeMs.labels(decision).observe(updated.decidedAt.getTime() - updated.createdAt.getTime());
      }

      await publishEvent(decision === 'approved' ? TOPICS.PRODUCTIVITY_ACTION_APPROVED : TOPICS.PRODUCTIVITY_ACTION_REJECTED, {
        type: decision === 'approved' ? 'crm.productivity.action-approved' : 'crm.productivity.action-rejected',
        source: '/services/gateway',
        id: uuidv4(),
        tenantid: req.tenantId!,
        correlationid: correlationId,
        data: {
          proposal_id: updated.id,
          tenant_id: updated.tenantId,
          user_id: updated.userId,
          decision,
          decided_by: req.user?.sub,
          decided_at: updated.decidedAt,
          decision_reason: reason,
        },
      });

      res.json(updated);
    } catch (error) {
      next(error);
    }
  }
);

export default router;


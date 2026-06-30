import { Router, Response } from 'express';
import { body, query, validationResult } from 'express-validator';
import { v4 as uuidv4 } from 'uuid';
import { withTenantDb } from '../services/prisma';
import { publishEvent, TOPICS } from '../services/kafka';
import { AuthenticatedRequest } from '../middleware/auth';
import { badRequest, notFound } from '../middleware/errorHandler';
import { logger } from '../utils/logger';

const router = Router();

// List deals
router.get('/',
  query('page').optional().isInt({ min: 1 }),
  query('limit').optional().isInt({ min: 1, max: 100 }),
  query('stage').optional().isString(),
  async (req: AuthenticatedRequest, res: Response, next) => {
    try {
      const page = parseInt(req.query.page as string) || 1;
      const limit = parseInt(req.query.limit as string) || 20;
      const skip = (page - 1) * limit;
      
      const where: any = { tenantId: req.tenantId };
      if (req.query.stage) where.stage = req.query.stage;
      if (req.query.assignedTo) where.assignedTo = req.query.assignedTo;
      
      const [deals, total] = await withTenantDb(req.tenantId!, async (db) => {
        return Promise.all([
          db.deal.findMany({
            where,
            skip,
            take: limit,
            orderBy: { createdAt: 'desc' },
            include: {
              lead: { select: { id: true, name: true } },
              customer: { select: { id: true, name: true } },
              assignedUser: { select: { id: true, name: true } },
            },
          }),
          db.deal.count({ where }),
        ]);
      });
      
      res.json({
        data: deals,
        pagination: { page, limit, total, totalPages: Math.ceil(total / limit) },
      });
    } catch (error) {
      next(error);
    }
  }
);

// Get deal
router.get('/:id', async (req: AuthenticatedRequest, res: Response, next) => {
  try {
    const deal = await withTenantDb(req.tenantId!, async (db) => {
      return db.deal.findFirst({
        where: { id: req.params.id, tenantId: req.tenantId },
        include: {
          lead: true,
          customer: true,
          assignedUser: { select: { id: true, name: true, email: true } },
        },
      });
    });
    
    if (!deal) throw notFound('Deal not found');
    res.json(deal);
  } catch (error) {
    next(error);
  }
});

// Create deal
router.post('/',
  body('name').isLength({ min: 1, max: 255 }),
  body('amount').optional().isDecimal(),
  body('leadId').optional().isUUID(),
  body('customerId').optional().isUUID(),
  async (req: AuthenticatedRequest, res: Response, next) => {
    try {
      const errors = validationResult(req);
      if (!errors.isEmpty()) throw badRequest('Validation failed', errors.array());
      
      const dealId = uuidv4();
      const deal = await withTenantDb(req.tenantId!, async (db) => {
        return db.deal.create({
          data: {
            id: dealId,
            tenantId: req.tenantId!,
            name: req.body.name,
            leadId: req.body.leadId,
            customerId: req.body.customerId,
            amount: req.body.amount,
            currency: req.body.currency || 'USD',
            stage: 'prospecting',
            probability: 10,
            expectedCloseDate: req.body.expectedCloseDate,
            createdBy: req.user?.sub,
            metadata: req.body.metadata || {},
          },
        });
      });
      
      await publishEvent(TOPICS.DEALS_CREATED, {
        type: 'crm.deals.created',
        source: '/services/gateway',
        id: uuidv4(),
        tenantid: req.tenantId!,
        data: { dealId: deal.id, name: deal.name, amount: deal.amount },
      });
      
      logger.info('Deal created', { dealId: deal.id, tenantId: req.tenantId });
      res.status(201).json(deal);
    } catch (error) {
      next(error);
    }
  }
);

// Update deal stage
router.patch('/:id/stage',
  body('stage').isIn(['prospecting', 'qualification', 'proposal', 'negotiation', 'closed_won', 'closed_lost']),
  body('probability').optional().isInt({ min: 0, max: 100 }),
  async (req: AuthenticatedRequest, res: Response, next) => {
    try {
      const { existing, deal } = await withTenantDb(req.tenantId!, async (db) => {
        const existing = await db.deal.findFirst({
          where: { id: req.params.id, tenantId: req.tenantId },
        });
        
        if (!existing) throw notFound('Deal not found');
        
        const deal = await db.deal.update({
          where: { id: req.params.id },
          data: {
            stage: req.body.stage,
            probability: req.body.probability,
            ...(req.body.stage.startsWith('closed') && {
              actualCloseDate: new Date(),
              won: req.body.stage === 'closed_won',
            }),
          },
        });

        return { existing, deal };
      });
      
      await publishEvent(TOPICS.DEALS_STAGE_CHANGED, {
        type: 'crm.deals.stage-changed',
        source: '/services/gateway',
        id: uuidv4(),
        tenantid: req.tenantId!,
        data: {
          dealId: deal.id,
          previousStage: existing.stage,
          newStage: deal.stage,
          amount: deal.amount,
        },
      });
      
      if (req.body.stage.startsWith('closed')) {
        await publishEvent(TOPICS.DEALS_CLOSED, {
          type: 'crm.deals.closed',
          source: '/services/gateway',
          id: uuidv4(),
          tenantid: req.tenantId!,
          data: { dealId: deal.id, won: deal.won, amount: deal.amount },
        });
      }
      
      res.json(deal);
    } catch (error) {
      next(error);
    }
  }
);

// Update deal
router.patch('/:id',
  async (req: AuthenticatedRequest, res: Response, next) => {
    try {
      const deal = await withTenantDb(req.tenantId!, async (db) => {
        const existing = await db.deal.findFirst({
          where: { id: req.params.id, tenantId: req.tenantId },
        });
        
        if (!existing) throw notFound('Deal not found');
        
        return db.deal.update({
          where: { id: req.params.id },
          data: {
            ...(req.body.name && { name: req.body.name }),
            ...(req.body.amount && { amount: req.body.amount }),
            ...(req.body.expectedCloseDate && { expectedCloseDate: new Date(req.body.expectedCloseDate) }),
            ...(req.body.assignedTo && { assignedTo: req.body.assignedTo }),
            ...(req.body.metadata && { metadata: req.body.metadata }),
          },
        });
      });
      
      await publishEvent(TOPICS.DEALS_UPDATED, {
        type: 'crm.deals.updated',
        source: '/services/gateway',
        id: uuidv4(),
        tenantid: req.tenantId!,
        data: { dealId: deal.id, changes: req.body },
      });
      
      res.json(deal);
    } catch (error) {
      next(error);
    }
  }
);

// Delete deal
router.delete('/:id', async (req: AuthenticatedRequest, res: Response, next) => {
  try {
    await withTenantDb(req.tenantId!, async (db) => {
      const deal = await db.deal.findFirst({
        where: { id: req.params.id, tenantId: req.tenantId },
      });
      
      if (!deal) throw notFound('Deal not found');
      
      await db.deal.delete({ where: { id: req.params.id } });
    });
    logger.info('Deal deleted', { dealId: req.params.id, tenantId: req.tenantId });
    res.status(204).send();
  } catch (error) {
    next(error);
  }
});

export default router;

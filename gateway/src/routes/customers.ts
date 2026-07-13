import { Router, Response } from 'express';
import { body, query, validationResult } from 'express-validator';
import { uuidv4 } from '../utils/uuid';
import { withTenantDb } from '../services/prisma';
import { publishEvent, TOPICS } from '../services/kafka';
import { AuthenticatedRequest } from '../middleware/auth';
import { badRequest, notFound } from '../middleware/errorHandler';
import { logger } from '../utils/logger';

const router = Router();

// List customers
router.get('/',
  query('page').optional().isInt({ min: 1 }),
  query('limit').optional().isInt({ min: 1, max: 100 }),
  query('segment').optional().isString(),
  async (req: AuthenticatedRequest, res: Response, next) => {
    try {
      const page = parseInt(req.query.page as string) || 1;
      const limit = parseInt(req.query.limit as string) || 20;
      const skip = (page - 1) * limit;
      
      const where: any = { tenantId: req.tenantId };
      if (req.query.segment) where.segment = req.query.segment;
      if (req.query.status) where.status = req.query.status;
      
      const [customers, total] = await withTenantDb(req.tenantId!, async (db) => {
        return Promise.all([
          db.customer.findMany({
            where,
            skip,
            take: limit,
            orderBy: { createdAt: 'desc' },
          }),
          db.customer.count({ where }),
        ]);
      });
      
      res.json({
        data: customers,
        pagination: { page, limit, total, totalPages: Math.ceil(total / limit) },
      });
    } catch (error) {
      next(error);
    }
  }
);

// Get customer
router.get('/:id', async (req: AuthenticatedRequest, res: Response, next) => {
  try {
    const customer = await withTenantDb(req.tenantId!, async (db) => {
      return db.customer.findFirst({
        where: { id: req.params.id, tenantId: req.tenantId },
        include: {
          deals: { orderBy: { createdAt: 'desc' }, take: 5 },
          tickets: { orderBy: { createdAt: 'desc' }, take: 5 },
        },
      });
    });
    
    if (!customer) throw notFound('Customer not found');
    res.json(customer);
  } catch (error) {
    next(error);
  }
});

// Customer timeline
router.get('/:id/timeline',
  query('limit').optional().isInt({ min: 1, max: 200 }),
  async (req: AuthenticatedRequest, res: Response, next) => {
    try {
      const limit = parseInt(req.query.limit as string) || 50;
      const customerId = req.params.id;
      const timeline = await withTenantDb(req.tenantId!, async (db) => {
        const exists = await db.customer.findFirst({ where: { id: customerId, tenantId: req.tenantId }, select: { id: true } });
        if (!exists) throw notFound('Customer not found');
        return db.customerTimeline.findMany({
          where: { tenantId: req.tenantId!, customerId },
          orderBy: { ts: 'desc' },
          take: limit,
        });
      });
      res.json({ data: timeline });
    } catch (error) {
      next(error);
    }
  }
);

// Customer stage + latest predictions
router.get('/:id/profile', async (req: AuthenticatedRequest, res: Response, next) => {
  try {
    const customerId = req.params.id;
    const result = await withTenantDb(req.tenantId!, async (db) => {
      const exists = await db.customer.findFirst({ where: { id: customerId, tenantId: req.tenantId }, select: { id: true } });
      if (!exists) throw notFound('Customer not found');

      const profile = await db.customerProfile.findUnique({ where: { customerId } });
      const preds = await db.prediction.findMany({
        where: { tenantId: req.tenantId!, entityType: 'customer', entityId: customerId },
        orderBy: { createdAt: 'desc' },
        take: 25,
      });

      const latestByType: Record<string, any> = {};
      for (const p of preds) {
        if (!latestByType[p.predictionType]) latestByType[p.predictionType] = p;
      }

      return { profile, predictions: Object.values(latestByType) };
    });

    res.json({
      stage: result.profile?.stage || 'awareness',
      confidence: result.profile?.stageConfidence || 0,
      stageUpdatedAt: result.profile?.stageUpdatedAt || null,
      features: result.profile?.features || {},
      predictions: result.predictions,
    });
  } catch (error) {
    next(error);
  }
});

// Create customer
router.post('/',
  body('name').isLength({ min: 1, max: 255 }),
  body('email').optional().isEmail(),
  body('phone').optional().isString(),
  body('company').optional().isString(),
  body('segment').optional().isString(),
  async (req: AuthenticatedRequest, res: Response, next) => {
    try {
      const errors = validationResult(req);
      if (!errors.isEmpty()) throw badRequest('Validation failed', errors.array());
      
      const customerId = uuidv4();
      const customer = await withTenantDb(req.tenantId!, async (db) => {
        return db.customer.create({
          data: {
            id: customerId,
            tenantId: req.tenantId!,
            name: req.body.name,
            email: req.body.email,
            phone: req.body.phone,
            company: req.body.company,
            segment: req.body.segment,
            createdBy: req.user?.sub,
            metadata: req.body.metadata || {},
          },
        });
      });
      
      await publishEvent(TOPICS.CUSTOMERS_CREATED, {
        type: 'crm.customers.created',
        source: '/services/gateway',
        id: uuidv4(),
        tenantid: req.tenantId!,
        data: { customerId: customer.id, name: customer.name, segment: customer.segment },
      });
      
      logger.info('Customer created', { customerId: customer.id, tenantId: req.tenantId });
      res.status(201).json(customer);
    } catch (error) {
      next(error);
    }
  }
);

// Update customer
router.patch('/:id',
  async (req: AuthenticatedRequest, res: Response, next) => {
    try {
      const customer = await withTenantDb(req.tenantId!, async (db) => {
        const existing = await db.customer.findFirst({
          where: { id: req.params.id, tenantId: req.tenantId },
        });
        
        if (!existing) throw notFound('Customer not found');
        
        return db.customer.update({
          where: { id: req.params.id },
          data: {
            ...(req.body.name && { name: req.body.name }),
            ...(req.body.email && { email: req.body.email }),
            ...(req.body.phone && { phone: req.body.phone }),
            ...(req.body.company && { company: req.body.company }),
            ...(req.body.segment && { segment: req.body.segment }),
            ...(req.body.status && { status: req.body.status }),
            ...(req.body.metadata && { metadata: req.body.metadata }),
          },
        });
      });
      
      await publishEvent(TOPICS.CUSTOMERS_UPDATED, {
        type: 'crm.customers.updated',
        source: '/services/gateway',
        id: uuidv4(),
        tenantid: req.tenantId!,
        data: { customerId: customer.id, changes: req.body },
      });
      
      res.json(customer);
    } catch (error) {
      next(error);
    }
  }
);

// Delete customer
router.delete('/:id', async (req: AuthenticatedRequest, res: Response, next) => {
  try {
    await withTenantDb(req.tenantId!, async (db) => {
      const customer = await db.customer.findFirst({
        where: { id: req.params.id, tenantId: req.tenantId },
      });
      
      if (!customer) throw notFound('Customer not found');
      
      await db.customer.delete({ where: { id: req.params.id } });
    });
    logger.info('Customer deleted', { customerId: req.params.id, tenantId: req.tenantId });
    res.status(204).send();
  } catch (error) {
    next(error);
  }
});

export default router;

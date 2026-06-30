import { Router, Response } from 'express';
import { query } from 'express-validator';
import { withTenantDb } from '../services/prisma';
import { AuthenticatedRequest } from '../middleware/auth';
import { notFound } from '../middleware/errorHandler';

const router = Router();

// List registered AI agents
router.get('/', async (req: AuthenticatedRequest, res: Response, next) => {
  try {
    const agents = await withTenantDb(req.tenantId!, async (db) => {
      return db.aiAgent.findMany({
        where: { isActive: true },
        orderBy: { name: 'asc' },
      });
    });
    
    res.json({ data: agents });
  } catch (error) {
    next(error);
  }
});

// Get agent details
router.get('/:id', async (req: AuthenticatedRequest, res: Response, next) => {
  try {
    const agent = await withTenantDb(req.tenantId!, async (db) => {
      return db.aiAgent.findUnique({
        where: { id: req.params.id },
      });
    });
    
    if (!agent) throw notFound('Agent not found');
    res.json(agent);
  } catch (error) {
    next(error);
  }
});

// List agent tasks
router.get('/:id/tasks',
  query('status').optional().isIn(['pending', 'running', 'completed', 'failed']),
  async (req: AuthenticatedRequest, res: Response, next) => {
    try {
      const page = parseInt(req.query.page as string) || 1;
      const limit = parseInt(req.query.limit as string) || 20;
      const skip = (page - 1) * limit;
      
      const where: any = {
        tenantId: req.tenantId,
        agentId: req.params.id,
      };
      
      if (req.query.status) {
        where.status = req.query.status;
      }
      
      const [tasks, total] = await withTenantDb(req.tenantId!, async (db) => {
        return Promise.all([
          db.agentTask.findMany({
            where,
            skip,
            take: limit,
            orderBy: { createdAt: 'desc' },
            include: {
              agent: { select: { id: true, name: true, type: true } },
            },
          }),
          db.agentTask.count({ where }),
        ]);
      });
      
      res.json({
        data: tasks,
        pagination: { page, limit, total, totalPages: Math.ceil(total / limit) },
      });
    } catch (error) {
      next(error);
    }
  }
);

// Get task details
router.get('/tasks/:taskId', async (req: AuthenticatedRequest, res: Response, next) => {
  try {
    const task = await withTenantDb(req.tenantId!, async (db) => {
      return db.agentTask.findFirst({
        where: {
          id: req.params.taskId,
          tenantId: req.tenantId,
        },
        include: {
          agent: true,
          events: {
            orderBy: { createdAt: 'desc' },
            take: 50,
          },
        },
      });
    });
    
    if (!task) throw notFound('Task not found');
    res.json(task);
  } catch (error) {
    next(error);
  }
});

// List agent events
router.get('/:id/events',
  query('eventType').optional().isString(),
  async (req: AuthenticatedRequest, res: Response, next) => {
    try {
      const page = parseInt(req.query.page as string) || 1;
      const limit = parseInt(req.query.limit as string) || 50;
      const skip = (page - 1) * limit;
      
      const where: any = {
        tenantId: req.tenantId,
        agentId: req.params.id,
      };
      
      if (req.query.eventType) {
        where.eventType = req.query.eventType;
      }
      
      const [events, total] = await withTenantDb(req.tenantId!, async (db) => {
        return Promise.all([
          db.agentEvent.findMany({
            where,
            skip,
            take: limit,
            orderBy: { createdAt: 'desc' },
          }),
          db.agentEvent.count({ where }),
        ]);
      });
      
      res.json({
        data: events,
        pagination: { page, limit, total, totalPages: Math.ceil(total / limit) },
      });
    } catch (error) {
      next(error);
    }
  }
);

// Get event reasoning details
router.get('/events/:eventId/reasoning', async (req: AuthenticatedRequest, res: Response, next) => {
  try {
    const event = await withTenantDb(req.tenantId!, async (db) => {
      return db.agentEvent.findFirst({
        where: {
          id: req.params.eventId,
          tenantId: req.tenantId,
        },
        include: {
          agent: { select: { id: true, name: true, type: true } },
          task: { select: { id: true, taskType: true } },
        },
      });
    });
    
    if (!event) throw notFound('Event not found');
    
    res.json({
      eventId: event.id,
      agentId: event.agentId,
      agent: event.agent,
      task: event.task,
      eventType: event.eventType,
      actionType: event.actionType,
      targetEntity: event.targetEntity,
      targetId: event.targetId,
      reasoning: event.reasoning,
      confidence: event.confidence,
      requiresApproval: event.requiresApproval,
      isApproved: event.isApproved,
      metadata: event.metadata,
      createdAt: event.createdAt,
    });
  } catch (error) {
    next(error);
  }
});

export default router;

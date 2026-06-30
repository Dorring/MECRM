import { Router, Response } from 'express';
import axios from 'axios';
import { body, query, validationResult } from 'express-validator';
import { v4 as uuidv4 } from 'uuid';
import { withTenantDb } from '../services/prisma';
import { publishEvent, TOPICS } from '../services/kafka';
import { AuthenticatedRequest } from '../middleware/auth';
import { badRequest, notFound } from '../middleware/errorHandler';
import { automationsActive } from '../services/metrics';

const router = Router();

const AGENTS_URL = (process.env.AGENTS_URL || 'http://localhost:5010').replace(/\/$/, '');

router.get(
  '/',
  query('status').optional().isIn(['draft', 'simulating', 'active', 'paused', 'disabled']),
  query('page').optional().isInt({ min: 1 }),
  query('limit').optional().isInt({ min: 1, max: 200 }),
  async (req: AuthenticatedRequest, res: Response, next) => {
    try {
      const errors = validationResult(req);
      if (!errors.isEmpty()) throw badRequest('Validation failed', errors.array());

      const page = parseInt(req.query.page as string) || 1;
      const limit = parseInt(req.query.limit as string) || 50;
      const skip = (page - 1) * limit;
      const status = req.query.status ? String(req.query.status) : undefined;

      const where: any = { tenantId: req.tenantId };
      if (status) where.status = status;

      const [items, total] = await withTenantDb(req.tenantId!, async (db) => {
        return Promise.all([
          db.automationPolicy.findMany({
            where,
            skip,
            take: limit,
            orderBy: { updatedAt: 'desc' },
            select: {
              id: true,
              tenantId: true,
              createdBy: true,
              status: true,
              nlRuleText: true,
              triggerType: true,
              version: true,
              lastSimulationId: true,
              createdAt: true,
              updatedAt: true,
            },
          }),
          db.automationPolicy.count({ where }),
        ]);
      });

      res.json({ data: items, pagination: { page, limit, total, totalPages: Math.ceil(total / limit) } });
    } catch (error) {
      next(error);
    }
  }
);

router.get('/:id', async (req: AuthenticatedRequest, res: Response, next) => {
  try {
    const policy = await withTenantDb(req.tenantId!, async (db) => {
      return db.automationPolicy.findFirst({
        where: { id: req.params.id, tenantId: req.tenantId },
        include: {
          simulations: { orderBy: { createdAt: 'desc' }, take: 1 },
        },
      });
    });
    if (!policy) throw notFound('Automation policy not found');
    res.json(policy);
  } catch (error) {
    next(error);
  }
});

router.get('/:id/simulations', async (req: AuthenticatedRequest, res: Response, next) => {
  try {
    const limit = parseInt(req.query.limit as string) || 20;
    const simulations = await withTenantDb(req.tenantId!, async (db) => {
      const policy = await db.automationPolicy.findFirst({ where: { id: req.params.id, tenantId: req.tenantId }, select: { id: true } });
      if (!policy) throw notFound('Automation policy not found');
      return db.automationSimulation.findMany({
        where: { tenantId: req.tenantId, policyId: req.params.id },
        orderBy: { createdAt: 'desc' },
        take: Math.min(200, Math.max(1, limit)),
      });
    });
    res.json({ data: simulations });
  } catch (error) {
    next(error);
  }
});

router.post(
  '/parse',
  body('nlRuleText').isString().isLength({ min: 1, max: 2000 }),
  async (req: AuthenticatedRequest, res: Response, next) => {
    try {
      const errors = validationResult(req);
      if (!errors.isEmpty()) throw badRequest('Validation failed', errors.array());

      const correlationId = req.headers['x-correlation-id'] as string;
      const response = await axios.post(
        `${AGENTS_URL}/api/v1/automation/parse`,
        { nl_rule_text: req.body.nlRuleText },
        {
          timeout: 5000,
          headers: {
            'Content-Type': 'application/json',
            'X-Tenant-Id': req.tenantId,
            'X-User-Id': req.user?.sub,
            'X-User-Roles': (req.user?.roles || []).join(','),
            'X-Correlation-Id': correlationId,
            ...(req.headers.authorization ? { Authorization: String(req.headers.authorization) } : {}),
          },
        }
      );

      res.json(response.data || {});
    } catch (error) {
      next(error);
    }
  }
);

router.post(
  '/',
  body('nlRuleText').isString().isLength({ min: 1, max: 2000 }),
  async (req: AuthenticatedRequest, res: Response, next) => {
    try {
      const errors = validationResult(req);
      if (!errors.isEmpty()) throw badRequest('Validation failed', errors.array());

      const correlationId = req.headers['x-correlation-id'] as string;
      const parsed = await axios.post(
        `${AGENTS_URL}/api/v1/automation/parse`,
        { nl_rule_text: req.body.nlRuleText },
        {
          timeout: 5000,
          headers: {
            'Content-Type': 'application/json',
            'X-Tenant-Id': req.tenantId,
            'X-User-Id': req.user?.sub,
            'X-User-Roles': (req.user?.roles || []).join(','),
            'X-Correlation-Id': correlationId,
            ...(req.headers.authorization ? { Authorization: String(req.headers.authorization) } : {}),
          },
        }
      );

      const payload = parsed.data || {};
      const workflowJson = payload.workflow || payload.workflow_json;
      const compiledJson = payload.compiled || payload.compiled_json;
      const triggerType = String(payload.trigger_type || payload.triggerType || '');

      if (!workflowJson || !compiledJson || !triggerType) throw badRequest('Automation parse failed');

      const policy = await withTenantDb(req.tenantId!, async (db) => {
        return db.automationPolicy.create({
          data: {
            id: uuidv4(),
            tenantId: req.tenantId!,
            createdBy: req.user!.sub,
            status: 'draft',
            nlRuleText: String(req.body.nlRuleText),
            triggerType,
            workflowJson,
            compiledJson,
            version: 1,
          },
        });
      });

      await publishEvent(TOPICS.AUTOMATION_POLICY_CREATED, {
        type: 'crm.automation.policy-created',
        source: '/services/gateway',
        id: uuidv4(),
        tenantid: req.tenantId!,
        correlationid: correlationId,
        data: { policy_id: policy.id, trigger_type: triggerType, status: policy.status },
      });

      res.status(201).json({ policy, parsed: payload });
    } catch (error) {
      next(error);
    }
  }
);

router.put(
  '/:id',
  body('nlRuleText').isString().isLength({ min: 1, max: 2000 }),
  async (req: AuthenticatedRequest, res: Response, next) => {
    try {
      const errors = validationResult(req);
      if (!errors.isEmpty()) throw badRequest('Validation failed', errors.array());

      const correlationId = req.headers['x-correlation-id'] as string;
      const parsed = await axios.post(
        `${AGENTS_URL}/api/v1/automation/parse`,
        { nl_rule_text: req.body.nlRuleText },
        {
          timeout: 5000,
          headers: {
            'Content-Type': 'application/json',
            'X-Tenant-Id': req.tenantId,
            'X-User-Id': req.user?.sub,
            'X-User-Roles': (req.user?.roles || []).join(','),
            'X-Correlation-Id': correlationId,
            ...(req.headers.authorization ? { Authorization: String(req.headers.authorization) } : {}),
          },
        }
      );

      const payload = parsed.data || {};
      const workflowJson = payload.workflow || payload.workflow_json;
      const compiledJson = payload.compiled || payload.compiled_json;
      const triggerType = String(payload.trigger_type || payload.triggerType || '');
      if (!workflowJson || !compiledJson || !triggerType) throw badRequest('Automation parse failed');

      const updated = await withTenantDb(req.tenantId!, async (db) => {
        const existing = await db.automationPolicy.findFirst({ where: { id: req.params.id, tenantId: req.tenantId } });
        if (!existing) throw notFound('Automation policy not found');
        if (existing.status === 'active') throw badRequest('Disable policy before editing');
        return db.automationPolicy.update({
          where: { id: req.params.id },
          data: {
            nlRuleText: String(req.body.nlRuleText),
            triggerType,
            workflowJson,
            compiledJson,
            version: existing.version + 1,
            status: 'draft',
            lastSimulationId: null,
          },
        });
      });

      await publishEvent(TOPICS.AUTOMATION_POLICY_UPDATED, {
        type: 'crm.automation.policy-updated',
        source: '/services/gateway',
        id: uuidv4(),
        tenantid: req.tenantId!,
        correlationid: correlationId,
        data: { policy_id: updated.id, trigger_type: triggerType, status: updated.status, version: updated.version },
      });

      res.json({ policy: updated, parsed: payload });
    } catch (error) {
      next(error);
    }
  }
);

router.post(
  '/:id/simulate',
  body('fromTs').optional().isISO8601(),
  body('toTs').optional().isISO8601(),
  async (req: AuthenticatedRequest, res: Response, next) => {
    try {
      const errors = validationResult(req);
      if (!errors.isEmpty()) throw badRequest('Validation failed', errors.array());

      const correlationId = req.headers['x-correlation-id'] as string;

      const policy = await withTenantDb(req.tenantId!, async (db) => {
        const existing = await db.automationPolicy.findFirst({ where: { id: req.params.id, tenantId: req.tenantId } });
        if (!existing) throw notFound('Automation policy not found');
        return db.automationPolicy.update({ where: { id: req.params.id }, data: { status: 'simulating' } });
      });

      await publishEvent(TOPICS.AUTOMATION_SIMULATION_REQUESTED, {
        type: 'crm.automation.simulation.requested',
        source: '/services/gateway',
        id: uuidv4(),
        tenantid: req.tenantId!,
        correlationid: correlationId,
        data: {
          policy_id: policy.id,
          requested_by: req.user?.sub,
          from_ts: req.body.fromTs || null,
          to_ts: req.body.toTs || null,
        },
      });

      res.status(202).json({ status: 'queued', policy_id: policy.id });
    } catch (error) {
      next(error);
    }
  }
);

router.post('/:id/request-activation', async (req: AuthenticatedRequest, res: Response, next) => {
  try {
    const correlationId = req.headers['x-correlation-id'] as string;

    const { policy, approval } = await withTenantDb(req.tenantId!, async (db) => {
      const policy = await db.automationPolicy.findFirst({ where: { id: req.params.id, tenantId: req.tenantId } });
      if (!policy) throw notFound('Automation policy not found');
      if (!policy.lastSimulationId) throw badRequest('Run simulation before activation');
      if (policy.status === 'active') throw badRequest('Policy already active');

      const expiresAt = new Date();
      expiresAt.setHours(expiresAt.getHours() + 24);

      const approval = await db.approval.create({
        data: {
          id: uuidv4(),
          tenantId: req.tenantId!,
          requestType: 'automation_activation',
          requestorType: 'user',
          requestorId: req.user!.sub,
          actionType: 'automations:activate',
          targetEntity: 'automation_policy',
          targetId: policy.id,
          context: {
            policy_id: policy.id,
            trigger_type: policy.triggerType,
            last_simulation_id: policy.lastSimulationId,
            nl_rule_text: policy.nlRuleText,
          },
          status: 'pending',
          expiresAt,
        },
      });

      return { policy, approval };
    });

    await publishEvent(TOPICS.APPROVALS_REQUIRED, {
      type: 'crm.approvals.required',
      source: '/services/gateway',
      id: uuidv4(),
      tenantid: req.tenantId!,
      correlationid: correlationId,
      data: {
        approvalId: approval.id,
        requestType: approval.requestType,
        actionType: approval.actionType,
        expiresAt: approval.expiresAt,
        targetEntity: approval.targetEntity,
        targetId: approval.targetId,
      },
    });

    res.status(201).json({ policy_id: policy.id, approval });
  } catch (error) {
    next(error);
  }
});

router.post('/:id/pause', async (req: AuthenticatedRequest, res: Response, next) => {
  try {
    const updated = await withTenantDb(req.tenantId!, async (db) => {
      const existing = await db.automationPolicy.findFirst({ where: { id: req.params.id, tenantId: req.tenantId } });
      if (!existing) throw notFound('Automation policy not found');
      if (existing.status !== 'active') throw badRequest('Only active policies can be paused');
      return db.automationPolicy.update({ where: { id: req.params.id }, data: { status: 'paused' } });
    });
    const activeCount = await withTenantDb(req.tenantId!, (db) => db.automationPolicy.count({ where: { tenantId: req.tenantId, status: 'active' } }));
    automationsActive.labels(req.tenantId!).set(activeCount);
    res.json(updated);
  } catch (error) {
    next(error);
  }
});

router.post('/:id/resume', async (req: AuthenticatedRequest, res: Response, next) => {
  try {
    const updated = await withTenantDb(req.tenantId!, async (db) => {
      const existing = await db.automationPolicy.findFirst({ where: { id: req.params.id, tenantId: req.tenantId } });
      if (!existing) throw notFound('Automation policy not found');
      if (existing.status !== 'paused') throw badRequest('Only paused policies can be resumed');
      return db.automationPolicy.update({ where: { id: req.params.id }, data: { status: 'active' } });
    });
    const activeCount = await withTenantDb(req.tenantId!, (db) => db.automationPolicy.count({ where: { tenantId: req.tenantId, status: 'active' } }));
    automationsActive.labels(req.tenantId!).set(activeCount);
    res.json(updated);
  } catch (error) {
    next(error);
  }
});

router.post('/:id/deactivate', async (req: AuthenticatedRequest, res: Response, next) => {
  try {
    const updated = await withTenantDb(req.tenantId!, async (db) => {
      const existing = await db.automationPolicy.findFirst({ where: { id: req.params.id, tenantId: req.tenantId } });
      if (!existing) throw notFound('Automation policy not found');
      return db.automationPolicy.update({ where: { id: req.params.id }, data: { status: 'disabled' } });
    });
    const activeCount = await withTenantDb(req.tenantId!, (db) => db.automationPolicy.count({ where: { tenantId: req.tenantId, status: 'active' } }));
    automationsActive.labels(req.tenantId!).set(activeCount);
    res.json(updated);
  } catch (error) {
    next(error);
  }
});

export default router;


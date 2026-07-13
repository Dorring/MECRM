import { Router, Response } from 'express';
import { body, query, validationResult } from 'express-validator';
import { uuidv4 } from '../utils/uuid';
import { withTenantDb } from '../services/prisma';
import { AuthenticatedRequest } from '../middleware/auth';
import { badRequest, notFound } from '../middleware/errorHandler';
import { redisClient } from '../services/redis';
import { publishEvent, TOPICS } from '../services/kafka';
import { auditQueriesTotal, killSwitchUsageTotal } from '../services/metrics';

const router = Router();

router.get(
  '/decisions',
  query('agentId').optional().isString(),
  query('page').optional().isInt({ min: 1 }),
  query('limit').optional().isInt({ min: 1, max: 200 }),
  async (req: AuthenticatedRequest, res: Response, next) => {
    try {
      const roles = req.user?.roles || [];
      if (!roles.includes('admin') && !roles.includes('super_admin') && !roles.includes('auditor')) {
        throw badRequest('Insufficient privileges');
      }

      const errors = validationResult(req);
      if (!errors.isEmpty()) throw badRequest('Validation failed', errors.array());

      const page = parseInt(req.query.page as string) || 1;
      const limit = parseInt(req.query.limit as string) || 50;
      const skip = (page - 1) * limit;

      const where: any = { tenantId: req.tenantId };
      if (req.query.agentId) where.agentId = String(req.query.agentId);

      const [items, total] = await withTenantDb(req.tenantId!, async (db) => {
        return Promise.all([
          db.agentDecision.findMany({
            where,
            skip,
            take: limit,
            orderBy: { createdAt: 'desc' },
          }),
          db.agentDecision.count({ where }),
        ]);
      });

      const correlationId = (req.headers['x-correlation-id'] as string) || uuidv4();
      await publishEvent(TOPICS.AUDIT_ACCESSED, {
        type: 'crm.audit.accessed',
        source: '/services/gateway',
        id: uuidv4(),
        tenantid: req.tenantId!,
        correlationid: correlationId,
        data: {
          actor_type: 'user',
          actor_id: req.user?.sub,
          access_type: 'decisions_list',
          agent_id: req.query.agentId ? String(req.query.agentId) : null,
          page,
          limit,
        },
      });
      auditQueriesTotal.labels('decisions_list').inc();

      res.json({
        data: items,
        pagination: { page, limit, total, totalPages: Math.ceil(total / limit) },
      });
    } catch (error) {
      next(error);
    }
  }
);

router.get('/decisions/:id', async (req: AuthenticatedRequest, res: Response, next) => {
  try {
    const roles = req.user?.roles || [];
    if (!roles.includes('admin') && !roles.includes('super_admin') && !roles.includes('auditor')) {
      throw badRequest('Insufficient privileges');
    }

    const decision = await withTenantDb(req.tenantId!, async (db) => {
      return db.agentDecision.findFirst({ where: { id: req.params.id, tenantId: req.tenantId } });
    });
    if (!decision) throw notFound('Decision not found');

    const correlationId = (req.headers['x-correlation-id'] as string) || uuidv4();
    await publishEvent(TOPICS.AUDIT_ACCESSED, {
      type: 'crm.audit.accessed',
      source: '/services/gateway',
      id: uuidv4(),
      tenantid: req.tenantId!,
      correlationid: correlationId,
      data: {
        actor_type: 'user',
        actor_id: req.user?.sub,
        access_type: 'decision_view',
        decision_id: req.params.id,
      },
    });
    auditQueriesTotal.labels('decision_view').inc();

    res.json(decision);
  } catch (error) {
    next(error);
  }
});

router.get('/killswitch/status', async (req: AuthenticatedRequest, res: Response, next) => {
  try {
    let cursor = '0';
    const keys: string[] = [];
    do {
      const [nextCursor, batch] = await redisClient.scan(cursor, 'MATCH', 'governance:killswitch:*', 'COUNT', '100');
      cursor = nextCursor;
      keys.push(...batch);
    } while (cursor !== '0');

    const values = keys.length ? await redisClient.mget(...keys) : [];

    const global: any = {};
    const tenants: Record<string, any> = {};
    const agents: Record<string, any> = {};
    const tenantAgents: Record<string, any> = {};

    keys.forEach((key, idx) => {
      const raw = values[idx];
      if (!raw) return;
      let parsed: any;
      try {
        parsed = JSON.parse(raw);
      } catch {
        return;
      }
      if (key === 'governance:killswitch:global') {
        Object.assign(global, parsed);
        return;
      }
      const parts = key.split(':');
      if (parts[0] !== 'governance' || parts[1] !== 'killswitch') return;
      if (parts[2] === 'tenant' && parts.length === 4) {
        tenants[parts[3]] = parsed;
      } else if (parts[2] === 'agent' && parts.length === 4) {
        agents[parts[3]] = parsed;
      } else if (parts[2] === 'tenant' && parts[4] === 'agent' && parts.length === 6) {
        tenantAgents[`${parts[3]}:${parts[5]}`] = parsed;
      }
    });

    res.json({ global: Object.keys(global).length ? global : null, tenants, agents, tenant_agents: tenantAgents });
  } catch (error) {
    next(error);
  }
});

router.post(
  '/killswitch/pause',
  body('tenantId').optional().isUUID(),
  body('reason').optional().isString(),
  async (req: AuthenticatedRequest, res: Response, next) => {
    try {
      const errors = validationResult(req);
      if (!errors.isEmpty()) throw badRequest('Validation failed', errors.array());

      const roles = req.user?.roles || [];
      if (!roles.includes('admin') && !roles.includes('super_admin')) {
        throw badRequest('Insufficient privileges');
      }

      const requestedTenant = req.body.tenantId;
      const tenantId = req.user?.roles?.includes('super_admin') && requestedTenant ? requestedTenant : req.tenantId;
      if (!tenantId) throw badRequest('Missing tenantId');

      const key = `governance:killswitch:tenant:${tenantId}`;
      const payload = { state: 'paused', updated_at_ms: Date.now(), reason: req.body.reason || null };

      await redisClient.set(key, JSON.stringify(payload));
      await redisClient.publish('governance:killswitch:events', JSON.stringify({ key, ...payload }));

      await publishEvent(TOPICS.KILLSWITCH_ACTIVATED, {
        type: 'crm.killswitch.activated',
        source: '/services/gateway',
        id: uuidv4(),
        tenantid: tenantId,
        correlationid: (req.headers['x-correlation-id'] as string) || uuidv4(),
        data: { scope: 'tenant', state: 'paused', actor_id: req.user?.sub, reason: payload.reason },
      });
      killSwitchUsageTotal.labels('tenant', 'paused').inc();

      res.json({ ok: true, key, ...payload });
    } catch (error) {
      next(error);
    }
  }
);

router.post(
  '/killswitch/resume',
  body('tenantId').optional().isUUID(),
  body('reason').optional().isString(),
  async (req: AuthenticatedRequest, res: Response, next) => {
    try {
      const errors = validationResult(req);
      if (!errors.isEmpty()) throw badRequest('Validation failed', errors.array());

      const roles = req.user?.roles || [];
      if (!roles.includes('admin') && !roles.includes('super_admin')) {
        throw badRequest('Insufficient privileges');
      }

      const requestedTenant = req.body.tenantId;
      const tenantId = req.user?.roles?.includes('super_admin') && requestedTenant ? requestedTenant : req.tenantId;
      if (!tenantId) throw badRequest('Missing tenantId');

      const key = `governance:killswitch:tenant:${tenantId}`;
      const payload = { state: 'running', updated_at_ms: Date.now(), reason: req.body.reason || null };

      await redisClient.set(key, JSON.stringify(payload));
      await redisClient.publish('governance:killswitch:events', JSON.stringify({ key, ...payload }));

      await publishEvent(TOPICS.KILLSWITCH_ACTIVATED, {
        type: 'crm.killswitch.activated',
        source: '/services/gateway',
        id: uuidv4(),
        tenantid: tenantId,
        correlationid: (req.headers['x-correlation-id'] as string) || uuidv4(),
        data: { scope: 'tenant', state: 'running', actor_id: req.user?.sub, reason: payload.reason },
      });
      killSwitchUsageTotal.labels('tenant', 'running').inc();

      res.json({ ok: true, key, ...payload });
    } catch (error) {
      next(error);
    }
  }
);

router.post(
  '/killswitch/emergency-stop',
  body('agentId').optional().isString(),
  body('reason').optional().isString(),
  async (req: AuthenticatedRequest, res: Response, next) => {
    try {
      const errors = validationResult(req);
      if (!errors.isEmpty()) throw badRequest('Validation failed', errors.array());

      if (!req.user?.roles?.includes('super_admin') && !req.user?.roles?.includes('admin')) {
        throw badRequest('Insufficient privileges');
      }

      const agentId = req.body.agentId ? String(req.body.agentId) : null;
      const key = agentId ? `governance:killswitch:agent:${agentId}` : 'governance:killswitch:global';
      const payload = { state: 'killed', updated_at_ms: Date.now(), reason: req.body.reason || null };

      await redisClient.set(key, JSON.stringify(payload));
      await redisClient.publish('governance:killswitch:events', JSON.stringify({ key, ...payload }));

      await publishEvent(TOPICS.KILLSWITCH_ACTIVATED, {
        type: 'crm.killswitch.activated',
        source: '/services/gateway',
        id: uuidv4(),
        tenantid: req.tenantId!,
        correlationid: (req.headers['x-correlation-id'] as string) || uuidv4(),
        data: { scope: agentId ? 'agent' : 'global', state: 'killed', actor_id: req.user?.sub, agent_id: agentId, reason: payload.reason },
      });
      killSwitchUsageTotal.labels(agentId ? 'agent' : 'global', 'killed').inc();

      res.json({ ok: true, key, ...payload });
    } catch (error) {
      next(error);
    }
  }
);

export default router;

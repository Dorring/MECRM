import { Router, Response, NextFunction } from 'express';
import axios from 'axios';
import { body, validationResult } from 'express-validator';
import { AuthenticatedRequest } from '../middleware/auth';
import { badRequest } from '../middleware/errorHandler';
import { publishEvent, TOPICS } from '../services/kafka';
import { v4 as uuidv4 } from 'uuid';

const router = Router();

const AGENTS_URL = (process.env.AGENTS_URL || 'http://localhost:5010').replace(/\/$/, '');

const queryValidators = [
  body('query').isString().isLength({ min: 1, max: 500 }),
  body('module').optional().isString().isLength({ min: 1, max: 200 }),
];

const handleQuery = async (req: AuthenticatedRequest, res: Response, next: NextFunction) => {
  try {
    const errors = validationResult(req);
    if (!errors.isEmpty()) {
      throw badRequest('Validation failed', errors.array());
    }

    const queryText = String(req.body.query || '').trim();
    const module = req.body.module ? String(req.body.module) : undefined;
    const correlationId = req.headers['x-correlation-id'] as string;

    await publishEvent(TOPICS.INTELLIGENCE_USER_QUERY, {
      type: 'crm.intelligence.user-query',
      source: '/services/gateway',
      id: uuidv4(),
      tenantid: req.tenantId!,
      correlationid: correlationId,
      data: {
        query: queryText,
        module,
        userId: req.user?.sub,
        roles: req.user?.roles || [],
      },
    });

    const t0 = Date.now();
    const response = await axios.post(`${AGENTS_URL}/api/v1/intelligence/query`, req.body, {
      timeout: 2500,
      headers: {
        'Content-Type': 'application/json',
        'X-Tenant-Id': req.tenantId,
        'X-User-Id': req.user?.sub,
        'X-User-Roles': (req.user?.roles || []).join(','),
        'X-Correlation-Id': correlationId,
        ...(module ? { 'X-Client-Module': module } : {}),
        ...(req.headers.authorization ? { Authorization: String(req.headers.authorization) } : {}),
      },
    });

    const durationMs = Date.now() - t0;
    const payload = response.data || {};

    if (payload.search_id) {
      await publishEvent(TOPICS.INTELLIGENCE_SEARCH_PERFORMED, {
        type: 'crm.intelligence.search-performed',
        source: '/services/gateway',
        id: uuidv4(),
        tenantid: req.tenantId!,
        correlationid: correlationId,
        data: {
          searchId: payload.search_id,
          query: queryText,
          intent: payload.intent,
          resultCount: Array.isArray(payload.results) ? payload.results.length : 0,
          durationMs,
          module,
          userId: req.user?.sub,
        },
      });
    }

    res.json(payload);
  } catch (error) {
    next(error);
  }
};

router.post('/query', ...queryValidators, handleQuery);
router.post('/search', ...queryValidators, handleQuery);

router.post(
  '/click',
  body('searchId').isString().isLength({ min: 1, max: 100 }),
  body('entityType').isString().isLength({ min: 1, max: 50 }),
  body('entityId').isString().isLength({ min: 1, max: 100 }),
  async (req: AuthenticatedRequest, res: Response, next) => {
    try {
      const errors = validationResult(req);
      if (!errors.isEmpty()) {
        throw badRequest('Validation failed', errors.array());
      }

      const correlationId = req.headers['x-correlation-id'] as string;
      await publishEvent(TOPICS.INTELLIGENCE_SEARCH_CLICKED, {
        type: 'crm.intelligence.search-clicked',
        source: '/services/gateway',
        id: uuidv4(),
        tenantid: req.tenantId!,
        correlationid: correlationId,
        data: {
          searchId: req.body.searchId,
          entityType: req.body.entityType,
          entityId: req.body.entityId,
          userId: req.user?.sub,
        },
      });

      res.status(204).send();
    } catch (error) {
      next(error);
    }
  }
);

router.post(
  '/abandon',
  body('searchId').isString().isLength({ min: 1, max: 100 }),
  body('query').optional().isString().isLength({ min: 1, max: 500 }),
  async (req: AuthenticatedRequest, res: Response, next) => {
    try {
      const errors = validationResult(req);
      if (!errors.isEmpty()) {
        throw badRequest('Validation failed', errors.array());
      }

      const correlationId = req.headers['x-correlation-id'] as string;
      await publishEvent(TOPICS.INTELLIGENCE_SEARCH_ABANDONED, {
        type: 'crm.intelligence.search-abandoned',
        source: '/services/gateway',
        id: uuidv4(),
        tenantid: req.tenantId!,
        correlationid: correlationId,
        data: {
          searchId: req.body.searchId,
          query: req.body.query,
          userId: req.user?.sub,
        },
      });

      res.status(204).send();
    } catch (error) {
      next(error);
    }
  }
);

router.post(
  '/conversations/:id/close',
  body('module').optional().isString().isLength({ min: 1, max: 200 }),
  body('context').optional().isObject(),
  async (req: AuthenticatedRequest, res: Response, next) => {
    try {
      const errors = validationResult(req);
      if (!errors.isEmpty()) throw badRequest('Validation failed', errors.array());

      const correlationId = (req.headers['x-correlation-id'] as string) || uuidv4();
      const conversationId = String(req.params.id || '').trim();
      if (!conversationId) throw badRequest('Missing conversation id');

      await publishEvent(TOPICS.CONVERSATION_CLOSED, {
        type: 'crm.conversations.closed',
        source: '/services/gateway',
        id: uuidv4(),
        tenantid: req.tenantId!,
        correlationid: correlationId,
        data: {
          conversationId,
          module: req.body.module || null,
          context: req.body.context || null,
          userId: req.user?.sub,
        },
      });

      res.status(204).send();
    } catch (error) {
      next(error);
    }
  }
);

export default router;


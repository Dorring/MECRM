import { Router, Response } from 'express';
import axios from 'axios';
import { body, query, validationResult } from 'express-validator';
import { v4 as uuidv4 } from 'uuid';
import { AuthenticatedRequest } from '../middleware/auth';
import { badRequest } from '../middleware/errorHandler';
import { publishEvent, TOPICS } from '../services/kafka';
import { auditQueriesTotal } from '../services/metrics';

const router = Router();

const AGENTS_URL = (process.env.AGENTS_URL || 'http://localhost:5010').replace(/\/$/, '');

function requireAuditRole(req: AuthenticatedRequest): void {
  const roles = req.user?.roles || [];
  if (!roles.includes('admin') && !roles.includes('super_admin') && !roles.includes('auditor')) {
    throw badRequest('Insufficient privileges');
  }
}

router.post(
  '/search',
  body('query').isString().isLength({ min: 1, max: 2000 }),
  body('fromTs').optional().isString(),
  body('toTs').optional().isString(),
  body('agentName').optional().isString(),
  body('actionType').optional().isString(),
  body('status').optional().isString(),
  body('riskLevel').optional().isString(),
  async (req: AuthenticatedRequest, res: Response, next) => {
    try {
      requireAuditRole(req);

      const errors = validationResult(req);
      if (!errors.isEmpty()) throw badRequest('Validation failed', errors.array());

      const correlationId = (req.headers['x-correlation-id'] as string) || uuidv4();

      const resp = await axios.post(
        `${AGENTS_URL}/api/v1/audit/search`,
        {
          query: String(req.body.query),
          fromTs: req.body.fromTs,
          toTs: req.body.toTs,
          agentName: req.body.agentName,
          actionType: req.body.actionType,
          status: req.body.status,
          riskLevel: req.body.riskLevel,
        },
        {
          timeout: 7000,
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

      await publishEvent(TOPICS.AUDIT_ACCESSED, {
        type: 'crm.audit.accessed',
        source: '/services/gateway',
        id: uuidv4(),
        tenantid: req.tenantId!,
        correlationid: correlationId,
        data: {
          actor_type: 'user',
          actor_id: req.user?.sub,
          access_type: 'semantic_search',
          query: String(req.body.query).slice(0, 200),
          filters: {
            fromTs: req.body.fromTs || null,
            toTs: req.body.toTs || null,
            agentName: req.body.agentName || null,
            actionType: req.body.actionType || null,
            status: req.body.status || null,
            riskLevel: req.body.riskLevel || null,
          },
          hit_count: Array.isArray(resp.data?.hits) ? resp.data.hits.length : null,
        },
      });

      auditQueriesTotal.labels('semantic_search').inc();
      res.json(resp.data);
    } catch (error) {
      next(error);
    }
  }
);

router.get(
  '/policies',
  query('format').optional().isIn(['summary']),
  async (req: AuthenticatedRequest, res: Response, next) => {
    try {
      requireAuditRole(req);
      const errors = validationResult(req);
      if (!errors.isEmpty()) throw badRequest('Validation failed', errors.array());

      const format = String(req.query.format || 'summary');

      const fs = await import('fs/promises');
      const path = await import('path');
      const candidates = [path.resolve(process.cwd(), 'policies'), path.resolve(process.cwd(), '..', 'policies')];
      let policiesDir = candidates[0];
      for (const c of candidates) {
        try {
          await fs.access(c);
          policiesDir = c;
          break;
        } catch (err) {
          // continue to next candidate
        }
      }

      const files: { path: string; size: number }[] = [];
      const walk = async (dir: string) => {
        const entries = await fs.readdir(dir, { withFileTypes: true });
        for (const e of entries) {
          const full = path.join(dir, e.name);
          if (e.isDirectory()) {
            await walk(full);
          } else if (e.isFile() && (e.name.endsWith('.rego') || e.name.endsWith('.json'))) {
            const st = await fs.stat(full);
            files.push({ path: path.relative(policiesDir, full).replace(/\\/g, '/'), size: st.size });
          }
        }
      };
      await walk(policiesDir);

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
          access_type: 'policy_visibility',
          policy_files: files.length,
        },
      });

      res.json({ format, data: files.sort((a, b) => a.path.localeCompare(b.path)) });
    } catch (error) {
      next(error);
    }
  }
);

export default router;


import { Router, Response } from 'express';
import { body, query, validationResult } from 'express-validator';
import { uuidv4 } from '../utils/uuid';
import { withTenantDb } from '../services/prisma';
import { AuthenticatedRequest } from '../middleware/auth';
import { badRequest, notFound } from '../middleware/errorHandler';
import { publishEvent, TOPICS } from '../services/kafka';
import { knowledgeApprovalRate, knowledgeArticleReadsTotal, knowledgeArticleReuseTotal, knowledgeDraftDecisionsTotal } from '../services/metrics';

const router = Router();

function requireKnowledgeRead(req: AuthenticatedRequest): void {
  const roles = req.user?.roles || [];
  if (!roles.includes('admin') && !roles.includes('super_admin') && !roles.includes('auditor')) {
    throw badRequest('Insufficient privileges');
  }
}

function requireKnowledgeApprove(req: AuthenticatedRequest): void {
  const roles = req.user?.roles || [];
  if (!roles.includes('admin') && !roles.includes('super_admin')) {
    throw badRequest('Insufficient privileges');
  }
}

router.get(
  '/drafts',
  query('status').optional().isIn(['draft', 'approved', 'rejected']),
  query('page').optional().isInt({ min: 1 }),
  query('limit').optional().isInt({ min: 1, max: 200 }),
  async (req: AuthenticatedRequest, res: Response, next) => {
    try {
      requireKnowledgeRead(req);
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
          db.knowledgeDraft.findMany({ where, skip, take: limit, orderBy: { createdAt: 'desc' } }),
          db.knowledgeDraft.count({ where }),
        ]);
      });

      const correlationId = (req.headers['x-correlation-id'] as string) || uuidv4();
      await publishEvent(TOPICS.AUDIT_ACCESSED, {
        type: 'crm.audit.accessed',
        source: '/services/gateway',
        id: uuidv4(),
        tenantid: req.tenantId!,
        correlationid: correlationId,
        data: { actor_type: 'user', actor_id: req.user?.sub, access_type: 'knowledge_drafts_list', status, page, limit },
      });

      res.json({ data: items, pagination: { page, limit, total, totalPages: Math.ceil(total / limit) } });
    } catch (error) {
      next(error);
    }
  }
);

router.get('/drafts/:id', async (req: AuthenticatedRequest, res: Response, next) => {
  try {
    requireKnowledgeRead(req);
    const draft = await withTenantDb(req.tenantId!, (db) => db.knowledgeDraft.findFirst({ where: { id: req.params.id, tenantId: req.tenantId } }));
    if (!draft) throw notFound('Draft not found');

    const correlationId = (req.headers['x-correlation-id'] as string) || uuidv4();
    await publishEvent(TOPICS.AUDIT_ACCESSED, {
      type: 'crm.audit.accessed',
      source: '/services/gateway',
      id: uuidv4(),
      tenantid: req.tenantId!,
      correlationid: correlationId,
      data: { actor_type: 'user', actor_id: req.user?.sub, access_type: 'knowledge_draft_view', draft_id: req.params.id },
    });

    res.json(draft);
  } catch (error) {
    next(error);
  }
});

router.put(
  '/drafts/:id',
  body('title').optional().isString().isLength({ min: 1, max: 500 }),
  body('problemSummary').optional().isString().isLength({ min: 1, max: 10000 }),
  body('solutionSteps').optional().isArray({ max: 50 }),
  body('preconditions').optional().isArray({ max: 50 }),
  body('tags').optional().isArray({ max: 50 }),
  body('topic').optional().isString().isLength({ min: 1, max: 50 }),
  body('confidence').optional().isFloat({ min: 0, max: 1 }),
  async (req: AuthenticatedRequest, res: Response, next) => {
    try {
      requireKnowledgeApprove(req);
      const errors = validationResult(req);
      if (!errors.isEmpty()) throw badRequest('Validation failed', errors.array());

      const updated = await withTenantDb(req.tenantId!, async (db) => {
        const existing = await db.knowledgeDraft.findFirst({ where: { id: req.params.id, tenantId: req.tenantId } });
        if (!existing) throw notFound('Draft not found');
        if (existing.status !== 'draft') throw badRequest('Only draft items can be edited');

        return db.knowledgeDraft.update({
          where: { id: req.params.id },
          data: {
            title: req.body.title ?? undefined,
            problemSummary: req.body.problemSummary ?? undefined,
            solutionSteps: req.body.solutionSteps ?? undefined,
            preconditions: req.body.preconditions ?? undefined,
            tags: req.body.tags ?? undefined,
            topic: req.body.topic ?? undefined,
            confidence: req.body.confidence ?? undefined,
          },
        });
      });

      res.json(updated);
    } catch (error) {
      next(error);
    }
  }
);

router.post(
  '/drafts/:id/reject',
  body('reason').optional().isString().isLength({ min: 1, max: 2000 }),
  async (req: AuthenticatedRequest, res: Response, next) => {
    try {
      requireKnowledgeApprove(req);
      const errors = validationResult(req);
      if (!errors.isEmpty()) throw badRequest('Validation failed', errors.array());

      const updated = await withTenantDb(req.tenantId!, async (db) => {
        const existing = await db.knowledgeDraft.findFirst({ where: { id: req.params.id, tenantId: req.tenantId } });
        if (!existing) throw notFound('Draft not found');
        if (existing.status !== 'draft') throw badRequest('Only draft items can be rejected');
        return db.knowledgeDraft.update({
          where: { id: req.params.id },
          data: {
            status: 'rejected',
            rejectedBy: req.user?.sub,
            rejectedAt: new Date(),
            rejectionReason: req.body.reason || null,
          },
        });
      });

      knowledgeDraftDecisionsTotal.labels('rejected').inc();
      await updateApprovalRate(req.tenantId!);
      res.json(updated);
    } catch (error) {
      next(error);
    }
  }
);

router.post('/drafts/:id/approve', async (req: AuthenticatedRequest, res: Response, next) => {
  try {
    requireKnowledgeApprove(req);
    const correlationId = (req.headers['x-correlation-id'] as string) || uuidv4();

    const { draft, article } = await withTenantDb(req.tenantId!, async (db) => {
      const existing = await db.knowledgeDraft.findFirst({ where: { id: req.params.id, tenantId: req.tenantId } });
      if (!existing) throw notFound('Draft not found');
      if (existing.status !== 'draft') throw badRequest('Only draft items can be approved');

      const content = renderMarkdown(existing);
      const tags = Array.isArray(existing.tags) ? existing.tags : [];
      const created = await db.knowledgeArticle.create({
        data: {
          tenantId: req.tenantId!,
          sourceDraftId: existing.id,
          title: existing.title,
          content,
          tags,
        },
      });

      const updatedDraft = await db.knowledgeDraft.update({
        where: { id: existing.id },
        data: {
          status: 'approved',
          approvedBy: req.user?.sub,
          approvedAt: new Date(),
        },
      });

      return { draft: updatedDraft, article: created };
    });

    await publishEvent(TOPICS.KNOWLEDGE_PUBLISHED, {
      type: 'crm.knowledge.published',
      source: '/services/gateway',
      id: uuidv4(),
      tenantid: req.tenantId!,
      correlationid: correlationId,
      data: { articleId: article.id, draftId: draft.id, title: article.title, tags: article.tags },
    });

    knowledgeDraftDecisionsTotal.labels('approved').inc();
    await updateApprovalRate(req.tenantId!);
    res.json({ draft, article });
  } catch (error) {
    next(error);
  }
});

router.get(
  '/articles',
  query('tag').optional().isString().isLength({ min: 1, max: 100 }),
  query('page').optional().isInt({ min: 1 }),
  query('limit').optional().isInt({ min: 1, max: 200 }),
  async (req: AuthenticatedRequest, res: Response, next) => {
    try {
      requireKnowledgeRead(req);
      const errors = validationResult(req);
      if (!errors.isEmpty()) throw badRequest('Validation failed', errors.array());

      const page = parseInt(req.query.page as string) || 1;
      const limit = parseInt(req.query.limit as string) || 50;
      const skip = (page - 1) * limit;
      const tag = req.query.tag ? String(req.query.tag) : null;

      const where: any = { tenantId: req.tenantId };
      if (tag) where.tags = { array_contains: [tag] };

      const [items, total] = await withTenantDb(req.tenantId!, async (db) => {
        return Promise.all([
          db.knowledgeArticle.findMany({ where, skip, take: limit, orderBy: { createdAt: 'desc' } }),
          db.knowledgeArticle.count({ where }),
        ]);
      });

      const correlationId = (req.headers['x-correlation-id'] as string) || uuidv4();
      await publishEvent(TOPICS.AUDIT_ACCESSED, {
        type: 'crm.audit.accessed',
        source: '/services/gateway',
        id: uuidv4(),
        tenantid: req.tenantId!,
        correlationid: correlationId,
        data: { actor_type: 'user', actor_id: req.user?.sub, access_type: 'knowledge_articles_list', tag, page, limit },
      });

      res.json({ data: items, pagination: { page, limit, total, totalPages: Math.ceil(total / limit) } });
    } catch (error) {
      next(error);
    }
  }
);

router.get('/articles/:id', async (req: AuthenticatedRequest, res: Response, next) => {
  try {
    requireKnowledgeRead(req);

    const article = await withTenantDb(req.tenantId!, async (db) => {
      const existing = await db.knowledgeArticle.findFirst({ where: { id: req.params.id, tenantId: req.tenantId } });
      if (!existing) return null;
      await db.knowledgeArticle.update({
        where: { id: existing.id },
        data: { reuseCount: { increment: 1 }, lastAccessedAt: new Date() },
      });
      return existing;
    });

    if (!article) throw notFound('Article not found');

    const correlationId = (req.headers['x-correlation-id'] as string) || uuidv4();
    await publishEvent(TOPICS.AUDIT_ACCESSED, {
      type: 'crm.audit.accessed',
      source: '/services/gateway',
      id: uuidv4(),
      tenantid: req.tenantId!,
      correlationid: correlationId,
      data: { actor_type: 'user', actor_id: req.user?.sub, access_type: 'knowledge_article_view', article_id: req.params.id },
    });

    knowledgeArticleReadsTotal.labels('article_view').inc();
    knowledgeArticleReuseTotal.labels(req.tenantId!).inc();
    res.json(article);
  } catch (error) {
    next(error);
  }
});

async function updateApprovalRate(tenantId: string): Promise<void> {
  const { approved, total } = await withTenantDb(tenantId, async (db) => {
    const [approvedCount, totalCount] = await Promise.all([
      db.knowledgeDraft.count({ where: { tenantId, status: 'approved' } }),
      db.knowledgeDraft.count({ where: { tenantId } }),
    ]);
    return { approved: approvedCount, total: totalCount };
  });
  const ratio = total > 0 ? approved / total : 0;
  knowledgeApprovalRate.labels(tenantId).set(ratio);
}

function renderMarkdown(draft: any): string {
  const steps = Array.isArray(draft.solutionSteps) ? draft.solutionSteps : [];
  const pre = Array.isArray(draft.preconditions) ? draft.preconditions : [];
  const tags = Array.isArray(draft.tags) ? draft.tags : [];
  const topic = draft.topic ? String(draft.topic) : null;

  const lines: string[] = [];
  lines.push(`# ${draft.title}`);
  if (topic) lines.push(`\n**Topic:** ${topic}`);
  if (tags.length) lines.push(`\n**Tags:** ${tags.map((t: any) => String(t)).join(', ')}`);
  lines.push(`\n## Problem\n${draft.problemSummary}`);
  if (pre.length) {
    lines.push(`\n## Preconditions`);
    pre.forEach((p: any) => lines.push(`- ${String(p)}`));
  }
  if (steps.length) {
    lines.push(`\n## Resolution Steps`);
    steps.forEach((s: any, idx: number) => lines.push(`${idx + 1}. ${String(s)}`));
  }
  if (draft.sourceTicketId) lines.push(`\n## Source\n- Ticket: ${String(draft.sourceTicketId)}`);
  if (draft.sourceConversationId) lines.push(`- Conversation: ${String(draft.sourceConversationId)}`);
  return lines.join('\n');
}

export default router;

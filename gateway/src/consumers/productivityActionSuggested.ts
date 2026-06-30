import { createConsumer, startConsumer, TOPICS, DomainEvent } from '../services/kafka';
import { logger } from '../utils/logger';
import { withTenantDb } from '../services/prisma';
import { productivityProposalsGenerated } from '../services/metrics';

const UUID_REGEX = /^[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/i;
const isUuid = (value: unknown): value is string => typeof value === 'string' && UUID_REGEX.test(value);

export const startProductivityActionSuggestedIngestor = async (): Promise<() => Promise<void>> => {
  const consumer = createConsumer(process.env.KAFKA_PRODUCTIVITY_GROUP_ID || 'gateway-productivity-action-suggested');

  await startConsumer(consumer, [TOPICS.PRODUCTIVITY_ACTION_SUGGESTED], async ({ topic, message }) => {
    if (!message.value) return;

    let event: DomainEvent;
    try {
      event = JSON.parse(message.value.toString('utf-8'));
    } catch (error) {
      logger.warn('Productivity ingestor received invalid JSON', { topic, error });
      return;
    }

    const tenantId = event.tenantid;
    if (!isUuid(tenantId)) return;
    const data: any = event.data || {};

    const proposalId = isUuid(data.proposal_id) ? data.proposal_id : (isUuid(data.proposalId) ? data.proposalId : null);
    const userId = isUuid(data.user_id) ? data.user_id : (isUuid(data.userId) ? data.userId : null);
    const actionType = typeof data.action_type === 'string' ? data.action_type : (typeof data.actionType === 'string' ? data.actionType : null);
    const targetEntity = typeof data.target_entity === 'string' ? data.target_entity : (typeof data.targetEntity === 'string' ? data.targetEntity : null);
    const targetId = typeof data.target_id === 'string' ? data.target_id : (typeof data.targetId === 'string' ? data.targetId : null);
    const priority = typeof data.priority === 'string' ? data.priority : 'medium';
    const justification = typeof data.justification === 'string' ? data.justification : '';
    const drafts = typeof data.drafts === 'object' && data.drafts ? data.drafts : {};
    const dedupeKey = typeof data.dedupe_key === 'string' ? data.dedupe_key : (typeof data.dedupeKey === 'string' ? data.dedupeKey : null);
    const signalType = typeof data.signal_type === 'string' ? data.signal_type : (typeof data.signalType === 'string' ? data.signalType : 'unknown');
    const signal = typeof data.signal === 'object' && data.signal ? data.signal : {};

    if (!proposalId || !userId || !actionType || !targetEntity || !targetId || !dedupeKey) return;
    if (!justification.trim()) return;

    await withTenantDb(tenantId, async (db) => {
      const existing = await db.productivityProposal.findFirst({
        where: { tenantId, dedupeKey, status: 'pending' },
        select: { id: true },
      });
      if (existing) return;

      await db.productivityProposal.create({
        data: {
          id: proposalId,
          tenantId,
          userId,
          actionType,
          targetEntity,
          targetId,
          priority,
          justification,
          drafts,
          status: 'pending',
          dedupeKey,
          signalType,
          signal,
        },
      });
    });
    productivityProposalsGenerated.labels(actionType, priority).inc();
  });

  logger.info('Productivity action-suggested ingestor started');

  return async () => {
    await consumer.disconnect();
  };
};


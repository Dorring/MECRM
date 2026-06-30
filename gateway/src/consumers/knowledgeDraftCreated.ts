import { createConsumer, startConsumer, TOPICS, DomainEvent } from '../services/kafka';
import { logger } from '../utils/logger';
import { knowledgeDraftsCreatedTotal } from '../services/metrics';

const UUID_REGEX = /^[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/i;
const isUuid = (value: unknown): value is string => typeof value === 'string' && UUID_REGEX.test(value);

export const startKnowledgeDraftCreatedIngestor = async (): Promise<() => Promise<void>> => {
  const consumer = createConsumer(process.env.KAFKA_KNOWLEDGE_GROUP_ID || 'gateway-knowledge-draft-created');

  await startConsumer(consumer, [TOPICS.KNOWLEDGE_DRAFT_CREATED], async ({ topic, message }) => {
    if (!message.value) return;

    let event: DomainEvent;
    try {
      event = JSON.parse(message.value.toString('utf-8'));
    } catch (error) {
      logger.warn('Knowledge ingestor received invalid JSON', { topic, error });
      return;
    }

    const tenantId = event.tenantid;
    if (!isUuid(tenantId)) return;
    const data: any = event.data || {};
    const sourceType = typeof data.sourceType === 'string' ? data.sourceType : (typeof data.source_type === 'string' ? data.source_type : 'unknown');
    const topicLabel = typeof data.topic === 'string' ? data.topic : 'unknown';
    knowledgeDraftsCreatedTotal.labels(tenantId, sourceType, topicLabel).inc();
  });

  logger.info('Knowledge draft created ingestor started');

  return async () => {
    await consumer.disconnect();
  };
};

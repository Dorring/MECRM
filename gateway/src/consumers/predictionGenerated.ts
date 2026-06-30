import { createConsumer, startConsumer, TOPICS, DomainEvent } from '../services/kafka';
import { logger } from '../utils/logger';
import { withTenantDb } from '../services/prisma';
import { badgeDistributionTotal, predictionLatencyMs } from '../services/metrics';

const UUID_REGEX = /^[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/i;
const isUuid = (value: unknown): value is string => typeof value === 'string' && UUID_REGEX.test(value);

export const startPredictionGeneratedIngestor = async (): Promise<() => Promise<void>> => {
  const consumer = createConsumer(process.env.KAFKA_PREDICTIONS_GROUP_ID || 'gateway-predictions');

  await startConsumer(consumer, [TOPICS.ANALYTICS_PREDICTION_GENERATED], async ({ topic, message }) => {
    if (!message.value) return;

    let event: DomainEvent;
    try {
      event = JSON.parse(message.value.toString('utf-8'));
    } catch (error) {
      logger.warn('Predictions ingestor received invalid JSON', { topic, error });
      return;
    }

    const tenantId = event.tenantid;
    if (!isUuid(tenantId)) return;
    const data: any = event.data || {};

    const entityId = typeof data.entity_id === 'string' ? data.entity_id : (typeof data.entityId === 'string' ? data.entityId : null);
    const entityType = typeof data.entity_type === 'string' ? data.entity_type : (typeof data.entityType === 'string' ? data.entityType : null);
    const predictionType = typeof data.prediction_type === 'string' ? data.prediction_type : (typeof data.predictionType === 'string' ? data.predictionType : null);
    const probability = typeof data.probability === 'number' ? data.probability : null;
    const riskLevel = typeof data.risk_level === 'string' ? data.risk_level : (typeof data.riskLevel === 'string' ? data.riskLevel : null);
    const explanation = typeof data.explanation === 'string' ? data.explanation : '';
    const features = typeof data.features === 'object' && data.features ? data.features : {};
    const modelVersion = typeof data.model_version === 'string' ? data.model_version : (typeof data.modelVersion === 'string' ? data.modelVersion : 'unknown');
    const createdAtRaw = typeof data.created_at === 'string' ? data.created_at : null;

    if (!entityId || !entityType || !predictionType || probability === null || !riskLevel || !explanation.trim()) return;

    const createdAt = createdAtRaw ? new Date(createdAtRaw) : new Date();
    const safeCreatedAt = Number.isNaN(createdAt.getTime()) ? new Date() : createdAt;

    await withTenantDb(tenantId, async (db) => {
      await db.prediction.create({
        data: {
          tenantId,
          entityType,
          entityId,
          predictionType,
          probability,
          riskLevel,
          explanation,
          features,
          modelVersion,
          createdAt: safeCreatedAt,
        },
      });
    });

    badgeDistributionTotal.labels(entityType, predictionType, riskLevel).inc();
    predictionLatencyMs.labels(predictionType, entityType, riskLevel).observe(Date.now() - safeCreatedAt.getTime());
  });

  logger.info('Predictions ingestor started');

  return async () => {
    await consumer.disconnect();
  };
};


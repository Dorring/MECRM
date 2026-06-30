import { createConsumer, startConsumer, TOPICS, DomainEvent } from '../services/kafka';
import { logger } from '../utils/logger';
import { withTenantDb } from '../services/prisma';
import { stageTransitionRateTotal } from '../services/metrics';

const UUID_REGEX = /^[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/i;
const isUuid = (value: unknown): value is string => typeof value === 'string' && UUID_REGEX.test(value);

export const startJourneyUpdatedIngestor = async (): Promise<() => Promise<void>> => {
  const consumer = createConsumer(process.env.KAFKA_JOURNEY_GROUP_ID || 'gateway-journey-updated');

  await startConsumer(consumer, [TOPICS.JOURNEY_UPDATED], async ({ topic, message }) => {
    if (!message.value) return;

    let event: DomainEvent;
    try {
      event = JSON.parse(message.value.toString('utf-8'));
    } catch (error) {
      logger.warn('Journey ingestor received invalid JSON', { topic, error });
      return;
    }

    const tenantId = event.tenantid;
    if (!isUuid(tenantId)) return;
    const data: any = event.data || {};

    const customerId = isUuid(data.customer_id) ? data.customer_id : (isUuid(data.customerId) ? data.customerId : null);
    const stage = typeof data.stage === 'string' ? data.stage : null;
    const confidence = typeof data.confidence === 'number' ? data.confidence : 0;
    const features = typeof data.features === 'object' && data.features ? data.features : {};
    const timeline = typeof data.timeline_entry === 'object' && data.timeline_entry ? data.timeline_entry : {};
    const eventType = typeof timeline.event_type === 'string' ? timeline.event_type : null;
    const eventPayload = typeof timeline.event_payload === 'object' && timeline.event_payload ? timeline.event_payload : {};
    const timestampRaw = typeof timeline.timestamp === 'string' ? timeline.timestamp : null;

    if (!customerId || !stage || !eventType || !timestampRaw) return;

    const ts = new Date(timestampRaw);
    const timestamp = Number.isNaN(ts.getTime()) ? new Date() : ts;

    await withTenantDb(tenantId, async (db) => {
      const existingProfile = await db.customerProfile.findUnique({ where: { customerId }, select: { stage: true } });
      await db.customerTimeline.create({
        data: {
          tenantId,
          customerId,
          eventType,
          eventPayload,
          ts: timestamp,
        },
      });

      await db.customerProfile.upsert({
        where: { customerId },
        create: {
          tenantId,
          customerId,
          stage,
          stageConfidence: confidence,
          stageUpdatedAt: new Date(),
          features,
        },
        update: {
          stage,
          stageConfidence: confidence,
          stageUpdatedAt: new Date(),
          features,
        },
      });

      const prev = existingProfile?.stage;
      if (prev && prev !== stage) stageTransitionRateTotal.labels(prev, stage).inc();
    });
  });

  logger.info('Journey updated ingestor started');

  return async () => {
    await consumer.disconnect();
  };
};


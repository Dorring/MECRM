import { createConsumer, startConsumer, TOPICS } from '../services/kafka';
import { logger } from '../utils/logger';
import { withTenantDb } from '../services/prisma';
import { v5 as uuidv5 } from 'uuid';

const UUID_REGEX = /^[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/i;
const NAMESPACE = '00000000-0000-0000-0000-000000000001';

const isUuid = (value: unknown): value is string => typeof value === 'string' && UUID_REGEX.test(value);

const actorUuid = (value: unknown): string => {
  if (isUuid(value)) return value;
  if (typeof value === 'string' && value.length > 0) return uuidv5(value, NAMESPACE);
  return uuidv5('unknown', NAMESPACE);
};

export const startAuditEventsIngestor = async (): Promise<() => Promise<void>> => {
  const consumer = createConsumer(process.env.KAFKA_AUDIT_GROUP_ID || 'gateway-audit-events');

  await startConsumer(consumer, [TOPICS.AUDIT_EVENTS], async ({ topic, message }) => {
    if (!message.value) return;

    let evt: any;
    try {
      evt = JSON.parse(message.value.toString('utf-8'));
    } catch (error) {
      logger.warn('Audit ingestor received invalid JSON', { topic, error });
      return;
    }

    const tenantId = evt.tenantId;
    if (!isUuid(tenantId)) return;

    const idSource = `${tenantId}|${evt.timestamp}|${evt.action}|${evt.resourceType}|${evt.resourceId || ''}|${evt.correlationId || ''}`;
    const auditId = uuidv5(idSource, NAMESPACE);

    const actorType = typeof evt.actorType === 'string' ? evt.actorType : 'unknown';
    const actorId = actorUuid(evt.actorId);
    const action = typeof evt.action === 'string' ? evt.action : 'unknown';
    const resourceType = typeof evt.resourceType === 'string' ? evt.resourceType : 'unknown';
    const resourceId = isUuid(evt.resourceId) ? evt.resourceId : null;
    const correlationId = isUuid(evt.correlationId) ? evt.correlationId : null;

    const newValue = {
      requestBody: evt.requestBody,
      responseStatus: evt.responseStatus,
      duration: evt.duration,
    };

    const createdAt = evt.timestamp ? new Date(evt.timestamp) : new Date();

    await withTenantDb(tenantId, async (db) => {
      const existing = await db.auditLog.findFirst({ where: { id: auditId, tenantId } });
      if (existing) return;
      await db.auditLog.create({
        data: {
          id: auditId,
          tenantId,
          actorType,
          actorId,
          action,
          resourceType,
          resourceId,
          oldValue: undefined,
          newValue,
          ipAddress: typeof evt.ipAddress === 'string' ? evt.ipAddress : undefined,
          userAgent: typeof evt.userAgent === 'string' ? evt.userAgent : undefined,
          correlationId: correlationId || undefined,
          createdAt,
        },
      });
    });
  });

  logger.info('Audit events ingestor started');

  return async () => {
    await consumer.disconnect();
  };
};

import { createConsumer, startConsumer, TOPICS, DomainEvent } from '../services/kafka';
import { logger } from '../utils/logger';
import { withTenantDb } from '../services/prisma';

const UUID_REGEX = /^[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/i;

const isUuid = (value: unknown): value is string => typeof value === 'string' && UUID_REGEX.test(value);

export const startApprovalsRequiredIngestor = async (): Promise<() => Promise<void>> => {
  const consumer = createConsumer(process.env.KAFKA_APPROVALS_GROUP_ID || 'gateway-approvals-required');

  await startConsumer(consumer, [TOPICS.APPROVALS_REQUIRED], async ({ topic, message }) => {
    if (!message.value) return;

    let event: DomainEvent;
    try {
      event = JSON.parse(message.value.toString('utf-8'));
    } catch (error) {
      logger.warn('Approvals ingestor received invalid JSON', { topic, error });
      return;
    }

    const tenantId = event.tenantid;
    const data: any = event.data || {};

    const approvalId = isUuid(data.approvalId) ? data.approvalId : (isUuid(event.id) ? event.id : null);
    if (!approvalId || !isUuid(tenantId)) return;

    const requestorType = data.requestorType || 'agent';
    const requestorId = isUuid(data.requestorId) ? data.requestorId : null;
    const actionType = data.actionType;
    if (!requestorId || typeof actionType !== 'string') return;

    const rawTargetId = data.targetId;
    const targetId = isUuid(rawTargetId) ? rawTargetId : null;
    const targetEntity = typeof data.targetEntity === 'string' ? data.targetEntity : null;

    const ttlSeconds = typeof data.ttlSeconds === 'number' ? data.ttlSeconds : (typeof data.expiresInSeconds === 'number' ? data.expiresInSeconds : null);
    const expiresAt = ttlSeconds ? new Date(Date.now() + ttlSeconds * 1000) : (data.expiresAt ? new Date(data.expiresAt) : null);

    const context = {
      ...(data.context || {}),
      reasoning: data.reasoning,
      confidence: data.confidence,
      agentType: data.agentType,
      approvalPolicy: {
        approvers: data.approvers,
        priority: data.priority,
        approvalLevels: data.approvalLevels,
      },
      originalTargetId: targetId ? undefined : rawTargetId,
    };

    await withTenantDb(tenantId, async (db) => {
      const existing = await db.approval.findFirst({ where: { id: approvalId, tenantId } });
      if (existing) return;

      await db.approval.create({
        data: {
          id: approvalId,
          tenantId,
          requestType: data.requestType || 'human_in_loop',
          requestorType,
          requestorId,
          actionType,
          targetEntity,
          targetId,
          context,
          status: 'pending',
          expiresAt,
        },
      });
    });
  });

  logger.info('Approvals required ingestor started');

  return async () => {
    await consumer.disconnect();
  };
};


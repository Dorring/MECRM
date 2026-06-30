import { createConsumer, startConsumer, TOPICS, DomainEvent } from '../services/kafka';
import { logger } from '../utils/logger';
import { withTenantDb } from '../services/prisma';
import { automationExecutionsTotal } from '../services/metrics';

const UUID_REGEX = /^[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/i;
const isUuid = (value: unknown): value is string => typeof value === 'string' && UUID_REGEX.test(value);

export const startAutomationExecutedIngestor = async (): Promise<() => Promise<void>> => {
  const consumer = createConsumer(process.env.KAFKA_AUTOMATION_EXECUTED_GROUP_ID || 'gateway-automation-executed');

  await startConsumer(consumer, [TOPICS.AUTOMATION_EXECUTED], async ({ topic, message }) => {
    if (!message.value) return;

    let event: DomainEvent;
    try {
      event = JSON.parse(message.value.toString('utf-8'));
    } catch (error) {
      logger.warn('Automation executed ingestor received invalid JSON', { topic, error });
      return;
    }

    const tenantId = event.tenantid;
    if (!isUuid(tenantId)) return;
    const data: any = event.data || {};

    const executionId = isUuid(data.execution_id) ? data.execution_id : (isUuid(data.executionId) ? data.executionId : null);
    const policyId = isUuid(data.policy_id) ? data.policy_id : (isUuid(data.policyId) ? data.policyId : null);
    const triggerEventId = isUuid(data.trigger_event_id) ? data.trigger_event_id : (isUuid(data.triggerEventId) ? data.triggerEventId : null);
    const triggerType = typeof data.trigger_type === 'string' ? data.trigger_type : (typeof data.triggerType === 'string' ? data.triggerType : null);
    const actions = Array.isArray(data.actions) ? data.actions : [];
    const dryRun = Boolean(data.dry_run ?? data.dryRun ?? false);
    const status = typeof data.status === 'string' ? data.status : 'completed';

    if (!executionId || !policyId || !triggerType) return;

    await withTenantDb(tenantId, async (db) => {
      const existing = await db.automationPolicy.findFirst({ where: { id: policyId, tenantId }, select: { id: true } });
      if (!existing) return;
      await db.automationExecution.create({
        data: {
          id: executionId,
          tenantId,
          policyId,
          triggerEventId,
          triggerType,
          actionsJson: actions,
          status,
          dryRun,
        },
      });
    });

    automationExecutionsTotal.labels(triggerType).inc();
  }, 'automation_executed');

  logger.info('Automation executed ingestor started');

  return async () => {
    await consumer.disconnect();
  };
};


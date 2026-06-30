import { createConsumer, startConsumer, TOPICS, DomainEvent } from '../services/kafka';
import { logger } from '../utils/logger';
import { withTenantDb } from '../services/prisma';
import { automationSimulationTriggerCount } from '../services/metrics';

const UUID_REGEX = /^[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/i;
const isUuid = (value: unknown): value is string => typeof value === 'string' && UUID_REGEX.test(value);

export const startAutomationSimulationResultIngestor = async (): Promise<() => Promise<void>> => {
  const consumer = createConsumer(process.env.KAFKA_AUTOMATION_SIMULATION_GROUP_ID || 'gateway-automation-simulation-result');

  await startConsumer(consumer, [TOPICS.AUTOMATION_SIMULATION_RESULT], async ({ topic, message }) => {
    if (!message.value) return;

    let event: DomainEvent;
    try {
      event = JSON.parse(message.value.toString('utf-8'));
    } catch (error) {
      logger.warn('Automation simulation ingestor received invalid JSON', { topic, error });
      return;
    }

    const tenantId = event.tenantid;
    if (!isUuid(tenantId)) return;
    const data: any = event.data || {};

    const simulationId = isUuid(data.simulation_id) ? data.simulation_id : (isUuid(data.simulationId) ? data.simulationId : null);
    const policyId = isUuid(data.policy_id) ? data.policy_id : (isUuid(data.policyId) ? data.policyId : null);
    const requestedBy = isUuid(data.requested_by) ? data.requested_by : (isUuid(data.requestedBy) ? data.requestedBy : null);
    const fromTs = typeof data.from_ts === 'string' ? data.from_ts : (typeof data.fromTs === 'string' ? data.fromTs : null);
    const toTs = typeof data.to_ts === 'string' ? data.to_ts : (typeof data.toTs === 'string' ? data.toTs : null);
    const result = typeof data.result === 'object' && data.result ? data.result : null;
    const wouldHaveTriggered = typeof result?.would_have_triggered === 'number' ? result.would_have_triggered : 0;
    let triggerType: string | null = null;

    if (!simulationId || !policyId || !result) return;

    await withTenantDb(tenantId, async (db) => {
      const existing = await db.automationPolicy.findFirst({ where: { id: policyId, tenantId }, select: { id: true, triggerType: true } });
      if (!existing) return;
      triggerType = existing.triggerType;

      await db.automationSimulation.create({
        data: {
          id: simulationId,
          tenantId,
          policyId,
          requestedBy,
          fromTs: fromTs ? new Date(fromTs) : null,
          toTs: toTs ? new Date(toTs) : null,
          result,
        },
      });

      await db.automationPolicy.update({
        where: { id: policyId },
        data: { lastSimulationId: simulationId, status: 'draft' },
      });
    });

    if (triggerType) automationSimulationTriggerCount.labels(triggerType).inc(wouldHaveTriggered);
  }, 'automation_simulation_result');

  logger.info('Automation simulation result ingestor started');

  return async () => {
    await consumer.disconnect();
  };
};


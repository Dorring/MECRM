import { createConsumer, startConsumer, TOPICS, DomainEvent } from '../services/kafka';
import { logger } from '../utils/logger';
import { withTenantDb } from '../services/prisma';
import { automationsActive } from '../services/metrics';

const UUID_REGEX = /^[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/i;
const isUuid = (value: unknown): value is string => typeof value === 'string' && UUID_REGEX.test(value);

export const startAutomationActivationDecisionConsumer = async (): Promise<() => Promise<void>> => {
  const consumer = createConsumer(process.env.KAFKA_AUTOMATIONS_GROUP_ID || 'gateway-automation-activation-decision');

  await startConsumer(consumer, [TOPICS.APPROVALS_DECISION], async ({ topic, message }) => {
    if (!message.value) return;

    let event: DomainEvent;
    try {
      event = JSON.parse(message.value.toString('utf-8'));
    } catch (error) {
      logger.warn('Automation activation consumer received invalid JSON', { topic, error });
      return;
    }

    const tenantId = event.tenantid;
    if (!isUuid(tenantId)) return;
    const data: any = event.data || {};
    const approvalId = isUuid(data.approvalId) ? data.approvalId : (isUuid(data.approval_id) ? data.approval_id : null);
    const decision = typeof data.decision === 'string' ? data.decision : null;
    const actionType = typeof data.actionType === 'string' ? data.actionType : (typeof data.action_type === 'string' ? data.action_type : null);
    const targetEntity = typeof data.targetEntity === 'string' ? data.targetEntity : (typeof data.target_entity === 'string' ? data.target_entity : null);
    const targetId = isUuid(data.targetId) ? data.targetId : (isUuid(data.target_id) ? data.target_id : null);

    if (!approvalId || !decision || actionType !== 'automations:activate' || targetEntity !== 'automation_policy' || !targetId) return;

    await withTenantDb(tenantId, async (db) => {
      const policy = await db.automationPolicy.findFirst({ where: { id: targetId, tenantId } });
      if (!policy) return;
      if (decision === 'approved') {
        await db.automationPolicy.update({ where: { id: targetId }, data: { status: 'active' } });
      } else if (decision === 'rejected') {
        await db.automationPolicy.update({ where: { id: targetId }, data: { status: 'draft' } });
      }
      const activeCount = await db.automationPolicy.count({ where: { tenantId, status: 'active' } });
      automationsActive.labels(tenantId).set(activeCount);
    });
  }, 'automation_activation_decision');

  logger.info('Automation activation decision consumer started');

  return async () => {
    await consumer.disconnect();
  };
};


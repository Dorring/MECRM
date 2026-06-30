import { createConsumer, startConsumer, TOPICS, DomainEvent } from '../services/kafka';
import { logger } from '../utils/logger';
import { withTenantDb } from '../services/prisma';
import { sendToUser } from '../services/websocket';

const UUID_REGEX = /^[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/i;
const isUuid = (value: unknown): value is string => typeof value === 'string' && UUID_REGEX.test(value);

async function resolveUsersByRole(tenantId: string, roleName: string): Promise<string[]> {
  if (!roleName) return [];
  const normalized = roleName.trim();
  if (!normalized) return [];

  return await withTenantDb(tenantId, async (db) => {
    const role = await db.role.findFirst({ where: { tenantId, name: normalized }, select: { id: true } });
    if (!role) return [];
    const userRoles = await db.userRole.findMany({ where: { tenantId, roleId: role.id }, select: { userId: true } });
    return userRoles.map((u) => u.userId);
  });
}

export const startAutomationActionRequestedConsumer = async (): Promise<() => Promise<void>> => {
  const consumer = createConsumer(process.env.KAFKA_AUTOMATION_ACTIONS_GROUP_ID || 'gateway-automation-action-requested');

  await startConsumer(consumer, [TOPICS.AUTOMATION_ACTION_REQUESTED], async ({ topic, message }) => {
    if (!message.value) return;

    let event: DomainEvent;
    try {
      event = JSON.parse(message.value.toString('utf-8'));
    } catch (error) {
      logger.warn('Automation action consumer received invalid JSON', { topic, error });
      return;
    }

    const tenantId = event.tenantid;
    if (!isUuid(tenantId)) return;
    const data: any = event.data || {};
    const policyId = isUuid(data.policy_id) ? data.policy_id : (isUuid(data.policyId) ? data.policyId : null);
    const executionId = isUuid(data.execution_id) ? data.execution_id : (isUuid(data.executionId) ? data.executionId : null);
    const triggerEventId = isUuid(data.trigger_event_id) ? data.trigger_event_id : (isUuid(data.triggerEventId) ? data.triggerEventId : null);
    const policyCreatedBy = isUuid(data.policy_created_by) ? data.policy_created_by : (isUuid(data.policyCreatedBy) ? data.policyCreatedBy : null);
    const actionIndex = typeof data.action_index === 'number' ? data.action_index : (typeof data.actionIndex === 'number' ? data.actionIndex : 0);
    const action = typeof data.action === 'object' && data.action ? data.action : null;

    if (!policyId || !executionId || !action) return;

    const actionType = String(action.type || '');
    if (!actionType) return;

    if (actionType === 'notify') {
      const roleName = String(action.role || '').trim();
      const users = await resolveUsersByRole(tenantId, roleName);
      const messageText = typeof action.message === 'string' && action.message.trim() ? String(action.message) : 'Automation notification';
      users.forEach((userId) => {
        sendToUser(tenantId, userId, {
          type: 'automation_notification',
          payload: {
            policyId,
            executionId,
            triggerEventId,
            role: roleName,
            message: messageText,
          },
        });
      });
      return;
    }

    if (actionType === 'create_task' || actionType === 'propose_followup') {
      const assigneeRole = actionType === 'create_task' ? String(action.assignee_role || '').trim() : '';
      const candidateUsers = assigneeRole ? await resolveUsersByRole(tenantId, assigneeRole) : [];
      const userId = candidateUsers[0] || policyCreatedBy;
      if (!userId) return;

      const dedupeKey = `automation:${policyId}:${triggerEventId || executionId}:${actionIndex}`;
      const priority = actionType === 'create_task' ? String(action.priority || 'medium') : 'medium';
      const justification = actionType === 'create_task'
        ? `Automation requested task: ${String(action.task || 'Task')}`
        : `Automation proposed follow-up`;
      const drafts = actionType === 'create_task'
        ? { task: String(action.task || ''), assignee_role: assigneeRole || null }
        : { entity_type: action.entity_type, note: action.note || null, entity_id_field: action.entity_id_field || null };

      await withTenantDb(tenantId, async (db) => {
        const existing = await db.productivityProposal.findFirst({ where: { tenantId, dedupeKey }, select: { id: true } });
        if (existing) return;
        await db.productivityProposal.create({
          data: {
            tenantId,
            userId,
            actionType: actionType === 'create_task' ? 'tasks:create' : 'followup:propose',
            targetEntity: 'automation_policy',
            targetId: policyId,
            priority,
            justification,
            drafts,
            status: 'pending',
            dedupeKey,
            signalType: 'automation_action',
            signal: {
              policy_id: policyId,
              execution_id: executionId,
              trigger_event_id: triggerEventId,
              action_type: actionType,
            },
          },
        });
      });
      return;
    }
  }, 'automation_action_requested');

  logger.info('Automation action requested consumer started');

  return async () => {
    await consumer.disconnect();
  };
};


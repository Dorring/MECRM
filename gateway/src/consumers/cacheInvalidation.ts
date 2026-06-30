import { createConsumer, startConsumer, TOPICS, DomainEvent } from '../services/kafka';
import { logger } from '../utils/logger';
import { secureCache } from '../services/secureCache';
import { cacheInvalidationTotal } from '../services/metrics';

const UUID_REGEX = /^[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/i;
const isUuid = (value: unknown): value is string => typeof value === 'string' && UUID_REGEX.test(value);

export const startCacheInvalidationConsumer = async (): Promise<() => Promise<void>> => {
  const consumer = createConsumer(process.env.KAFKA_CACHE_INVALIDATION_GROUP_ID || 'gateway-cache-invalidation');

  await startConsumer(
    consumer,
    [TOPICS.APPROVALS_DECISION, TOPICS.SECURITY_EVENTS, TOPICS.GDPR_FORGET],
    async ({ topic, message }) => {
      if (!message.value) return;

      let event: DomainEvent;
      try {
        event = JSON.parse(message.value.toString('utf-8'));
      } catch (error) {
        logger.warn('Cache invalidation consumer received invalid JSON', { topic, error });
        return;
      }

      const tenantId = event.tenantid;
      if (!isUuid(tenantId)) return;

      if (topic === TOPICS.APPROVALS_DECISION) {
        await secureCache.bumpTenantEpoch(tenantId);
        cacheInvalidationTotal.labels(tenantId, 'approval_decision').inc();
        return;
      }

      if (topic === TOPICS.GDPR_FORGET) {
        await secureCache.bumpTenantEpoch(tenantId);
        cacheInvalidationTotal.labels(tenantId, 'gdpr_forget').inc();
        return;
      }

      if (topic !== TOPICS.SECURITY_EVENTS) return;

      const data: any = event.data || {};
      const eventType = typeof data.eventType === 'string' ? data.eventType : null;
      if (!eventType) return;

      if (eventType === 'tenant_suspended') {
        await secureCache.bumpTenantEpoch(tenantId);
        cacheInvalidationTotal.labels(tenantId, 'tenant_suspended').inc();
        return;
      }

      if (eventType === 'policy_updated') {
        const policyId = typeof data.policyId === 'string' ? data.policyId : null;
        if (!policyId) return;
        await secureCache.bumpPolicyEpoch(policyId);
        cacheInvalidationTotal.labels(tenantId, `policy_updated:${policyId}`).inc();
        return;
      }

      if (eventType === 'role_changed' || eventType === 'permission_updated') {
        const userId = typeof data.userId === 'string' ? data.userId : null;
        if (!userId) return;
        await secureCache.bumpUserEpoch(tenantId, userId);
        cacheInvalidationTotal.labels(tenantId, `${eventType}:${userId}`).inc();
        return;
      }
    }
  );

  logger.info('Cache invalidation consumer started');

  return async () => {
    await consumer.disconnect();
  };
};

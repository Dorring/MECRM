import { Application } from 'express';
import { Registry, Counter, Histogram, Gauge, collectDefaultMetrics } from 'prom-client';

// Create a custom registry
const register = new Registry();

// Collect default metrics (CPU, memory, etc.)
collectDefaultMetrics({ register });

// Custom metrics
export const httpRequestsTotal = new Counter({
  name: 'http_requests_total',
  help: 'Total number of HTTP requests',
  labelNames: ['method', 'path', 'status'],
  registers: [register],
});

export const httpRequestDuration = new Histogram({
  name: 'http_request_duration_seconds',
  help: 'Duration of HTTP requests in seconds',
  labelNames: ['method', 'path', 'status'],
  buckets: [0.01, 0.05, 0.1, 0.5, 1, 2, 5, 10],
  registers: [register],
});

export const activeConnections = new Gauge({
  name: 'websocket_active_connections',
  help: 'Number of active WebSocket connections',
  labelNames: ['tenant'],
  registers: [register],
});

export const kafkaMessagesPublished = new Counter({
  name: 'kafka_messages_published_total',
  help: 'Total number of Kafka messages published',
  labelNames: ['topic'],
  registers: [register],
});

export const kafkaMessagesConsumed = new Counter({
  name: 'kafka_messages_consumed_total',
  help: 'Total number of Kafka messages consumed',
  labelNames: ['topic', 'group'],
  registers: [register],
});

export const auditQueriesTotal = new Counter({
  name: 'audit_queries_total',
  help: 'Total number of audit queries',
  labelNames: ['type'],
  registers: [register],
});

export const killSwitchUsageTotal = new Counter({
  name: 'kill_switch_usage_total',
  help: 'Total kill switch operations',
  labelNames: ['scope', 'state'],
  registers: [register],
});

export const knowledgeDraftDecisionsTotal = new Counter({
  name: 'knowledge_draft_decisions_total',
  help: 'Knowledge draft decisions',
  labelNames: ['decision'],
  registers: [register],
});

export const knowledgeArticleReadsTotal = new Counter({
  name: 'knowledge_article_reads_total',
  help: 'Knowledge article reads',
  labelNames: ['type'],
  registers: [register],
});

export const knowledgeDraftsCreatedTotal = new Counter({
  name: 'knowledge_drafts_created_total',
  help: 'Knowledge drafts created',
  labelNames: ['tenant', 'source_type', 'topic'],
  registers: [register],
});

export const knowledgeArticleReuseTotal = new Counter({
  name: 'knowledge_article_reuse_total',
  help: 'Knowledge article reuse events',
  labelNames: ['tenant'],
  registers: [register],
});

export const knowledgeApprovalRate = new Gauge({
  name: 'knowledge_approval_rate_ratio',
  help: 'Ratio of approved drafts to total drafts',
  labelNames: ['tenant'],
  registers: [register],
});

export const opaDecisions = new Counter({
  name: 'opa_decisions_total',
  help: 'Total number of OPA policy decisions',
  labelNames: ['result', 'policy'],
  registers: [register],
});

export const agentTasksTotal = new Counter({
  name: 'agent_tasks_total',
  help: 'Total number of agent tasks',
  labelNames: ['agent', 'status'],
  registers: [register],
});

export const approvalsPending = new Gauge({
  name: 'approvals_pending',
  help: 'Number of pending approvals',
  labelNames: ['tenant', 'type'],
  registers: [register],
});

export const automationsActive = new Gauge({
  name: 'automations_active',
  help: 'Number of active automation policies',
  labelNames: ['tenant'],
  registers: [register],
});

export const automationExecutionsTotal = new Counter({
  name: 'automation_executions_total',
  help: 'Total automation executions',
  labelNames: ['trigger_type'],
  registers: [register],
});

export const automationSimulationTriggerCount = new Counter({
  name: 'automation_simulation_trigger_count_total',
  help: 'Total triggers observed in simulations',
  labelNames: ['trigger_type'],
  registers: [register],
});

export const productivityProposalsGenerated = new Counter({
  name: 'proposals_generated_total',
  help: 'Total number of productivity proposals generated',
  labelNames: ['action_type', 'priority'],
  registers: [register],
});

export const productivityApprovalsTotal = new Counter({
  name: 'approvals_rate_total',
  help: 'Total number of productivity proposal decisions',
  labelNames: ['decision'],
  registers: [register],
});

export const productivityRejectionTotal = new Counter({
  name: 'rejection_rate_total',
  help: 'Total number of rejected productivity proposals',
  labelNames: ['decision'],
  registers: [register],
});

export const productivityResolutionTimeMs = new Histogram({
  name: 'avg_idle_resolution_time_ms',
  help: 'Time from proposal creation to decision in milliseconds',
  labelNames: ['decision'],
  buckets: [1000, 5000, 15000, 30000, 60000, 300000, 900000, 3600000, 21600000, 86400000],
  registers: [register],
});

export const predictionLatencyMs = new Histogram({
  name: 'prediction_latency_ms',
  help: 'Latency from prediction creation time to ingestion in milliseconds',
  labelNames: ['prediction_type', 'entity_type', 'risk_level'],
  buckets: [10, 50, 100, 250, 500, 1000, 5000, 15000, 60000, 300000],
  registers: [register],
});

export const badgeDistributionTotal = new Counter({
  name: 'badge_distribution_total',
  help: 'Count of predictions by risk badge level',
  labelNames: ['entity_type', 'prediction_type', 'risk_level'],
  registers: [register],
});

export const stageTransitionRateTotal = new Counter({
  name: 'stage_transition_rate_total',
  help: 'Count of customer stage transitions',
  labelNames: ['from_stage', 'to_stage'],
  registers: [register],
});

export const cacheHitTotal = new Counter({
  name: 'cache_hit_total',
  help: 'Total cache hits',
  labelNames: ['tenant', 'cache'],
  registers: [register],
});

export const cacheMissTotal = new Counter({
  name: 'cache_miss_total',
  help: 'Total cache misses',
  labelNames: ['tenant', 'cache'],
  registers: [register],
});

export const cacheInvalidationTotal = new Counter({
  name: 'cache_invalidation_total',
  help: 'Total cache invalidations',
  labelNames: ['tenant', 'reason'],
  registers: [register],
});

export const cacheFailClosedTotal = new Counter({
  name: 'cache_fail_closed_total',
  help: 'Total fail-closed security events during caching and auth',
  labelNames: ['component', 'reason'],
  registers: [register],
});

export const authRecheckLatencyMs = new Histogram({
  name: 'auth_recheck_latency_ms',
  help: 'Authorization recheck latency on cache miss in milliseconds',
  labelNames: ['component'],
  buckets: [1, 5, 10, 25, 50, 100, 250, 500, 1000, 2500],
  registers: [register],
});

export const authRevocationChecksTotal = new Counter({
  name: 'auth_revocation_checks_total',
  help: 'Token revocation checks by bounded result and reason',
  labelNames: ['result', 'reason'],
  registers: [register],
});

export const authRefreshConsumeTotal = new Counter({
  name: 'auth_refresh_consume_total',
  help: 'Atomic refresh-token consume outcomes',
  labelNames: ['outcome'],
  registers: [register],
});

export const authRevocationEventsTotal = new Counter({
  name: 'auth_revocation_events_total',
  help: 'Revocation Pub/Sub lifecycle and validation events',
  labelNames: ['result'],
  registers: [register],
});

export const websocketRevocationClosesTotal = new Counter({
  name: 'websocket_revocation_closes_total',
  help: 'WebSockets closed after a revocation event',
  labelNames: ['scope'],
  registers: [register],
});

export const websocketAuthHeartbeatDuration = new Histogram({
  name: 'websocket_auth_heartbeat_duration_seconds',
  help: 'Duration of WebSocket authentication heartbeat cycles',
  buckets: [0.01, 0.05, 0.1, 0.5, 1, 2, 5, 10, 30],
  registers: [register],
});

export const websocketAuthHeartbeatTotal = new Counter({
  name: 'websocket_auth_heartbeat_total',
  help: 'WebSocket authentication heartbeat outcomes',
  labelNames: ['result'],
  registers: [register],
});

// Setup metrics endpoint
export const setupMetrics = (app: Application): void => {
  app.get('/metrics', async (req, res) => {
    try {
      res.set('Content-Type', register.contentType);
      res.end(await register.metrics());
    } catch (error) {
      res.status(500).end(String(error));
    }
  });
};

export { register };

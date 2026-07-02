import { Kafka, Producer, Consumer, EachMessagePayload, logLevel } from 'kafkajs';
import { logger } from '../utils/logger';
import { kafkaMessagesConsumed, kafkaMessagesPublished } from './metrics';

const KAFKA_BROKERS = (process.env.KAFKA_BROKERS || 'localhost:9094').split(',');
const KAFKA_CLIENT_ID = process.env.KAFKA_CLIENT_ID || 'enterprise-crm-gateway';

// Create Kafka instance
const kafka = new Kafka({
  clientId: KAFKA_CLIENT_ID,
  brokers: KAFKA_BROKERS,
  logLevel: logLevel.WARN,
  retry: {
    initialRetryTime: 100,
    retries: 8,
  },
});

// Expose kafka client for admin/health checks
export const kafkaClient = kafka;

// Create producer
// ADR-001: topics are created explicitly by kafka-init; do not auto-create.
export const kafkaProducer: Producer = kafka.producer({
  allowAutoTopicCreation: false,
  transactionTimeout: 30000,
});

// Domain event types
export interface DomainEvent {
  specversion: string;
  type: string;
  source: string;
  id: string;
  time: string;
  datacontenttype: string;
  tenantid: string;
  correlationid?: string;
  data: Record<string, any>;
}

// Publish domain event
export const publishEvent = async (
  topic: string,
  event: Omit<DomainEvent, 'specversion' | 'time' | 'datacontenttype'>,
  options?: { key?: string }
): Promise<void> => {
  const fullEvent: DomainEvent = {
    ...event,
    specversion: '1.0',
    time: new Date().toISOString(),
    datacontenttype: 'application/json',
  };
  
  try {
    if (process.env.JEST_WORKER_ID) {
      return;
    }
    await kafkaProducer.send({
      topic,
      messages: [{
        key: options?.key ?? event.tenantid,
        value: JSON.stringify(fullEvent),
        headers: {
          'ce-type': event.type,
          'ce-source': event.source,
          'ce-id': event.id,
          'ce-tenantid': event.tenantid,
        },
      }],
    });

    kafkaMessagesPublished.labels(topic).inc();
    
    logger.debug('Event published', { topic, type: event.type, id: event.id });
  } catch (error) {
    logger.error('Failed to publish event', { topic, type: event.type, error });
    if (process.env.JEST_WORKER_ID) {
      return;
    }
    throw error;
  }
};

// Create consumer
export const createConsumer = (groupId: string): Consumer => {
  // ADR-001: topics are created explicitly by kafka-init; do not auto-create.
  return kafka.consumer({
    groupId,
    sessionTimeout: 30000,
    heartbeatInterval: 3000,
    allowAutoTopicCreation: false,
  });
};

// Consumer handler type
export type MessageHandler = (payload: EachMessagePayload) => Promise<void>;

// Start consuming
export const startConsumer = async (
  consumer: Consumer,
  topics: string[],
  handler: MessageHandler,
  groupId: string = 'unknown'
): Promise<void> => {
  await consumer.connect();
  
  for (const topic of topics) {
    await consumer.subscribe({ topic, fromBeginning: false });
  }
  
  await consumer.run({
    eachMessage: async (payload) => {
      try {
        await handler(payload);
        kafkaMessagesConsumed.labels(payload.topic, groupId).inc();
      } catch (error) {
        logger.error('Message processing failed', {
          topic: payload.topic,
          partition: payload.partition,
          offset: payload.message.offset,
          error,
        });
        
        // TODO: Send to DLQ
      }
    },
  });
  
  logger.info('Consumer started', { topics });
};

// Topic names
export const TOPICS = {
  // Leads
  LEADS_CREATED: 'crm.leads.created',
  LEADS_UPDATED: 'crm.leads.updated',
  LEADS_QUALIFIED: 'crm.leads.qualified',
  LEADS_EVENTS: 'crm.leads.events',
  
  // Deals
  DEALS_CREATED: 'crm.deals.created',
  DEALS_UPDATED: 'crm.deals.updated',
  DEALS_STAGE_CHANGED: 'crm.deals.stage-changed',
  DEALS_CLOSED: 'crm.deals.closed',
  
  // Tickets
  TICKETS_CREATED: 'crm.tickets.created',
  TICKETS_UPDATED: 'crm.tickets.updated',
  TICKETS_RESOLVED: 'crm.tickets.resolved',
  TICKETS_SLA_BREACHED: 'crm.tickets.sla-breached',
  TICKETS_EVENTS: 'crm.tickets.events',
  
  // Customers
  CUSTOMERS_CREATED: 'crm.customers.created',
  CUSTOMERS_UPDATED: 'crm.customers.updated',

  // Payments
  PAYMENTS_RECORDED: 'crm.payments.recorded',
  
  // Agents
  AGENTS_TASK_ASSIGNED: 'crm.agents.task-assigned',
  AGENTS_ACTION_PROPOSED: 'crm.agents.action-proposed',
  AGENTS_ACTION_EXECUTED: 'crm.agents.action-executed',
  AGENTS_REASONING: 'crm.agents.reasoning',
  
  // Approvals
  APPROVALS_REQUIRED: 'crm.approvals.required',
  APPROVALS_DECISION: 'crm.approvals.decision',

  // Compliance
  GDPR_FORGET: 'crm.gdpr.forget',

  // Conversations
  CONVERSATION_CLOSED: 'crm.conversations.closed',

  // Knowledge Base
  KNOWLEDGE_DRAFT_CREATED: 'crm.knowledge.draft.created',
  KNOWLEDGE_PUBLISHED: 'crm.knowledge.published',
  
  // Audit & Security
  AUDIT_EVENTS: 'crm.audit.events',
  SECURITY_EVENTS: 'crm.security.events',
  AUDIT_ACCESSED: 'crm.audit.accessed',
  KILLSWITCH_ACTIVATED: 'crm.killswitch.activated',
  
  // Dead Letter Queues
  DLQ_LEADS: 'crm.dlq.leads',
  DLQ_AGENTS: 'crm.dlq.agents',
  DLQ_APPROVALS: 'crm.dlq.approvals',

  // Intelligence
  INTELLIGENCE_USER_QUERY: 'crm.intelligence.user-query',
  INTELLIGENCE_SEARCH_PERFORMED: 'crm.intelligence.search-performed',
  INTELLIGENCE_SEARCH_CLICKED: 'crm.intelligence.search-clicked',
  INTELLIGENCE_SEARCH_ABANDONED: 'crm.intelligence.search-abandoned',

  // Productivity
  PRODUCTIVITY_SIGNAL: 'crm.productivity.signal',
  PRODUCTIVITY_ACTION_SUGGESTED: 'crm.productivity.action-suggested',
  PRODUCTIVITY_ACTION_APPROVED: 'crm.productivity.action-approved',
  PRODUCTIVITY_ACTION_REJECTED: 'crm.productivity.action-rejected',

  // Journey + Predictions
  JOURNEY_UPDATED: 'crm.journey.updated',
  ANALYTICS_PREDICTION_GENERATED: 'crm.analytics.prediction-generated',

  // Automations
  AUTOMATION_POLICY_CREATED: 'crm.automation.policy-created',
  AUTOMATION_POLICY_UPDATED: 'crm.automation.policy-updated',
  AUTOMATION_SIMULATION_REQUESTED: 'crm.automation.simulation.requested',
  AUTOMATION_SIMULATION_RESULT: 'crm.automation.simulation.result',
  AUTOMATION_EXECUTED: 'crm.automation.executed',
  AUTOMATION_ACTION_REQUESTED: 'crm.automation.action.requested',

  // Voice & i18n
  INTELLIGENCE_VOICE_RECEIVED: 'crm.intelligence.voice-received',
  INTELLIGENCE_LANGUAGE_DETECTED: 'crm.intelligence.language-detected',

  // Digital Twins
  TWIN_SIMULATION_EXECUTED: 'crm.intelligence.twin-simulation-executed',
  TWIN_PROFILE_UPDATED: 'crm.intelligence.twin-profile-updated',

  // Dev Experience Agent
  DEV_INSIGHT_GENERATED: 'crm.intelligence.dev-insight-generated',
  DEV_ANOMALY_DETECTED: 'crm.intelligence.dev-anomaly-detected',
};

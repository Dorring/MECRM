import express, { Application, Request, Response, NextFunction } from 'express';
import cors from 'cors';
import helmet from 'helmet';
import compression from 'compression';
import rateLimit from 'express-rate-limit';
import { createServer } from 'http';
import { WebSocketServer } from 'ws';
import { v4 as uuidv4 } from 'uuid';

import { logger } from './utils/logger';
import { errorHandler } from './middleware/errorHandler';
import { authMiddleware } from './middleware/auth';
import { tenantMiddleware } from './middleware/tenant';
import { opaMiddleware } from './middleware/opa';
import { auditMiddleware } from './middleware/audit';
import { requestLogger } from './middleware/requestLogger';

import leadsRoutes from './routes/leads';
import dealsRoutes from './routes/deals';
import ticketsRoutes from './routes/tickets';
import customersRoutes from './routes/customers';
import predictionsRoutes from './routes/predictions';
import approvalsRoutes from './routes/approvals';
import agentsRoutes from './routes/agents';
import replayRoutes from './routes/replay';
import aggregatesRoutes from './routes/aggregates';
import governanceRoutes from './routes/governance';
import securityRoutes from './routes/security';
import intelligenceRoutes from './routes/intelligence';
import productivityRoutes from './routes/productivity';
import automationsRoutes from './routes/automations';
import auditRoutes from './routes/audit';
import knowledgeRoutes from './routes/knowledge';
import voiceRoutes from './routes/voice';
import twinsRoutes from './routes/twins';
import devxRoutes from './routes/devx';

import { setupWebSocket, setRevocationServiceForWS } from './services/websocket';
import { kafkaProducer, kafkaClient } from './services/kafka';
import { setupMetrics } from './services/metrics';
import { startApprovalsRequiredIngestor } from './consumers/approvalsRequired';
import { startAuditEventsIngestor } from './consumers/auditEvents';
import { startCacheInvalidationConsumer } from './consumers/cacheInvalidation';
import { startProductivityActionSuggestedIngestor } from './consumers/productivityActionSuggested';
import { startJourneyUpdatedIngestor } from './consumers/journeyUpdated';
import { startPredictionGeneratedIngestor } from './consumers/predictionGenerated';
import { startAutomationActivationDecisionConsumer } from './consumers/automationActivationDecision';
import { startAutomationSimulationResultIngestor } from './consumers/automationSimulationResult';
import { startAutomationExecutedIngestor } from './consumers/automationExecuted';
import { startAutomationActionRequestedConsumer } from './consumers/automationActionRequested';
import { startKnowledgeDraftCreatedIngestor } from './consumers/knowledgeDraftCreated';
import { prisma } from './services/prisma';
import { redisClient } from './services/redis';
// Importing this module resolves & validates JWT_SECRET at boot. In production
// a missing/insecure secret throws here and is caught by startServer -> exit(1).
// Side-effect import: the validation runs at module load; no binding needed.
import './config/jwt';

// ---------------------------------------------------------------------------
// Auth dependencies — constructed at module load so that middleware and routes
// are available when app is created. The underlying Redis client uses
// lazyConnect: true so no TCP connection is opened until the first command.
// ---------------------------------------------------------------------------
import { TokenRevocationService } from './services/authSession';
import { initAuthModule } from './middleware/auth';
import { createAuthRoutes } from './routes/auth';

const _revocationSubscriber = redisClient.duplicate();
const revocationService = new TokenRevocationService(redisClient, _revocationSubscriber);
initAuthModule(revocationService);
const authRoutes = createAuthRoutes(revocationService);
setRevocationServiceForWS(revocationService);

const app: Application = express();
const PORT = process.env.GATEWAY_PORT || 4000;

// Trust proxy for rate limiting behind reverse proxy
app.set('trust proxy', 1);

// Security middleware
app.use(helmet({
  crossOriginEmbedderPolicy: false,
  contentSecurityPolicy: process.env.CSP_DISABLED === '1' ? false : undefined,
}));
app.use(cors({
  origin: process.env.CORS_ORIGINS?.split(',') || ['http://localhost:3000'],
  credentials: true,
}));
app.use(compression());

// Rate limiting
const limiter = rateLimit({
  windowMs: 15 * 60 * 1000, // 15 minutes
  max: 1000, // limit each IP to 1000 requests per windowMs
  standardHeaders: true,
  legacyHeaders: false,
  message: { error: 'Too many requests, please try again later' },
});
app.use(limiter);

// Stricter auth rate limit
const authLimiter = rateLimit({
  windowMs: 15 * 60 * 1000,
  max: 30,
  standardHeaders: true,
  legacyHeaders: false,
  message: { error: 'Too many auth attempts, please try again later' },
});

// Body parsing
app.use(express.json({ limit: '10mb' }));
app.use(express.urlencoded({ extended: true }));

// Add correlation ID to all requests
app.use((req: Request, res: Response, next: NextFunction) => {
  req.headers['x-correlation-id'] = req.headers['x-correlation-id'] || uuidv4();
  res.setHeader('x-correlation-id', req.headers['x-correlation-id']);
  next();
});

// Request logging
app.use(requestLogger);

// Metrics endpoint (before auth)
setupMetrics(app);

// Health check (before auth)
app.get('/health', (req: Request, res: Response) => {
  res.json({
    status: 'healthy',
    timestamp: new Date().toISOString(),
    version: process.env.npm_package_version || '1.0.0',
  });
});

// Readiness check
app.get('/ready', async (req: Request, res: Response) => {
  try {
    if (process.env.JEST_WORKER_ID) {
      return res.json({ status: 'ready', stubbed: true });
    }
    // Check PostgreSQL
    await prisma.$queryRaw`SELECT 1`;
    // Check Redis
    await redisClient.ping();
    // Check Kafka
    const kafkaAdmin = kafkaClient.admin();
    await kafkaAdmin.connect();
    await kafkaAdmin.disconnect();

    res.json({ status: 'ready' });
  } catch (error) {
    // P0-1: never expose raw dependency errors (connection strings, hosts, SQL)
    // to unauthenticated callers. Log full details server-side only.
    logger.error('Readiness check failed', {
      reason: error instanceof Error ? error.message : 'unknown',
      path: req.path,
    });
    res.status(503).json({ status: 'not ready' });
  }
});

// Public routes
app.use('/api/v1/auth', authLimiter, authRoutes);

// Protected routes - apply middleware stack
app.use('/api/v1',
  authMiddleware,
  tenantMiddleware,
  opaMiddleware,
  auditMiddleware
);

// API routes
app.use('/api/v1/leads', leadsRoutes);
app.use('/api/v1/deals', dealsRoutes);
app.use('/api/v1/tickets', ticketsRoutes);
app.use('/api/v1/customers', customersRoutes);
app.use('/api/v1/predictions', predictionsRoutes);
app.use('/api/v1/approvals', approvalsRoutes);
app.use('/api/v1/agents', agentsRoutes);
app.use('/api/v1/replay', replayRoutes);
app.use('/api/v1/aggregates', aggregatesRoutes);
app.use('/api/v1/governance', governanceRoutes);
app.use('/api/v1/security', securityRoutes);
app.use('/api/v1/intelligence', intelligenceRoutes);
app.use('/api/v1/productivity', productivityRoutes);
app.use('/api/v1/automations', automationsRoutes);
app.use('/api/v1/audit', auditRoutes);
app.use('/api/v1/knowledge', knowledgeRoutes);

app.use('/api/intelligence',
  authMiddleware,
  tenantMiddleware,
  opaMiddleware,
  auditMiddleware,
  intelligenceRoutes
);

app.use('/api/productivity',
  authMiddleware,
  tenantMiddleware,
  opaMiddleware,
  auditMiddleware,
  productivityRoutes
);

app.use('/api/intelligence/voice',
  authMiddleware,
  tenantMiddleware,
  opaMiddleware,
  auditMiddleware,
  voiceRoutes
);

app.use('/api/intelligence/twin',
  authMiddleware,
  tenantMiddleware,
  opaMiddleware,
  auditMiddleware,
  twinsRoutes
);

app.use('/api/intelligence/devx',
  authMiddleware,
  tenantMiddleware,
  opaMiddleware,
  auditMiddleware,
  devxRoutes
);

// 404 handler
app.use((req: Request, res: Response) => {
  res.status(404).json({
    error: {
      code: 'NOT_FOUND',
      message: `Route ${req.method} ${req.path} not found`,
    },
  });
});

// Error handler
app.use(errorHandler);

// Create HTTP server
const server = createServer(app);

// Setup WebSocket
const wss = new WebSocketServer({ server, path: '/ws' });
setupWebSocket(wss);

// Graceful shutdown
const shutdown = async () => {
  logger.info('Shutting down gracefully...');
  
  // Close WebSocket connections
  wss.clients.forEach((client) => {
    client.close(1001, 'Server shutting down');
  });
  
  if ((global as any).__approvalsIngestorStop) {
    await (global as any).__approvalsIngestorStop();
  }
  if ((global as any).__auditIngestorStop) {
    await (global as any).__auditIngestorStop();
  }
  if ((global as any).__cacheInvalidationStop) {
    await (global as any).__cacheInvalidationStop();
  }
  if ((global as any).__productivityIngestorStop) {
    await (global as any).__productivityIngestorStop();
  }
  if ((global as any).__journeyIngestorStop) {
    await (global as any).__journeyIngestorStop();
  }
  if ((global as any).__predictionsIngestorStop) {
    await (global as any).__predictionsIngestorStop();
  }
  if ((global as any).__automationActivationStop) {
    await (global as any).__automationActivationStop();
  }
  if ((global as any).__automationSimulationStop) {
    await (global as any).__automationSimulationStop();
  }
  if ((global as any).__automationExecutedStop) {
    await (global as any).__automationExecutedStop();
  }
  if ((global as any).__automationActionRequestedStop) {
    await (global as any).__automationActionRequestedStop();
  }
  if ((global as any).__knowledgeDraftCreatedStop) {
    await (global as any).__knowledgeDraftCreatedStop();
  }

  // Disconnect Kafka
  await kafkaProducer.disconnect();

  // Shutdown revocation service (unsubscribe + disconnect subscriber)
  await revocationService.shutdown();

  // Close HTTP server
  server.close(() => {
    logger.info('HTTP server closed');
    process.exit(0);
  });
  
  // Force exit after 30 seconds
  setTimeout(() => {
    logger.error('Forced shutdown after timeout');
    process.exit(1);
  }, 30000);
};

process.on('SIGTERM', shutdown);
process.on('SIGINT', shutdown);

// Start server
const startServer = async () => {
  try {
    // Startup security gate: JWT_SECRET was resolved at module load via
    // src/config/jwt.ts. In production a missing/insecure secret throws there
    // and aborts the process before listen(). Log only a sanitized confirmation
    // here — never the secret value.
    if (process.env.NODE_ENV === 'production') {
      logger.info('JWT_SECRET validated (length-ok, non-default) for production');
    } else if (!process.env.JWT_SECRET) {
      logger.warn('Running with insecure development JWT_SECRET — production will refuse to start without JWT_SECRET');
    }

    // Connect to Redis subscriber and initialize revocation event handler
    try {
      await revocationService.initSubscriber((event) => {
        // Will be wired to close matching WS connections (B4)
        logger.debug('Revocation event received', { type: event.type, tenantId: event.tenantId });
      });
      logger.info('Revocation subscriber initialized');
    } catch (err) {
      logger.error('Failed to initialize revocation subscriber', {
        error: err instanceof Error ? err.message : String(err),
      });
    }

    // Connect to Kafka
    await kafkaProducer.connect();
    logger.info('Connected to Kafka');

    if (process.env.ENABLE_APPROVAL_EVENT_INGESTOR !== 'false') {
      (global as any).__approvalsIngestorStop = await startApprovalsRequiredIngestor();
    }
    if (process.env.ENABLE_AUDIT_EVENT_INGESTOR !== 'false') {
      (global as any).__auditIngestorStop = await startAuditEventsIngestor();
    }
    if (process.env.ENABLE_CACHE_INVALIDATION_CONSUMER !== 'false') {
      (global as any).__cacheInvalidationStop = await startCacheInvalidationConsumer();
    }
    if (process.env.ENABLE_PRODUCTIVITY_INGESTOR !== 'false') {
      (global as any).__productivityIngestorStop = await startProductivityActionSuggestedIngestor();
    }
    if (process.env.ENABLE_JOURNEY_INGESTOR !== 'false') {
      (global as any).__journeyIngestorStop = await startJourneyUpdatedIngestor();
    }
    if (process.env.ENABLE_PREDICTIONS_INGESTOR !== 'false') {
      (global as any).__predictionsIngestorStop = await startPredictionGeneratedIngestor();
    }
    if (process.env.ENABLE_AUTOMATION_ACTIVATION_CONSUMER !== 'false') {
      (global as any).__automationActivationStop = await startAutomationActivationDecisionConsumer();
    }
    if (process.env.ENABLE_AUTOMATION_SIMULATION_INGESTOR !== 'false') {
      (global as any).__automationSimulationStop = await startAutomationSimulationResultIngestor();
    }
    if (process.env.ENABLE_AUTOMATION_EXECUTED_INGESTOR !== 'false') {
      (global as any).__automationExecutedStop = await startAutomationExecutedIngestor();
    }
    if (process.env.ENABLE_AUTOMATION_ACTION_CONSUMER !== 'false') {
      (global as any).__automationActionRequestedStop = await startAutomationActionRequestedConsumer();
    }
    if (process.env.ENABLE_KNOWLEDGE_DRAFT_INGESTOR !== 'false') {
      (global as any).__knowledgeDraftCreatedStop = await startKnowledgeDraftCreatedIngestor();
    }
    
    server.listen(PORT, () => {
      logger.info(`API Gateway running on port ${PORT}`);
      logger.info(`Environment: ${process.env.NODE_ENV || 'development'}`);
    });
  } catch (error) {
    // Log only the error message, never the full object/stack — startup errors
    // may embed connection strings (DATABASE_URL/REDIS_URL) or broker topology.
    const reason = error instanceof Error ? error.message : String(error);
    logger.error('Failed to start server', { reason });
    process.exit(1);
  }
};

// Top-level guard: a production misconfiguration (e.g. missing JWT_SECRET) is
// raised synchronously at module-load via src/config/jwt.ts. Surface it as a
// sanitized fatal log and a non-zero exit instead of an uncaught stack trace.
process.on('uncaughtException', (error: Error) => {
  // Do not log the full error/stack — it may contain secrets or topology.
  logger.error('Uncaught exception during startup', { reason: error.message });
  process.exit(1);
});

if (!process.env.JEST_WORKER_ID) {
  startServer();
}

export default app;

import { WebSocketServer, WebSocket } from 'ws';
import { IncomingMessage } from 'http';
import jwt, { VerifyOptions } from 'jsonwebtoken';
import { logger } from '../utils/logger';
import { JWT_SECRET } from '../config/jwt';
import type { TokenRevocationService, DecodedToken } from './authSession';
import { validateDecodedToken } from './authSession';
import {
  websocketAuthHeartbeatDuration,
  websocketAuthHeartbeatTotal,
  websocketRevocationClosesTotal,
} from './metrics';

// ---------------------------------------------------------------------------
// Constants
// ---------------------------------------------------------------------------

const JWT_VERIFY_OPTIONS: VerifyOptions = {
  algorithms: ['HS256'],
};

const DEFAULT_HEARTBEAT_INTERVAL_MS = 30000;

// ---------------------------------------------------------------------------
// Types
// ---------------------------------------------------------------------------

export interface WebSocketSetupOptions {
  /** Override the heartbeat interval (default 30000ms). For testing only. */
  heartbeatIntervalMs?: number;
}

interface AuthenticatedWebSocket extends WebSocket {
  userId: string;
  tenantId: string;
  jti: string;
  sid: string;
  uv: number;
  sexp: number;
  exp: number;
  isAlive?: boolean;
}

interface WebSocketMessage {
  type: string;
  payload: any;
}

// ---------------------------------------------------------------------------
// Indexes (module-level, per-process)
// ---------------------------------------------------------------------------

const connections = new Map<string, Map<string, Set<AuthenticatedWebSocket>>>();
const jtiIndex = new Map<string, AuthenticatedWebSocket>();
const sidIndex = new Map<string, Set<AuthenticatedWebSocket>>();
const subscriptions = new WeakMap<AuthenticatedWebSocket, Set<string>>();
const HEARTBEAT_CONCURRENCY = 25;
export { HEARTBEAT_CONCURRENCY };

function scopedIndexKey(tenantId: string, id: string): string {
  return `${tenantId}:${id}`;
}

// ---------------------------------------------------------------------------
// Setup — revocation service is required
// ---------------------------------------------------------------------------

export function setupWebSocket(
  wss: WebSocketServer,
  revocationService: TokenRevocationService,
  options?: WebSocketSetupOptions,
): void {
  const heartbeatIntervalMs = options?.heartbeatIntervalMs ?? DEFAULT_HEARTBEAT_INTERVAL_MS;

  // Heartbeat — re-checks revocation and token/session expiry
  let heartbeatRunning = false;
  const heartbeatInterval = setInterval(async () => {
    if (heartbeatRunning) {
      websocketAuthHeartbeatTotal.inc({ result: 'overlap_prevented' });
      logger.warn('Heartbeat cycle skipped — previous run still in progress');
      return;
    }
    heartbeatRunning = true;
    const heartbeatEnd = websocketAuthHeartbeatDuration.startTimer();

    try {
      const checks: Array<() => Promise<void>> = [];
      wss.clients.forEach((client: WebSocket) => {
        const ws = client as unknown as AuthenticatedWebSocket;
        if (ws.isAlive === false) {
          ws.terminate();
          return;
        }
        ws.isAlive = false;
        ws.ping();

        // Check token/session expiry
        const nowSec = Math.floor(Date.now() / 1000);
        if (ws.exp <= nowSec || ws.sexp <= nowSec) {
          ws.close(4401, 'Session expired');
          return;
        }

        // Re-check revocation
        checks.push(() =>
          revocationService
            .checkRevoked({
              jti: ws.jti,
              sid: ws.sid,
              sub: ws.userId,
              tenantId: ws.tenantId,
              type: 'access',
              uv: ws.uv,
              sexp: ws.sexp,
              iat: 0,
              exp: ws.exp,
            })
            .then((result) => {
              if (result.revoked) {
                ws.close(4401, 'Session terminated');
              }
            })
            .catch(() => {
              ws.close(1013, 'Service unavailable');
            }),
        );
      });

      for (let i = 0; i < checks.length; i += HEARTBEAT_CONCURRENCY) {
        await Promise.all(
          checks
            .slice(i, i + HEARTBEAT_CONCURRENCY)
            .map((check) => check()),
        );
      }
      websocketAuthHeartbeatTotal.inc({ result: 'completed' });
    } catch (err) {
      websocketAuthHeartbeatTotal.inc({ result: 'failed' });
      logger.error('WebSocket auth heartbeat failed', {
        error: err instanceof Error ? err.message : String(err),
      });
    } finally {
      heartbeatEnd();
      heartbeatRunning = false;
    }
  }, heartbeatIntervalMs);

  wss.on('close', () => {
    clearInterval(heartbeatInterval);
  });

  wss.on('connection', (rawWs: WebSocket, request: IncomingMessage) => {
    const token = extractToken(request);
    if (!token) {
      rawWs.close(4001, 'Authentication required');
      return;
    }

    // Verify JWT and check revocation synchronously in the handler
    authenticateAndRegister(rawWs, token, revocationService);
  });
}

// ---------------------------------------------------------------------------
// Authentication and registration
// ---------------------------------------------------------------------------

function authenticateAndRegister(
  rawWs: WebSocket,
  token: string,
  revocationService: TokenRevocationService,
): void {
  let decoded: Record<string, unknown>;
  try {
    decoded = jwt.verify(token, JWT_SECRET, JWT_VERIFY_OPTIONS) as Record<string, unknown>;
  } catch {
    rawWs.close(4401, 'Invalid token');
    return;
  }

  // Validate claim schema
  const validation = validateDecodedToken(decoded);
  if (!validation.valid) {
    rawWs.close(4401, 'Invalid token');
    return;
  }

  // Reject refresh tokens
  if (decoded.type === 'refresh') {
    rawWs.close(4401, 'Invalid token type');
    return;
  }

  const tenantId = decoded.tenantId as string;
  const sub = decoded.sub as string;
  const jti = decoded.jti as string;
  const sid = decoded.sid as string;
  const uv = decoded.uv as number;
  const sexp = decoded.sexp as number;
  const exp = decoded.exp as number;

  // Check session has not already expired
  const nowSec = Math.floor(Date.now() / 1000);
  if (sexp <= nowSec) {
    rawWs.close(4401, 'Session expired');
    return;
  }

  // Check revocation — if Redis is down, fail closed
  const tokenInfo: DecodedToken = {
    jti, sid, sub, tenantId, type: 'access' as const, uv, sexp, iat: decoded.iat as number, exp,
  };

  revocationService
    .checkRevoked(tokenInfo)
    .then((result) => {
      if (result.revoked) {
        rawWs.close(4401, 'Token revoked');
        return;
      }
      if (rawWs.readyState !== WebSocket.OPEN) return;

      // Auth succeeded — cast to AuthenticatedWebSocket and store metadata
      const ws = rawWs as unknown as AuthenticatedWebSocket;
      ws.userId = sub;
      ws.tenantId = tenantId;
      ws.jti = jti;
      ws.sid = sid;
      ws.uv = uv;
      ws.sexp = sexp;
      ws.exp = exp;
      ws.isAlive = true;

      addConnection(tenantId, sub, ws);
      jtiIndex.set(scopedIndexKey(tenantId, jti), ws);
      addToSidIndex(tenantId, sid, ws);

      logger.info('WebSocket connected', { userId: sub, tenantId });

      ws.send(
        JSON.stringify({ type: 'connected', payload: { message: 'Connected to CRM WebSocket' } }),
      );

      setupEventHandlers(ws, revocationService);
    })
    .catch(() => {
      rawWs.close(1013, 'Service unavailable');
    });
}

// ---------------------------------------------------------------------------
// Event handlers
// ---------------------------------------------------------------------------

function setupEventHandlers(
  ws: AuthenticatedWebSocket,
  revocationService: TokenRevocationService,
): void {
  ws.on('pong', () => {
    ws.isAlive = true;
  });

  ws.on('message', (data) => {
    try {
      const message: WebSocketMessage = JSON.parse(data.toString());
      handleMessage(ws, message, revocationService);
    } catch (error) {
      logger.error('Invalid WebSocket message', { error });
    }
  });

  ws.on('close', () => {
    cleanupSocket(ws);
    logger.info('WebSocket disconnected', { userId: ws.userId, tenantId: ws.tenantId });
  });

  ws.on('error', (error) => {
    logger.error('WebSocket error', { error, userId: ws.userId });
    cleanupSocket(ws);
  });
}

// ---------------------------------------------------------------------------
// Cleanup
// ---------------------------------------------------------------------------

function cleanupSocket(ws: AuthenticatedWebSocket): void {
  removeConnection(ws.tenantId, ws.userId, ws);
  jtiIndex.delete(scopedIndexKey(ws.tenantId, ws.jti));
  removeFromSidIndex(ws.tenantId, ws.sid, ws);
}

// ---------------------------------------------------------------------------
// Index helpers
// ---------------------------------------------------------------------------

function addToSidIndex(
  tenantId: string,
  sid: string,
  ws: AuthenticatedWebSocket,
): void {
  const key = scopedIndexKey(tenantId, sid);
  let set = sidIndex.get(key);
  if (!set) {
    set = new Set();
    sidIndex.set(key, set);
  }
  set.add(ws);
}

function removeFromSidIndex(
  tenantId: string,
  sid: string,
  ws: AuthenticatedWebSocket,
): void {
  const key = scopedIndexKey(tenantId, sid);
  const set = sidIndex.get(key);
  if (!set) return;
  set.delete(ws);
  if (set.size === 0) sidIndex.delete(key);
}

function extractToken(request: IncomingMessage): string | null {
  const url = new URL(request.url || '', `http://${request.headers.host}`);
  return url.searchParams.get('token');
}

// ---------------------------------------------------------------------------
// Connection map
// ---------------------------------------------------------------------------

function addConnection(tenantId: string, userId: string, ws: AuthenticatedWebSocket): void {
  if (!connections.has(tenantId)) {
    connections.set(tenantId, new Map());
  }
  const tenantConnections = connections.get(tenantId)!;
  if (!tenantConnections.has(userId)) {
    tenantConnections.set(userId, new Set());
  }
  tenantConnections.get(userId)!.add(ws);
}

function removeConnection(tenantId: string, userId: string, ws: AuthenticatedWebSocket): void {
  const tenantConnections = connections.get(tenantId);
  if (!tenantConnections) return;
  const userConnections = tenantConnections.get(userId);
  if (!userConnections) return;
  userConnections.delete(ws);
  if (userConnections.size === 0) tenantConnections.delete(userId);
  if (tenantConnections.size === 0) connections.delete(tenantId);
}

// ---------------------------------------------------------------------------
// Message handling
// ---------------------------------------------------------------------------

function handleMessage(
  ws: AuthenticatedWebSocket,
  message: WebSocketMessage,
  revocationService: TokenRevocationService,
): void {
  switch (message.type) {
    case 'ping':
      ws.send(JSON.stringify({ type: 'pong', payload: {} }));
      break;
    case 'subscribe':
      handleSubscribe(ws, message.payload?.topic, revocationService);
      break;
    default:
      logger.warn('Unknown message type', { type: message.type });
  }
}

// ---------------------------------------------------------------------------
// Subscribe (with revocation re-check)
// ---------------------------------------------------------------------------

function handleSubscribe(
  ws: AuthenticatedWebSocket,
  topic: unknown,
  revocationService: TokenRevocationService,
): void {
  if (typeof topic !== 'string' || topic.length === 0) {
    ws.send(JSON.stringify({ type: 'error', payload: { code: 'INVALID_TOPIC' } }));
    return;
  }

  const expectedPrefix = `tenant:${ws.tenantId}:`;
  if (!topic.startsWith(expectedPrefix)) {
    ws.close(4403, 'Forbidden');
    return;
  }

  // Re-check revocation before granting
  const tokenInfo: DecodedToken = {
    jti: ws.jti,
    sid: ws.sid,
    sub: ws.userId,
    tenantId: ws.tenantId,
    type: 'access',
    uv: ws.uv,
    sexp: ws.sexp,
    iat: 0,
    exp: ws.exp,
  };

  revocationService
    .checkRevoked(tokenInfo)
    .then((result) => {
      if (result.revoked) {
        ws.close(4401, 'Session revoked');
        return;
      }
      grantSubscription(ws, topic);
    })
    .catch(() => {
      ws.close(1013, 'Service unavailable');
    });
}

function grantSubscription(ws: AuthenticatedWebSocket, topic: string): void {
  let set = subscriptions.get(ws);
  if (!set) {
    set = new Set<string>();
    subscriptions.set(ws, set);
  }
  set.add(topic);
  logger.debug('Subscription granted', { userId: ws.userId, topic });
  ws.send(JSON.stringify({ type: 'subscribed', payload: { topic } }));
}

// ---------------------------------------------------------------------------
// Cross-instance revocation handler
// ---------------------------------------------------------------------------

export function closeConnectionsByEvent(event: {
  type: 'jti' | 'sid' | 'user';
  tenantId: string;
  id: string;
  userId?: string;
}): void {
  switch (event.type) {
    case 'jti': {
      const ws = jtiIndex.get(scopedIndexKey(event.tenantId, event.id));
      if (ws && ws.tenantId === event.tenantId) {
        websocketRevocationClosesTotal.inc({ scope: 'jti' });
        ws.close(4401, 'Session terminated');
        cleanupSocket(ws);
      }
      break;
    }
    case 'sid': {
      const key = scopedIndexKey(event.tenantId, event.id);
      const set = sidIndex.get(key);
      if (set) {
        for (const ws of set) {
          if (ws.tenantId === event.tenantId) {
            websocketRevocationClosesTotal.inc({ scope: 'sid' });
            ws.close(4401, 'Session terminated');
            cleanupSocket(ws);
          }
        }
        sidIndex.delete(key);
      }
      break;
    }
    case 'user': {
      // Must have userId — rejecting events without it prevents tenant-wide closure
      if (!event.userId) {
        logger.error('User revocation event missing userId, ignoring', { tenantId: event.tenantId });
        return;
      }
      const tenantConnections = connections.get(event.tenantId);
      if (!tenantConnections) break;
      for (const [userId, wsSet] of tenantConnections) {
        if (userId === event.userId) {
          for (const ws of wsSet) {
            websocketRevocationClosesTotal.inc({ scope: 'user' });
            ws.close(4401, 'Session terminated');
            cleanupSocket(ws);
          }
          tenantConnections.delete(userId);
        }
      }
      if (tenantConnections.size === 0) {
        connections.delete(event.tenantId);
      }
      break;
    }
  }
}

// ---------------------------------------------------------------------------
// Send helpers
// ---------------------------------------------------------------------------

export function sendToUser(tenantId: string, userId: string, message: WebSocketMessage): void {
  const tenantConnections = connections.get(tenantId);
  if (!tenantConnections) return;
  const userConnections = tenantConnections.get(userId);
  if (!userConnections) return;
  const messageStr = JSON.stringify(message);
  userConnections.forEach((ws) => {
    if (ws.readyState === WebSocket.OPEN) ws.send(messageStr);
  });
}

export function sendToTenant(tenantId: string, message: WebSocketMessage): void {
  const tenantConnections = connections.get(tenantId);
  if (!tenantConnections) return;
  const messageStr = JSON.stringify(message);
  tenantConnections.forEach((userConnections) => {
    userConnections.forEach((ws) => {
      if (ws.readyState === WebSocket.OPEN) ws.send(messageStr);
    });
  });
}

export function broadcast(message: WebSocketMessage): void {
  const messageStr = JSON.stringify(message);
  connections.forEach((tenantConnections) => {
    tenantConnections.forEach((userConnections) => {
      userConnections.forEach((ws) => {
        if (ws.readyState === WebSocket.OPEN) ws.send(messageStr);
      });
    });
  });
}

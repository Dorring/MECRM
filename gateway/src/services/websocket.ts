import { WebSocketServer, WebSocket } from 'ws';
import { IncomingMessage } from 'http';
import jwt, { VerifyOptions } from 'jsonwebtoken';
import { logger } from '../utils/logger';
import { JWT_SECRET } from '../config/jwt';
import type { TokenRevocationService, DecodedToken } from './authSession';
import { validateDecodedToken } from './authSession';

// ---------------------------------------------------------------------------
// Constants
// ---------------------------------------------------------------------------

const JWT_VERIFY_OPTIONS: VerifyOptions = {
  algorithms: ['HS256'],
};

const HEARTBEAT_INTERVAL_MS = 30000;

// ---------------------------------------------------------------------------
// Types
// ---------------------------------------------------------------------------

interface AuthenticatedWebSocket extends WebSocket {
  userId?: string;
  tenantId?: string;
  jti?: string;
  sid?: string;
  uv?: number;
  sexp?: number;
  isAlive?: boolean;
}

interface WebSocketMessage {
  type: string;
  payload: any;
}

// ---------------------------------------------------------------------------
// Indexes
// ---------------------------------------------------------------------------

// tenant → user → Set<WebSocket>
const connections = new Map<string, Map<string, Set<AuthenticatedWebSocket>>>();

// jti → WebSocket (for direct jti revocation lookup)
const jtiIndex = new Map<string, AuthenticatedWebSocket>();

// sid → Set<WebSocket> (for session-level revocation)
const sidIndex = new Map<string, Set<AuthenticatedWebSocket>>();

const subscriptions = new WeakMap<AuthenticatedWebSocket, Set<string>>();

// ---------------------------------------------------------------------------
// Revocation service
// ---------------------------------------------------------------------------

let _revocationService: TokenRevocationService | null = null;

export function setRevocationServiceForWS(svc: TokenRevocationService): void {
  _revocationService = svc;
}

// ---------------------------------------------------------------------------
// Setup
// ---------------------------------------------------------------------------

export const setupWebSocket = (wss: WebSocketServer): void => {
  // Heartbeat interval — also acts as periodic revocation re-validation
  let heartbeatRunning = false;
  const heartbeatInterval = setInterval(async () => {
    // Prevent overlapping heartbeat runs
    if (heartbeatRunning) {
      logger.warn('Heartbeat cycle skipped — previous run still in progress');
      return;
    }
    heartbeatRunning = true;

    try {
      const promises: Promise<void>[] = [];
      wss.clients.forEach((ws: AuthenticatedWebSocket) => {
        // Terminate if client did not respond to previous ping
        if (ws.isAlive === false) {
          ws.terminate();
          return;
        }
        ws.isAlive = false;
        ws.ping();

        // Re-check revocation on active connections
        if (ws.jti && ws.sid && ws.tenantId && ws.userId && _revocationService) {
          promises.push(
            _revocationService
              .checkRevoked({
                jti: ws.jti,
                sid: ws.sid,
                sub: ws.userId,
                tenantId: ws.tenantId,
                type: 'access',
                uv: ws.uv ?? 0,
                sexp: ws.sexp ?? 0,
                iat: 0,
                exp: 0,
              })
              .then((result) => {
                if (result.revoked) {
                  ws.close(4401, 'Session terminated');
                }
              })
              .catch(() => {
                // Redis unavailable during heartbeat — close with 1013
                ws.close(1013, 'Service unavailable');
              }),
          );
        }
      });

      await Promise.all(promises);
    } finally {
      heartbeatRunning = false;
    }
  }, HEARTBEAT_INTERVAL_MS);

  wss.on('close', () => {
    clearInterval(heartbeatInterval);
  });

  wss.on('connection', (ws: AuthenticatedWebSocket, request: IncomingMessage) => {
    // 1. Extract token
    const token = extractToken(request);
    if (!token) {
      ws.close(4001, 'Authentication required');
      return;
    }

    // 2. Authenticate connection
    try {
      const decoded = jwt.verify(
        token,
        JWT_SECRET,
        JWT_VERIFY_OPTIONS,
      ) as Record<string, unknown>;

      // 3. Validate strict claim schema
      const validation = validateDecodedToken(decoded);
      if (!validation.valid) {
        logger.warn('WS connection rejected — missing or invalid claims');
        ws.close(4401, 'Invalid token');
        return;
      }

      // 4. Reject refresh tokens used as access tokens
      if (decoded.type === 'refresh') {
        ws.close(4401, 'Invalid token type');
        return;
      }

      const tenantId = decoded.tenantId as string;
      const sub = decoded.sub as string;
      const jti = decoded.jti as string;
      const sid = decoded.sid as string;
      const uv = decoded.uv as number;
      const sexp = decoded.sexp as number;

      // 5. Check revocation state
      if (_revocationService) {
        const tokenInfo: DecodedToken = {
          jti,
          sid,
          sub,
          tenantId,
          type: 'access',
          uv,
          sexp,
          iat: decoded.iat as number,
          exp: decoded.exp as number,
        };

        _revocationService
          .checkRevoked(tokenInfo)
          .then((result) => {
            if (result.revoked) {
              ws.close(4401, 'Token revoked');
              return;
            }

            // 6. Auth succeeded — store metadata
            ws.userId = sub;
            ws.tenantId = tenantId;
            ws.jti = jti;
            ws.sid = sid;
            ws.uv = uv;
            ws.sexp = sexp;
            ws.isAlive = true;

            // 7. Register in indexes
            addConnection(tenantId, sub, ws);
            jtiIndex.set(jti, ws);
            addToSidIndex(sid, ws);

            logger.info('WebSocket connected', { userId: sub, tenantId });

            // Send welcome message
            ws.send(
              JSON.stringify({
                type: 'connected',
                payload: { message: 'Connected to CRM WebSocket' },
              }),
            );

            // ---- Event handlers ----
            setupEventHandlers(ws);
          })
          .catch(() => {
            // Redis unavailable — fail closed with 1013
            ws.close(1013, 'Service unavailable');
          });
      } else {
        // No revocation service configured — still allow connection with basic auth
        // (This occurs in tests that don't inject the service)
        ws.userId = sub;
        ws.tenantId = tenantId;
        ws.jti = jti;
        ws.sid = sid;
        ws.uv = uv;
        ws.sexp = sexp;
        ws.isAlive = true;

        addConnection(tenantId, sub, ws);
        jtiIndex.set(jti, ws);
        addToSidIndex(sid, ws);

        logger.info('WebSocket connected (no revocation service)', {
          userId: sub,
          tenantId,
        });

        ws.send(
          JSON.stringify({
            type: 'connected',
            payload: { message: 'Connected to CRM WebSocket' },
          }),
        );

        setupEventHandlers(ws);
      }
    } catch (error) {
      ws.close(4401, 'Invalid token');
    }
  });
};

// ---------------------------------------------------------------------------
// Event handlers (extracted to avoid duplication)
// ---------------------------------------------------------------------------

function setupEventHandlers(ws: AuthenticatedWebSocket): void {
  // Pong
  ws.on('pong', () => {
    ws.isAlive = true;
  });

  // Messages
  ws.on('message', (data) => {
    try {
      const message: WebSocketMessage = JSON.parse(data.toString());
      handleMessage(ws, message);
    } catch (error) {
      logger.error('Invalid WebSocket message', { error });
    }
  });

  // Close
  ws.on('close', () => {
    cleanupSocket(ws);
    logger.info('WebSocket disconnected', {
      userId: ws.userId,
      tenantId: ws.tenantId,
    });
  });

  // Error
  ws.on('error', (error) => {
    logger.error('WebSocket error', { error, userId: ws.userId });
    cleanupSocket(ws);
  });
}

// ---------------------------------------------------------------------------
// Cleanup
// ---------------------------------------------------------------------------

function cleanupSocket(ws: AuthenticatedWebSocket): void {
  if (ws.tenantId && ws.userId) {
    removeConnection(ws.tenantId, ws.userId, ws);
  }
  if (ws.jti) {
    jtiIndex.delete(ws.jti);
  }
  if (ws.sid) {
    removeFromSidIndex(ws.sid, ws);
  }
}

// ---------------------------------------------------------------------------
// Index helpers
// ---------------------------------------------------------------------------

function addToSidIndex(sid: string, ws: AuthenticatedWebSocket): void {
  let set = sidIndex.get(sid);
  if (!set) {
    set = new Set();
    sidIndex.set(sid, set);
  }
  set.add(ws);
}

function removeFromSidIndex(sid: string, ws: AuthenticatedWebSocket): void {
  const set = sidIndex.get(sid);
  if (!set) return;
  set.delete(ws);
  if (set.size === 0) sidIndex.delete(sid);
}

// ---------------------------------------------------------------------------
// Token extraction
// ---------------------------------------------------------------------------

function extractToken(request: IncomingMessage): string | null {
  const url = new URL(request.url || '', `http://${request.headers.host}`);
  return url.searchParams.get('token');
}

// ---------------------------------------------------------------------------
// Tenant/user connection map
// ---------------------------------------------------------------------------

function addConnection(
  tenantId: string,
  userId: string,
  ws: AuthenticatedWebSocket,
): void {
  if (!connections.has(tenantId)) {
    connections.set(tenantId, new Map());
  }
  const tenantConnections = connections.get(tenantId)!;
  if (!tenantConnections.has(userId)) {
    tenantConnections.set(userId, new Set());
  }
  tenantConnections.get(userId)!.add(ws);
}

function removeConnection(
  tenantId: string,
  userId: string,
  ws: AuthenticatedWebSocket,
): void {
  const tenantConnections = connections.get(tenantId);
  if (!tenantConnections) return;
  const userConnections = tenantConnections.get(userId);
  if (!userConnections) return;
  userConnections.delete(ws);
  if (userConnections.size === 0) {
    tenantConnections.delete(userId);
  }
  if (tenantConnections.size === 0) {
    connections.delete(tenantId);
  }
}

// ---------------------------------------------------------------------------
// Message handling
// ---------------------------------------------------------------------------

function handleMessage(
  ws: AuthenticatedWebSocket,
  message: WebSocketMessage,
): void {
  switch (message.type) {
    case 'ping':
      ws.send(JSON.stringify({ type: 'pong', payload: {} }));
      break;

    case 'subscribe':
      handleSubscribe(ws, message.payload?.topic);
      break;

    default:
      logger.warn('Unknown message type', { type: message.type });
  }
}

// ---------------------------------------------------------------------------
// Subscribe
// ---------------------------------------------------------------------------

function handleSubscribe(
  ws: AuthenticatedWebSocket,
  topic: unknown,
): void {
  if (!ws.tenantId || !ws.userId) {
    ws.close(4401, 'Authentication required');
    return;
  }

  if (typeof topic !== 'string' || topic.length === 0) {
    ws.send(
      JSON.stringify({ type: 'error', payload: { code: 'INVALID_TOPIC' } }),
    );
    return;
  }

  // Tenant isolation check
  const expectedPrefix = `tenant:${ws.tenantId}:`;
  if (!topic.startsWith(expectedPrefix)) {
    ws.close(4403, 'Forbidden');
    return;
  }

  // Re-check revocation before granting subscription
  if (_revocationService && ws.jti && ws.sid) {
    const tokenInfo: DecodedToken = {
      jti: ws.jti,
      sid: ws.sid,
      sub: ws.userId,
      tenantId: ws.tenantId,
      type: 'access',
      uv: ws.uv ?? 0,
      sexp: ws.sexp ?? 0,
      iat: 0,
      exp: 0,
    };

    _revocationService
      .checkRevoked(tokenInfo)
      .then((result) => {
        if (result.revoked) {
          ws.close(4401, 'Session revoked');
          return;
        }

        grantSubscription(ws, topic);
      })
      .catch(() => {
        // Redis unavailable — deny subscription with 1013
        ws.close(1013, 'Service unavailable');
      });
  } else {
    // No revocation service — grant based on tenant check alone (test mode)
    grantSubscription(ws, topic);
  }
}

function grantSubscription(
  ws: AuthenticatedWebSocket,
  topic: string,
): void {
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

/**
 * Close WebSocket connections matching a revocation event.
 * Called by the revocation service's Pub/Sub subscriber.
 */
export function closeConnectionsByEvent(event: {
  type: 'jti' | 'sid' | 'user';
  tenantId: string;
  id: string;
  userId?: string;
}): void {
  switch (event.type) {
    case 'jti': {
      const ws = jtiIndex.get(event.id);
      if (ws && ws.tenantId === event.tenantId) {
        ws.close(4401, 'Session terminated');
        cleanupSocket(ws);
      }
      break;
    }
    case 'sid': {
      const set = sidIndex.get(event.id);
      if (set) {
        for (const ws of set) {
          if (ws.tenantId === event.tenantId) {
            ws.close(4401, 'Session terminated');
            cleanupSocket(ws);
          }
        }
        sidIndex.delete(event.id);
      }
      break;
    }
    case 'user': {
      const tenantConnections = connections.get(event.tenantId);
      if (!tenantConnections) break;
      for (const [userId, wsSet] of tenantConnections) {
        if (userId === event.userId || !event.userId) {
          for (const ws of wsSet) {
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

// Send message to specific user
export const sendToUser = (
  tenantId: string,
  userId: string,
  message: WebSocketMessage,
): void => {
  const tenantConnections = connections.get(tenantId);
  if (!tenantConnections) return;
  const userConnections = tenantConnections.get(userId);
  if (!userConnections) return;
  const messageStr = JSON.stringify(message);
  userConnections.forEach((ws) => {
    if (ws.readyState === WebSocket.OPEN) {
      ws.send(messageStr);
    }
  });
};

// Send message to all users in tenant
export const sendToTenant = (
  tenantId: string,
  message: WebSocketMessage,
): void => {
  const tenantConnections = connections.get(tenantId);
  if (!tenantConnections) return;
  const messageStr = JSON.stringify(message);
  tenantConnections.forEach((userConnections) => {
    userConnections.forEach((ws) => {
      if (ws.readyState === WebSocket.OPEN) {
        ws.send(messageStr);
      }
    });
  });
};

// Broadcast to all connections
export const broadcast = (message: WebSocketMessage): void => {
  const messageStr = JSON.stringify(message);
  connections.forEach((tenantConnections) => {
    tenantConnections.forEach((userConnections) => {
      userConnections.forEach((ws) => {
        if (ws.readyState === WebSocket.OPEN) {
          ws.send(messageStr);
        }
      });
    });
  });
};

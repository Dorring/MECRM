import { WebSocketServer, WebSocket } from 'ws';
import { IncomingMessage } from 'http';
import jwt from 'jsonwebtoken';
import { logger } from '../utils/logger';
import { TokenPayload } from '../middleware/auth';
import { JWT_SECRET } from '../config/jwt';

interface AuthenticatedWebSocket extends WebSocket {
  userId?: string;
  tenantId?: string;
  isAlive?: boolean;
}

interface WebSocketMessage {
  type: string;
  payload: any;
}

// Store connections by tenant and user
const connections = new Map<string, Map<string, Set<AuthenticatedWebSocket>>>();
const subscriptions = new WeakMap<AuthenticatedWebSocket, Set<string>>();

export const setupWebSocket = (wss: WebSocketServer): void => {
  // Heartbeat interval
  const heartbeatInterval = setInterval(() => {
    wss.clients.forEach((ws: AuthenticatedWebSocket) => {
      if (ws.isAlive === false) {
        return ws.terminate();
      }
      ws.isAlive = false;
      ws.ping();
    });
  }, 30000);
  
  wss.on('close', () => {
    clearInterval(heartbeatInterval);
  });
  
  wss.on('connection', (ws: AuthenticatedWebSocket, request: IncomingMessage) => {
    // Authenticate connection
    const token = extractToken(request);
    if (!token) {
      ws.close(4001, 'Authentication required');
      return;
    }
    
    try {
      const decoded = jwt.verify(
        token,
        JWT_SECRET
      ) as TokenPayload;
      const tenantId = decoded.tenantId || decoded.tenant_id;
      if (!tenantId) {
        ws.close(4001, 'Invalid token');
        return;
      }
      
      ws.userId = decoded.sub;
      ws.tenantId = tenantId;
      ws.isAlive = true;
      
      // Add to connections map
      addConnection(tenantId, decoded.sub, ws);
      
      logger.info('WebSocket connected', {
        userId: decoded.sub,
        tenantId,
      });
      
      // Send welcome message
      ws.send(JSON.stringify({
        type: 'connected',
        payload: { message: 'Connected to CRM WebSocket' },
      }));
      
    } catch (error) {
      ws.close(4001, 'Invalid token');
      return;
    }
    
    // Handle pong
    ws.on('pong', () => {
      ws.isAlive = true;
    });
    
    // Handle messages
    ws.on('message', (data) => {
      try {
        const message: WebSocketMessage = JSON.parse(data.toString());
        handleMessage(ws, message);
      } catch (error) {
        logger.error('Invalid WebSocket message', { error });
      }
    });
    
    // Handle close
    ws.on('close', () => {
      if (ws.tenantId && ws.userId) {
        removeConnection(ws.tenantId, ws.userId, ws);
        logger.info('WebSocket disconnected', {
          userId: ws.userId,
          tenantId: ws.tenantId,
        });
      }
    });
    
    // Handle errors
    ws.on('error', (error) => {
      logger.error('WebSocket error', { error, userId: ws.userId });
    });
  });
};

function extractToken(request: IncomingMessage): string | null {
  const url = new URL(request.url || '', `http://${request.headers.host}`);
  return url.searchParams.get('token');
}

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
  
  if (userConnections.size === 0) {
    tenantConnections.delete(userId);
  }
  
  if (tenantConnections.size === 0) {
    connections.delete(tenantId);
  }
}

function handleMessage(ws: AuthenticatedWebSocket, message: WebSocketMessage): void {
  switch (message.type) {
    case 'ping':
      ws.send(JSON.stringify({ type: 'pong', payload: {} }));
      break;
      
    case 'subscribe':
      // Handle topic subscriptions
      handleSubscribe(ws, message.payload?.topic);
      break;
      
    default:
      logger.warn('Unknown message type', { type: message.type });
  }
}

function handleSubscribe(ws: AuthenticatedWebSocket, topic: unknown): void {
  if (!ws.tenantId || !ws.userId) {
    ws.close(4001, 'Authentication required');
    return;
  }

  if (typeof topic !== 'string' || topic.length === 0) {
    ws.send(JSON.stringify({ type: 'error', payload: { code: 'INVALID_TOPIC' } }));
    return;
  }

  const expectedPrefix = `tenant:${ws.tenantId}:`;
  if (!topic.startsWith(expectedPrefix)) {
    ws.send(JSON.stringify({ type: 'error', payload: { code: 'CROSS_TENANT_SUBSCRIBE_DENY' } }));
    return;
  }

  let set = subscriptions.get(ws);
  if (!set) {
    set = new Set<string>();
    subscriptions.set(ws, set);
  }
  set.add(topic);

  logger.debug('Subscription granted', { userId: ws.userId, topic });
  ws.send(JSON.stringify({ type: 'subscribed', payload: { topic } }));
}

// Send message to specific user
export const sendToUser = (tenantId: string, userId: string, message: WebSocketMessage): void => {
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
export const sendToTenant = (tenantId: string, message: WebSocketMessage): void => {
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

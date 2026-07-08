'use client';

import { useEffect, useRef, useState, useCallback, createContext, useContext, ReactNode } from 'react';
import { getAccessToken, tryCookieRefresh, CSRF_HEADER, getCsrfToken } from '@/lib/api';

// ---------------------------------------------------------------------------
// WebSocket URL: same-origin ws(s)://<host>/ws
// ---------------------------------------------------------------------------
// No NEXT_PUBLIC_WS_URL — the WS URL is always derived from window.location.
// In production, an infrastructure proxy (nginx/Ingress/compose sidecar)
// forwards /ws to the Gateway. In local dev, /api/config may supply a custom
// wsUrl for direct Gateway connections.

function deriveWsUrl(): string {
  if (typeof window === 'undefined') return 'ws://localhost:4000';
  const protocol = window.location.protocol === 'https:' ? 'wss:' : 'ws:';
  return `${protocol}//${window.location.host}/ws`;
}

const WS_URL = deriveWsUrl();

// ---------------------------------------------------------------------------
// Ticket exchange
// ---------------------------------------------------------------------------

interface WsTicketResponse {
  ticket: string;
}

/**
 * Request a single-use WS connection ticket from the gateway.
 * Returns the ticket UUID on success, null on failure.
 * Does NOT retry — the caller handles retry/backoff.
 */
async function requestWsTicket(): Promise<string | null> {
  const token = getAccessToken();
  if (!token) return null;

  const controller = new AbortController();
  const timeout = setTimeout(() => controller.abort(), 5000);

  try {
    const headers: Record<string, string> = {
      'Content-Type': 'application/json',
      Authorization: token.startsWith('Bearer ') ? token : `Bearer ${token}`,
    };

    // Inject CSRF token (POST is a mutating method)
    const csrfToken = getCsrfToken();
    if (csrfToken) {
      headers[CSRF_HEADER] = csrfToken;
    }

    const resp = await fetch('/api/v1/auth/ws-ticket', {
      method: 'POST',
      headers,
      credentials: 'include',
      signal: controller.signal,
    });

    if (!resp.ok) return null;

    const body: WsTicketResponse = await resp.json();
    if (!body?.ticket) return null;

    return body.ticket;
  } catch {
    return null;
  } finally {
    clearTimeout(timeout);
  }
}

// ---------------------------------------------------------------------------
// Reconnect policy
// ---------------------------------------------------------------------------

const MAX_RECONNECT_ATTEMPTS = 5;
const RECONNECT_BASE_MS = 3000;

export interface WebSocketMessage {
  type: string;
  payload: any;
}

type MessageHandler = (message: WebSocketMessage) => void;

export function useWebSocket() {
  const wsRef = useRef<WebSocket | null>(null);
  const [isConnected, setIsConnected] = useState(false);
  const [lastMessage, setLastMessage] = useState<WebSocketMessage | null>(null);
  const handlersRef = useRef<Map<string, Set<MessageHandler>>>(new Map());
  const reconnectTimeoutRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  const reconnectAttemptsRef = useRef(0);
  const stoppedRef = useRef(false); // true when reconnecting is permanently stopped

  // Ensure a valid access token exists before connecting.
  // Tries cookie refresh if the in-memory token is missing or expired.
  const ensureAccessToken = useCallback(async (): Promise<string | null> => {
    let token = getAccessToken();
    if (token) return token;

    // No in-memory token — try cookie-based refresh
    const newToken = await tryCookieRefresh();
    if (newToken) return newToken;

    return null;
  }, []);

  const connect = useCallback(async () => {
    if (stoppedRef.current) return;

    // 1. Ensure we have a valid access token
    const token = await ensureAccessToken();
    if (!token) {
      // No token and refresh failed — user is unauthenticated.
      // Don't reconnect; the auth provider will handle the login redirect.
      stoppedRef.current = true;
      return;
    }

    // 2. Request a single-use WS ticket
    const ticket = await requestWsTicket();
    if (!ticket) {
      // Ticket request failed. Check if it was an auth failure (401)
      // or a transient error (503/network). We can't distinguish here
      // because fetch doesn't give us the status through the helper.
      // Fall through to bounded retry.
      handleReconnect();
      return;
    }

    // 3. Close existing connection if any
    if (wsRef.current) {
      wsRef.current.close();
      wsRef.current = null;
    }

    // 4. Connect with ticket (no JWT in URL)
    const ws = new WebSocket(`${WS_URL}?ticket=${ticket}`);

    ws.onopen = () => {
      console.log('WebSocket connected');
      setIsConnected(true);
      reconnectAttemptsRef.current = 0; // reset on successful connection
    };

    ws.onmessage = (event) => {
      try {
        const message: WebSocketMessage = JSON.parse(event.data);
        setLastMessage(message);

        // Notify type-specific handlers
        const handlers = handlersRef.current.get(message.type);
        if (handlers) {
          handlers.forEach((handler) => handler(message));
        }

        // Notify wildcard handlers
        const wildcardHandlers = handlersRef.current.get('*');
        if (wildcardHandlers) {
          wildcardHandlers.forEach((handler) => handler(message));
        }
      } catch (error) {
        console.error('Failed to parse WebSocket message:', error);
      }
    };

    ws.onclose = (event) => {
      console.log('WebSocket disconnected', event.code);
      setIsConnected(false);
      wsRef.current = null;

      // 4401 Unauthorized — ticket expired or already consumed.
      // Try to reconnect with a fresh ticket (not counted against limit).
      if (event.code === 4401) {
        reconnectAttemptsRef.current = 0;
        handleReconnect();
        return;
      }

      // Normal closure (1000) — only reconnect if user is still authenticated
      if (event.code === 1000) {
        if (getAccessToken()) {
          reconnectAttemptsRef.current = 0;
          handleReconnect();
        }
        return;
      }

      // Other close codes — bounded retry
      handleReconnect();
    };

    ws.onerror = (error) => {
      console.error('WebSocket error:', error);
    };

    wsRef.current = ws;
  }, [ensureAccessToken]);

  // Schedule reconnect with exponential backoff.
  // Respects MAX_RECONNECT_ATTEMPTS and stoppedRef.
  const handleReconnect = useCallback(() => {
    if (stoppedRef.current) return;

    const attempt = reconnectAttemptsRef.current;
    if (attempt >= MAX_RECONNECT_ATTEMPTS) {
      console.error(
        `WebSocket: max reconnect attempts (${MAX_RECONNECT_ATTEMPTS}) reached — giving up`
      );
      stoppedRef.current = true;
      return;
    }

    // Exponential backoff with jitter
    const base = RECONNECT_BASE_MS * Math.pow(2, Math.min(attempt, 4));
    const jitter = Math.floor(Math.random() * 500);
    const delay = Math.min(20000, base + jitter);
    reconnectAttemptsRef.current = attempt + 1;

    reconnectTimeoutRef.current = setTimeout(() => {
      connect();
    }, delay);
  }, [connect]);

  const disconnect = useCallback(() => {
    stoppedRef.current = true;
    if (reconnectTimeoutRef.current) {
      clearTimeout(reconnectTimeoutRef.current);
      reconnectTimeoutRef.current = null;
    }
    if (wsRef.current) {
      wsRef.current.close();
      wsRef.current = null;
    }
    setIsConnected(false);
    reconnectAttemptsRef.current = 0;
  }, []);

  const send = useCallback((type: string, payload: any) => {
    if (wsRef.current?.readyState === WebSocket.OPEN) {
      wsRef.current.send(JSON.stringify({ type, payload }));
    }
  }, []);

  const subscribe = useCallback((type: string, handler: MessageHandler) => {
    if (!handlersRef.current.has(type)) {
      handlersRef.current.set(type, new Set());
    }
    handlersRef.current.get(type)!.add(handler);

    // Return unsubscribe function
    return () => {
      handlersRef.current.get(type)?.delete(handler);
    };
  }, []);

  // Connect on mount, disconnect on unmount
  useEffect(() => {
    stoppedRef.current = false;
    reconnectAttemptsRef.current = 0;
    connect();
    return () => disconnect();
  }, [connect, disconnect]);

  return {
    isConnected,
    lastMessage,
    send,
    subscribe,
    connect,
    disconnect,
  };
}

// ---------------------------------------------------------------------------
// Context for sharing WebSocket across components
// ---------------------------------------------------------------------------

interface WebSocketContextValue {
  isConnected: boolean;
  lastMessage: WebSocketMessage | null;
  send: (type: string, payload: any) => void;
  subscribe: (type: string, handler: MessageHandler) => () => void;
}

const WebSocketContext = createContext<WebSocketContextValue | null>(null);

export function WebSocketProvider({ children }: { children: ReactNode }) {
  const ws = useWebSocket();

  return (
    <WebSocketContext.Provider value={ws}>
      {children}
    </WebSocketContext.Provider>
  );
}

export function useWebSocketContext() {
  const context = useContext(WebSocketContext);
  if (!context) {
    throw new Error('useWebSocketContext must be used within WebSocketProvider');
  }
  return context;
}

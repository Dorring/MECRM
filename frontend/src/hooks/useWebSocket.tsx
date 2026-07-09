'use client';

import { useEffect, useRef, useState, useCallback, createContext, useContext, ReactNode } from 'react';
import { getAccessToken, tryCookieRefresh, decodeToken, CSRF_HEADER, getCsrfToken } from '@/lib/api';
import { deriveWsUrl } from '@/lib/runtime-config';

// ---------------------------------------------------------------------------
// Ticket exchange
// ---------------------------------------------------------------------------

interface WsTicketResult {
  ok: boolean;
  ticket: string | null;
  status: number;
  reason: string;
}

/**
 * Request a single-use WS connection ticket from the gateway.
 * Returns a result object so the caller can distinguish transient
 * errors (503, network) from permanent failures (401, 403, other 4xx).
 * On success: { ok: true, ticket: "<uuid>", status: 200, reason: "" }
 * On failure: { ok: false, ticket: null, status: <http>, reason: "<msg>" }
 */
async function requestWsTicket(): Promise<WsTicketResult> {
  const token = getAccessToken();
  if (!token) return { ok: false, ticket: null, status: 401, reason: 'No access token' };

  const controller = new AbortController();
  const timeout = setTimeout(() => controller.abort(), 5000);

  try {
    const headers: Record<string, string> = {
      'Content-Type': 'application/json',
      Authorization: token.startsWith('Bearer ') ? token : `Bearer ${token}`,
    };

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

    if (!resp.ok) {
      return { ok: false, ticket: null, status: resp.status, reason: `HTTP ${resp.status}` };
    }

    const body = await resp.json();
    if (!body?.ticket) {
      return { ok: false, ticket: null, status: 502, reason: 'Invalid response body' };
    }

    return { ok: true, ticket: body.ticket as string, status: 200, reason: '' };
  } catch (err: unknown) {
    const e = err as Error & { name?: string };
    if (e?.name === 'AbortError') {
      return { ok: false, ticket: null, status: 0, reason: 'Request timed out' };
    }
    return { ok: false, ticket: null, status: 0, reason: e?.message || 'Network error' };
  } finally {
    clearTimeout(timeout);
  }
}

// ---------------------------------------------------------------------------
// Reconnect policy
// ---------------------------------------------------------------------------

const MAX_RECONNECT_ATTEMPTS = 5;
const RECONNECT_BASE_MS = 3000;

/** Returns true if the status is a permanent failure — do NOT retry. */
function isPermanentFailure(status: number): boolean {
  // 401 Unauthorized — token expired/revoked
  // 403 Forbidden — origin not allowed
  // Other 4xx (400, 404, 405, etc.) — client error, won't fix itself
  // EXCEPT 429 Too Many Requests — rate limit is transient, should retry with backoff
  return status >= 400 && status < 500 && status !== 429;
}

export interface WebSocketMessage {
  type: string;
  payload: any;
}

type MessageHandler = (message: WebSocketMessage) => void;

export function useWebSocket({ enabled }: { enabled: boolean } = { enabled: true }) {
  const wsRef = useRef<WebSocket | null>(null);
  const [isConnected, setIsConnected] = useState(false);
  const [lastMessage, setLastMessage] = useState<WebSocketMessage | null>(null);
  const handlersRef = useRef<Map<string, Set<MessageHandler>>>(new Map());
  const reconnectTimeoutRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  const reconnectAttemptsRef = useRef(0);
  const stoppedRef = useRef(false);
  // Tracks whether the ONE 4401 auth retry has already been used for this
  // connection cycle. Reset only on explicit reconnect (login, enable).
  // NEVER reset in onopen — that would create an infinite open→close(4401)
  // → retry → open → close(4401) loop.
  const wsAuthRetryUsedRef = useRef(false);

  // Resolve wsUrl from runtime-config, falling back to deriveWsUrl().
  // Updated asynchronously — same-origin default is always correct for prod.
  const wsUrlRef = useRef<string>(deriveWsUrl());
  useEffect(() => {
    let cancelled = false;
    import('@/lib/runtime-config')
      .then(({ getRuntimeConfig }) => getRuntimeConfig())
      .then((cfg: { wsUrl?: string }) => {
        if (!cancelled && cfg.wsUrl) {
          wsUrlRef.current = cfg.wsUrl;
        }
      })
      .catch(() => { /* use default */ });
    return () => { cancelled = true; };
  }, []);

  // Ensure a valid (non-expired) access token exists before connecting.
  const ensureAccessToken = useCallback(async (): Promise<string | null> => {
    const token = getAccessToken();
    if (token) {
      // Check expiry before using — don't just check presence.
      const claims = decodeToken(token);
      if (claims && typeof claims.exp === 'number') {
        // Token must have at least 5s of remaining validity.
        // Using Date.now() + 5000 (not -5000) ensures we don't
        // accept tokens that expire within the next 5 seconds.
        if (claims.exp * 1000 > Date.now() + 5000) {
          return token;
        }
        // Token exists but is expired or malformed — try refresh
      }
    }
    // No valid token — try cookie-based refresh
    return await tryCookieRefresh();
  }, []);

  // ---- scheduleReconnect: bounded exponential backoff ----
  const scheduleReconnect = useCallback(() => {
    if (stoppedRef.current) return;

    const attempt = reconnectAttemptsRef.current;
    if (attempt >= MAX_RECONNECT_ATTEMPTS) {
      console.error(
        `WebSocket: max reconnect attempts (${MAX_RECONNECT_ATTEMPTS}) reached — giving up`
      );
      stoppedRef.current = true;
      return;
    }

    const base = RECONNECT_BASE_MS * Math.pow(2, Math.min(attempt, 4));
    const jitter = Math.floor(Math.random() * 500);
    const delay = Math.min(20000, base + jitter);
    reconnectAttemptsRef.current = attempt + 1;

    reconnectTimeoutRef.current = setTimeout(() => {
      connectRef.current();
    }, delay);
  }, []);

  // ---- connect: get token → get ticket → open WebSocket ----
  const connect = useCallback(async () => {
    if (stoppedRef.current) return;

    // 1. Ensure we have a valid access token
    const token = await ensureAccessToken();
    if (!token) {
      stoppedRef.current = true;
      return;
    }

    // 2. Request a single-use WS ticket
    const tr = await requestWsTicket();

    if (!tr.ok) {
      if (isPermanentFailure(tr.status)) {
        console.error(
          `WebSocket: permanent failure (${tr.status}) — stopping. ${tr.reason}`
        );
        stoppedRef.current = true;
        return;
      }
      scheduleReconnect();
      return;
    }

    const ticket = tr.ticket!; // ok === true guarantees ticket is non-null

    // 3. Close existing connection if any
    if (wsRef.current) {
      wsRef.current.close();
      wsRef.current = null;
    }

    // 4. Connect with ticket (no JWT in URL)
    const ws = new WebSocket(`${wsUrlRef.current}?ticket=${ticket}`);

    ws.onopen = () => {
      console.log('WebSocket connected');
      setIsConnected(true);
      reconnectAttemptsRef.current = 0;
    };

    ws.onmessage = (event) => {
      try {
        const message: WebSocketMessage = JSON.parse(event.data);
        setLastMessage(message);
        const handlers = handlersRef.current.get(message.type);
        if (handlers) handlers.forEach((handler) => handler(message));
        const wildcardHandlers = handlersRef.current.get('*');
        if (wildcardHandlers) wildcardHandlers.forEach((handler) => handler(message));
      } catch (error) {
        console.error('Failed to parse WebSocket message:', error);
      }
    };

    ws.onclose = (event) => {
      console.log('WebSocket disconnected', event.code);
      setIsConnected(false);
      wsRef.current = null;

      if (event.code === 4401) {
        // 4401 Unauthorized — ticket expired/revoked/session invalid.
        // Allow at most ONE retry (ticket race: old ticket consumed
        // between /ws-ticket call and WebSocket upgrade).
        // Uses a dedicated ref — NOT reconnectAttemptsRef — because onopen
        // resets reconnectAttemptsRef to 0, which would create an infinite
        // open→close(4401)→retry→open→close(4401) loop.
        if (wsAuthRetryUsedRef.current) {
          console.error('WebSocket: second 4401 — auth failure, stopping');
          stoppedRef.current = true;
          return;
        }
        wsAuthRetryUsedRef.current = true;
        scheduleReconnect();
        return;
      }
      if (event.code === 1000) {
        if (getAccessToken()) {
          reconnectAttemptsRef.current = 0;
          scheduleReconnect();
        }
        return;
      }
      scheduleReconnect();
    };

    ws.onerror = (error) => {
      console.error('WebSocket error:', error);
    };

    wsRef.current = ws;
  }, [ensureAccessToken, scheduleReconnect]);

  // Keep a ref to the latest connect for scheduleReconnect (avoids circular dep)
  const connectRef = useRef(connect);
  useEffect(() => {
    connectRef.current = connect;
  }, [connect]);

  // ---- disconnect: clean shutdown ----
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

  /** Manually trigger a reconnect (e.g. after login). Resets stopped state. */
  const reconnect = useCallback(() => {
    stoppedRef.current = false;
    reconnectAttemptsRef.current = 0;
    wsAuthRetryUsedRef.current = false;
    connectRef.current();
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
    return () => {
      handlersRef.current.get(type)?.delete(handler);
    };
  }, []);

  // Connect/disconnect based on enabled flag (driven by auth state).
  // When enabled=false (loading or not authenticated), disconnect.
  // When enabled=true (auth ready and authenticated), connect.
  useEffect(() => {
    if (!enabled) {
      disconnect();
      return;
    }
    stoppedRef.current = false;
    reconnectAttemptsRef.current = 0;
    wsAuthRetryUsedRef.current = false;
    connect();
    return () => disconnect();
  }, [enabled, connect, disconnect]);

  return {
    isConnected,
    lastMessage,
    send,
    subscribe,
    reconnect,
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
  reconnect: () => void;
  disconnect: () => void;
}

const WebSocketContext = createContext<WebSocketContextValue | null>(null);

/**
 * WebSocketProvider accepts an `enabled` prop.
 * The parent (WsBridge in providers.tsx) passes `!isLoading && isAuthenticated`
 * from useAuth(). This prevents WebSocket from racing with AuthProvider's
 * tryCookieRefresh during boot — WS only connects after auth boot is complete.
 */
export function WebSocketProvider({ children, enabled = true }: { children: ReactNode; enabled?: boolean }) {
  const ws = useWebSocket({ enabled });

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

/**
 * Runtime API/WS URL resolution.
 *
 * Priority chain:
 * 1. /api/config response (server-side, no NEXT_PUBLIC_* leak).
 *    Used only in local/dev direct mode where the frontend connects
 *    directly to the Gateway (no same-origin proxy).
 * 2. Relative defaults: empty string for API (browser uses same-origin
 *    /api/v1/... paths), ws(s)://window.location.host/ws for WebSocket.
 *
 * /api/config is NOT a cross-origin cookie auth mechanism. Group C does
 * not support browser cross-origin cookie auth. Any production deployment
 * requiring cross-origin must implement SameSite=None + full CORS
 * credentials in a separate ADR amendment.
 *
 * In production, apiUrl is empty (same-origin proxy) and wsUrl is derived
 * from window.location (infrastructure proxy forwards /ws to Gateway).
 */

import { setApiBaseUrl } from './api';

export interface RuntimeConfig {
  apiUrl: string;
  wsUrl: string;
}

let cached: RuntimeConfig | null = null;

function deriveWsUrl(): string {
  if (typeof window === 'undefined') return 'ws://localhost:4000';
  const protocol = window.location.protocol === 'https:' ? 'wss:' : 'ws:';
  return `${protocol}//${window.location.host}/ws`;
}

export async function getRuntimeConfig(): Promise<RuntimeConfig> {
  if (cached) return cached;

  try {
    const resp = await fetch('/api/config');
    if (resp.ok) {
      const cfg = await resp.json();
      cached = {
        apiUrl: cfg.apiUrl || '',
        wsUrl: cfg.wsUrl || deriveWsUrl(),
      };
      // Apply apiUrl to the ApiClient immediately
      if (cached.apiUrl) {
        setApiBaseUrl(cached.apiUrl);
      }
      return cached!;
    }
  } catch {
    // /api/config unavailable — fall back to relative defaults.
    // This is the normal production path (same-origin proxy).
  }

  cached = {
    apiUrl: '',        // browser uses same-origin relative paths
    wsUrl: deriveWsUrl(),
  };
  return cached;
}

/** Initialize runtime config early. Safe to call multiple times. */
export function initRuntimeConfig(): void {
  if (typeof window === 'undefined') return;
  // Fire and forget — config caches on first resolution.
  getRuntimeConfig().catch(() => { /* silent; defaults apply */ });
}

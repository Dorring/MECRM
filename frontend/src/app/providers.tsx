'use client';

import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { useEffect, useState, useCallback, createContext, useContext, useMemo, useRef, ReactNode } from 'react';
import { TelemetryProvider } from '@/components/TelemetryProvider';
import { WebSocketProvider } from '@/hooks/useWebSocket';
import {
  AuthUser,
  getCachedUser,
  hasValidAccessToken,
  clearSession,
  persistSession,
  authApi,
  tryCookieRefresh,
  migrateFromLocalStorage,
  cacheAuthUserDisplay,
  setAccessToken,
} from '@/lib/api';
import { initRuntimeConfig, getRuntimeConfig } from '@/lib/runtime-config';

// ---------------------------------------------------------------------------
// Helper: resolve user profile via /auth/me (authoritative identity source)
// ---------------------------------------------------------------------------
// /auth/me is the SOLE authority for user identity (id, email, tenantId, roles).
// localStorage cache is a DISPLAY-ONLY supplement — it must NEVER be used to
// maintain an authenticated state when /me fails.
//
// On success: /me claims are authoritative; cached name and tenant.name are
// merged ONLY when identity matches (same id + tenantId). The merged profile
// is written back to the display cache.
//
// On failure (401, 503, network error): returns null. The caller MUST clear
// local auth state — no authenticated UI without a verified /me response.
async function resolveUserProfile(): Promise<AuthUser | null> {
  try {
    const resp = await authApi.me();
    const profile = resp.data;
    const cached = getCachedUser();

    // Identity must match before we trust cached display fields
    const sameIdentity =
      cached?.id === profile.id &&
      cached?.tenant?.id === profile.tenantId;

    const user: AuthUser = {
      id: profile.id,
      email: profile.email,
      name: sameIdentity ? (cached!.name || profile.name || '') : (profile.name || ''),
      roles: profile.roles || [],
      tenant: {
        id: profile.tenantId,
        name: sameIdentity ? (cached!.tenant?.name || '') : '',
      },
    };

    // Update display cache for next boot (display-only, no tokens)
    cacheAuthUserDisplay(user);
    return user;
  } catch {
    // /me failed — do NOT fall back to localStorage for authenticated state.
    // The caller must clear local state and show login.
    return null;
  }
}

// Clear local auth artifacts without calling server logout (preserves HttpOnly
// cookie for next boot). Used when /me fails after a successful cookie refresh
// or migration — the session may still be valid, but we can't prove the user's
// identity right now.
function clearLocalAuthState(): void {
  setAccessToken(null);
  if (typeof window !== 'undefined') {
    try { window.localStorage.removeItem('authUser'); } catch { /* ignore */ }
  }
}

// ---------------------------------------------------------------------------
// Auth context (defined here to keep the change within scope; exported for
// Header / login / settings consumption).
//
// IMPORTANT: roles/tenant exposed via this context are DISPLAY-ONLY. The UI
// must never treat them as an authorization decision — the Gateway + OPA
// remain the sole authorization authority. Local state can be stale or
// tampered with in the browser.
// ---------------------------------------------------------------------------

interface AuthContextValue {
  user: AuthUser | null;
  isAuthenticated: boolean;
  isLoading: boolean;
  login: (credentials: { email: string; password: string; tenantSlug: string }) => Promise<AuthUser>;
  /** Returns { success: true } on successful server logout.
   *  Returns { success: false, error } on 503, network error, or any failure
   *  — local session is PRESERVED in this case. Caller must show error to user. */
  logout: () => Promise<{ success: boolean; error?: string }>;
  refreshUser: () => void;
}

const AuthContext = createContext<AuthContextValue | null>(null);

const AUTH_EVENT = 'magent:auth-change';

function emitAuthChange(): void {
  if (typeof window === 'undefined') return;
  window.dispatchEvent(new CustomEvent(AUTH_EVENT));
}

export function AuthProvider({ children }: { children: ReactNode }) {
  // Start null on the server and on first client render to avoid hydration
  // mismatch; resolved in the boot effect below.
  const [user, setUser] = useState<AuthUser | null>(null);
  const [isLoading, setIsLoading] = useState(true);

  // -------------------------------------------------------------------
  // Boot recovery — concurrency guards (§3 in ADR-004 plan)
  // -------------------------------------------------------------------
  // React StrictMode double-invokes effects in development. We use refs
  // (not state) to guard the boot flow so a second concurrent invocation
  // cannot overwrite the first successful result.
  //
  // - bootStartedRef: prevents two boots from starting simultaneously.
  // - mountedRef: lets us bail out if the component unmounts mid-boot.
  //   (setState on an unmounted component is a no-op in React 18+, but
  //   we avoid the wasted work.)
  const bootStartedRef = useRef(false);
  const mountedRef = useRef(true);

  useEffect(() => {
    mountedRef.current = true;
    return () => { mountedRef.current = false; };
  }, []);

  useEffect(() => {
    // If boot already started (StrictMode double-fire), skip.
    if (bootStartedRef.current) return;
    bootStartedRef.current = true;

    async function boot(): Promise<void> {
      try {
        // Ensure runtime config (API_URL/WS_URL from /api/config) is resolved
        // before any API call. In same-origin mode this returns the cached
        // default immediately; in direct mode it fetches /api/config first.
        await getRuntimeConfig();

        // Step 1: attempt cookie-based refresh
        const newToken = await tryCookieRefresh();
        if (!mountedRef.current) return;

        if (newToken) {
          // C5: /auth/me is the sole authority for user identity.
          // If /me fails (401/503/network), clear local state and show login —
          // the HttpOnly cookie is preserved for next boot retry.
          const user = await resolveUserProfile();
          if (!mountedRef.current) return;

          if (user) {
            setUser(user);
          } else {
            clearLocalAuthState();
          }
          setIsLoading(false);
          return;
        }

        // Step 2: no cookie session — try legacy localStorage migration
        const migrated = await migrateFromLocalStorage();
        if (!mountedRef.current) return;

        if (migrated) {
          const user = await resolveUserProfile();
          if (!mountedRef.current) return;

          if (user) {
            setUser(user);
          } else {
            clearLocalAuthState();
          }
          setIsLoading(false);
          return;
        }

        // Step 3: both failed — unauthenticated
        if (mountedRef.current) {
          setUser(null);
          setIsLoading(false);
        }
      } catch {
        if (mountedRef.current) {
          setUser(null);
          setIsLoading(false);
        }
      }
    }

    boot();
  }, []);

  const refreshUser = useCallback(() => {
    if (!hasValidAccessToken()) {
      setUser(null);
      return;
    }
    const cached = getCachedUser();
    if (cached) setUser(cached);
    // If no display cache exists, user stays at its previous value.
    // Next boot will call /auth/me to re-resolve the authoritative profile.
  }, []);

  // Listen for auth-change events and cross-tab storage changes.
  useEffect(() => {
    const onChange = () => refreshUser();
    const onStorage = (e: StorageEvent) => {
      if (e.key === 'authUser' || e.key === null) {
        refreshUser();
      }
    };
    window.addEventListener(AUTH_EVENT, onChange);
    window.addEventListener('storage', onStorage);
    return () => {
      window.removeEventListener(AUTH_EVENT, onChange);
      window.removeEventListener('storage', onStorage);
    };
  }, [refreshUser]);

  const login = useCallback(
    async (credentials: { email: string; password: string; tenantSlug: string }) => {
      const resp = await authApi.login(credentials);
      const u = persistSession(resp.data);
      setUser(u);
      emitAuthChange();
      return u;
    },
    []
  );

  // -----------------------------------------------------------------------
  // Safe logout (§4 in ADR-004 implementation plan)
  // -----------------------------------------------------------------------
  const logout = useCallback(async (): Promise<{ success: boolean; error?: string }> => {
    try {
      await authApi.logout();
      // Server confirmed logout (2xx) — safe to clear local state.
      clearSession();
      setUser(null);
      emitAuthChange();
      return { success: true };
    } catch (err: any) {
      // Do NOT clear local session. Server did not persist revocation.
      const status = err?.status;
      let error: string;
      if (status === 503) {
        error = 'Logout failed — service temporarily unavailable. Please try again.';
      } else if (err?.name === 'TimeoutError' || err?.message?.includes('timed out')) {
        error = 'Logout timed out. Please check your connection and try again.';
      } else {
        error = 'Logout failed. Please try again.';
      }
      console.error('Logout error (local session preserved)', status || err?.message);
      return { success: false, error };
    }
  }, []);

  const value = useMemo<AuthContextValue>(
    () => ({ user, isAuthenticated: !!user, isLoading, login, logout, refreshUser }),
    [user, isLoading, login, logout, refreshUser]
  );

  return <AuthContext.Provider value={value}>{children}</AuthContext.Provider>;
}

export function useAuth() {
  const ctx = useContext(AuthContext);
  if (!ctx) {
    throw new Error('useAuth must be used within AuthProvider');
  }
  return ctx;
}

// Post-login redirect target, defaulting to home. Only relative paths are
// allowed to prevent open-redirect abuse.
export function getPostLoginRedirect(): string {
  if (typeof window === 'undefined') return '/';
  const redirect = new URLSearchParams(window.location.search).get('redirect');
  if (redirect && redirect.startsWith('/') && !redirect.startsWith('//')) {
    return redirect;
  }
  return '/';
}

// ---------------------------------------------------------------------------
// Root providers
// ---------------------------------------------------------------------------

/**
 * Bridge component: reads auth state and controls WebSocket connection.
 * WebSocket MUST NOT connect before auth boot completes — otherwise it races
 * AuthProvider's tryCookieRefresh for the single-use refresh token.
 * When loading or not authenticated, WebSocket is disconnected.
 */
function WsBridge({ children }: { children: ReactNode }) {
  const { isAuthenticated, isLoading } = useAuth();
  return (
    <WebSocketProvider enabled={!isLoading && isAuthenticated}>
      {children}
    </WebSocketProvider>
  );
}

export function Providers({ children }: { children: React.ReactNode }) {
  const [queryClient] = useState(
    () =>
      new QueryClient({
        defaultOptions: {
          queries: {
            staleTime: 60 * 1000, // 1 minute
            refetchOnWindowFocus: false,
          },
        },
      })
  );

  // Initialize runtime config early — before AuthProvider boot.
  // Fire-and-forget: same-origin defaults are always correct for production;
  // /api/config is only needed for local/dev direct mode.
  useEffect(() => {
    initRuntimeConfig();
  }, []);

  // Capture unhandled errors and promise rejections to keep UI stable.
  useEffect(() => {
    const onError = (event: ErrorEvent) => {
      const summary =
        (event.error && (event.error.message || event.error.name)) || event.message || 'unknown error';
      console.error('Unhandled error', summary);
    };
    const onRejection = (event: PromiseRejectionEvent) => {
      const reason = event.reason;
      const summary =
        reason instanceof Error
          ? `${reason.name}: ${reason.message}`
          : typeof reason === 'string'
            ? reason
            : 'unhandled rejection';
      console.error('Unhandled rejection', summary);
    };
    window.addEventListener('error', onError);
    window.addEventListener('unhandledrejection', onRejection);
    return () => {
      window.removeEventListener('error', onError);
      window.removeEventListener('unhandledrejection', onRejection);
    };
  }, []);

  return (
    <AuthProvider>
      <TelemetryProvider>
        <QueryClientProvider client={queryClient}>
          <WsBridge>{children}</WsBridge>
        </QueryClientProvider>
      </TelemetryProvider>
    </AuthProvider>
  );
}

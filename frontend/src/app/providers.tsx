'use client';

import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { useEffect, useState, useCallback, createContext, useContext, useMemo, ReactNode } from 'react';
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
  setAccessToken,
} from '@/lib/api';

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
  // Track whether boot recovery has run to avoid double execution in StrictMode.
  const [booted, setBooted] = useState(false);

  const refreshUser = useCallback(() => {
    if (!hasValidAccessToken()) {
      setUser(null);
      return;
    }
    const cached = getCachedUser();
    if (cached) setUser(cached);
    // If no cached user exists (e.g. after cookie refresh without /auth/me),
    // user stays at its previous value. TD-C3-1: add GET /auth/me to fill this gap.
  }, []);

  // -----------------------------------------------------------------------
  // Boot recovery (§3 in ADR-004 implementation plan)
  // -----------------------------------------------------------------------
  // 1. Try cookie-based refresh (for users who already have an HttpOnly
  //    refresh_token cookie from a previous login).
  // 2. If no cookie session, try legacy localStorage → cookie migration.
  // 3. If both fail, user is unauthenticated — login page will show.
  // 4. Finally, clean up any leftover legacy localStorage keys.
  useEffect(() => {
    if (booted) return;

    async function boot(): Promise<void> {
      try {
        // Step 1: attempt cookie-based refresh
        const newToken = await tryCookieRefresh();
        if (newToken) {
          // /refresh only returns { accessToken } — no user profile.
          // Restore user from the cached authUser (display-only cache).
          // If cache is missing, user stays null (TD-C3-1: /auth/me gap).
          const cachedUser = getCachedUser();
          if (cachedUser) {
            setUser(cachedUser);
          }
          setBooted(true);
          setIsLoading(false);
          return;
        }

        // Step 2: no cookie session — try legacy localStorage migration
        const migrated = await migrateFromLocalStorage();
        if (migrated) {
          // Migration succeeded: accessToken is now in memory.
          // Restore user from legacy authUser cache if present.
          const cachedUser = getCachedUser();
          if (cachedUser) {
            setUser(cachedUser);
          }
          setBooted(true);
          setIsLoading(false);
          return;
        }

        // Step 3: both failed — unauthenticated
        setUser(null);
        setBooted(true);
        setIsLoading(false);
      } catch {
        // Unexpected error during boot — treat as unauthenticated
        setUser(null);
        setBooted(true);
        setIsLoading(false);
      }
    }

    boot();
  }, [booted]);

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
  // POST /api/v1/auth/logout with credentials + CSRF header.
  // On 2xx: clear local session (memory token + authUser cache).
  // On 503 or network error: PRESERVE local session — the server did NOT
  //   persist the revocation (C2 fail-closed contract), so clearing locally
  //   would leave the user in an inconsistent state (valid cookie, no
  //   access token). Show an error to the user instead.
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

  // Capture unhandled errors and promise rejections to keep UI stable.
  // Deliberately log only a compact summary, never raw objects that could
  // contain tokens or PII.
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
          <WebSocketProvider>{children}</WebSocketProvider>
        </QueryClientProvider>
      </TelemetryProvider>
    </AuthProvider>
  );
}

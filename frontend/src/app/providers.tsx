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
  getRefreshToken,
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
  logout: () => Promise<void>;
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
  // mismatch; resolve from localStorage in an effect.
  const [user, setUser] = useState<AuthUser | null>(null);
  const [isLoading, setIsLoading] = useState(true);

  const refreshUser = useCallback(() => {
    if (!hasValidAccessToken()) {
      setUser(null);
      return;
    }
    setUser(getCachedUser());
  }, []);

  useEffect(() => {
    refreshUser();
    setIsLoading(false);

    const onChange = () => refreshUser();
    const onStorage = (e: StorageEvent) => {
      if (e.key === 'accessToken' || e.key === 'authUser' || e.key === null) {
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

  const logout = useCallback(async () => {
    const refreshToken = getRefreshToken();
    // Best-effort server-side blacklist. Never block UI on a failed logout:
    // access tokens are short-lived and the refresh token is also blacklisted.
    try {
      if (refreshToken) await authApi.logout(refreshToken);
    } catch {
      // swallow: we clear local state regardless
    }
    clearSession();
    setUser(null);
    emitAuthChange();
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

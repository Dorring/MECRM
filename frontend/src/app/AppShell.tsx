'use client';

import { useEffect } from 'react';
import { usePathname, useRouter } from 'next/navigation';
import { Sidebar } from '@/components/layout/Sidebar';
import { Header } from '@/components/layout/Header';
import { ChatPanel } from '@/components/ChatPanel';
import { useAuth } from './providers';

// Routes that must render WITHOUT the authenticated chrome (sidebar/header/
// chat panel). Today this is only the login page; keeping a set makes it
// trivial to add e.g. /forgot-password later.
const PUBLIC_ROUTES = new Set(['/login']);

function isPublicRoute(pathname: string): boolean {
  return PUBLIC_ROUTES.has(pathname);
}

/**
 * AppShell decides whether to render the authenticated chrome (sidebar, header,
 * chat panel) based on the current route and session state.
 *
 * - Public routes (e.g. /login) render bare.
 * - Protected routes redirect unauthenticated users to /login?redirect=...
 *
 * NOTE: this is a UX/auth-gate convenience, NOT a security control. The
 * Gateway + OPA enforce authorization on every request regardless of what
 * the UI renders.
 */
export function AppShell({ children }: { children: React.ReactNode }) {
  const pathname = usePathname();
  const router = useRouter();
  const { isAuthenticated, isLoading } = useAuth();

  const isPublic = isPublicRoute(pathname);

  useEffect(() => {
    if (isLoading || isPublic) return;
    if (!isAuthenticated) {
      const redirect = encodeURIComponent(pathname || '/');
      router.replace(`/login?redirect=${redirect}`);
    }
  }, [isLoading, isPublic, isAuthenticated, pathname, router]);

  if (isPublic) {
    return <>{children}</>;
  }

  // While the session is still resolving on a protected route, avoid flashing
  // the full chrome before the redirect fires.
  if (isLoading) {
    return (
      <div className="flex h-screen items-center justify-center bg-gray-50 dark:bg-gray-900">
        <div className="text-gray-500">Loading…</div>
      </div>
    );
  }

  // Not authenticated and not public: the effect above is redirecting. Render
  // nothing to avoid briefly mounting authenticated UI.
  if (!isAuthenticated) {
    return null;
  }

  return (
    <div className="flex h-screen bg-gray-50 dark:bg-gray-900">
      <Sidebar />
      <div className="flex-1 flex flex-col overflow-hidden">
        <Header />
        <main className="flex-1 overflow-y-auto p-6">{children}</main>
      </div>
      <ChatPanel />
    </div>
  );
}

'use client';

import { Bell, User, Moon, Sun, LogOut, ChevronDown } from 'lucide-react';
import { useState, useEffect, useRef } from 'react';
import { useRouter } from 'next/navigation';
import { CommandBar } from '@/components/CommandBar';
import { useAuth } from '@/app/providers';

export function Header() {
  const [darkMode, setDarkMode] = useState(false);
  // TODO(Phase 5): wire notifications to a real endpoint (e.g. a pending
  // approvals/notifications API). Until that contract exists, we intentionally
  // do NOT show a fake count — the previous hardcoded "3" was misleading.
  // Set to null so the badge renders nothing.
  const notifications: number | null = null;
  const { user, logout } = useAuth();
  const router = useRouter();
  const [menuOpen, setMenuOpen] = useState(false);
  const menuRef = useRef<HTMLDivElement | null>(null);

  useEffect(() => {
    if (darkMode) {
      document.documentElement.classList.add('dark');
    } else {
      document.documentElement.classList.remove('dark');
    }
  }, [darkMode]);

  // Close the user menu on outside click / Escape for keyboard accessibility.
  useEffect(() => {
    if (!menuOpen) return;
    const onPointerDown = (e: MouseEvent) => {
      if (menuRef.current && !menuRef.current.contains(e.target as Node)) {
        setMenuOpen(false);
      }
    };
    const onKey = (e: KeyboardEvent) => {
      if (e.key === 'Escape') setMenuOpen(false);
    };
    document.addEventListener('mousedown', onPointerDown);
    document.addEventListener('keydown', onKey);
    return () => {
      document.removeEventListener('mousedown', onPointerDown);
      document.removeEventListener('keydown', onKey);
    };
  }, [menuOpen]);

  const handleLogout = async () => {
    setMenuOpen(false);
    const result = await logout();
    if (result.success) {
      router.replace('/login');
    }
    // On failure (503/network error), local session is preserved.
    // User stays on current page; error is logged by the auth provider.
  };

  const displayName = user?.name || user?.email || 'User';
  // Display-only role summary. Never use this for access decisions.
  const displayRole = user?.roles?.length ? user.roles.join(', ') : 'Member';
  const initials = (user?.name || user?.email || '?')
    .split(/[\s@._-]+/)
    .filter(Boolean)
    .slice(0, 2)
    .map((s) => s[0]?.toUpperCase())
    .join('') || '?';

  return (
    <header className="h-16 bg-white dark:bg-gray-900 border-b border-gray-200 dark:border-gray-800 flex items-center justify-between px-6">
      {/* Search */}
      <div className="flex-1 max-w-lg">
        <CommandBar />
      </div>

      {/* Actions */}
      <div className="flex items-center gap-4">
        {/* Dark mode toggle */}
        <button
          onClick={() => setDarkMode(!darkMode)}
          className="p-2 rounded-md hover:bg-gray-100 dark:hover:bg-gray-800"
          aria-label={darkMode ? 'Switch to light mode' : 'Switch to dark mode'}
        >
          {darkMode ? <Sun size={20} /> : <Moon size={20} />}
        </button>

        {/* Notifications */}
        {/* TODO(Phase 5): connect to a real notifications endpoint; badge is
            hidden until we have live data. */}
        <button
          className="relative p-2 rounded-md hover:bg-gray-100 dark:hover:bg-gray-800"
          aria-label="Notifications"
          disabled={notifications === null}
        >
          <Bell size={20} />
          {notifications && notifications > 0 ? (
            <span className="absolute top-1 right-1 w-4 h-4 bg-red-500 text-white text-xs rounded-full flex items-center justify-center">
              {notifications}
            </span>
          ) : null}
        </button>

        {/* User menu */}
        <div className="relative flex items-center pl-4 border-l border-gray-200 dark:border-gray-700" ref={menuRef}>
          <button
            onClick={() => setMenuOpen((v) => !v)}
            className="flex items-center gap-3 focus:outline-none rounded-md"
            aria-haspopup="menu"
            aria-expanded={menuOpen}
            aria-label="Open user menu"
          >
            <div className="text-right hidden sm:block">
              <div className="text-sm font-medium text-gray-900 dark:text-white truncate max-w-[12rem]">
                {displayName}
              </div>
              {/* display-only; not a security control */}
              <div className="text-xs text-gray-500 dark:text-gray-400 truncate max-w-[12rem]">
                {displayRole}
              </div>
            </div>
            <div className="w-10 h-10 rounded-full bg-primary-100 dark:bg-primary-900 flex items-center justify-center">
              <span className="text-sm font-semibold text-primary-600">{initials}</span>
            </div>
            <ChevronDown size={16} className="text-gray-400" />
          </button>

          {menuOpen && (
            <div
              role="menu"
              className="absolute right-0 top-full mt-2 w-56 bg-white dark:bg-gray-800 border border-gray-200 dark:border-gray-700 rounded-md shadow-lg z-50"
            >
              <div className="px-4 py-3 border-b border-gray-200 dark:border-gray-700">
                <div className="text-sm font-medium text-gray-900 dark:text-white truncate">
                  {displayName}
                </div>
                {user?.email && (
                  <div className="text-xs text-gray-500 dark:text-gray-400 truncate">
                    {user.email}
                  </div>
                )}
              </div>
              <button
                onClick={() => { setMenuOpen(false); router.push('/settings'); }}
                role="menuitem"
                className="w-full flex items-center gap-2 px-4 py-2 text-sm text-gray-700 dark:text-gray-300 hover:bg-gray-100 dark:hover:bg-gray-700"
              >
                <User size={16} />
                Settings
              </button>
              <button
                onClick={handleLogout}
                role="menuitem"
                className="w-full flex items-center gap-2 px-4 py-2 text-sm text-red-600 dark:text-red-400 hover:bg-red-50 dark:hover:bg-red-900/20"
              >
                <LogOut size={16} />
                Sign out
              </button>
            </div>
          )}
        </div>
      </div>
    </header>
  );
}

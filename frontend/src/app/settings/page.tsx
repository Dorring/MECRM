'use client';

import { useState } from 'react';
import { User, Building, Shield, LogOut, Mail, CheckCircle2, X, AlertTriangle } from 'lucide-react';
import { useAuth } from '../providers';

export default function SettingsPage() {
  const { user, logout } = useAuth();
  const [logoutError, setLogoutError] = useState<string | null>(null);
  const [loggingOut, setLoggingOut] = useState(false);

  const handleLogout = async () => {
    setLogoutError(null);
    setLoggingOut(true);
    try {
      const result = await logout();
      if (result.success) {
        window.location.href = '/login';
      } else {
        setLogoutError(result.error || 'Logout failed');
      }
    } catch {
      setLogoutError('Logout failed — unexpected error');
    } finally {
      setLoggingOut(false);
    }
  };

  return (
    <div className="space-y-6 max-w-3xl">
      {/* Page header */}
      <div>
        <h1 className="text-2xl font-bold text-gray-900 dark:text-white">Settings</h1>
        <p className="text-gray-500 dark:text-gray-400">
          Your account and workspace information
        </p>
      </div>

      {/* User card */}
      <div className="card">
        <div className="flex items-center gap-2 mb-4">
          <User size={18} className="text-primary-600" />
          <h2 className="text-lg font-semibold text-gray-900 dark:text-white">User</h2>
        </div>
        <dl className="divide-y divide-gray-200 dark:divide-gray-700">
          <Row label="Name" value={user?.name} />
          <Row label="Email" value={user?.email} icon={<Mail size={14} className="text-gray-400" />} />
          <Row label="User ID" value={user?.id} mono />
          <Row label="Roles" value={user?.roles?.join(', ') || '—'} />
        </dl>
      </div>

      {/* Tenant card */}
      <div className="card">
        <div className="flex items-center gap-2 mb-4">
          <Building size={18} className="text-primary-600" />
          <h2 className="text-lg font-semibold text-gray-900 dark:text-white">Tenant</h2>
        </div>
        <dl className="divide-y divide-gray-200 dark:divide-gray-700">
          <Row label="Tenant name" value={user?.tenant?.name} />
          <Row label="Tenant ID" value={user?.tenant?.id} mono />
        </dl>
      </div>

      {/* Authorization note — explicit, because local roles are display-only */}
      <div className="card border-l-4 border-primary-500">
        <div className="flex items-start gap-3">
          <Shield size={18} className="text-primary-600 mt-0.5" />
          <div>
            <h3 className="text-sm font-semibold text-gray-900 dark:text-white">
              Authorization
            </h3>
            <p className="text-sm text-gray-500 dark:text-gray-400 mt-1">
              The roles shown here are for display only and reflect the claims in
              your session at sign-in. The API Gateway and OPA policy service are
              the authoritative authorization check on every request; the UI
              never grants access on its own.
            </p>
          </div>
        </div>
      </div>

      {/* Session */}
      <div className="card">
        <div className="flex items-center gap-2 mb-4">
          <CheckCircle2 size={18} className="text-primary-600" />
          <h2 className="text-lg font-semibold text-gray-900 dark:text-white">Session</h2>
        </div>
        <p className="text-sm text-gray-500 dark:text-gray-400 mb-4">
          Signing out asks the gateway to revoke your session tokens.
          You will need to sign in again.
        </p>
        {logoutError && (
          <div
            className="flex items-center gap-2 mb-3 px-3 py-2 bg-red-50 dark:bg-red-900/20 border border-red-200 dark:border-red-800 rounded-md text-sm text-red-700 dark:text-red-300"
            role="alert"
            aria-live="assertive"
          >
            <AlertTriangle size={14} className="shrink-0" />
            <span className="flex-1">{logoutError}</span>
            <button
              onClick={() => setLogoutError(null)}
              className="text-red-400 hover:text-red-600 dark:hover:text-red-200 shrink-0"
              aria-label="Dismiss"
            >
              <X size={14} />
            </button>
          </div>
        )}
        <button
          onClick={handleLogout}
          disabled={loggingOut}
          className="btn btn-secondary"
        >
          <LogOut size={16} className="mr-2" />
          {loggingOut ? 'Signing out…' : 'Sign out'}
        </button>
      </div>
    </div>
  );
}

function Row({
  label,
  value,
  mono,
  icon,
}: {
  label: string;
  value?: string;
  mono?: boolean;
  icon?: React.ReactNode;
}) {
  return (
    <div className="py-3 flex items-center justify-between gap-4">
      <dt className="text-sm text-gray-500 dark:text-gray-400">{label}</dt>
      <dd
        className={`text-sm text-gray-900 dark:text-white text-right truncate max-w-[60%] ${
          mono ? 'font-mono' : ''
        }`}
      >
        <span className="inline-flex items-center gap-1">
          {icon}
          {value || '—'}
        </span>
      </dd>
    </div>
  );
}

'use client';

import { useState, FormEvent, useEffect } from 'react';
import { useRouter } from 'next/navigation';
import { useAuth, getPostLoginRedirect } from '../providers';

interface FieldErrors {
  tenantSlug?: string;
  email?: string;
  password?: string;
}

function validate(values: {
  tenantSlug: string;
  email: string;
  password: string;
}): FieldErrors {
  const errors: FieldErrors = {};
  // Mirror the gateway's express-validator constraints
  // (auth.ts: tenantSlug 2-50 [a-z0-9-], email isEmail, password min 8).
  if (!values.tenantSlug) {
    errors.tenantSlug = 'Tenant slug is required';
  } else if (!/^[a-z0-9-]{2,50}$/.test(values.tenantSlug)) {
    errors.tenantSlug = 'Tenant slug must be 2-50 chars, lowercase letters/digits/hyphens';
  }
  if (!values.email) {
    errors.email = 'Email is required';
  } else if (!/^[^\s@]+@[^\s@]+\.[^\s@]+$/.test(values.email)) {
    errors.email = 'Enter a valid email';
  }
  if (!values.password) {
    errors.password = 'Password is required';
  } else if (values.password.length < 8) {
    errors.password = 'Password must be at least 8 characters';
  }
  return errors;
}

export default function LoginPage() {
  const router = useRouter();
  const { login, isAuthenticated, isLoading } = useAuth();
  const [values, setValues] = useState({ tenantSlug: '', email: '', password: '' });
  const [fieldErrors, setFieldErrors] = useState<FieldErrors>({});
  const [submitError, setSubmitError] = useState<string | null>(null);
  const [submitting, setSubmitting] = useState(false);

  // If a valid session already exists, skip the login form and go to the
  // post-login redirect (or home). This prevents a logged-in user from
  // re-submitting credentials needlessly.
  useEffect(() => {
    if (!isLoading && isAuthenticated) {
      router.replace(getPostLoginRedirect());
    }
  }, [isLoading, isAuthenticated, router]);

  const onChange = (field: keyof typeof values) => (e: React.ChangeEvent<HTMLInputElement>) => {
    const next = { ...values, [field]: e.target.value };
    setValues(next);
    // Clear field-level error on edit; keep submit-level error until retry.
    if (fieldErrors[field]) {
      setFieldErrors((prev) => ({ ...prev, [field]: undefined }));
    }
    if (submitError) setSubmitError(null);
  };

  const onSubmit = async (e: FormEvent) => {
    e.preventDefault();
    setSubmitError(null);

    const validation = validate(values);
    if (Object.keys(validation).length > 0) {
      setFieldErrors(validation);
      return;
    }
    setFieldErrors({});

    setSubmitting(true);
    try {
      await login(values);
      // Redirect to the origin page the user was trying to reach, else home.
      // Use replace so the login page doesn't linger in history.
      router.replace(getPostLoginRedirect());
    } catch (err: any) {
      // The ApiClient normalizes gateway errors. Map known statuses to
      // user-facing messages without echoing any token/PII back.
      const status = err?.status;
      if (status === 401) {
        setSubmitError('Invalid email, password, or tenant.');
      } else if (status === 429) {
        setSubmitError('Too many attempts. Please try again later.');
      } else if (err?.name === 'TimeoutError') {
        setSubmitError('Login timed out. Please try again.');
      } else {
        setSubmitError(err?.message || 'Unable to sign in. Please try again.');
      }
    } finally {
      setSubmitting(false);
    }
  };

  return (
    <div className="min-h-screen flex items-center justify-center bg-gray-50 dark:bg-gray-900 px-4">
      <div className="w-full max-w-md">
        <div className="text-center mb-8">
          <h1 className="text-3xl font-bold text-primary-600">Enterprise CRM</h1>
          <p className="text-sm text-gray-500 dark:text-gray-400 mt-2">
            Sign in to your workspace
          </p>
        </div>

        <div className="card p-8">
          <form onSubmit={onSubmit} className="space-y-4" noValidate>
            <div>
              <label htmlFor="tenantSlug" className="block text-sm font-medium text-gray-700 dark:text-gray-300 mb-1">
                Tenant slug
              </label>
              <input
                id="tenantSlug"
                name="tenantSlug"
                type="text"
                autoComplete="organization"
                autoCapitalize="none"
                spellCheck={false}
                className="input w-full"
                placeholder="acme-corp"
                value={values.tenantSlug}
                onChange={onChange('tenantSlug')}
                aria-invalid={!!fieldErrors.tenantSlug}
                aria-describedby={fieldErrors.tenantSlug ? 'tenantSlug-error' : undefined}
                disabled={submitting}
              />
              {fieldErrors.tenantSlug && (
                <p id="tenantSlug-error" className="text-xs text-red-500 mt-1" role="alert">
                  {fieldErrors.tenantSlug}
                </p>
              )}
            </div>

            <div>
              <label htmlFor="email" className="block text-sm font-medium text-gray-700 dark:text-gray-300 mb-1">
                Email
              </label>
              <input
                id="email"
                name="email"
                type="email"
                autoComplete="email"
                autoCapitalize="none"
                spellCheck={false}
                className="input w-full"
                placeholder="you@acme-corp.com"
                value={values.email}
                onChange={onChange('email')}
                aria-invalid={!!fieldErrors.email}
                aria-describedby={fieldErrors.email ? 'email-error' : undefined}
                disabled={submitting}
              />
              {fieldErrors.email && (
                <p id="email-error" className="text-xs text-red-500 mt-1" role="alert">
                  {fieldErrors.email}
                </p>
              )}
            </div>

            <div>
              <label htmlFor="password" className="block text-sm font-medium text-gray-700 dark:text-gray-300 mb-1">
                Password
              </label>
              <input
                id="password"
                name="password"
                type="password"
                autoComplete="current-password"
                className="input w-full"
                placeholder="••••••••"
                value={values.password}
                onChange={onChange('password')}
                aria-invalid={!!fieldErrors.password}
                aria-describedby={fieldErrors.password ? 'password-error' : undefined}
                disabled={submitting}
              />
              {fieldErrors.password && (
                <p id="password-error" className="text-xs text-red-500 mt-1" role="alert">
                  {fieldErrors.password}
                </p>
              )}
            </div>

            {submitError && (
              <div
                className="text-sm text-red-600 dark:text-red-400 bg-red-50 dark:bg-red-900/20 border border-red-200 dark:border-red-800 rounded-md px-3 py-2"
                role="alert"
                aria-live="assertive"
              >
                {submitError}
              </div>
            )}

            <button
              type="submit"
              className="btn btn-primary w-full"
              disabled={submitting}
              aria-busy={submitting}
            >
              {submitting ? 'Signing in…' : 'Sign in'}
            </button>
          </form>

          <p className="text-xs text-gray-400 dark:text-gray-500 mt-6 text-center">
            Your tenant administrator can provide the tenant slug and credentials.
          </p>
        </div>
      </div>
    </div>
  );
}

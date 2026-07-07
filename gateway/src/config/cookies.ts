import { CookieOptions } from 'express';

export interface CookieConfig {
  refresh: CookieOptions;
  csrf: CookieOptions;
}

/**
 * Derive cookie options from explicit env vars with NODE_ENV fallback.
 *
 * COOKIE_SECURE: explicit true/false > NODE_ENV=production → true > else false
 * COOKIE_SAME_SITE: explicit lax/strict > NODE_ENV=production → strict > else lax
 */
export function getCookieOptions(): CookieConfig {
  const secure =
    process.env.COOKIE_SECURE !== undefined
      ? process.env.COOKIE_SECURE === 'true'
      : process.env.NODE_ENV === 'production';

  let sameSite: 'strict' | 'lax';
  if (process.env.COOKIE_SAME_SITE === 'lax') {
    sameSite = 'lax';
  } else if (process.env.COOKIE_SAME_SITE === 'strict') {
    sameSite = 'strict';
  } else {
    sameSite = process.env.NODE_ENV === 'production' ? 'strict' : 'lax';
  }

  return {
    refresh: {
      httpOnly: true,
      secure,
      sameSite,
      path: '/api/v1/auth',
      maxAge: 604_800_000, // 7 days
    },
    csrf: {
      httpOnly: false,
      secure,
      sameSite,
      path: '/',
      maxAge: 604_800_000,
    },
  };
}

export const REFRESH_COOKIE = 'refresh_token';
export const CSRF_COOKIE = 'csrf_token';
export const CSRF_HEADER = 'x-csrf-token';

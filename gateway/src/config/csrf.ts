import { randomBytes } from 'crypto';
import { Request } from 'express';
import { CSRF_COOKIE, CSRF_HEADER } from './cookies';

/**
 * Generate a 32-byte hex CSRF token.
 */
export function generateCsrfToken(): string {
  return randomBytes(32).toString('hex');
}

/**
 * Validate CSRF double-submit: header value must equal cookie value.
 * Both must be present and identical.
 */
export function validateCsrf(req: Request): boolean {
  const header = req.headers[CSRF_HEADER];
  const cookie = req.cookies?.[CSRF_COOKIE];

  if (typeof header !== 'string' || !header) return false;
  if (typeof cookie !== 'string' || !cookie) return false;

  return header === cookie;
}

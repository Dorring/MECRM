import { logger } from '../utils/logger';

/**
 * Resolve and validate the JWT secret.
 *
 * Security policy:
 * - Production (NODE_ENV === 'production'): JWT_SECRET MUST be provided and MUST
 *   not equal any known insecure default. If missing or insecure, this throws,
 *   which startServer turns into a non-zero exit BEFORE the server accepts
 *   traffic. There is NO default fallback in production.
 * - Development/test: a known insecure default is permitted ONLY for local
 *   convenience, with an explicit loud warning. Tests (JEST_WORKER_ID) are
 *   allowed to omit the secret entirely and receive a deterministic default.
 *
 * IMPORTANT: Never log the secret value. Only log length/preceding-char metadata.
 */

const KNOWN_INSECURE_DEFAULTS = new Set<string>([
  'supersecret',
  'development-secret-change-in-production',
  'your-super-secret-jwt-key-change-in-production',
  'change-me',
  'changeme',
  'secret',
]);

const DEVELOPMENT_FALLBACK = 'development-secret-change-in-production';

function isProd(): boolean {
  return process.env.NODE_ENV === 'production';
}

function isTest(): boolean {
  return process.env.NODE_ENV === 'test' || !!process.env.JEST_WORKER_ID;
}

export function resolveJwtSecret(): string {
  const raw = process.env.JWT_SECRET;
  const trimmed = typeof raw === 'string' ? raw.trim() : '';

  // Production: hard requirement, no fallback.
  if (isProd()) {
    if (!trimmed) {
      throw new Error(
        'JWT_SECRET is not set in production. Refusing to start. Set JWT_SECRET to a strong random value (>= 32 bytes).'
      );
    }
    if (trimmed.length < 32) {
      throw new Error('JWT_SECRET is shorter than 32 bytes in production. Refusing to start.');
    }
    if (KNOWN_INSECURE_DEFAULTS.has(trimmed)) {
      throw new Error(
        'JWT_SECRET matches a known insecure default in production. Refusing to start.'
      );
    }
    return trimmed;
  }

  // Development / test: allow fallback but warn loudly.
  if (!trimmed) {
    if (isTest()) {
      // Tests get a deterministic secret; never used in production.
      return DEVELOPMENT_FALLBACK;
    }
    logger.warn(
      'WARNING: JWT_SECRET is not set. Using insecure development default. DO NOT use in production.'
    );
    return DEVELOPMENT_FALLBACK;
  }

  if (KNOWN_INSECURE_DEFAULTS.has(trimmed)) {
    logger.warn(
      'WARNING: JWT_SECRET matches a known insecure default. Acceptable only for local development.'
    );
  } else if (trimmed.length < 32) {
    logger.warn(
      'WARNING: JWT_SECRET is shorter than 32 bytes. Use a stronger secret for any non-local environment.'
    );
  }

  return trimmed;
}

/**
 * The resolved JWT secret. Resolved exactly once at module load time so the
 * value is stable for the lifetime of the process.
 */
export const JWT_SECRET: string = resolveJwtSecret();


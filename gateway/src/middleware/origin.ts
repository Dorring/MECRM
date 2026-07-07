import { Request, Response, NextFunction } from 'express';

/**
 * Origin validation middleware for auth POST endpoints.
 *
 * Reads the Origin header and compares against ALLOWED_ORIGINS.
 * Missing Origin (same-origin browser or non-browser client) → allow.
 * Disallowed Origin → 403.
 */
export function createOriginValidation() {
  const allowedRaw = process.env.ALLOWED_ORIGINS || '';
  const allowed = allowedRaw
    .split(',')
    .map((o) => o.trim())
    .filter(Boolean);

  return (req: Request, res: Response, next: NextFunction): void => {
    const origin = req.headers.origin;

    // No Origin header: same-origin browser request or non-browser client.
    // Defense-in-depth is handled by CSRF for cookie-bearing requests.
    if (!origin) {
      next();
      return;
    }

    if (allowed.length === 0 || allowed.includes(origin)) {
      next();
      return;
    }

    res.status(403).json({
      error: {
        code: 'ORIGIN_NOT_ALLOWED',
        message: 'Request origin is not allowed',
      },
    });
  };
}

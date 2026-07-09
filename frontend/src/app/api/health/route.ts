/**
 * GET /api/health — Liveness/readiness probe for K8s pod-level probes.
 *
 * Returns 200 immediately. This is a lightweight process-alive check;
 * it does not validate downstream dependencies (gateway, database).
 *
 * Routing note (Compose topology):
 *   In nginx frontend-proxy, `location /api/` proxies to Gateway, so
 *   `GET /api/health` through the proxy reaches Gateway, not this route.
 *   This route is used by:
 *     - K8s pod probes (which hit the container port 3000 directly)
 *     - Local Next.js dev (no nginx proxy)
 *     - Internal container healthchecks (e.g., `wget localhost:3000/api/health`)
 *   The frontend-proxy has its own `location = /health` for Compose healthchecks.
 */
export async function GET() {
  return Response.json({ status: 'ok' });
}

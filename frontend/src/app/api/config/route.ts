/**
 * GET /api/config — runtime URL resolution for local/dev direct mode.
 *
 * This endpoint is served by Next.js (not proxied to Gateway). It reads
 * server-side environment variables and returns the API and WebSocket
 * URLs to the browser.
 *
 * In production (same-origin proxy mode), apiUrl is empty and wsUrl is
 * derived from window.location — this endpoint still returns valid
 * values but the browser falls back to relative defaults.
 *
 * This is NOT a cross-origin cookie auth mechanism.
 */

export async function GET() {
  const apiUrl = process.env.API_URL || '';
  const wsUrl = process.env.WS_URL || 'ws://localhost:4000';

  return Response.json({ apiUrl, wsUrl });
}

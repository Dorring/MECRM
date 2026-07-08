// BASE_URL is empty in same-origin mode (browser uses relative /api/v1/...).
// In local/dev direct mode, runtime-config sets it from /api/config (API_URL).
// Never use NEXT_PUBLIC_* — all API URLs are resolved at runtime, not build time.
let BASE_URL = '';

/** Set the API base URL at runtime. Called by runtime-config boot. */
export function setApiBaseUrl(url: string): void {
  BASE_URL = url.replace(/\/$/, '');
}

const API_PREFIX = '/api/v1';

// ---------------------------------------------------------------------------
// Memory-only access token
// ---------------------------------------------------------------------------
// accessToken lives ONLY in this module-level variable. It is never written
// to localStorage, sessionStorage, or any other persistent store. On page
// reload it is lost and must be recovered via cookie-based refresh (§3 boot
// recovery in ADR-004 plan) or re-login.

let accessToken: string | null = null;

export function getAccessToken(): string | null {
  return accessToken;
}

export function setAccessToken(token: string | null): void {
  accessToken = token;
}

// Clear memory-only access token and legacy localStorage keys.
// Called on logout success and during boot cleanup.
// Does NOT clear authUser cache — that is handled by clearSession().
function clearAccessToken(): void {
  accessToken = null;
  // Remove legacy localStorage keys (pre-C3 migration cleanup)
  if (typeof window !== 'undefined') {
    try { window.localStorage.removeItem('accessToken'); } catch { /* ignore */ }
    try { window.localStorage.removeItem('refreshToken'); } catch { /* ignore */ }
  }
}

// ---------------------------------------------------------------------------
// CSRF double-submit helpers
// ---------------------------------------------------------------------------
// The gateway sets a csrf_token cookie (Path=/, NOT HttpOnly) on login,
// register, refresh, and migrate-cookie. The frontend reads it via
// document.cookie and echoes it back in the X-CSRF-Token header on mutating
// requests. GET/HEAD/OPTIONS do NOT send the header (no side effects).
// This is stateless double-submit: the server compares header === cookie.

export const CSRF_HEADER = 'x-csrf-token';
const CSRF_COOKIE_NAME = 'csrf_token';

export function getCsrfToken(): string | null {
  if (typeof window === 'undefined') return null;
  const match = document.cookie.match(
    new RegExp(`(?:^|;\\s*)${CSRF_COOKIE_NAME}=([^;]*)`)
  );
  return match ? decodeURIComponent(match[1]) : null;
}

/** Methods that require CSRF header injection. */
const MUTATING_METHODS = new Set(['POST', 'PUT', 'PATCH', 'DELETE']);

// Decode a JWT payload WITHOUT verifying the signature. Verification happens
// server-side. We only use the decoded claims for UI display (name, roles,
// tenant) and never for authorization decisions.
export interface DecodedTokenClaims {
  sub: string;
  email?: string;
  tenantId?: string;
  tenant_id?: string;
  roles?: string[];
  iat?: number;
  exp?: number;
  [key: string]: any;
}

export function decodeToken(token: string): DecodedTokenClaims | null {
  try {
    const part = token.split('.')[1];
    if (!part) return null;
    // base64url -> base64 -> JSON
    const normalized = part.replace(/-/g, '+').replace(/_/g, '/');
    const json = decodeURIComponent(
      atob(normalized)
        .split('')
        .map((c) => '%' + ('00' + c.charCodeAt(0).toString(16)).slice(-2))
        .join('')
    );
    return JSON.parse(json) as DecodedTokenClaims;
  } catch {
    return null;
  }
}

// Returns true if a token is present in memory and not past its `exp`. Used
// only to decide whether to show the login page / attempt a request; the
// server still rejects expired/revoked tokens.
export function hasValidAccessToken(): boolean {
  const token = accessToken;
  if (!token) return false;
  const claims = decodeToken(token);
  if (!claims) return false;
  if (typeof claims.exp === 'number') {
    // 5s skew window
    return claims.exp * 1000 > Date.now() - 5000;
  }
  return true;
}

export interface AuthUser {
  id: string;
  email: string;
  name?: string;
  roles: string[];
  tenant: {
    id: string;
    name?: string;
  };
}

// Read the cached user profile written at login time. This is purely a UI
// convenience; role/tenant claims are NOT trusted for authorization.
export function getCachedUser(): AuthUser | null {
  if (typeof window === 'undefined') return null;
  const raw = window.localStorage.getItem('authUser');
  if (!raw) return null;
  try {
    return JSON.parse(raw) as AuthUser;
  } catch {
    return null;
  }
}

function setCachedUser(user: AuthUser): void {
  if (typeof window === 'undefined') return;
  window.localStorage.setItem('authUser', JSON.stringify(user));
}

class TimeoutError extends Error {
  constructor(message = 'Request timed out') {
    super(message);
    this.name = 'TimeoutError';
  }
}

const normalizeEndpoint = (endpoint: string) => {
  if (endpoint.startsWith('http')) return endpoint;
  const path = endpoint.startsWith('/') ? endpoint : `/${endpoint}`;
  if (path.startsWith('/api/')) return path;
  return `${API_PREFIX}${path}`;
};

// ---------------------------------------------------------------------------
// Standalone cookie-based refresh (used by boot recovery in providers.tsx)
// ---------------------------------------------------------------------------
// Same logic as ApiClient.tryRefresh() but callable outside the class.
// Returns the new accessToken on success, null on failure.
export async function tryCookieRefresh(): Promise<string | null> {
  const csrfToken = getCsrfToken();
  if (!csrfToken) return null;
  const controller = new AbortController();
  const timeout = setTimeout(() => controller.abort(), 10000);
  try {
    const resp = await fetch(`${BASE_URL}/api/v1/auth/refresh`, {
      method: 'POST',
      headers: {
        'Content-Type': 'application/json',
        [CSRF_HEADER]: csrfToken,
      },
      credentials: 'include',
      signal: controller.signal,
    });
    if (!resp.ok) return null;
    const body = await resp.json();
    if (!body?.accessToken) return null;
    setAccessToken(body.accessToken);
    return body.accessToken;
  } catch {
    return null;
  } finally {
    clearTimeout(timeout);
  }
}

class ApiClient {
  private refreshing: Promise<boolean> | null = null;

  // Delegates to standalone tryCookieRefresh().
  private async tryRefresh(): Promise<boolean> {
    return (await tryCookieRefresh()) !== null;
  }

  private async request<T>(endpoint: string, options?: RequestInit & { timeoutMs?: number; _retried?: boolean }): Promise<{ data: T }> {
    const normalized = normalizeEndpoint(endpoint);
    const url = `${BASE_URL}${normalized}`;
    const controller = new AbortController();
    const timeout = setTimeout(() => controller.abort(), options?.timeoutMs ?? 10000);
    const token = getAccessToken();
    const method = (options?.method || 'GET').toUpperCase();

    // Build headers: Authorization if token present, CSRF for mutating methods
    const headers: Record<string, string> = {
      'Content-Type': 'application/json',
      ...(token ? { Authorization: token.startsWith('Bearer ') ? token : `Bearer ${token}` } : {}),
      ...options?.headers as Record<string, string> | undefined,
    };

    // Inject CSRF token on POST/PUT/PATCH/DELETE only — never on GET/HEAD/OPTIONS
    if (MUTATING_METHODS.has(method)) {
      const csrfToken = getCsrfToken();
      if (csrfToken) {
        headers[CSRF_HEADER] = csrfToken;
      }
    }

    try {
      const response = await fetch(url, {
        ...options,
        method,
        signal: controller.signal,
        headers,
        credentials: 'include', // send HttpOnly cookies on all requests
      });

      if (!response.ok) {
        // Parse the canonical Gateway error envelope `{ error: { code, message, details? } }`.
        let code: string | undefined;
        let message = `API Error: ${response.status} ${response.statusText}`;
        let details: any;
        try {
          const body = await response.json();
          if (body?.error?.message) {
            message = body.error.message;
            code = body.error.code;
            details = body.error.details;
          } else if (body?.error) {
            message = typeof body.error === 'string' ? body.error : message;
          } else if (body?.message) {
            message = body.message;
          }
        } catch (_) {
          // ignore parse error; keep default statusText message
        }

        // On 401, attempt a single silent refresh+retry for non-auth endpoints.
        // Auth endpoints (/auth/login, /auth/refresh, /auth/logout) are not
        // retried: a 401 there means the credentials are genuinely invalid.
        if (
          response.status === 401 &&
          !options?._retried &&
          !normalized.startsWith('/api/v1/auth/')
        ) {
          // Coalesce concurrent refresh attempts into one.
          this.refreshing ??= this.tryRefresh().finally(() => {
            this.refreshing = null;
          });
          const refreshed = await this.refreshing;
          if (refreshed) {
            return this.request<T>(endpoint, { ...options, _retried: true });
          }
        }

        const err = new Error(message) as Error & { status?: number; code?: string; details?: any };
        err.status = response.status;
        err.code = code;
        err.details = details;
        throw err;
      }

      // Some endpoints might return empty body (e.g. 204)
      if (response.status === 204) {
          return { data: {} as T };
      }

      const data = await response.json();
      return { data };
    } catch (err: any) {
      if (err?.name === 'AbortError') {
        throw new TimeoutError();
      }
      throw err;
    } finally {
      clearTimeout(timeout);
    }
  }

  get<T>(endpoint: string, options?: RequestInit): Promise<{ data: T }> {
    return this.request<T>(endpoint, { ...options, method: 'GET' });
  }

  post<T>(endpoint: string, body: any, options?: RequestInit): Promise<{ data: T }> {
    return this.request<T>(endpoint, {
      ...options,
      method: 'POST',
      body: JSON.stringify(body),
    });
  }

  put<T>(endpoint: string, body: any, options?: RequestInit): Promise<{ data: T }> {
    return this.request<T>(endpoint, {
      ...options,
      method: 'PUT',
      body: JSON.stringify(body),
    });
  }

  patch<T>(endpoint: string, body: any, options?: RequestInit): Promise<{ data: T }> {
    return this.request<T>(endpoint, {
      ...options,
      method: 'PATCH',
      body: JSON.stringify(body),
    });
  }

  delete<T>(endpoint: string, options?: RequestInit): Promise<{ data: T }> {
    return this.request<T>(endpoint, { ...options, method: 'DELETE' });
  }
}

export const api = new ApiClient();

// Interfaces
export interface Approval {
  id: string;
  status: 'pending' | 'approved' | 'rejected' | 'expired';
  requestorType: string;
  actionType: string;
  expiresAt?: string;
  decidedAt?: string;
  decisionReason?: string;
  context?: {
    score?: number;
    confidence: number;
    amount?: number;
    reasoning?: string;
  };
}

export interface Ticket {
  id: string;
  [key: string]: any;
}

export interface Deal {
  id: string;
  [key: string]: any;
}

export interface Customer {
  id: string;
  [key: string]: any;
}

export interface Lead {
  id: string;
  name: string;
  email?: string;
  phone?: string;
  company?: string;
  source?: string;
  status: 'new' | 'contacted' | 'qualified' | 'unqualified' | 'converted';
  score?: number;
  assignedUserId?: string;
  assignedUser?: { id: string; name: string };
  createdAt: string;
  updatedAt: string;
}

export interface KillSwitchStatus {
  tenants?: Record<string, { state: string; [key: string]: any }>;
  [key: string]: any;
}

export interface ProductivityProposal {
  id: string;
  actionType: string;
  targetEntity: string;
  targetId: string;
  priority: 'low' | 'medium' | 'high' | string;
  justification: string;
  drafts?: any;
  status: 'pending' | 'approved' | 'rejected' | string;
  createdAt?: string;
  decidedAt?: string;
  decisionReason?: string;
  signalType?: string;
  signal?: any;
}

export interface Prediction {
  id: string;
  entityType: string;
  entityId: string;
  predictionType: string;
  probability: number;
  riskLevel: 'green' | 'yellow' | 'red' | string;
  explanation: string;
  createdAt?: string;
  modelVersion?: string;
  features?: any;
}

export interface AutomationPolicySummary {
  id: string;
  tenantId: string;
  createdBy: string;
  status: 'draft' | 'simulating' | 'active' | 'paused' | 'disabled' | string;
  nlRuleText: string;
  triggerType: string;
  version: number;
  lastSimulationId?: string | null;
  createdAt?: string;
  updatedAt?: string;
}

export interface AutomationSimulation {
  id: string;
  policyId: string;
  createdAt: string;
  fromTs?: string | null;
  toTs?: string | null;
  result: any;
}

// Helper to create resource APIs
const createServiceApi = (prefix: string) => ({
  list: (params?: any) => {
    const searchParams = new URLSearchParams();
    if (params) {
      Object.entries(params).forEach(([key, value]) => {
        if (value !== undefined && value !== null) {
          searchParams.append(key, String(value));
        }
      });
    }
    const queryString = searchParams.toString();
    return api.get<any>(`${prefix}${queryString ? '?' + queryString : ''}`);
  },
  get: (id: string) => api.get<any>(`${prefix}/${id}`),
  create: (data: any) => api.post<any>(`${prefix}`, data),
  update: (id: string, data: any) => api.patch<any>(`${prefix}/${id}`, data),
  delete: (id: string) => api.delete<any>(`${prefix}/${id}`),
});

// Export specific APIs
export const approvalsApi = {
  ...createServiceApi('/api/v1/approvals'),
  decide: (id: string, decision: 'approved' | 'rejected', reason?: string) => 
    api.post<any>(`/api/v1/approvals/${id}/decide`, { decision, reason }), 
};

export const productivityApi = {
  listProposals: (params?: { status?: 'pending' | 'approved' | 'rejected'; priority?: 'low' | 'medium' | 'high'; limit?: number }) => {
    const searchParams = new URLSearchParams();
    if (params?.status) searchParams.append('status', params.status);
    if (params?.priority) searchParams.append('priority', params.priority);
    if (params?.limit) searchParams.append('limit', String(params.limit));
    const qs = searchParams.toString();
    return api.get<{ data: ProductivityProposal[] }>(`/api/v1/productivity/proposals${qs ? '?' + qs : ''}`);
  },
  decide: (id: string, decision: 'approved' | 'rejected', reason?: string) =>
    api.post<ProductivityProposal>(`/api/v1/productivity/proposals/${id}/decide`, { decision, reason }),
};

export const automationsApi = {
  list: (params?: { status?: string; page?: number; limit?: number }) => {
    const searchParams = new URLSearchParams();
    if (params?.status) searchParams.append('status', params.status);
    if (params?.page) searchParams.append('page', String(params.page));
    if (params?.limit) searchParams.append('limit', String(params.limit));
    const qs = searchParams.toString();
    return api.get<{ data: AutomationPolicySummary[]; pagination: any }>(`/api/v1/automations${qs ? '?' + qs : ''}`);
  },
  get: (id: string) => api.get<any>(`/api/v1/automations/${id}`),
  parse: (nlRuleText: string) => api.post<any>('/api/v1/automations/parse', { nlRuleText }),
  create: (nlRuleText: string) => api.post<any>('/api/v1/automations', { nlRuleText }),
  update: (id: string, nlRuleText: string) => api.put<any>(`/api/v1/automations/${id}`, { nlRuleText }),
  simulate: (id: string, params?: { fromTs?: string; toTs?: string }) => api.post<any>(`/api/v1/automations/${id}/simulate`, params || {}),
  simulations: (id: string, params?: { limit?: number }) => {
    const searchParams = new URLSearchParams();
    if (params?.limit) searchParams.append('limit', String(params.limit));
    const qs = searchParams.toString();
    return api.get<{ data: AutomationSimulation[] }>(`/api/v1/automations/${id}/simulations${qs ? '?' + qs : ''}`);
  },
  requestActivation: (id: string) => api.post<any>(`/api/v1/automations/${id}/request-activation`, {}),
  pause: (id: string) => api.post<any>(`/api/v1/automations/${id}/pause`, {}),
  resume: (id: string) => api.post<any>(`/api/v1/automations/${id}/resume`, {}),
  deactivate: (id: string) => api.post<any>(`/api/v1/automations/${id}/deactivate`, {}),
};

export const customersApi = {
  ...createServiceApi('/api/v1/customers'),
  timeline: (id: string, params?: { limit?: number }) => {
    const searchParams = new URLSearchParams();
    if (params?.limit) searchParams.append('limit', String(params.limit));
    const qs = searchParams.toString();
    return api.get<{ data: any[] }>(`/api/v1/customers/${id}/timeline${qs ? '?' + qs : ''}`);
  },
  profile: (id: string) => api.get<any>(`/api/v1/customers/${id}/profile`),
};

export const predictionsApi = {
  latest: (entityType: string, entityIds: string[]) => {
    const ids = entityIds.filter(Boolean).slice(0, 200).join(',');
    return api.get<{ entityType: string; data: Record<string, Record<string, Prediction>> }>(
      `/api/v1/predictions/latest?entityType=${encodeURIComponent(entityType)}&entityIds=${encodeURIComponent(ids)}`
    );
  },
};

export const dealsApi = {
  ...createServiceApi('/api/v1/deals'),
  updateStage: (id: string, stage: string) => api.put<any>(`/api/v1/deals/${id}/stage`, { stage }),
};

export const governanceApi = {
  ...createServiceApi('/api/v1/governance'),
  killSwitchStatus: () => api.get<KillSwitchStatus>('/api/v1/governance/killswitch/status'),
  decisions: (params?: any) => {
    // Re-implement generic list logic or reuse if accessible? 
    // Just use api.get with params logic or simplified
    const searchParams = new URLSearchParams();
    if (params) Object.entries(params).forEach(([k, v]) => v && searchParams.append(k, String(v)));
    return api.get<any>(`/api/v1/governance/decisions?${searchParams.toString()}`);
  },
  decision: (id: string) => api.get<any>(`/api/v1/governance/decisions/${id}`),
  pauseTenantAgents: (tenantId?: string, reason?: string) => api.post<any>('/api/v1/governance/killswitch/pause', { tenantId, reason }),
  resumeTenantAgents: (tenantId?: string, reason?: string) => api.post<any>('/api/v1/governance/killswitch/resume', { tenantId, reason }),
  emergencyStop: (agentId?: string, reason?: string) => api.post<any>('/api/v1/governance/killswitch/emergency-stop', { agentId, reason }),
};

export const auditApi = {
  search: (data: {
    query: string;
    fromTs?: string;
    toTs?: string;
    agentName?: string;
    actionType?: string;
    status?: string;
    riskLevel?: string;
    limit?: number;
  }) => api.post<any>('/api/v1/audit/search', data),
  policies: () => api.get<any>('/api/v1/audit/policies?format=summary'),
};

export const knowledgeApi = {
  listDrafts: (params?: { status?: 'draft' | 'approved' | 'rejected'; page?: number; limit?: number }) => {
    const searchParams = new URLSearchParams();
    if (params?.status) searchParams.append('status', params.status);
    if (params?.page) searchParams.append('page', String(params.page));
    if (params?.limit) searchParams.append('limit', String(params.limit));
    const qs = searchParams.toString();
    return api.get<any>(`/api/v1/knowledge/drafts${qs ? `?${qs}` : ''}`);
  },
  getDraft: (id: string) => api.get<any>(`/api/v1/knowledge/drafts/${id}`),
  updateDraft: (id: string, data: any) => api.put<any>(`/api/v1/knowledge/drafts/${id}`, data),
  approveDraft: (id: string) => api.post<any>(`/api/v1/knowledge/drafts/${id}/approve`, {}),
  rejectDraft: (id: string, reason?: string) => api.post<any>(`/api/v1/knowledge/drafts/${id}/reject`, { reason }),
  listArticles: (params?: { tag?: string; page?: number; limit?: number }) => {
    const searchParams = new URLSearchParams();
    if (params?.tag) searchParams.append('tag', params.tag);
    if (params?.page) searchParams.append('page', String(params.page));
    if (params?.limit) searchParams.append('limit', String(params.limit));
    const qs = searchParams.toString();
    return api.get<any>(`/api/v1/knowledge/articles${qs ? `?${qs}` : ''}`);
  },
  getArticle: (id: string) => api.get<any>(`/api/v1/knowledge/articles/${id}`),
};

export const leadsApi = createServiceApi('/api/v1/leads');

export const ticketsApi = {
  ...createServiceApi('/api/v1/tickets'),
  resolve: (id: string, resolution: any) => api.post<any>(`/api/v1/tickets/${id}/resolve`, { resolution }),
};

export const replayApi = {
  ...createServiceApi('/api/v1/replay'),
  start: (data: any) => api.post<any>('/api/v1/replay/jobs', data),
  status: (jobId: string) => api.get<any>(`/api/v1/replay/jobs/${jobId}`),
  timeline: (aggregateType: string, aggregateId: string, tenantId: string) =>
    api.get<any>(`/api/v1/replay/timeline?aggregateType=${aggregateType}&aggregateId=${aggregateId}&tenantId=${tenantId}`),
  diff: (jobId: string, fromVersion: number, toVersion: number) =>
    api.get<any>(`/api/v1/replay/jobs/${jobId}/diff?from=${fromVersion}&to=${toVersion}`),
};

// ---------------------------------------------------------------------------
// Auth + session API
// ---------------------------------------------------------------------------

export interface LoginResponse {
  accessToken: string;
  // refreshToken is NOT in the body — it is set as an HttpOnly cookie by the
  // gateway (C2 contract). The frontend never sees or stores the refresh token.
  user: {
    id: string;
    email: string;
    name: string;
    roles: string[];
    tenant: { id: string; name: string };
  };
}

export const authApi = {
  // Gateway contract: { email, password, tenantSlug } -> LoginResponse
  // Refresh token is set as HttpOnly cookie (not in response body).
  login: (payload: { email: string; password: string; tenantSlug: string }) =>
    api.post<LoginResponse>('/api/v1/auth/login', payload),
  register: (payload: {
    tenantName: string;
    tenantSlug: string;
    email: string;
    password: string;
    name: string;
  }) => api.post<LoginResponse>('/api/v1/auth/register', payload),
  // C2 contract: no request body — refresh token is in HttpOnly cookie.
  // Access token from Authorization header identifies the session.
  logout: () =>
    api.post<{ message: string }>('/api/v1/auth/logout', undefined),
};

// Persist the login/migrate response: access token in memory only, user profile
// cached in localStorage for UI display (NOT for authorization decisions).
export function persistSession(login: LoginResponse): AuthUser {
  setAccessToken(login.accessToken);
  // Refresh token is in HttpOnly cookie (set by gateway) — we never see it.
  const user: AuthUser = {
    id: login.user.id,
    email: login.user.email,
    name: login.user.name,
    roles: login.user.roles,
    tenant: { id: login.user.tenant.id, name: login.user.tenant.name },
  };
  setCachedUser(user);
  return user;
}

// Clear client-side session artifacts. Called ONLY after a successful server
// logout (2xx). Does NOT clear on 503/network error — the caller must show
// an error to the user and keep the local session intact.
// Also cleans up any leftover legacy localStorage token keys.
export function clearSession(): void {
  clearAccessToken();
  if (typeof window !== 'undefined') {
    window.localStorage.removeItem('authUser');
  }
}

// ---------------------------------------------------------------------------
// Legacy localStorage migration (one-shot, called at app boot)
// ---------------------------------------------------------------------------
// Before C3, refresh tokens were stored in localStorage. This function reads
// any legacy token, sends it to the /migrate-cookie endpoint, and clears
// localStorage regardless of outcome.
//
// Returns true if migration succeeded (cookie issued, accessToken stored).
// Returns false if there was no legacy token or migration failed.
// Caller should fall back to cookie refresh, then to login.
export async function migrateFromLocalStorage(): Promise<boolean> {
  if (typeof window === 'undefined') return false;

  const legacyRefresh = window.localStorage.getItem('refreshToken');
  if (!legacyRefresh) return false;

  try {
    const resp = await fetch(`${BASE_URL}/api/v1/auth/migrate-cookie`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ refreshToken: legacyRefresh }),
      credentials: 'include', // accept Set-Cookie from gateway
    });

    if (!resp.ok) return false;

    const body = await resp.json();
    if (body?.accessToken) {
      setAccessToken(body.accessToken);
      return true;
    }
    return false;
  } catch {
    return false;
  } finally {
    // Always clear legacy localStorage regardless of migration outcome.
    // This prevents stale tokens from lingering after a failed migration.
    window.localStorage.removeItem('refreshToken');
    window.localStorage.removeItem('accessToken');
    // NOTE: authUser cache is NOT cleared here — it may be needed to restore
    // the user profile after a successful migration.
  }
}

const BASE_URL = (process.env.NEXT_PUBLIC_API_URL || 'http://localhost:4000').replace(/\/$/, '');
const API_PREFIX = '/api/v1';

// Token storage keys. NOTE: storing access/refresh tokens in localStorage is a
// known XSS-risk surface (see project-optimization-plan.md Phase 5 task 2).
// The gateway currently issues JWTs; HttpOnly Secure cookies would be safer
// but require a gateway-side change. This is the interim strategy and does
// not change the authorization model: the backend (Gateway + OPA) remains the
// final authorization authority; the UI must never treat local role claims as
// a security control.
const ACCESS_TOKEN_KEY = 'accessToken';
const REFRESH_TOKEN_KEY = 'refreshToken';

export function getAccessToken(): string | null {
  if (typeof window === 'undefined') return null;
  return window.localStorage.getItem(ACCESS_TOKEN_KEY);
}

export function getRefreshToken(): string | null {
  if (typeof window === 'undefined') return null;
  return window.localStorage.getItem(REFRESH_TOKEN_KEY);
}

function setTokens(accessToken: string, refreshToken: string): void {
  if (typeof window === 'undefined') return;
  window.localStorage.setItem(ACCESS_TOKEN_KEY, accessToken);
  window.localStorage.setItem(REFRESH_TOKEN_KEY, refreshToken);
}

function clearTokens(): void {
  if (typeof window === 'undefined') return;
  window.localStorage.removeItem(ACCESS_TOKEN_KEY);
  window.localStorage.removeItem(REFRESH_TOKEN_KEY);
}

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

// Returns true if a token is present and not past its `exp`. Used only to
// decide whether to show the login page / attempt a request; the server still
// rejects expired/revoked tokens.
export function hasValidAccessToken(): boolean {
  const token = getAccessToken();
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

class ApiClient {
  private refreshing: Promise<boolean> | null = null;

  // Attempt to refresh the access token using the stored refresh token.
  // Returns true on success. Returns false if there is no refresh token, the
  // gateway rejects it, or the request errors. Errors are intentionally
  // swallowed: callers re-throw the original 401 so the UI can redirect to
  // login. The gateway blacklists the old refresh token on successful refresh.
  private async tryRefresh(): Promise<boolean> {
    const refreshToken = getRefreshToken();
    if (!refreshToken) return false;
    const controller = new AbortController();
    const timeout = setTimeout(() => controller.abort(), 10000);
    try {
      const resp = await fetch(`${BASE_URL}/api/v1/auth/refresh`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ refreshToken }),
        signal: controller.signal,
      });
      if (!resp.ok) return false;
      const body = await resp.json();
      if (!body?.accessToken || !body?.refreshToken) return false;
      setTokens(body.accessToken, body.refreshToken);
      return true;
    } catch {
      return false;
    } finally {
      clearTimeout(timeout);
    }
  }

  private async request<T>(endpoint: string, options?: RequestInit & { timeoutMs?: number; _retried?: boolean }): Promise<{ data: T }> {
    const normalized = normalizeEndpoint(endpoint);
    const url = `${BASE_URL}${normalized}`;
    const controller = new AbortController();
    const timeout = setTimeout(() => controller.abort(), options?.timeoutMs ?? 10000);
    const token = getAccessToken();
    const headers = {
      'Content-Type': 'application/json',
      ...(token ? { Authorization: token.startsWith('Bearer ') ? token : `Bearer ${token}` } : {}),
      ...options?.headers,
    };

    try {
      const response = await fetch(url, {
        ...options,
        signal: controller.signal,
        headers,
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
  refreshToken: string;
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
  login: (payload: { email: string; password: string; tenantSlug: string }) =>
    api.post<LoginResponse>('/api/v1/auth/login', payload),
  register: (payload: {
    tenantName: string;
    tenantSlug: string;
    email: string;
    password: string;
    name: string;
  }) => api.post<LoginResponse>('/api/v1/auth/register', payload),
  logout: (refreshToken: string) =>
    api.post<{ message: string }>('/api/v1/auth/logout', { refreshToken }),
};

// Persist the full login response: tokens + cached user profile, so the UI can
// render the header/identity without an extra /auth/me round-trip (none exists
// today). Roles read from here are for display only.
export function persistSession(login: LoginResponse): AuthUser {
  setTokens(login.accessToken, login.refreshToken);
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

// Clear all client-side session artifacts. Always called on logout. We do not
// log token values anywhere.
export function clearSession(): void {
  clearTokens();
  if (typeof window !== 'undefined') {
    window.localStorage.removeItem('authUser');
  }
}

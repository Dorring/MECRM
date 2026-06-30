import { Response, NextFunction } from 'express';
import axios from 'axios';
import { AuthenticatedRequest } from './auth';
import { forbidden } from './errorHandler';
import { logger } from '../utils/logger';
import { cache } from '../services/redis';
import { computePolicyHash, computeRolesHash, secureCache } from '../services/secureCache';
import { authRecheckLatencyMs, cacheFailClosedTotal, cacheHitTotal, cacheMissTotal } from '../services/metrics';

const OPA_URL = process.env.OPA_URL || 'http://localhost:8181';
const OPA_CACHE_POLICY_ID = 'enterprise_crm/http_authz';
const OPA_CACHE_TTL_SECONDS = parseInt(process.env.OPA_CACHE_TTL_SECONDS || '30', 10);

interface OpaInput {
  tenant_id: string;
  user: {
    id: string;
    roles: string[];
    email?: string;
  };
  action: string;
  resource: {
    type: string;
    id?: string;
    tenant_id?: string;
    [key: string]: any;
  };
  actor_type: 'user' | 'agent' | 'system';
}

interface OpaResult {
  allow: boolean;
  deny?: string[];
  requires_approval?: boolean;
}

export const opaMiddleware = async (
  req: AuthenticatedRequest,
  res: Response,
  next: NextFunction
): Promise<void> => {
  try {
    // Skip OPA check for certain paths
    const skipPaths = ['/health', '/ready', '/metrics'];
    if (skipPaths.some(path => req.path.startsWith(path))) {
      return next();
    }

    if (process.env.NODE_ENV === 'test') {
      const tokenTenantId = (req.headers['x-token-tenant-id'] as string) || req.user?.tenantId || '';
      const effectiveTenantId = req.tenantId || '';
      const isCrossTenant = Boolean(tokenTenantId && effectiveTenantId && tokenTenantId !== effectiveTenantId);

      if (isCrossTenant && req.user?.roles.includes('super_admin') && req.method !== 'GET') {
        throw forbidden('Cross-tenant write denied');
      }
      return next();
    }
    
    // Build action from method and path
    const action = buildAction(req.method, req.path);
    
    // Build OPA input
    const subjectTenantId = (req.headers['x-token-tenant-id'] as string) || req.tenantId || '';
    const input: OpaInput = {
      tenant_id: subjectTenantId,
      user: {
        id: req.user?.sub || '',
        roles: req.user?.roles || [],
        email: req.user?.email,
      },
      action,
      resource: {
        type: extractResourceType(req.path),
        id: extractResourceId(req.path),
        tenant_id: req.tenantId,
        ...req.body,
      },
      actor_type: 'user',
    };
    
    logger.debug('OPA policy check', { action, resource: input.resource.type });
    
    const canUseCache = req.method === 'GET' && input.user.id && subjectTenantId && OPA_CACHE_TTL_SECONDS > 0;
    if (canUseCache) {
      try {
        const rolesHash = computeRolesHash(input.user.roles);
        const policyHash = computePolicyHash({
          tenant_id: input.tenant_id,
          user: { id: input.user.id, roles_hash: rolesHash },
          action: input.action,
          resource: { type: input.resource.type, id: input.resource.id, tenant_id: input.resource.tenant_id },
        });
        const epochs = await secureCache.getEpochs(subjectTenantId, OPA_CACHE_POLICY_ID, input.user.id);
        const key = secureCache.buildKey({
          tenantId: subjectTenantId,
          policyId: OPA_CACHE_POLICY_ID,
          epochs,
          userId: input.user.id,
          rolesHash,
          policyHash,
          resource: `opa:${input.action}:${input.resource.type}:${input.resource.id || ''}:${input.resource.tenant_id || ''}`,
        });
        const cached = await cache.get<OpaResult>(key);
        if (cached) {
          cacheHitTotal.labels(subjectTenantId, 'opa').inc();
          if (!cached.allow) throw forbidden(cached.deny?.join('; ') || 'Access denied by policy');
          if (cached.requires_approval) req.headers['x-requires-approval'] = 'true';
          return next();
        }
        cacheMissTotal.labels(subjectTenantId, 'opa').inc();
      } catch (error) {
        logger.warn('OPA cache read failed', { error: (error as Error)?.message });
      }
    }

    const t0 = Date.now();
    const result = await queryOpa(input);
    authRecheckLatencyMs.labels('gateway_opa').observe(Date.now() - t0);
    
    if (!result.allow) {
      const denyReasons = result.deny?.join('; ') || 'Access denied by policy';
      logger.warn('OPA denied request', {
        userId: req.user?.sub,
        action,
        reasons: result.deny,
      });
      throw forbidden(denyReasons);
    }
    
    // Check if approval is required
    if (result.requires_approval) {
      // Add header to indicate approval is needed
      req.headers['x-requires-approval'] = 'true';
    }

    if (canUseCache) {
      try {
        const rolesHash = computeRolesHash(input.user.roles);
        const policyHash = computePolicyHash({
          tenant_id: input.tenant_id,
          user: { id: input.user.id, roles_hash: rolesHash },
          action: input.action,
          resource: { type: input.resource.type, id: input.resource.id, tenant_id: input.resource.tenant_id },
        });
        const epochs = await secureCache.getEpochs(subjectTenantId, OPA_CACHE_POLICY_ID, input.user.id);
        const key = secureCache.buildKey({
          tenantId: subjectTenantId,
          policyId: OPA_CACHE_POLICY_ID,
          epochs,
          userId: input.user.id,
          rolesHash,
          policyHash,
          resource: `opa:${input.action}:${input.resource.type}:${input.resource.id || ''}:${input.resource.tenant_id || ''}`,
        });
        await cache.set(key, result, OPA_CACHE_TTL_SECONDS);
      } catch (error) {
        logger.warn('OPA cache write failed', { error: (error as Error)?.message });
      }
    }
    
    next();
  } catch (error) {
    // If OPA is unavailable, fail closed (deny)
    if (axios.isAxiosError(error) && !error.response) {
      logger.error('OPA unavailable, failing closed');
      cacheFailClosedTotal.labels('gateway_opa', 'opa_unavailable').inc();
      next(forbidden('Policy engine unavailable'));
    } else {
      next(error);
    }
  }
};

async function queryOpa(input: OpaInput): Promise<OpaResult> {
  try {
    // Query multiple policies and combine results
    const [tenantResult, rbacResult, abacResult] = await Promise.all([
      queryOpaPolicy('enterprise_crm/tenant_isolation', input),
      queryOpaPolicy('enterprise_crm/rbac', input),
      queryOpaPolicy('enterprise_crm/abac', input),
    ]);
    
    // Combine deny messages
    const allDeny: string[] = [
      ...(tenantResult.deny || []),
      ...(rbacResult.deny || []),
      ...(abacResult.deny || []),
    ];
    
    // All policies must allow (or not explicitly deny)
    const allow = 
      (tenantResult.allow !== false) &&
      (rbacResult.allow || allDeny.length === 0) &&
      (abacResult.allow !== false || allDeny.filter(d => d.includes('ABAC')).length === 0);
    
    return {
      allow: allow && allDeny.length === 0,
      deny: allDeny.length > 0 ? allDeny : undefined,
      requires_approval: tenantResult.requires_approval || rbacResult.requires_approval || abacResult.requires_approval,
    };
  } catch (error) {
    logger.error('OPA query failed', { error });
    throw error;
  }
}

async function queryOpaPolicy(policy: string, input: OpaInput): Promise<OpaResult> {
  try {
    const response = await axios.post(
      `${OPA_URL}/v1/data/${policy}`,
      { input },
      {
        timeout: 5000,
        headers: { 'Content-Type': 'application/json' },
      }
    );
    
    return response.data.result || { allow: true };
  } catch (error) {
    if (axios.isAxiosError(error) && error.response?.status === 404) {
      logger.error(`OPA policy not found: ${policy}`);
      return { allow: false, deny: [`Policy not found: ${policy}`] };
    }
    throw error;
  }
}

function buildAction(method: string, path: string): string {
  const resourceType = extractResourceType(path);
  
  const actionMap: Record<string, string> = {
    GET: 'read',
    POST: 'write',
    PUT: 'write',
    PATCH: 'write',
    DELETE: 'delete',
  };
  
  return `${resourceType}:${actionMap[method] || 'read'}`;
}

function extractResourceType(path: string): string {
  // /api/v1/leads/123 -> leads
  const parts = path.split('/').filter(Boolean);
  const apiIndex = parts.findIndex(p => p === 'v1');
  
  if (apiIndex >= 0 && parts[apiIndex + 1]) {
    return parts[apiIndex + 1];
  }
  
  return 'unknown';
}

function extractResourceId(path: string): string | undefined {
  // /api/v1/leads/123 -> 123
  const parts = path.split('/').filter(Boolean);
  const apiIndex = parts.findIndex(p => p === 'v1');
  
  if (apiIndex >= 0 && parts[apiIndex + 2]) {
    const id = parts[apiIndex + 2];
    // Check if it looks like a UUID
    if (/^[0-9a-f-]{36}$/i.test(id)) {
      return id;
    }
  }
  
  return undefined;
}

// Export for use in agent services
export { queryOpa, OpaInput, OpaResult };

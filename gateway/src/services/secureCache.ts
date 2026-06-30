import crypto from 'crypto';
import { redisClient } from './redis';

export type CacheEpochs = { tenantEpoch: string; policyEpoch: string; userEpoch: string };

export const computeRolesHash = (roles: string[]): string => {
  const normalized = roles.map(r => r.trim().toLowerCase()).filter(Boolean).sort();
  return crypto.createHash('sha256').update(normalized.join(','), 'utf8').digest('hex');
};

export const computePolicyHash = (input: unknown): string => {
  return crypto.createHash('sha256').update(stableStringify(input), 'utf8').digest('hex');
};

export const secureCache = {
  async getEpochs(tenantId: string, policyId: string, userId: string): Promise<CacheEpochs> {
    const [t, p, u] = await redisClient.mget(
      tenantEpochKey(tenantId),
      policyEpochKey(policyId),
      userEpochKey(tenantId, userId)
    );
    return { tenantEpoch: t || '0', policyEpoch: p || '0', userEpoch: u || '0' };
  },

  async bumpTenantEpoch(tenantId: string): Promise<number> {
    return await redisClient.incr(tenantEpochKey(tenantId));
  },

  async bumpPolicyEpoch(policyId: string): Promise<number> {
    return await redisClient.incr(policyEpochKey(policyId));
  },

  async bumpUserEpoch(tenantId: string, userId: string): Promise<number> {
    return await redisClient.incr(userEpochKey(tenantId, userId));
  },

  buildKey(parts: {
    tenantId: string;
    policyId: string;
    epochs: CacheEpochs;
    userId: string;
    rolesHash: string;
    policyHash: string;
    resource: string;
  }): string {
    return `sc:v1:t:${parts.tenantId}:tv:${parts.epochs.tenantEpoch}:p:${parts.policyId}:pv:${parts.epochs.policyEpoch}:u:${parts.userId}:uv:${parts.epochs.userEpoch}:rh:${parts.rolesHash}:ph:${parts.policyHash}:r:${parts.resource}`;
  },
};

const tenantEpochKey = (tenantId: string): string => `sc:v1:tenant_epoch:${tenantId}`;
const policyEpochKey = (policyId: string): string => `sc:v1:policy_epoch:${policyId}`;
const userEpochKey = (tenantId: string, userId: string): string => `sc:v1:user_epoch:${tenantId}:${userId}`;

const stableStringify = (value: any): string => {
  if (value === null || value === undefined) return 'null';
  if (typeof value !== 'object') return JSON.stringify(value);
  if (Array.isArray(value)) return `[${value.map(v => stableStringify(v)).join(',')}]`;
  const keys = Object.keys(value).sort();
  const body = keys.map(k => `${JSON.stringify(k)}:${stableStringify(value[k])}`).join(',');
  return `{${body}}`;
};

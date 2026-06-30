import Redis from 'ioredis';
import { logger } from '../utils/logger';

const REDIS_URL = process.env.REDIS_URL || 'redis://localhost:6379';

export const redisClient = new Redis(REDIS_URL, {
  maxRetriesPerRequest: 3,
  lazyConnect: true,
  retryStrategy: (times) => {
    if (times > 3) return null;
    return Math.min(times * 100, 3000);
  },
});

redisClient.on('connect', () => {
  logger.info('Redis connected');
});

redisClient.on('error', (error) => {
  logger.error('Redis error', { error: error.message });
});

export const tenantKey = (tenantId: string, key: string): string => {
  return `tenant:${tenantId}:${key}`;
};

// Cache utilities
export const cache = {
  // Get with JSON parse
  async get<T>(key: string): Promise<T | null> {
    const value = await redisClient.get(key);
    if (!value) return null;
    try {
      return JSON.parse(value) as T;
    } catch {
      return value as unknown as T;
    }
  },
  
  // Set with JSON stringify and optional TTL
  async set(key: string, value: any, ttlSeconds?: number): Promise<void> {
    const stringValue = typeof value === 'string' ? value : JSON.stringify(value);
    if (ttlSeconds) {
      await redisClient.setex(key, ttlSeconds, stringValue);
    } else {
      await redisClient.set(key, stringValue);
    }
  },
  
  // Delete key
  async del(key: string): Promise<void> {
    await redisClient.del(key);
  },
  
  // Check if key exists
  async exists(key: string): Promise<boolean> {
    const result = await redisClient.exists(key);
    return result === 1;
  },
  
  // Get or set with callback
  async getOrSet<T>(
    key: string,
    fetchFn: () => Promise<T>,
    ttlSeconds: number
  ): Promise<T> {
    const cached = await this.get<T>(key);
    if (cached !== null) return cached;
    
    const value = await fetchFn();
    await this.set(key, value, ttlSeconds);
    return value;
  },
};

// Rate limiting utilities
export const rateLimiter = {
  async check(key: string, limit: number, windowSeconds: number): Promise<boolean> {
    const current = await redisClient.incr(key);
    
    if (current === 1) {
      await redisClient.expire(key, windowSeconds);
    }
    
    return current <= limit;
  },
  
  async remaining(key: string, limit: number): Promise<number> {
    const current = await redisClient.get(key);
    return Math.max(0, limit - (parseInt(current || '0', 10)));
  },
};

// Session management
export const sessions = {
  async create(userId: string, sessionData: any, ttlSeconds: number = 86400): Promise<string> {
    const sessionId = `session:${userId}:${Date.now()}`;
    await cache.set(sessionId, sessionData, ttlSeconds);
    return sessionId;
  },
  
  async get(sessionId: string): Promise<any> {
    return cache.get(sessionId);
  },
  
  async destroy(sessionId: string): Promise<void> {
    await cache.del(sessionId);
  },
  
  async destroyAllForUser(userId: string): Promise<void> {
    const keys = await redisClient.keys(`session:${userId}:*`);
    if (keys.length > 0) {
      await redisClient.del(...keys);
    }
  },
};

// Distributed locks
export const locks = {
  async acquire(lockName: string, ttlMs: number = 30000): Promise<boolean> {
    const result = await redisClient.set(
      `lock:${lockName}`,
      Date.now(),
      'PX',
      ttlMs,
      'NX'
    );
    return result === 'OK';
  },
  
  async release(lockName: string): Promise<void> {
    await redisClient.del(`lock:${lockName}`);
  },
  
  async withLock<T>(
    lockName: string,
    fn: () => Promise<T>,
    ttlMs: number = 30000
  ): Promise<T> {
    const acquired = await this.acquire(lockName, ttlMs);
    if (!acquired) {
      throw new Error(`Failed to acquire lock: ${lockName}`);
    }
    
    try {
      return await fn();
    } finally {
      await this.release(lockName);
    }
  },
};

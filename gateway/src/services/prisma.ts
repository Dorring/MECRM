import { PrismaPg } from '@prisma/adapter-pg';
import { PrismaClient, Prisma } from '../generated/prisma/client';
import { logger } from '../utils/logger';

const connectionString = process.env.DATABASE_URL;
if (!connectionString) {
  throw new Error('DATABASE_URL is required to initialize Prisma');
}

const adapter = new PrismaPg({
  connectionString,
  connectionTimeoutMillis: 5000,
  idleTimeoutMillis: 300000,
});

// Create Prisma client with logging
export const prisma = new PrismaClient({
  adapter,
  log: [
    { level: 'query', emit: 'event' },
    { level: 'error', emit: 'stdout' },
    { level: 'warn', emit: 'stdout' },
  ],
});

if (process.env.JEST_WORKER_ID) {
  const cleanupGlobal = globalThis as typeof globalThis & {
    __gatewayTestCleanups?: Array<() => void | Promise<void>>;
  };
  cleanupGlobal.__gatewayTestCleanups ??= [];
  cleanupGlobal.__gatewayTestCleanups.push(() => prisma.$disconnect());
}

// Log slow queries in development
if (process.env.NODE_ENV !== 'production') {
  prisma.$on('query' as never, (e: { duration: number; query: string }) => {
    if (e.duration > 100) {
      logger.warn('Slow query detected', {
        query: e.query,
        duration: `${e.duration}ms`,
      });
    }
  });
}

const UUID_REGEX = /^[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/i;

export type TenantDbClient = Prisma.TransactionClient;

export const withTenantDb = async <T>(
  tenantId: string,
  fn: (db: TenantDbClient) => Promise<T>
): Promise<T> => {
  if (!tenantId || !UUID_REGEX.test(tenantId)) {
    throw new Error('Missing or invalid tenant_id');
  }

  return prisma.$transaction(async (db: Prisma.TransactionClient) => {
    await db.$executeRaw`SELECT set_config('app.tenant_id', ${tenantId}, true)`;
    return fn(db);
  });
};

// Graceful shutdown
process.on('beforeExit', async () => {
  await prisma.$disconnect();
});

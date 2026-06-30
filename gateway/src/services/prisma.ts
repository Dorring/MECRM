import { PrismaClient, Prisma } from '@prisma/client';
import { logger } from '../utils/logger';

// Create Prisma client with logging
export const prisma = new PrismaClient({
  log: [
    { level: 'query', emit: 'event' },
    { level: 'error', emit: 'stdout' },
    { level: 'warn', emit: 'stdout' },
  ],
});

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
    await db.$executeRaw`SET LOCAL app.tenant_id = ${tenantId}`;
    return fn(db);
  });
};

// Graceful shutdown
process.on('beforeExit', async () => {
  await prisma.$disconnect();
});

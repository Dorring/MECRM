import 'dotenv/config';
import { defineConfig } from 'prisma/config';

// Prisma loads config for generate as well as migrate. Image builds do not
// connect to a database, so use a non-secret placeholder only when the runtime
// or migration environment has not supplied DATABASE_URL.
const databaseUrl =
  process.env.DATABASE_URL ??
  'postgresql://prisma:prisma@localhost:5432/prisma';

export default defineConfig({
  schema: 'prisma/schema.prisma',
  migrations: {
    path: 'prisma/migrations',
    seed: 'ts-node src/scripts/seed.ts',
  },
  datasource: {
    url: databaseUrl,
  },
});
import { afterAll } from '@jest/globals';

type TestCleanup = () => void | Promise<void>;

interface CleanupGlobal {
  __gatewayTestCleanups?: TestCleanup[];
}

afterAll(async () => {
  const cleanupGlobal = globalThis as typeof globalThis & CleanupGlobal;
  const cleanups = cleanupGlobal.__gatewayTestCleanups ?? [];
  cleanupGlobal.__gatewayTestCleanups = [];

  const results = await Promise.allSettled(
    cleanups.reverse().map((cleanup) => cleanup()),
  );
  const failures = results.filter(
    (result): result is PromiseRejectedResult => result.status === 'rejected',
  );
  if (failures.length > 0) {
    throw new AggregateError(
      failures.map((failure) => failure.reason),
      'Gateway test dependency cleanup failed',
    );
  }
});

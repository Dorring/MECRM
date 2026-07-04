import { ChildProcess, fork } from 'child_process';
import { randomUUID } from 'crypto';
import path from 'path';
import jwt from 'jsonwebtoken';
import Redis from 'ioredis';
import WebSocket from 'ws';
import { authKeys } from '../services/authSession';

const describeRedis =
  process.env.CRM_REDIS_AVAILABLE === '1' ? describe : describe.skip;
const redisUrl = process.env.REDIS_URL || 'redis://localhost:6379';
const jwtSecret = process.env.JWT_SECRET || 'development-secret-change-in-production';

interface ChildMessage {
  type: 'READY' | 'REVOKED' | 'ERROR';
  port?: number;
  message?: string;
}

function startGatewayProcess(): ChildProcess {
  return fork(
    path.join(__dirname, 'helpers', 'ws_gateway_process.ts'),
    [],
    {
      execArgv: ['-r', 'ts-node/register/transpile-only'],
      env: {
        ...process.env,
        NODE_ENV: 'test',
        JEST_WORKER_ID: '',
        JWT_SECRET: jwtSecret,
        REDIS_URL: redisUrl,
      },
      silent: true,
    },
  );
}

function waitForMessage(
  child: ChildProcess,
  expected: ChildMessage['type'],
  timeoutMs = 10000,
): Promise<ChildMessage> {
  return new Promise((resolve, reject) => {
    const timeout = setTimeout(() => {
      cleanup();
      reject(new Error(`Timed out waiting for child message ${expected}`));
    }, timeoutMs);

    const onMessage = (message: ChildMessage) => {
      if (message.type === 'ERROR') {
        cleanup();
        reject(new Error(message.message || 'Gateway child failed'));
      } else if (message.type === expected) {
        cleanup();
        resolve(message);
      }
    };
    const onExit = (code: number | null) => {
      cleanup();
      reject(new Error(`Gateway child exited early with code ${code}`));
    };
    const cleanup = () => {
      clearTimeout(timeout);
      child.off('message', onMessage);
      child.off('exit', onExit);
    };

    child.on('message', onMessage);
    child.on('exit', onExit);
  });
}

async function stopChild(child: ChildProcess): Promise<void> {
  if (child.exitCode !== null || child.killed) return;
  child.send({ type: 'SHUTDOWN' });
  await new Promise<void>((resolve) => {
    const timeout = setTimeout(() => {
      child.kill();
      resolve();
    }, 5000);
    child.once('exit', () => {
      clearTimeout(timeout);
      resolve();
    });
  });
}

describeRedis('WebSocket revocation across Gateway processes', () => {
  it('revocation published by Gateway A closes a socket on Gateway B', async () => {
    const redis = new Redis(redisUrl);
    const gatewayA = startGatewayProcess();
    const gatewayB = startGatewayProcess();
    let socket: WebSocket | undefined;
    let cleanupKey: string | undefined;

    try {
      const [readyA, readyB] = await Promise.all([
        waitForMessage(gatewayA, 'READY'),
        waitForMessage(gatewayB, 'READY'),
      ]);
      expect(readyA.port).toBeGreaterThan(0);
      expect(readyB.port).toBeGreaterThan(0);

      const now = Math.floor(Date.now() / 1000);
      const tenantId = randomUUID();
      const userId = randomUUID();
      const sid = randomUUID();
      cleanupKey = authKeys.revokedSid(tenantId, sid);
      const sexp = now + 3600;
      const token = jwt.sign(
        {
          jti: randomUUID(),
          sid,
          sub: userId,
          tenantId,
          type: 'access',
          uv: 0,
          sexp,
          roles: ['admin'],
          email: 'ws-test@example.com',
        },
        jwtSecret,
        { algorithm: 'HS256', expiresIn: 600 },
      );

      socket = new WebSocket(
        `ws://127.0.0.1:${readyB.port}/ws?token=${encodeURIComponent(token)}`,
      );
      await new Promise<void>((resolve, reject) => {
        const timeout = setTimeout(
          () => reject(new Error('WebSocket connection timed out')),
          5000,
        );
        socket?.once('message', (data) => {
          const message = JSON.parse(data.toString());
          if (message.type === 'connected') {
            clearTimeout(timeout);
            resolve();
          }
        });
        socket?.once('error', reject);
      });

      const closed = new Promise<number>((resolve, reject) => {
        const timeout = setTimeout(
          () => reject(new Error('Remote WebSocket was not revoked')),
          5000,
        );
        socket?.once('close', (code) => {
          clearTimeout(timeout);
          resolve(code);
        });
      });

      gatewayA.send({ type: 'REVOKE_SID', tenantId, sid, sexp });
      await waitForMessage(gatewayA, 'REVOKED');
      await expect(closed).resolves.toBe(4401);
    } finally {
      socket?.terminate();
      await Promise.all([stopChild(gatewayA), stopChild(gatewayB)]);
      if (cleanupKey) await redis.del(cleanupKey);
      redis.disconnect();
    }
  }, 30000);
});

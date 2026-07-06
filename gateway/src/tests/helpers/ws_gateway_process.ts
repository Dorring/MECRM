import { createServer } from 'http';
import Redis from 'ioredis';
import { WebSocketServer } from 'ws';
import { TokenRevocationService } from '../../services/authSession';
import {
  closeConnectionsByEvent,
  setupWebSocket,
} from '../../services/websocket';

interface ParentCommand {
  type: 'REVOKE_SID' | 'SHUTDOWN';
  tenantId?: string;
  sid?: string;
  sexp?: number;
}

async function main(): Promise<void> {
  const redisUrl = process.env.REDIS_URL;
  if (!redisUrl) throw new Error('REDIS_URL is required');

  const commandClient = new Redis(redisUrl);
  const subscriber = commandClient.duplicate();
  const revocationService = new TokenRevocationService(
    commandClient,
    subscriber,
  );

  const server = createServer();
  const wss = new WebSocketServer({ server, path: '/ws' });
  setupWebSocket(wss, revocationService);
  await revocationService.initSubscriber(closeConnectionsByEvent);

  server.listen(0, '127.0.0.1', () => {
    const address = server.address();
    if (!address || typeof address === 'string') {
      throw new Error('Unable to determine WebSocket test port');
    }
    process.send?.({ type: 'READY', port: address.port });
  });

  process.on('message', async (command: ParentCommand) => {
    if (command.type === 'REVOKE_SID') {
      if (!command.tenantId || !command.sid || !command.sexp) {
        process.send?.({ type: 'ERROR', message: 'Invalid revoke command' });
        return;
      }
      await revocationService.revokeSid(
        command.tenantId,
        command.sid,
        command.sexp,
      );
      process.send?.({ type: 'REVOKED' });
      return;
    }

    if (command.type === 'SHUTDOWN') {
      for (const client of wss.clients) {
        client.terminate();
      }
      await new Promise<void>((resolve) => wss.close(() => resolve()));
      await new Promise<void>((resolve) => server.close(() => resolve()));
      await revocationService.shutdown();
      commandClient.disconnect();
      process.exit(0);
    }
  });
}

main().catch((error) => {
  process.send?.({
    type: 'ERROR',
    message: error instanceof Error ? error.message : String(error),
  });
  process.exit(1);
});

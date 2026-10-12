import type { ChildProcess } from 'node:child_process';
import { EventEmitter } from 'node:events';
import type { Server } from 'node:http';
import express from 'express';
import { afterEach, describe, expect, it, vi } from 'vitest';

import { openBrowser } from '../src/browser/browser-open.js';
import { isLocalSameOrigin } from '../src/origin-validation.js';
import { registerMediaRoutes } from '../src/routes/media.js';

const servers: Server[] = [];
afterEach(async () => {
  await Promise.all(servers.splice(0).map((server) => new Promise<void>((resolve, reject) => {
    server.close((error) => error ? reject(error) : resolve());
  })));
});

async function startServer(env: NodeJS.ProcessEnv = {}) {
  const child = new EventEmitter() as ChildProcess;
  child.unref = vi.fn() as ChildProcess['unref'];
  const spawn = vi.fn((_command: string, _args: string[], _options: unknown) => child);
  const opener = vi.fn((url: string) => openBrowser(url, { platform: 'linux', env, spawn }));
  const port = { current: 0 };
  const dataDir = process.env.OD_DATA_DIR!;
  const deps = {
    db: {}, design: {},
    http: {
      isLocalSameOrigin, resolvedPortRef: port,
      requireLocalDaemonRequest: (_req: unknown, _res: unknown, next: () => void) => next(),
    },
    paths: { PROJECT_ROOT: dataDir, PROJECTS_DIR: dataDir, RUNTIME_DATA_DIR: dataDir },
    ids: {}, auth: {}, media: {}, appConfig: {}, orbit: {},
    nativeDialogs: { openBrowser: opener },
    projectStore: {}, projectFiles: {}, conversations: {}, research: {},
  } as unknown as Parameters<typeof registerMediaRoutes>[1];
  const app = express();
  app.use(express.json());
  registerMediaRoutes(app, deps);
  const server = app.listen(0, '127.0.0.1');
  servers.push(server);
  await new Promise<void>((resolve) => server.once('listening', resolve));
  const address = server.address();
  if (!address || typeof address === 'string') throw new Error('server did not bind');
  port.current = address.port;
  const url = `http://127.0.0.1:${address.port}`;
  return {
    opener, spawn,
    post: (target: string, origin = url) => fetch(`${url}/api/system/open-external`, {
      method: 'POST',
      headers: { 'content-type': 'application/json', origin },
      body: JSON.stringify({ url: target }),
    }),
    wrongPortOrigin: `http://127.0.0.1:${address.port === 65535 ? address.port - 1 : address.port + 1}`,
  };
}

describe('system external URL route', () => {
  it('returns ok false for headless Linux so the web client can navigate', async () => {
    const route = await startServer();
    const response = await route.post('https://omnigent.example/');

    expect(response.status).toBe(200);
    expect(await response.json()).toEqual({ ok: false });
    expect(route.opener).toHaveBeenCalledOnce();
    expect(route.spawn).not.toHaveBeenCalled();
  });

  it.each(['not a URL', 'javascript:alert(1)', 'file:///example'])('rejects invalid target %s before opening', async (target) => {
    const route = await startServer();
    expect((await route.post(target)).status).toBe(400);
    expect(route.opener).not.toHaveBeenCalled();
    expect(route.spawn).not.toHaveBeenCalled();
  });

  it.each(['foreign-origin', 'wrong-port'])('rejects %s before opening', async (kind) => {
    const route = await startServer();
    const origin = kind === 'wrong-port' ? route.wrongPortOrigin : 'https://foreign.example';
    expect((await route.post('https://omnigent.example/', origin)).status).toBe(403);
    expect(route.opener).not.toHaveBeenCalled();
    expect(route.spawn).not.toHaveBeenCalled();
  });

  it('retains desktop success for an authorized HTTPS URL', async () => {
    const route = await startServer({ DISPLAY: ':0' });
    const response = await route.post('https://omnigent.example/');
    expect(response.status).toBe(200);
    expect(await response.json()).toEqual({ ok: true });
    expect(route.spawn).toHaveBeenCalledWith('xdg-open', ['https://omnigent.example/'], {
      detached: true, stdio: 'ignore',
    });
  });
});

// WoowTech built-in launcher plugins (`od.launch`): the two bundled
// launchers register on every boot, keep their launch path through the
// installed-record round trip, and are refused by every apply path.

import express from 'express';
import type { AddressInfo } from 'node:net';
import { cp, mkdtemp, rm } from 'node:fs/promises';
import os from 'node:os';
import path from 'node:path';
import Database from 'better-sqlite3';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import {
  InstalledPluginRecordSchema,
  builtInLaunchPath,
} from '@open-design/contracts';
import { migratePlugins } from '../src/plugins/persistence.js';
import { listInstalledPlugins } from '../src/plugins/registry.js';
import { registerBundledPlugins } from '../src/plugins/bundled.js';
import { resolvePluginSnapshot } from '../src/plugins/resolve-snapshot.js';
import { registerPluginRoutes } from '../src/routes/plugins/index.js';

const WOOW_TIER = path.resolve(import.meta.dirname, '../../../plugins/_official/woow');

let db: Database.Database;
let tmpRoot: string;
const servers: Array<ReturnType<express.Express['listen']>> = [];

beforeEach(async () => {
  tmpRoot = await mkdtemp(path.join(os.tmpdir(), 'od-launchers-'));
  db = new Database(':memory:');
  db.exec(`
    CREATE TABLE projects (id TEXT PRIMARY KEY, name TEXT);
    CREATE TABLE conversations (id TEXT PRIMARY KEY, project_id TEXT, title TEXT);
  `);
  migratePlugins(db);
});

afterEach(async () => {
  db.close();
  await rm(tmpRoot, { recursive: true, force: true });
  await Promise.all(
    servers.splice(0).map((server) => new Promise<void>((resolve, reject) => {
      server.close((error) => error ? reject(error) : resolve());
    })),
  );
});

async function registerWoowTier() {
  const bundledRoot = path.join(tmpRoot, '_official');
  await cp(WOOW_TIER, path.join(bundledRoot, 'woow'), { recursive: true });
  return registerBundledPlugins({ db, bundledRoot });
}

describe('bundled built-in launchers', () => {
  it('registers MCP Server and CLI Terminal as bundled launchers', async () => {
    const result = await registerWoowTier();
    expect(result.warnings).toEqual([]);

    const byId = new Map(listInstalledPlugins(db).map((plugin) => [plugin.id, plugin]));
    const mcp = byId.get('woow-mcp-server');
    const cli = byId.get('woow-cli-terminal');
    expect(mcp?.sourceKind).toBe('bundled');
    expect(cli?.sourceKind).toBe('bundled');
    expect(builtInLaunchPath(mcp!)).toBe('/mcp-admin/');
    expect(builtInLaunchPath(cli!)).toBe('/console/');
    expect(mcp?.title).toBe('MCP Server');
    expect(cli?.manifest.title_i18n).toMatchObject({
      en: 'CLI Terminal',
      'zh-CN': 'CLI 终端',
      'zh-TW': 'CLI 終端機',
    });
    // Launchers carry no prompt or skill context.
    expect(cli?.manifest.od?.context).toBeUndefined();
    expect(cli?.manifest.compat).toBeUndefined();
  });

  it('keeps od.launch through the installed-record contract the web receives', async () => {
    await registerWoowTier();
    const wire = JSON.parse(JSON.stringify(listInstalledPlugins(db)));
    const parsed = wire.map((row: unknown) => InstalledPluginRecordSchema.parse(row));
    const mcp = parsed.find((plugin: { id: string }) => plugin.id === 'woow-mcp-server');
    expect(mcp?.manifest.od?.launch).toEqual({ path: '/mcp-admin/' });
  });

  it('refuses to build an applied-plugin snapshot for a launcher', async () => {
    await registerWoowTier();
    const result = resolvePluginSnapshot({
      db,
      body: { pluginId: 'woow-cli-terminal' },
      projectId: 'project-1',
      registry: { skills: [], designSystems: [], craft: [], atoms: [] },
    } as unknown as Parameters<typeof resolvePluginSnapshot>[0]);
    expect(result).toMatchObject({
      ok: false,
      status: 409,
      body: { error: { code: 'plugin-not-applicable' } },
    });
  });

  it('rejects POST /api/plugins/:id/apply for a launcher without applying it', async () => {
    await registerWoowTier();
    const launcher = listInstalledPlugins(db).find((plugin) => plugin.id === 'woow-mcp-server');
    const applyPlugin = vi.fn();
    const middleware: express.RequestHandler = (_req, _res, next) => next();
    const app = express();
    app.use(express.json());
    registerPluginRoutes(app, {
      db: {
        prepare: () => ({ all: () => [], get: () => null, run: () => undefined }),
        transaction: (run: () => unknown) => () => run(),
      },
      paths: { PROJECTS_DIR: '', PLUGIN_REGISTRY_ROOTS: [], PLUGIN_LOCKFILE_PATH: '' },
      ids: { randomId: () => 'unused' },
      projectStore: {},
      conversations: {},
      plugins: {
        getInstalledPlugin: () => launcher,
        getWorkspacePlugin: async () => launcher,
        listInstalledPlugins: () => [launcher],
        applyPlugin,
        MissingInputError: class MissingInputError extends Error {
          fields: string[] = [];
        },
      },
      helpers: {
        requireLocalDaemonRequest: middleware,
        pluginUpload: { single: () => middleware, array: () => middleware },
        loadPluginRegistryView: async () => ({}),
        buildConnectorProbe: () => ({}),
        connectorService: {},
        sendApiError: (res: express.Response, status: number, code: string, message: string) =>
          res.status(status).json({ error: { code, message } }),
      },
    } as unknown as Parameters<typeof registerPluginRoutes>[1]);
    const server = app.listen(0, '127.0.0.1');
    servers.push(server);
    await new Promise<void>((resolve) => server.once('listening', resolve));
    const { port } = server.address() as AddressInfo;

    const response = await fetch(`http://127.0.0.1:${port}/api/plugins/woow-mcp-server/apply`, {
      method: 'POST',
      headers: { 'content-type': 'application/json' },
      body: '{}',
    });
    expect(response.status).toBe(409);
    expect(await response.json()).toMatchObject({ error: { code: 'plugin-not-applicable' } });
    expect(applyPlugin).not.toHaveBeenCalled();
  });
});

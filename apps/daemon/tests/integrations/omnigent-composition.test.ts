import express from 'express';
import type { Server } from 'node:http';
import { mkdtemp, readFile, rm, writeFile } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import path from 'node:path';
import { afterEach, beforeEach, describe, expect, it, vi, type Mock } from 'vitest';
import type { InstalledPluginRecord, OmnigentIntegrationStatus } from '@open-design/contracts';
import { closeDatabase, deleteConversation, ensureWorkspaceResource, getWorkspaceProjectByProjectId, listMessages, openDatabase } from '../../src/db.js';
import { apiTokenAuthorizationMatches } from '../../src/api-token-auth.js';
import { deleteInstalledPlugin, getInstalledPlugin, listInstalledPlugins, upsertInstalledPlugin } from '../../src/plugins/registry.js';
import { createAuthorizeOmnigentRequest, createOmnigentInstallationLookup, createOmnigentOnboarding } from '../../src/integrations/omnigent/onboarding.js';
import { OMNIGENT_PLUGIN_ID, OmnigentIntegrationService, type OmnigentServiceOptions } from '../../src/integrations/omnigent/service.js';
import { registerOmnigentRoutes } from '../../src/routes/omnigent.js';

let root: string;
let db: ReturnType<typeof openDatabase>;
let server: Server | null;
let url: string;
let service: OmnigentIntegrationService;
let adapter: Mock<NonNullable<OmnigentServiceOptions['adapter']>>;
let teamPlugin: InstalledPluginRecord | null;

const workspaceHeaders = (workspaceId = 'workspace-a', workspaceMemberId = 'member-a') => ({
  authorization: 'Bearer native-fixture',
  'x-od-workspace-id': workspaceId,
  'x-od-workspace-member-id': workspaceMemberId,
  'x-od-workspace-type': 'team',
});

beforeEach(async () => {
  root = await mkdtemp(path.join(tmpdir(), 'od-omnigent-composition-'));
  db = openDatabase(root, { dataDir: root });
  teamPlugin = null;
  const now = Date.now();
  upsertInstalledPlugin(db, {
    id: OMNIGENT_PLUGIN_ID, title: 'Omnigent', version: '1.0.0', sourceKind: 'local',
    source: 'fixture', fsPath: root, installedAt: now, updatedAt: now, trust: 'trusted',
    capabilitiesGranted: ['prompt'], manifest: { name: OMNIGENT_PLUGIN_ID, version: '1.0.0' },
  } as InstalledPluginRecord);
  ensureWorkspaceResource(db, 'plugin', 'workspace-a', OMNIGENT_PLUGIN_ID, {
    createdByWorkspaceMemberId: 'member-a', visibility: 'personal', resourceState: 'active',
  });
  const onboarding = createOmnigentOnboarding({ db, projectsDir: path.join(root, 'projects') });
  let teamReads = 0;
  const getPlugin = createOmnigentInstallationLookup(async context => {
    if (teamPlugin && context?.workspaceId === 'workspace-a' && context.workspaceMemberId === 'member-a') {
      // Native Team folder resolution restamps these fields on every read.
      return { ...teamPlugin, installedAt: 1000 + ++teamReads, updatedAt: 1000 + teamReads };
    }
    return listInstalledPlugins(db, context?.workspaceId ?? null, context?.workspaceMemberId ?? null)
      .find(plugin => plugin.id === OMNIGENT_PLUGIN_ID) ?? null;
  });
  adapter = vi.fn<NonNullable<OmnigentServiceOptions['adapter']>>(async () => ({ ok: true, data: { installed: true, host: { connected: false }, engines: {} } }));
  service = new OmnigentIntegrationService({
    dataDir: root, getPlugin: scope => getPlugin(onboarding.context(scope)),
    createOnboarding: onboarding.create, adapter,
  });
  const app = express();
  app.use(express.json());
  const resolvedPortRef = { current: 0 };
  registerOmnigentRoutes(app, {
    service, origin: { resolvedPortRef },
    authorize: createAuthorizeOmnigentRequest({
      origin: { resolvedPortRef }, onboarding,
      authorizeNativeRequest: req => apiTokenAuthorizationMatches(req.get('authorization'), 'native-fixture'),
      getPlugin,
    }),
  });
  server = await new Promise<Server>(resolve => { const listening = app.listen(0, '127.0.0.1', () => resolve(listening)); });
  const address = server.address();
  if (!address || typeof address === 'string') throw new Error('missing test address');
  resolvedPortRef.current = address.port;
  url = `http://127.0.0.1:${address.port}`;
});

afterEach(async () => {
  server?.closeAllConnections();
  if (server?.listening) await new Promise<void>(resolve => server!.close(() => resolve()));
  server = null;
  closeDatabase();
  await rm(root, { recursive: true, force: true });
});

describe('Omnigent native route composition', () => {
  it('registers the native status/action routes and returns one scoped onboarding pair on repeated enable', async () => {
    const status = await fetch(`${url}/api/integrations/omnigent/status`, { headers: workspaceHeaders() });
    expect(status.status).toBe(200);
    expect(status.headers.get('cache-control')).toBe('no-store');
    expect(await status.json()).toMatchObject({ installed: true, loginConfirmed: false });
    const enable = () => fetch(`${url}/api/integrations/omnigent/actions`, {
      method: 'POST', headers: { ...workspaceHeaders(), 'content-type': 'application/json', origin: url },
      body: JSON.stringify({ action: 'enable' }),
    });
    const firstResponse = await enable();
    const first = await firstResponse.json() as { status: OmnigentIntegrationStatus };
    expect(firstResponse.status).toBe(200);
    expect(first.status.onboarding).toBeDefined();
    const second = await (await enable()).json() as { status: OmnigentIntegrationStatus };
    expect(second.status.onboarding).toEqual(first.status.onboarding);
    expect(getWorkspaceProjectByProjectId(db, first.status.onboarding!.projectId)).toMatchObject({
      workspaceId: 'workspace-a', createdByWorkspaceMemberId: 'member-a', visibility: 'personal', syncState: 'local_only',
    });
  });

  it('does not expose another workspace or member installation, onboarding, or approval', async () => {
    for (const headers of [workspaceHeaders('workspace-b', 'member-a'), workspaceHeaders('workspace-a', 'member-b'),
      { authorization: 'Bearer native-fixture' }]) {
      const status = await fetch(`${url}/api/integrations/omnigent/status`, { headers });
      expect(status.status).toBe(200);
      const body = await status.json();
      expect(body).toMatchObject({ installed: false, loginConfirmed: false, verification: {} });
      expect(body).not.toHaveProperty('onboarding');
      const denied = await fetch(`${url}/api/integrations/omnigent/actions`, {
        method: 'POST', headers: { ...headers, 'content-type': 'application/json' }, body: JSON.stringify({ action: 'enable' }),
      });
      expect(denied.status).toBe(409);
      expect(await denied.json()).toMatchObject({ error: { code: 'PLUGIN_NOT_INSTALLED' } });
    }
    expect(adapter).not.toHaveBeenCalled();
  });

  it('rejects unauthenticated native requests and cross-origin actions before calling the adapter', async () => {
    const noAuth = await fetch(`${url}/api/integrations/omnigent/status`, { headers: { 'x-od-workspace-id': 'workspace-a', 'x-od-workspace-member-id': 'member-a' } });
    expect(noAuth.status).toBe(401);
    expect(await noAuth.json()).toMatchObject({ error: { code: 'API_TOKEN_REQUIRED' } });
    const crossed = await fetch(`${url}/api/integrations/omnigent/actions`, {
      method: 'POST', headers: { ...workspaceHeaders(), 'content-type': 'application/json', origin: 'https://unrelated.example' },
      body: JSON.stringify({ action: 'enable' }),
    });
    expect(crossed.status).toBe(403);
    expect(adapter).not.toHaveBeenCalled();
  });

  it('accepts native browser Basic authentication and rejects the wrong API token without echoing it', async () => {
    const basic = `Basic ${Buffer.from('open-design:native-fixture').toString('base64')}`;
    const accepted = await fetch(`${url}/api/integrations/omnigent/status`, {
      headers: { ...workspaceHeaders(), authorization: basic, origin: url },
    });
    expect(accepted.status).toBe(200);
    const denied = await fetch(`${url}/api/integrations/omnigent/status`, {
      headers: { ...workspaceHeaders(), authorization: 'Bearer fake-wrong-token' },
    });
    expect(denied.status).toBe(401);
    expect(await denied.text()).not.toContain('fake-wrong-token');
  });

  it('repairs a deleted onboarding conversation through the registered API without starting a chat', async () => {
    const enable = async () => {
      const response = await fetch(`${url}/api/integrations/omnigent/actions`, {
        method: 'POST', headers: { ...workspaceHeaders(), 'content-type': 'application/json', origin: url },
        body: JSON.stringify({ action: 'enable' }),
      });
      expect(response.status).toBe(200);
      return (await response.json() as { status: OmnigentIntegrationStatus }).status.onboarding!;
    };
    const first = await enable();
    deleteConversation(db, first.conversationId);
    const repaired = await enable();
    expect(repaired.projectId).toBe(first.projectId);
    expect(repaired.conversationId).not.toBe(first.conversationId);
    expect(listMessages(db, repaired.conversationId)).toHaveLength(1);
    expect(listMessages(db, repaired.conversationId)[0]?.runId).toBeUndefined();
    expect(adapter.mock.calls.every(call => (call[0] as { action: string }).action === 'status')).toBe(true);
  });

  it('uses the same Team-only installation for route access and service generation without a local registry row', async () => {
    teamPlugin = { ...getInstalledPlugin(db, OMNIGENT_PLUGIN_ID)!, source: 'team:plugin:workspace-a:woow-omnigent', fsPath: root };
    deleteInstalledPlugin(db, OMNIGENT_PLUGIN_ID);
    await writeFile(path.join(root, '.od-team-resource.json'), JSON.stringify({ kind: 'plugin', workspaceId: 'workspace-a', resourceId: OMNIGENT_PLUGIN_ID }));
    expect(getInstalledPlugin(db, OMNIGENT_PLUGIN_ID)).toBeNull();
    const enabled = await fetch(`${url}/api/integrations/omnigent/actions`, {
      method: 'POST', headers: { ...workspaceHeaders(), 'content-type': 'application/json' }, body: JSON.stringify({ action: 'enable' }),
    });
    expect(enabled.status).toBe(200);
    const first = (await enabled.json() as { status: OmnigentIntegrationStatus }).status;
    expect(first.installed).toBe(true);
    expect(first.onboarding).toBeDefined();
    const observed = await fetch(`${url}/api/integrations/omnigent/status`, { headers: workspaceHeaders() });
    expect(observed.status).toBe(200);
    const second = await observed.json() as OmnigentIntegrationStatus;
    expect(second.installed).toBe(true);
    expect(second.onboarding).toEqual(first.onboarding);
  });

  it('rejects partial attribution and cannot borrow a confirmed login to verify', async () => {
    const partial = await fetch(`${url}/api/integrations/omnigent/status`, {
      headers: { authorization: 'Bearer native-fixture', 'x-od-workspace-id': 'workspace-a' },
    });
    expect(partial.status).toBe(400);
    expect(await partial.json()).toMatchObject({ error: { code: 'WORKSPACE_CONTEXT_INCOMPLETE' } });
    const unapproved = await fetch(`${url}/api/integrations/omnigent/actions`, {
      method: 'POST', headers: { ...workspaceHeaders(), 'content-type': 'application/json' },
      body: JSON.stringify({ action: 'verify', engine: 'claude' }),
    });
    expect(unapproved.status).toBe(409);
    expect(await unapproved.json()).toMatchObject({ error: { code: 'FRESH_LOGIN_REQUIRED' } });
    expect(adapter).not.toHaveBeenCalled();
  });

  it('uses the composition root native authentication and exact scoped plugin lookup', async () => {
    const source = await readFile(new URL('../../src/server.ts', import.meta.url), 'utf8');
    expect(source).toContain('registerOmnigentRoutes(app, {');
    expect(source).toContain('dataDir: RUNTIME_DATA_DIR');
    expect(source).toContain('createOnboarding: omnigentOnboarding.create');
    expect(source).toContain('getPlugin: (scope) => getOmnigentPlugin(omnigentOnboarding.context(scope))');
    expect(source).toContain('getPlugin: getOmnigentPlugin');
    expect(source).toContain("isApiTokenAuthorization(req.get('authorization'))");
    expect(source).toContain('db, OMNIGENT_PLUGIN_ID, context?.workspaceId ?? null');
  });
});

import { mkdtemp, readFile, rm, writeFile } from 'node:fs/promises';
import path from 'node:path';
import { tmpdir } from 'node:os';
import { afterEach, describe, expect, it, vi } from 'vitest';
import type { InstalledPluginRecord } from '@open-design/contracts';
import { OmnigentIntegrationService, normalizeOmnigentServer, omnigentPython, type OmnigentServiceOptions } from '../src/integrations/omnigent/service.js';

const dirs: string[] = [];
afterEach(async () => { await Promise.all(dirs.splice(0).map(dir => rm(dir, { recursive: true, force: true }))); });
async function fixture(sourceKind?: InstalledPluginRecord['sourceKind']) {
  const dir = await mkdtemp(path.join(tmpdir(), 'od-omnigent-service-')); dirs.push(dir);
  let plugin: Awaited<ReturnType<OmnigentServiceOptions['getPlugin']>> = { id: 'woow-omnigent', installedAt: 1, ...(sourceKind ? { sourceKind } : {}) };
  const engines = { claude: { installed: true, ready: true, version: '2.1.284', agentId: 'agent-c', models: [{ id: 'sonnet', isDefault: true }] }, opencode: { installed: true, ready: true, version: '1.18.33', agentId: 'agent-o', models: [{ id: 'anthropic/claude-sonnet' }] }, codex: { installed: false }, pi: { installed: false } };
  const host = { id: 'this-host', connected: true };
  const adapter = vi.fn<NonNullable<OmnigentServiceOptions['adapter']>>(async (input: Record<string, unknown>) => {
    if (input.action === 'verify') return { ok: true, data: { ok: true, reply: 'OMNIGENT_CONNECTION_OK', cleanup: 'stopped', sessionId: `probe-${input.engine}`, model: input.model ?? (input.engine === 'opencode' ? 'anthropic/claude-sonnet' : 'sonnet') } };
    return { ok: true, data: { host: { ...host }, engines: structuredClone(engines) } };
  });
  const onboarding = vi.fn(async (scope: string) => ({ projectId: `project-${scope}`, conversationId: `chat-${scope}` }));
  const options = { dataDir: dir, getPlugin: () => plugin, adapter, createOnboarding: onboarding, env: { HOME: dir } };
  const service = new OmnigentIntegrationService(options);
  const setup = async () => { await service.action({ action: 'setup', server: 'https://omni.example', username: 'fresh-user', password: 'unique-transient-password' }); await service.waitForIdle(); return service.status('local', true); };
  return { dir, service, options, engines, host, adapter, onboarding, setup, setPlugin: (value: typeof plugin) => { plugin = value; } };
}
describe('Omnigent native service', () => {
  it('shuts down once without revoking approved login, then resumes on the next daemon', async () => {
    const f = await fixture('bundled'); await f.setup();
    const file = path.join(f.dir, 'integrations/omnigent/state.json');
    const before = await readFile(file, 'utf8');
    f.adapter.mockClear();
    await Promise.all([f.service.shutdown(), f.service.shutdown()]);
    expect(f.adapter.mock.calls.map(([input]) => input.action)).toEqual(['shutdown']);
    expect(await readFile(file, 'utf8')).toBe(before);
    await expect(f.service.action({ action: 'repair' })).rejects.toMatchObject({ code: 'SERVICE_SHUTTING_DOWN' });
    await f.service.status('local', true);
    expect(f.adapter.mock.calls.map(([input]) => input.action)).toEqual(['shutdown']);
    f.adapter.mockClear();
    await new OmnigentIntegrationService(f.options).resumeLocalHost();
    expect(f.adapter.mock.calls[0]?.[0]).toMatchObject({ action: 'catalog', allowManagedHost: true });
  });
  it('prevents a pending setup dependency result from starting a host after shutdown', async () => {
    const f = await fixture('bundled');
    let finish!: () => void;
    const dependencies = new Promise<void>(resolve => { finish = resolve; });
    const prepareDependencies = vi.fn(() => dependencies);
    const service = new OmnigentIntegrationService({ ...f.options, prepareDependencies });
    await service.action({ action: 'setup', server: 'https://omni.example', username: 'u', password: 'p' });
    await vi.waitFor(() => expect(prepareDependencies).toHaveBeenCalled());
    await service.shutdown();
    finish(); await service.waitForIdle();
    expect(f.adapter.mock.calls.map(([input]) => input.action)).toEqual(['shutdown']);
  });
  it('disable stops the private host without logout and still requires fresh GUI login after enable', async () => {
    const f = await fixture('bundled'); await f.setup(); f.adapter.mockClear();
    await f.service.action({ action: 'disable' });
    expect(f.adapter.mock.calls.map(([input]) => input.action)).toEqual(['shutdown']);
    expect((await f.service.status()).host.connected).toBe(false);
    f.adapter.mockClear();
    await f.service.action({ action: 'enable' });
    await expect(f.service.action({ action: 'repair' })).rejects.toMatchObject({ code: 'FRESH_LOGIN_REQUIRED' });
    expect(f.adapter.mock.calls.every(([input]) => input.allowManagedHost !== true)).toBe(true);
  });
  it('waits for a real cancelled adapter to exit before host cleanup, preventing late setup spawn', async () => {
    const f = await fixture('bundled');
    const python = path.join(f.dir, 'adapter-lifecycle');
    const ready = path.join(f.dir, 'connect-ready');
    const events = path.join(f.dir, 'events');
    await writeFile(python, `#!${process.execPath}
const fs = require('node:fs');
let raw = '';
process.stdin.on('data', chunk => { raw += chunk; });
process.stdin.on('end', () => {
  const input = JSON.parse(raw);
  if (input.action === 'connect') {
    const late = setTimeout(() => fs.appendFileSync(${JSON.stringify(events)}, 'LATE-HOST\\n'), 800);
    process.on('SIGTERM', () => {
      clearTimeout(late);
      setTimeout(() => { fs.appendFileSync(${JSON.stringify(events)}, 'adapter-exit\\n'); process.exit(0); }, 100);
    });
    fs.writeFileSync(${JSON.stringify(ready)}, 'ready');
  } else {
    fs.appendFileSync(${JSON.stringify(events)}, input.action + '\\n');
    process.stdout.write(JSON.stringify({ ok: true, data: { stopped: true } }));
  }
});
`, { mode: 0o700 });
    const service = new OmnigentIntegrationService({ dataDir: f.dir, getPlugin: f.options.getPlugin,
      createOnboarding: f.onboarding, prepareDependencies: async () => {},
      env: { HOME: f.dir, OD_OMNIGENT_PYTHON: python } });
    await service.action({ action: 'setup', server: 'https://omni.example', username: 'u', password: 'p' });
    await vi.waitFor(async () => expect(await readFile(ready, 'utf8')).toBe('ready'));
    await service.shutdown(); await service.waitForIdle();
    expect(await readFile(events, 'utf8')).toBe('adapter-exit\nshutdown\n');
  });
  it('fails closed within 32 seconds when host shutdown cannot be confirmed', async () => {
    const f = await fixture('bundled'); await f.setup();
    f.adapter.mockImplementation(async () => new Promise(() => {}));
    vi.useFakeTimers();
    try {
      const stopping = f.service.shutdown();
      const assertion = expect(stopping).rejects.toMatchObject({ code: 'HOST_SHUTDOWN_TIMEOUT' });
      await vi.advanceTimersByTimeAsync(32_000); await assertion;
      await expect(f.service.action({ action: 'setup', server: 'https://omni.example', username: 'u', password: 'p' }))
        .rejects.toMatchObject({ code: 'SERVICE_SHUTTING_DOWN' });
    } finally { vi.useRealTimers(); }
  });
  it('forces a real adapter ignoring TERM to exit within the five-second cancellation budget', async () => {
    const f = await fixture('bundled');
    const python = path.join(f.dir, 'adapter-ignore-term');
    const ready = path.join(f.dir, 'ready');
    const cleaned = path.join(f.dir, 'cleaned');
    await writeFile(python, `#!${process.execPath}
const fs = require('node:fs'); let raw = '';
process.stdin.on('data', chunk => raw += chunk);
process.stdin.on('end', () => {
  const input = JSON.parse(raw);
  if (input.action === 'connect') {
    process.on('SIGTERM', () => {});
    fs.writeFileSync(${JSON.stringify(ready)}, String(process.pid));
    setInterval(() => {}, 1000);
  } else {
    const pid = Number(fs.readFileSync(${JSON.stringify(ready)}, 'utf8'));
    try { process.kill(pid, 0); throw new Error('adapter still running'); }
    catch (error) { if (error.code !== 'ESRCH') throw error; }
    fs.writeFileSync(${JSON.stringify(cleaned)}, 'stopped');
    process.stdout.write(JSON.stringify({ ok: true, data: { stopped: true } }));
  }
});
`, { mode: 0o700 });
    const service = new OmnigentIntegrationService({ dataDir: f.dir, getPlugin: f.options.getPlugin,
      createOnboarding: f.onboarding, prepareDependencies: async () => {},
      env: { HOME: f.dir, OD_OMNIGENT_PYTHON: python } });
    await service.action({ action: 'setup', server: 'https://omni.example', username: 'u', password: 'p' });
    await vi.waitFor(async () => expect(await readFile(ready, 'utf8')).toMatch(/^\d+$/));
    const started = Date.now();
    await service.shutdown(); await service.waitForIdle();
    expect(Date.now() - started).toBeLessThan(7_000);
    expect(await readFile(cleaned, 'utf8')).toBe('stopped');
  });
  it('keeps confirmed approval when shutdown interrupts a repair', async () => {
    const f = await fixture('bundled'); await f.setup(); f.adapter.mockClear();
    let finish!: () => void;
    const dependencies = new Promise<void>(resolve => { finish = resolve; });
    const prepareDependencies = vi.fn(() => dependencies);
    const service = new OmnigentIntegrationService({ ...f.options, prepareDependencies });
    await service.action({ action: 'repair' });
    await vi.waitFor(() => expect(prepareDependencies).toHaveBeenCalled());
    await service.shutdown(); finish(); await service.waitForIdle();
    const state = JSON.parse(await readFile(path.join(f.dir, 'integrations/omnigent/state.json'), 'utf8'));
    expect(state).toMatchObject({ enabled: true, loginConfirmed: true, approvalScope: 'local', installationScope: 'local' });
    f.adapter.mockClear();
    await new OmnigentIntegrationService(f.options).resumeLocalHost();
    expect(f.adapter.mock.calls[0]?.[0]).toMatchObject({ action: 'catalog', allowManagedHost: true });
  });
  it('preserves the missing SDK diagnostic when the managed runner cannot spawn', async () => {
    const f = await fixture('bundled');
    const service = new OmnigentIntegrationService({ dataDir: f.dir, getPlugin: f.options.getPlugin,
      createOnboarding: f.onboarding, prepareDependencies: async () => {},
      env: { HOME: f.dir, OD_OMNIGENT_PYTHON: path.join(f.dir, 'missing-python') } });
    await service.action({ action: 'setup', server: 'https://omni.example', username: 'u', password: 'p' });
    await service.waitForIdle();
    expect((await service.status()).issue?.code).toBe('DEPENDENCIES_MISSING');
  });
  it('rediscovers the private SDK after a daemon restart without reinstalling it', () => {
    const state = '/fixture/integrations/omnigent';
    const managed = path.join(state, 'deps/uv/tools/omnigent/bin/python');
    expect(omnigentPython(state, {}, p => p === managed)).toBe(managed);
    expect(omnigentPython(state, {}, () => true)).toBe('/opt/omnigent/venv/bin/python');
    expect(omnigentPython(state, { OD_OMNIGENT_PYTHON: '/explicit/python' }, () => true)).toBe('/explicit/python');
    expect(omnigentPython(state, {}, p => p === '/opt/omnigent/venv/bin/python')).toBe('/opt/omnigent/venv/bin/python');
    expect(omnigentPython(state, {}, () => false)).toBe('python3');
  });
  it('uses a private persistent host home and resumes only after confirmed setup', async () => {
    const f = await fixture('bundled');
    await f.service.status();
    expect(f.adapter.mock.calls[0]?.[0]).toMatchObject({ configHome: path.join(f.dir, 'integrations/omnigent/host') });
    expect(f.adapter.mock.calls[0]?.[0]).not.toHaveProperty('allowManagedHost');
    await f.setup();
    f.adapter.mockClear();
    const restarted = new OmnigentIntegrationService(f.options);
    await restarted.status('local', true);
    expect(f.adapter.mock.calls[0]?.[0]).toMatchObject({ action: 'catalog', allowManagedHost: true });
    await restarted.action({ action: 'disable' });
    f.adapter.mockClear();
    await restarted.status('local', true);
    expect(f.adapter).not.toHaveBeenCalled();
  });
  it('ignores legacy host homes and requires fresh setup even when inherited environment advertises a host', async () => {
    const f = await fixture('bundled');
    const env = { ...f.options.env, OMNIGENT_CONFIG_HOME: path.join(f.dir, 'legacy-config'),
      OMNIGENT_DATA_DIR: path.join(f.dir, 'legacy-data') };
    const fresh = new OmnigentIntegrationService({ ...f.options, env });
    await fresh.resumeLocalHost();
    expect(f.adapter).not.toHaveBeenCalled();
    const status = await fresh.status();
    expect(status).toMatchObject({ phase: 'needs-login', server: null, loginConfirmed: false });
    expect(f.adapter.mock.calls[0]?.[0]).toMatchObject({ action: 'status', localOnly: true,
      configHome: path.join(f.dir, 'integrations/omnigent/host'),
      dataDir: path.join(f.dir, 'integrations/omnigent/host') });
    expect(f.adapter.mock.calls[0]?.[0]).not.toHaveProperty('allowManagedHost');
    expect(f.adapter.mock.calls[0]?.[0]).not.toHaveProperty('server');
    await expect(fresh.action({ action: 'repair' })).rejects.toMatchObject({ code: 'FRESH_LOGIN_REQUIRED' });
    await fresh.action({ action: 'setup', server: 'https://new.example', username: 'new-user', password: 'test-only' });
    await fresh.waitForIdle();
    const connect = f.adapter.mock.calls.find(([input]) => input.action === 'connect')?.[0];
    expect(connect).toMatchObject({ server: 'https://new.example', allowManagedHost: true,
      configHome: path.join(f.dir, 'integrations/omnigent/host'),
      dataDir: path.join(f.dir, 'integrations/omnigent/host') });
    expect(env.OMNIGENT_CONFIG_HOME).toBe(path.join(f.dir, 'legacy-config'));
    expect(env.OMNIGENT_DATA_DIR).toBe(path.join(f.dir, 'legacy-data'));
  });
  it('passes only daemon-owned host configuration to the adapter subprocess', async () => {
    const f = await fixture('bundled');
    const legacyKeys = ['OG_CONFIG_HOME', 'OG_DATA_DIR', 'OG_STATE_DIR', 'OG_STATUS_FILE',
      'OG_SERVER_URL', 'OG_SERVER_URL_FILE', 'OG_PROVIDERS', 'OG_HELPER', 'OG_HOME'];
    const python = path.join(f.dir, 'adapter-probe');
    await writeFile(python, `#!${process.execPath}
const assert = require('node:assert/strict');
let raw = '';
process.stdin.on('data', chunk => { raw += chunk; });
process.stdin.on('end', () => {
  const input = JSON.parse(raw);
  for (const key of ${JSON.stringify(legacyKeys)}) assert.equal(process.env[key], undefined, key);
  assert.equal(process.env.OMNIGENT_CONFIG_HOME, input.configHome);
  assert.equal(process.env.OMNIGENT_DATA_DIR, input.dataDir);
  assert.equal(input.configHome, require('node:path').join(input.stateDir, 'host'));
  assert.equal(input.dataDir, input.configHome);
  process.stdout.write(JSON.stringify({ ok: true, data: { host: { name: 'isolated-fixture' } } }));
});
`, { mode: 0o700 });
    const service = new OmnigentIntegrationService({ dataDir: f.dir, getPlugin: f.options.getPlugin,
      createOnboarding: f.onboarding, env: {
      ...f.options.env, OD_OMNIGENT_PYTHON: python,
      OMNIGENT_CONFIG_HOME: path.join(f.dir, 'legacy-config'), OMNIGENT_DATA_DIR: path.join(f.dir, 'legacy-data'),
      ...Object.fromEntries(legacyKeys.map(key => [key, 'legacy-fixture'])),
    } });
    expect((await service.status()).host.name).toBe('isolated-fixture');
  });
  it('resumes an approved local host at daemon startup without a browser request', async () => {
    const f = await fixture('bundled');
    await f.setup(); f.adapter.mockClear(); f.onboarding.mockClear();
    const restarted = new OmnigentIntegrationService(f.options);
    await restarted.resumeLocalHost();
    expect(f.adapter.mock.calls.map(([input]) => input.action)).toEqual(['catalog']);
    expect(f.adapter.mock.calls[0]?.[0]).toMatchObject({ allowManagedHost: true });
    expect(f.onboarding).not.toHaveBeenCalled();
  });
  it.each(['fresh', 'disabled', 'disconnected', 'other-scope'] as const)(
    'does not resume a %s host at daemon startup', async mode => {
      const f = await fixture('bundled');
      if (mode === 'other-scope') {
        await f.service.action({ action: 'setup', server: 'https://omni.example', username: 'u', password: 'p' }, 'workspace:member');
        await f.service.waitForIdle();
      } else if (mode !== 'fresh') {
        await f.setup();
        await f.service.action({ action: mode === 'disabled' ? 'disable' : 'disconnect' });
        await f.service.waitForIdle();
      }
      f.adapter.mockClear();
      await new OmnigentIntegrationService(f.options).resumeLocalHost();
      expect(f.adapter).not.toHaveBeenCalled();
    },
  );
  it.each(['removed', 'reinstalled'] as const)('rechecks a %s plugin before resuming its host', async mode => {
    const f = await fixture('bundled'); await f.setup();
    f.setPlugin(mode === 'removed' ? null : { id: 'woow-omnigent', installedAt: 2, sourceKind: 'bundled' });
    f.adapter.mockClear();
    await new OmnigentIntegrationService(f.options).resumeLocalHost();
    expect(f.adapter.mock.calls.every(([input]) => input.allowManagedHost !== true)).toBe(true);
  });
  it.each(['https://u:p@omni.example', 'http://remote.example', 'https://omni.example?q=password', 'https://omni.example/#secret', 'file:///tmp/omni', 'https://omni.example\\evil'])('rejects credential-bearing or unsafe server %s', value => expect(() => normalizeOmnigentServer(value)).toThrow());
  it('normalizes the selected server', () => expect(normalizeOmnigentServer(' https://OMNI.example/ ')).toBe('https://omni.example'));
  it('starts without inheriting approval and checks only local inventory', async () => {
    const f = await fixture(); const s = await f.service.status();
    expect(s.phase).toBe('needs-login'); expect(s.loginConfirmed).toBe(false);
    expect(f.adapter.mock.calls[0]?.[0]).toMatchObject({ action: 'status', localOnly: true });
    expect(s.installedEngines).toEqual(['claude', 'opencode']);
  });
  it.each(['bundled', 'user', 'github', 'project', undefined] as const)('reports installation source %s without starting onboarding', async sourceKind => {
    const f = await fixture(sourceKind); const status = await f.service.status();
    expect(status.installationSource).toBe(sourceKind === 'bundled' ? 'bundled' : 'user');
    expect(status.phase).toBe('needs-login'); expect(status.loginConfirmed).toBe(false);
    expect(status.onboarding).toBeUndefined(); expect(f.onboarding).not.toHaveBeenCalled();
    expect(f.adapter.mock.calls.every(call => call[0].action === 'status')).toBe(true);
  });
  it.each(['bundled', 'user', undefined] as const)('explicit enable creates onboarding per scope for source %s without model activity', async sourceKind => {
    const f = await fixture(sourceKind); const enabled = await f.service.action({ action: 'enable' }, 'a'); await f.service.action({ action: 'enable' }, 'a'); await f.service.action({ action: 'enable' }, 'b');
    expect(enabled.installationSource).toBe(sourceKind ?? 'user');
    expect(enabled.onboarding?.conversationId).toBe('chat-a'); expect(enabled.loginConfirmed).toBe(false);
    expect(f.onboarding.mock.calls.map(call => call[0])).toEqual(['a', 'a', 'b']);
    expect((await f.service.status('a')).onboarding?.conversationId).toBe('chat-a');
    expect(f.adapter.mock.calls.every(call => call[0].action === 'status')).toBe(true);
  });
  it('requires new credentials before verify or repair even with an online host', async () => {
    const f = await fixture(); await f.service.status();
    await expect(f.service.action({ action: 'verify' })).rejects.toMatchObject({ code: 'FRESH_LOGIN_REQUIRED' });
    await expect(f.service.action({ action: 'repair' })).rejects.toMatchObject({ code: 'FRESH_LOGIN_REQUIRED' });
  });
  it('verifies exactly existing engines and never persists or exposes login inputs', async () => {
    const f = await fixture(); const s = await f.setup();
    expect(s.phase).toBe('ready'); expect(s.verification.claude?.ok).toBe(true); expect(s.verification.opencode?.cleanup).toBe('stopped');
    expect(f.adapter.mock.calls.filter(call => call[0].action === 'verify').map(call => call[0].engine)).toEqual(['claude', 'opencode']);
    const persisted = await readFile(path.join(f.dir, 'integrations/omnigent/state.json'), 'utf8');
    expect(persisted + JSON.stringify(s)).not.toContain('unique-transient-password'); expect(persisted).not.toContain('fresh-user');
    expect(s.engines.opencode?.models?.[0]?.id).toBe('anthropic/claude-sonnet');
  });
  it('does not accept a stopped session without an assistant sentinel', async () => {
    const f = await fixture(); const original = f.adapter.getMockImplementation()!;
    f.adapter.mockImplementation(async input => input.action === 'verify' ? { ok: true, data: { ok: true, reply: 'user-echo', cleanup: 'stopped' } } : original(input));
    const s = await f.setup(); expect(s.phase).toBe('error'); expect(s.verification.claude?.ok).toBe(false);
  });
  it.each(['RATE_LIMITED', 'FREE_USAGE_LIMIT_REACHED', 'INSUFFICIENT_QUOTA', 'QUOTA_EXCEEDED'])(
    'preserves structured provider reason %s in persisted verification receipts', async reason => {
      const f = await fixture(); const original = f.adapter.getMockImplementation()!;
      f.adapter.mockImplementation(async input => input.action === 'verify' && input.engine === 'opencode'
        ? { ok: true, data: { ok: false, reason, cleanup: 'stopped', sessionId: 'probe-opencode',
          model: 'anthropic/claude-sonnet', message: 'synthetic-private-provider-message' } }
        : original(input));
      const status = await f.setup();
      expect(status.phase).toBe('error');
      expect(status.verification.opencode).toMatchObject({ ok: false, reason, cleanup: 'stopped', sessionId: 'probe-opencode' });
      expect(status.issue?.code).toBe('INCOMPLETE_CHAT_VERIFICATION');
      const persisted = await readFile(path.join(f.dir, 'integrations/omnigent/state.json'), 'utf8');
      expect(JSON.parse(persisted).verification.opencode.reason).toBe(reason);
      const restarted = await new OmnigentIntegrationService(f.options).status('local', true);
      expect(restarted.verification.opencode?.reason).toBe(reason);
      expect(persisted + JSON.stringify(status) + JSON.stringify(restarted)).not.toContain('synthetic-private-provider-message');
    },
  );
  it('does not accept a reply whose runner stop is unconfirmed', async () => {
    const f = await fixture(); const original = f.adapter.getMockImplementation()!;
    f.adapter.mockImplementation(async input => input.action === 'verify' ? { ok: true, data: { ok: true, reply: 'OMNIGENT_CONNECTION_OK', cleanup: 'stop_unconfirmed' } } : original(input));
    const s = await f.setup(); expect(s.phase).toBe('error'); expect(s.verification.opencode?.ok).toBe(false);
  });
  it('invalidates readiness when a local engine changes version', async () => {
    const f = await fixture(); await f.setup(); f.engines.claude.version = '2.2.0';
    expect((await f.service.status('local', true)).phase).toBe('needs-verification');
  });
  it('binds readiness to the exact host and current connection', async () => {
    const f = await fixture(); await f.setup(); f.host.id = 'other-host';
    expect((await f.service.status('local', true)).phase).toBe('needs-verification');
    f.host.id = 'this-host'; f.host.connected = false;
    expect((await f.service.status('local', true)).phase).toBe('needs-verification');
  });
  it('disable stops integration checks and re-enable requires new login', async () => {
    const f = await fixture(); await f.setup(); await f.service.action({ action: 'disable' }); f.adapter.mockClear();
    const disabled = await f.service.status('local', true); expect(disabled.phase).toBe('disabled'); expect(f.adapter).not.toHaveBeenCalled();
    const enabled = await f.service.action({ action: 'enable' }); expect(enabled.loginConfirmed).toBe(false); expect(enabled.phase).toBe('needs-login');
  });
  it('rejects operations while disabled', async () => {
    const f = await fixture(); await f.service.action({ action: 'disable' });
    await expect(f.service.action({ action: 'setup', server: 'https://omni.example', username: 'u', password: 'p' })).rejects.toMatchObject({ code: 'PLUGIN_DISABLED' });
  });
  it('rejects concurrent actions before asynchronous onboarding completes', async () => {
    const f = await fixture(); let release!: () => void;
    f.onboarding.mockImplementationOnce(async scope => { await new Promise<void>(resolve => { release = resolve; }); return { projectId: scope, conversationId: scope }; });
    const first = f.service.action({ action: 'enable' });
    await vi.waitFor(() => expect(release).toBeTypeOf('function'));
    await expect(f.service.action({ action: 'disable' })).rejects.toMatchObject({ code: 'OPERATION_BUSY' }); release(); await first;
  });
  it('reinstallation invalidates previous approval even if removal was not polled', async () => {
    const f = await fixture(); await f.setup(); f.setPlugin({ id: 'woow-omnigent', installedAt: 2 });
    const s = await f.service.status('local', true); expect(s.loginConfirmed).toBe(false); expect(s.verification).toEqual({});
  });
  it('uninstall stops checks and gates all operations', async () => {
    const f = await fixture(); await f.setup(); f.setPlugin(null); f.adapter.mockClear();
    const status = await f.service.status('local', true);
    expect(status.phase).toBe('not-installed'); expect(status).not.toHaveProperty('installationSource'); expect(f.adapter).not.toHaveBeenCalled();
    await expect(f.service.action({ action: 'enable' })).rejects.toMatchObject({ code: 'PLUGIN_NOT_INSTALLED' });
  });
  it('derives installation source from the current record without persisting it or invalidating approval', async () => {
    const f = await fixture('bundled'); const before = await f.setup();
    expect(before.installationSource).toBe('bundled');
    const stateFile = path.join(f.dir, 'integrations/omnigent/state.json');
    const persisted = await readFile(stateFile, 'utf8');
    expect(JSON.parse(persisted)).not.toHaveProperty('installationSource');
    f.setPlugin({ id: 'woow-omnigent', installedAt: 1, sourceKind: 'user' });
    const status = await f.service.status();
    expect(status.installationSource).toBe('user'); expect(status.phase).toBe('ready');
    expect(status.loginConfirmed).toBe(true); expect(status.verification).toEqual(before.verification);
    expect(await readFile(stateFile, 'utf8')).toBe(persisted);
    expect((await f.service.action({ action: 'disable' })).installationSource).toBe('user');
  });
  it('reports each scope installation source without sharing approval', async () => {
    const f = await fixture('bundled');
    const service = new OmnigentIntegrationService({ ...f.options, getPlugin: scope => scope === 'other-workspace'
      ? { id: 'woow-omnigent', installedAt: 999, sourceKind: 'user' } : f.options.getPlugin() });
    await service.action({ action: 'setup', server: 'https://omni.example', username: 'fresh', password: 'transient' }); await service.waitForIdle();
    const foreign = await service.status('other-workspace', true);
    expect(foreign.installationSource).toBe('user'); expect(foreign.loginConfirmed).toBe(false); expect(foreign.verification).toEqual({});
    const owner = await service.status();
    expect(owner.installationSource).toBe('bundled'); expect(owner.phase).toBe('ready');
  });
  it('update cannot turn ambient credentials into fresh approval', async () => {
    const f = await fixture(); await f.service.action({ action: 'update' }); await f.service.waitForIdle();
    expect((await f.service.status()).loginConfirmed).toBe(false); expect(f.adapter.mock.calls.some(call => call[0].action === 'connect')).toBe(false);
  });
  it('disconnect clears approval and affects only its selected server', async () => {
    const f = await fixture(); await f.setup(); await f.service.action({ action: 'disconnect' }); await f.service.waitForIdle();
    expect(f.adapter.mock.calls.find(call => call[0].action === 'disconnect')?.[0].server).toBe('https://omni.example');
    const s = await f.service.status(); expect(s.loginConfirmed).toBe(false); expect(s.verification).toEqual({});
  });
  it('does not share approval, model catalogs or receipts with another workspace scope', async () => {
    const f = await fixture(); await f.setup(); const other = await f.service.status('other-workspace', true);
    expect(other.loginConfirmed).toBe(false); expect(other.server).toBeNull(); expect(other.verification).toEqual({});
    expect(other.engines.opencode?.models).toBeUndefined();
    await expect(f.service.action({ action: 'verify' }, 'other-workspace')).rejects.toMatchObject({ code: 'FRESH_LOGIN_REQUIRED' });
    await expect(f.service.action({ action: 'repair' }, 'other-workspace')).rejects.toMatchObject({ code: 'FRESH_LOGIN_REQUIRED' });
    await expect(f.service.action({ action: 'disconnect' }, 'other-workspace')).rejects.toMatchObject({ code: 'FRESH_LOGIN_REQUIRED' });
  });
  it('cannot approve a reinstalled plugin with an earlier in-flight login', async () => {
    const f = await fixture(); let release!: () => void; const original = f.adapter.getMockImplementation()!;
    f.adapter.mockImplementation(async input => { if (input.action === 'connect') await new Promise<void>(resolve => { release = resolve; }); return original(input); });
    await f.service.action({ action: 'setup', server: 'https://omni.example', username: 'fresh-user', password: 'transient-password' });
    await vi.waitFor(() => expect(release).toBeTypeOf('function'));
    f.setPlugin({ id: 'woow-omnigent', installedAt: 2 }); await f.service.status(); release(); await f.service.waitForIdle();
    const status = await f.service.status('local', true);
    expect(status.loginConfirmed).toBe(false); expect(status.verification).toEqual({}); expect(status.phase).not.toBe('ready');
    expect(f.adapter.mock.calls.some(call => call[0].action === 'verify')).toBe(false);
  });
  it('another scope with a different installation generation cannot invalidate the owning scope', async () => {
    const f = await fixture(); const original = f.options.getPlugin;
    f.options.getPlugin = ((scope: string) => scope === 'other-workspace' ? { id: 'woow-omnigent', installedAt: 999 } : original()) as typeof f.options.getPlugin;
    const service = new OmnigentIntegrationService(f.options);
    await service.action({ action: 'setup', server: 'https://omni.example', username: 'fresh', password: 'transient' }); await service.waitForIdle();
    expect((await service.status()).phase).toBe('ready');
    expect((await service.status('other-workspace', true)).loginConfirmed).toBe(false);
    expect((await service.status('local', true)).phase).toBe('ready');
  });
  it('uninstall while persisting one receipt prevents launching the next probe', async () => {
    const f = await fixture(); const original = f.adapter.getMockImplementation()!;
    f.adapter.mockImplementation(async input => { const response = await original(input); if (input.action === 'verify' && input.engine === 'claude') f.setPlugin(null); return response; });
    await f.setup(); expect(f.adapter.mock.calls.filter(call => call[0].action === 'verify').map(call => call[0].engine)).toEqual(['claude']);
    expect((await f.service.status()).loginConfirmed).toBe(false);
  });
  it('a foreign scope cannot replace the owning catalog with unapproved local inventory', async () => {
    const f = await fixture(); await f.setup(); f.adapter.mockClear();
    const original = f.adapter.getMockImplementation()!;
    f.adapter.mockImplementation(async input => input.localOnly ? { ok: true, data: { host: { connected: false }, engines: {} } } : original(input));
    const foreign = await f.service.status('other-workspace', true);
    expect(foreign.loginConfirmed).toBe(false); expect(f.adapter).not.toHaveBeenCalled();
    expect((await f.service.status()).phase).toBe('ready');
  });
  it('a slow pre-login status response cannot overwrite a completed setup catalog', async () => {
    const f = await fixture(); let release!: () => void; const original = f.adapter.getMockImplementation()!;
    f.adapter.mockImplementation(async input => { if (input.action === 'status' && input.localOnly) { await new Promise<void>(resolve => { release = resolve; }); return { ok: true, data: { host: { connected: false }, engines: {} } }; } return original(input); });
    const first = f.service.status('local', true); await vi.waitFor(() => expect(release).toBeTypeOf('function'));
    await f.service.action({ action: 'setup', server: 'https://omni.example', username: 'fresh', password: 'transient' }); await f.service.waitForIdle();
    release(); expect((await first).phase).toBe('ready'); expect((await f.service.status()).phase).toBe('ready');
  });
  it('keeps the full installed OpenCode catalog when it exceeds two hundred models', async () => {
    const f = await fixture();
    f.engines.opencode.models = Array.from({ length: 389 }, (_, index) => ({ id: `openrouter/provider/model-${index}` })).concat([{ id: 'anthropic/claude-sonnet' }]);
    const result = await f.setup();
    expect(result.phase).toBe('ready'); expect(result.engines.opencode?.models).toHaveLength(390);
    expect(result.engines.opencode?.models?.some(model => model.id === result.verification.opencode?.model)).toBe(true);
  });
  it('persists readiness and onboarding across daemon restart', async () => {
    const f = await fixture(); await f.setup(); const restarted = new OmnigentIntegrationService(f.options);
    const s = await restarted.status('local', true); expect(s.phase).toBe('ready'); expect(s.onboarding?.conversationId).toBe('chat-local');
  });
});

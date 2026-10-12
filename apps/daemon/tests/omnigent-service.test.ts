import { mkdtemp, readFile, rm } from 'node:fs/promises';
import path from 'node:path';
import { tmpdir } from 'node:os';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { OmnigentIntegrationService, normalizeOmnigentServer, type OmnigentServiceOptions } from '../src/integrations/omnigent/service.js';

const dirs: string[] = [];
afterEach(async () => { await Promise.all(dirs.splice(0).map(dir => rm(dir, { recursive: true, force: true }))); });
async function fixture() {
  const dir = await mkdtemp(path.join(tmpdir(), 'od-omnigent-service-')); dirs.push(dir);
  let plugin: { id: string; installedAt: number } | null = { id: 'woow-omnigent', installedAt: 1 };
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
  it.each(['https://u:p@omni.example', 'http://remote.example', 'https://omni.example?q=password', 'https://omni.example/#secret', 'file:///tmp/omni', 'https://omni.example\\evil'])('rejects credential-bearing or unsafe server %s', value => expect(() => normalizeOmnigentServer(value)).toThrow());
  it('normalizes the selected server', () => expect(normalizeOmnigentServer(' https://OMNI.example/ ')).toBe('https://omni.example'));
  it('starts without inheriting approval and checks only local inventory', async () => {
    const f = await fixture(); const s = await f.service.status();
    expect(s.phase).toBe('needs-login'); expect(s.loginConfirmed).toBe(false);
    expect(f.adapter.mock.calls[0]?.[0]).toMatchObject({ action: 'status', localOnly: true });
    expect(s.installedEngines).toEqual(['claude', 'opencode']);
  });
  it('creates one native onboarding per scope without model activity', async () => {
    const f = await fixture(); await f.service.action({ action: 'enable' }, 'a'); await f.service.action({ action: 'enable' }, 'a'); await f.service.action({ action: 'enable' }, 'b');
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
    expect((await f.service.status('local', true)).phase).toBe('not-installed'); expect(f.adapter).not.toHaveBeenCalled();
    await expect(f.service.action({ action: 'enable' })).rejects.toMatchObject({ code: 'PLUGIN_NOT_INSTALLED' });
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

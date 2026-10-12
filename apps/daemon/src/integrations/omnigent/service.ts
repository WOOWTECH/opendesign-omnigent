import { randomUUID } from 'node:crypto';
import { spawn } from 'node:child_process';
import { existsSync } from 'node:fs';
import { mkdir, readFile, rename, writeFile } from 'node:fs/promises';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import type { OmnigentActionRequest, OmnigentIntegrationStatus, OmnigentIssue, OmnigentOnboarding, OmnigentOperation, OmnigentReceipt, OmnigentEngine } from '@open-design/contracts';
import { ensureOmnigentDependencies } from './dependencies.js';

export const OMNIGENT_PLUGIN_ID = 'woow-omnigent';
const ENGINE_IDS = ['claude', 'codex', 'pi', 'opencode'] as const;
type AdapterResult = { ok: boolean; data?: Record<string, unknown>; error?: { code?: string } };
type AdapterInput = Record<string, unknown>;
interface PluginIdentity { id: string; installedAt: number; updatedAt?: number; version?: string; }
type StoredReceipt = OmnigentReceipt & { server: string | null; hostId?: string };
interface State {
  schema: 1; installedGeneration?: number; enabled: boolean; phase: string; server: string | null;
  loginConfirmed: boolean; verification: Record<string, StoredReceipt>;
  approvalScope?: string;
  installationScope?: string;
  onboarding: Record<string, OmnigentOnboarding>; operation?: OmnigentOperation; issue?: OmnigentIssue;
}
export interface OmnigentServiceOptions {
  dataDir: string;
  getPlugin: (scope: string) => PluginIdentity | null | Promise<PluginIdentity | null>;
  adapter?: (input: AdapterInput) => Promise<AdapterResult>;
  createOnboarding: (scope: string, existing?: OmnigentOnboarding) => Promise<OmnigentOnboarding>;
  env?: NodeJS.ProcessEnv;
  prepareDependencies?: (update: boolean) => Promise<void>;
}
const MESSAGES: Record<string, string> = {
  OPERATION_BUSY: 'Another Omnigent operation is running. Please wait.',
  PLUGIN_NOT_INSTALLED: 'Install the Omnigent plugin first.',
  PLUGIN_DISABLED: 'Enable the Omnigent plugin before connecting or verifying.',
  INVALID_SERVER: 'Enter an HTTPS server URL without credentials, query parameters or fragments.',
  INVALID_CREDENTIALS: 'Enter the server, account and password in the connection dialog.',
  FRESH_LOGIN_REQUIRED: 'Enter fresh server, account and password in the connection dialog.',
  NEEDS_LOGIN: 'Omnigent did not accept the login. Enter the account and password again.',
  NEEDS_BROWSER_AUTH: 'This server requires the official browser sign-in flow.',
  UNSUPPORTED_AUTH: 'This server requires the official browser sign-in flow.',
  SERVER_UNREACHABLE: 'The Omnigent server could not be reached. Check its URL and network.',
  DEPENDENCIES_MISSING: 'The Omnigent dependency is unavailable. Use Repair to prepare it.',
  ADAPTER_TIMEOUT: 'The operation exceeded its time limit. Retry after checking the host.',
  ADAPTER_FAILED: 'The Omnigent operation did not finish. Check the connection and retry.',
  OPERATION_INTERRUPTED: 'The previous operation was interrupted. Check the connection and retry.',
  OPERATION_SUPERSEDED: 'This operation belongs to an earlier installation. Enter fresh connection information.',
  INCOMPLETE_CHAT_VERIFICATION: 'Every installed supported engine must reply and stop its test session before setup is complete.',
  SESSION_STOP_FAILED: 'The test session could not be stopped. Check Omnigent before retrying.',
  NO_AVAILABLE_ENGINE: 'No supported engine is installed. Only existing engines are connected.',
  INVALID_ENGINE: 'Choose an installed supported engine.',
  INVALID_MODEL: 'Choose a model listed by this host.',
  STATE_UNREADABLE: 'The integration state could not be read. Restore its private backup before retrying.',
  FORBIDDEN: 'This account cannot access the Omnigent resource.',
};
export class OmnigentServiceError extends Error {
  constructor(public readonly code: string, public readonly status = 409) { super(MESSAGES[code] ?? MESSAGES.ADAPTER_FAILED); }
}
function issue(code: string): OmnigentIssue { return { code, message: MESSAGES[code] ?? MESSAGES.ADAPTER_FAILED! }; }
function text(value: unknown, max = 256): string | undefined {
  return typeof value === 'string' ? value.replace(/[\x00-\x1f\x7f]/g, '').slice(0, max) : undefined;
}
function object(value: unknown): Record<string, unknown> { return value && typeof value === 'object' && !Array.isArray(value) ? value as Record<string, unknown> : {}; }
export function normalizeOmnigentServer(input: unknown): string {
  try {
    if (typeof input !== 'string' || /[\s\\]/.test(input.trim())) throw new Error();
    const url = new URL(input.trim());
    if (url.protocol !== 'https:' || !url.hostname || url.username || url.password || url.search || url.hash) throw new Error();
    return url.href.replace(/\/+$/, '');
  } catch { throw new OmnigentServiceError('INVALID_SERVER', 400); }
}
function empty(): State { return { schema: 1, enabled: true, phase: 'needs-login', server: null, loginConfirmed: false, verification: {}, onboarding: {} }; }
export class OmnigentIntegrationService {
  private readonly stateDir: string;
  private readonly file: string;
  private readonly env: NodeJS.ProcessEnv;
  private readonly adapter: (input: AdapterInput) => Promise<AdapterResult>;
  private state: State = empty();
  private loaded = false;
  private initialization: Promise<void> | undefined;
  private active: Promise<void> | undefined;
  private mutationBusy = false;
  private activeAbort: AbortController | undefined;
  private local: Record<string, unknown> = {};
  private statusCheck: Promise<void> | undefined;
  private lastCheck = 0;
  private writeChain: Promise<void> = Promise.resolve();
  private onboardingWork = new Map<string, Promise<OmnigentOnboarding>>();
  constructor(private readonly options: OmnigentServiceOptions) {
    this.stateDir = path.join(options.dataDir, 'integrations', 'omnigent');
    this.file = path.join(this.stateDir, 'state.json');
    this.env = { ...(options.env ?? process.env) };
    this.adapter = options.adapter ?? (input => this.executeAdapter(input));
  }
  private async load(): Promise<void> {
    if (this.loaded) return;
    if (this.initialization) return this.initialization;
    this.initialization = (async () => {
      try {
        const raw = JSON.parse(await readFile(this.file, 'utf8')) as State;
        if (raw.schema !== 1 || typeof raw.enabled !== 'boolean' || typeof raw.loginConfirmed !== 'boolean') throw new Error();
        this.state = { ...empty(), ...raw };
        if (this.state.operation?.phase === 'running') {
          this.state.operation = { ...this.state.operation, phase: 'error', finishedAt: new Date().toISOString(), issue: issue('OPERATION_INTERRUPTED') };
          this.state.phase = 'error'; this.state.issue = issue('OPERATION_INTERRUPTED');
          await this.save();
        }
      } catch (error) {
        if ((error as NodeJS.ErrnoException).code !== 'ENOENT') throw new OmnigentServiceError('STATE_UNREADABLE', 500);
      }
      this.loaded = true;
    })();
    try { await this.initialization; } finally { this.initialization = undefined; }
  }
  private async save(): Promise<void> {
    const contents = JSON.stringify(this.state, null, 2) + '\n';
    const next = this.writeChain.catch(() => undefined).then(async () => {
      await mkdir(this.stateDir, { recursive: true, mode: 0o700 });
      const temporary = `${this.file}.${randomUUID()}.tmp`;
      await writeFile(temporary, contents, { mode: 0o600, flag: 'wx' });
      await rename(temporary, this.file);
    });
    this.writeChain = next; await next;
  }
  private async reconcile(scope: string): Promise<boolean> {
    await this.load();
    const plugin = await this.options.getPlugin(scope);
    // A reader's visible installation cannot replace another scope's active host setup.
    if (this.state.installationScope && this.state.installationScope !== scope) return !!plugin;
    if (!plugin) {
      if (this.state.loginConfirmed || this.state.phase !== 'not-installed') {
        this.activeAbort?.abort();
        this.state.loginConfirmed = false; this.state.verification = {}; this.state.phase = 'not-installed'; await this.save();
      }
      return false;
    }
    if (this.state.phase === 'not-installed' || this.state.installedGeneration !== plugin.installedAt) {
      this.activeAbort?.abort();
      this.state.installedGeneration = plugin.installedAt;
      this.state.installationScope = scope;
      this.state.enabled = true; this.state.loginConfirmed = false; this.state.verification = {}; this.state.phase = 'needs-login';
      this.state.onboarding = {};
      delete this.state.approvalScope;
      delete this.state.issue; await this.save();
    }
    return true;
  }
  private async call(action: string, additional: AdapterInput = {}): Promise<AdapterResult> {
    try {
      return await this.adapter({ action, stateDir: this.stateDir,
        configHome: this.env.OMNIGENT_CONFIG_HOME ?? path.join(this.env.HOME ?? '', '.omnigent'),
        dataDir: this.env.OMNIGENT_DATA_DIR ?? this.env.OMNIGENT_CONFIG_HOME ?? path.join(this.env.HOME ?? '', '.omnigent'),
        workspace: this.env.OG_WORKSPACE ?? this.env.HOME,
        ...(this.state.server ? { server: this.state.server } : {}), ...additional });
    } catch (error) {
      return { ok: false, error: { code: error instanceof OmnigentServiceError ? error.code : 'ADAPTER_FAILED' } };
    }
  }
  private publicStatus(installed: boolean, scope: string): OmnigentIntegrationStatus {
    const approved = this.state.approvalScope === scope;
    const data = this.local; const rawHost = object(data.host); const rawEngines = object(data.engines);
    const engines: Record<string, OmnigentEngine> = {};
    for (const id of ENGINE_IDS) {
      const value = object(rawEngines[id]);
      engines[id] = { installed: value.installed === true || value.available === true, ready: approved && value.ready === true,
        ...(text(value.version, 64) ? { version: text(value.version, 64)! } : {}),
        ...(text(value.authStatus ?? value.configured, 32) ? { authStatus: text(value.authStatus ?? value.configured, 32)! } : {}),
        ...(text(value.agentId) ? { agentId: text(value.agentId)! } : {}),
        ...(text(value.harness) ? { harness: text(value.harness)! } : {}),
        ...(approved && Array.isArray(value.models) ? { models: value.models.slice(0, 1000).map(model => object(model)).filter(model => text(model.id)).map(model => ({ id: text(model.id)!, ...(text(model.displayName) ? { displayName: text(model.displayName)! } : {}), isDefault: model.isDefault === true })) } : {}) };
    }
    const installedEngines = ENGINE_IDS.filter(id => engines[id]?.installed);
    const hostId = text(rawHost.id ?? rawHost.host_id);
    const allVerified = installedEngines.length > 0 && installedEngines.every(id => {
      const receipt = this.state.verification[id];
      return !!hostId && !!engines[id]?.version && engines[id]?.ready === true && receipt?.ok === true && receipt.cleanup === 'stopped' && receipt.reply === 'OMNIGENT_CONNECTION_OK'
        && typeof receipt.model === 'string' && engines[id]?.models?.some(model => model.id === receipt.model)
        && receipt.cliVersion === engines[id]?.version && receipt.hostId === hostId
        && receipt.server === this.state.server;
    });
    let phase = !installed ? 'not-installed' : !this.state.enabled ? 'disabled' : this.state.phase;
    if (installed && this.state.enabled && !approved && !this.active) phase = 'needs-login';
    if (phase === 'ready' && (!this.state.loginConfirmed || !allVerified || rawHost.connected !== true)) phase = 'needs-verification';
    return { installed, enabled: installed && this.state.enabled, phase, server: approved ? this.state.server : null,
      loginConfirmed: approved && this.state.loginConfirmed,
      host: { connected: approved && rawHost.connected === true, ...(hostId ? { id: hostId } : {}), ...(text(rawHost.name) ? { name: text(rawHost.name)! } : {}), ...(text(rawHost.manager) ? { manager: text(rawHost.manager)! } : {}) },
      installedEngines, engines, verification: approved ? { ...this.state.verification } : {},
      ...(approved && Array.isArray(data.modules) ? { modules: data.modules.slice(0, 100).map(object).filter(row => text(row.key)).map(row => ({ key: text(row.key)!, name: text(row.name) ?? text(row.key)! })) } : {}),
      ...(this.state.onboarding[scope] ? { onboarding: this.state.onboarding[scope]! } : {}),
      ...(this.state.operation ? { operation: { ...this.state.operation } } : {}),
      ...(this.state.issue ? { issue: { ...this.state.issue } } : {}) };
  }
  async status(scope = 'local', fresh = false): Promise<OmnigentIntegrationStatus> {
    const installed = await this.reconcile(scope);
    if (this.state.installationScope && this.state.installationScope !== scope) return this.publicStatus(installed, scope);
    if (installed && this.state.enabled && !this.active && (fresh || !this.lastCheck || Date.now() - this.lastCheck > 10_000)) {
      this.statusCheck ??= (async () => {
        const operationId = this.state.operation?.id;
        const generation = this.state.installedGeneration;
        const approvalScope = this.state.approvalScope;
        const enabled = this.state.enabled;
        const remote = this.state.loginConfirmed && this.state.approvalScope === scope;
        const response = await this.call(remote ? 'catalog' : 'status', remote ? {} : { localOnly: true });
        if (this.active || operationId !== this.state.operation?.id || generation !== this.state.installedGeneration
          || approvalScope !== this.state.approvalScope || enabled !== this.state.enabled) return;
        this.local = response.ok ? object(response.data) : { engines: this.local.engines, host: { connected: false } };
        this.lastCheck = Date.now();
      })().finally(() => { this.statusCheck = undefined; });
      await this.statusCheck;
    }
    return this.publicStatus(installed, scope);
  }
  private async onboarding(scope: string): Promise<void> {
    let pending = this.onboardingWork.get(scope);
    if (!pending) {
      pending = this.options.createOnboarding(scope, this.state.onboarding[scope]); this.onboardingWork.set(scope, pending);
    }
    try { this.state.onboarding[scope] = await pending; await this.save(); }
    finally { this.onboardingWork.delete(scope); }
  }
  async action(input: OmnigentActionRequest, scope = 'local'): Promise<OmnigentIntegrationStatus> {
    if (!await this.reconcile(scope)) throw new OmnigentServiceError('PLUGIN_NOT_INSTALLED');
    if (this.active || this.mutationBusy) throw new OmnigentServiceError('OPERATION_BUSY');
    this.mutationBusy = true;
    try {
      if (this.state.installationScope !== scope) {
        if (!['enable', 'setup'].includes(input.action)) throw new OmnigentServiceError('FRESH_LOGIN_REQUIRED');
        const plugin = await this.options.getPlugin(scope);
        if (!plugin) throw new OmnigentServiceError('PLUGIN_NOT_INSTALLED');
        this.state.installedGeneration = plugin.installedAt; this.state.installationScope = scope;
        this.state.loginConfirmed = false; this.state.verification = {}; this.state.phase = 'needs-login'; this.state.enabled = true;
        delete this.state.approvalScope; await this.save();
      }
      return await this.actionReserved(input, scope);
    }
    finally { this.mutationBusy = false; }
  }
  private async actionReserved(input: OmnigentActionRequest, scope: string): Promise<OmnigentIntegrationStatus> {
    if (input.action === 'enable') {
      if (!this.state.enabled) { this.state.loginConfirmed = false; this.state.verification = {}; this.state.phase = 'needs-login'; }
      this.state.enabled = true; delete this.state.issue; await this.onboarding(scope); await this.save();
      if (this.options.adapter && !this.options.prepareDependencies) return this.status(scope);
    }
    if (input.action === 'disable') {
      this.state.enabled = false; this.state.loginConfirmed = false; this.state.verification = {}; this.state.phase = 'disabled'; delete this.state.issue; await this.save();
      return this.publicStatus(true, scope);
    }
    if (!this.state.enabled) throw new OmnigentServiceError('PLUGIN_DISABLED');
    if (input.action === 'setup') {
      const server = normalizeOmnigentServer(input.server);
      if (typeof input.username !== 'string' || !input.username.trim() || input.username.length > 254 || typeof input.password !== 'string' || !input.password || input.password.length > 4096) throw new OmnigentServiceError('INVALID_CREDENTIALS', 400);
      this.state.server = server; this.state.loginConfirmed = false; this.state.verification = {}; this.state.phase = 'connecting';
      this.state.approvalScope = scope;
    } else if (input.action === 'verify' || input.action === 'repair') {
      if (!this.state.loginConfirmed || this.state.approvalScope !== scope) throw new OmnigentServiceError('FRESH_LOGIN_REQUIRED');
      if (input.engine && !ENGINE_IDS.includes(input.engine as typeof ENGINE_IDS[number])) throw new OmnigentServiceError('INVALID_ENGINE', 400);
    }
    if (input.action === 'disconnect') {
      if (this.state.approvalScope !== scope) throw new OmnigentServiceError('FRESH_LOGIN_REQUIRED');
      this.state.loginConfirmed = false; this.state.verification = {};
    }
    if (input.action === 'update' && this.state.loginConfirmed && this.state.approvalScope !== scope) throw new OmnigentServiceError('FRESH_LOGIN_REQUIRED');
    if (input.action !== 'enable') await this.onboarding(scope);
    this.state.operation = { id: randomUUID(), action: input.action, phase: 'running', startedAt: new Date().toISOString() };
    this.state.phase = input.action === 'verify' ? 'verifying' : input.action === 'disconnect' ? 'disconnecting' : input.action === 'update' ? 'checking' : 'connecting';
    delete this.state.issue;
    await this.save();
    this.activeAbort = new AbortController();
    const generation = this.state.installedGeneration;
    const operationId = this.state.operation.id;
    const assertCurrent = async () => {
      const plugin = await this.options.getPlugin(scope);
      if (!plugin || plugin.installedAt !== generation || this.state.installedGeneration !== generation || this.state.operation?.id !== operationId || this.activeAbort?.signal.aborted) {
        this.activeAbort?.abort();
        throw new OmnigentServiceError('OPERATION_SUPERSEDED');
      }
    };
    const pending = this.perform(input, assertCurrent).catch(async error => {
      const code = error instanceof OmnigentServiceError ? error.code : 'ADAPTER_FAILED';
      if (code === 'OPERATION_SUPERSEDED') { this.state.loginConfirmed = false; this.state.verification = {}; this.state.phase = 'needs-login'; }
      this.state.issue = issue(code); this.state.phase = ['NEEDS_LOGIN', 'FRESH_LOGIN_REQUIRED'].includes(code) ? 'needs-login' : 'error';
      if (this.state.operation) this.state.operation = { ...this.state.operation, phase: 'error', finishedAt: new Date().toISOString(), issue: issue(code) };
      await this.save();
    }).finally(() => { this.active = undefined; this.activeAbort = undefined; this.lastCheck = 0; delete input.password; delete input.username; });
    this.active = pending;
    return this.publicStatus(true, scope);
  }
  private async perform(input: OmnigentActionRequest, assertCurrent: () => Promise<void>): Promise<void> {
    const operationCall = async (action: string, additional: AdapterInput = {}) => {
      await assertCurrent();
      const response = await this.call(action, additional);
      await assertCurrent();
      return response;
    };
    await assertCurrent();
    if (['enable', 'setup', 'repair', 'update'].includes(input.action)) {
      if (this.options.prepareDependencies) await this.options.prepareDependencies(input.action === 'update');
      else if (!this.options.adapter) {
        try {
          const ready = await ensureOmnigentDependencies({ stateDir: this.stateDir, env: this.env, update: input.action === 'update' });
          Object.assign(this.env, ready.env, { OD_OMNIGENT_PYTHON: ready.python });
        } catch { throw new OmnigentServiceError('DEPENDENCIES_MISSING'); }
      }
    }
    await assertCurrent();
    if (input.action === 'enable') {
      this.state.phase = this.state.loginConfirmed ? 'needs-verification' : 'needs-login';
      if (this.state.operation) this.state.operation = { ...this.state.operation, phase: 'done', finishedAt: new Date().toISOString() };
      delete this.state.issue; await this.save(); return;
    }
    if (input.action === 'disconnect') {
      const response = await operationCall('disconnect');
      await assertCurrent();
      if (!response.ok) throw new OmnigentServiceError(text(response.error?.code) ?? 'ADAPTER_FAILED');
      this.state.phase = 'needs-login'; this.local = { ...this.local, host: { connected: false } };
    } else {
      if (input.action === 'update' && !this.state.loginConfirmed) {
        this.state.phase = 'needs-login';
        if (this.state.operation) this.state.operation = { ...this.state.operation, phase: 'done', finishedAt: new Date().toISOString() };
        await this.save(); return;
      }
      if (input.action === 'setup' || input.action === 'repair' || input.action === 'update') {
        const response = await operationCall('connect', input.action === 'setup' ? { username: input.username, password: input.password, allowManagedHost: true } : { allowManagedHost: true });
        // Release secret references as soon as this one login attempt returns.
        delete input.password; delete input.username;
        await assertCurrent();
        if (!response.ok) throw new OmnigentServiceError(text(response.error?.code) ?? 'ADAPTER_FAILED');
        this.state.loginConfirmed = true;
      }
      if (!this.state.loginConfirmed) throw new OmnigentServiceError('FRESH_LOGIN_REQUIRED');
      const catalog = await operationCall('catalog');
      await assertCurrent();
      if (!catalog.ok) throw new OmnigentServiceError(text(catalog.error?.code) ?? 'ADAPTER_FAILED');
      this.local = object(catalog.data);
      const inventory = await operationCall('status');
      await assertCurrent();
      if (inventory.ok) {
        const current = object(inventory.data);
        const catalogEngines = object(this.local.engines);
        const currentEngines = object(current.engines);
        this.local = { ...this.local, ...current, engines: Object.fromEntries(ENGINE_IDS.map(id => [id,
          { ...object(catalogEngines[id]), ...object(currentEngines[id]), models: object(catalogEngines[id]).models },
        ])) };
      }
      const raw = object(this.local.engines);
      const installed = ENGINE_IDS.filter(id => object(raw[id]).installed === true || object(raw[id]).available === true);
      if (!installed.length) throw new OmnigentServiceError('NO_AVAILABLE_ENGINE');
      if (input.engine && !installed.includes(input.engine as typeof ENGINE_IDS[number])) throw new OmnigentServiceError('INVALID_ENGINE');
      this.state.phase = 'verifying'; await this.save();
      for (const engine of input.engine ? [input.engine] : installed) {
        const response = await operationCall('verify', { engine, ...(input.model ? { model: input.model } : {}), timeoutSeconds: 120 });
        await assertCurrent();
        const proof = response.ok ? object(response.data) : {};
        const hostId = text(object(this.local.host).id ?? object(this.local.host).host_id);
        const sessionId = text(proof.sessionId);
        const receipt: OmnigentReceipt & { server: string | null; hostId?: string } = {
          engine, ok: proof.ok === true && proof.reply === 'OMNIGENT_CONNECTION_OK' && proof.cleanup === 'stopped'
            && !!text(proof.model) && Array.isArray(object(raw[engine]).models)
            && (object(raw[engine]).models as unknown[]).some(model => object(model).id === proof.model),
          checkedAt: new Date().toISOString(), server: this.state.server,
          ...(hostId ? { hostId } : {}), ...(text(object(raw[engine]).version, 64) ? { cliVersion: text(object(raw[engine]).version, 64)! } : {}),
          ...(sessionId ? { sessionId, url: `${this.state.server}/c/${encodeURIComponent(sessionId)}` } : {}),
          ...(text(proof.model) ? { model: text(proof.model)! } : {}),
          ...(text(proof.cleanup, 32) ? { cleanup: text(proof.cleanup, 32)! } : {}),
          ...(proof.reply === 'OMNIGENT_CONNECTION_OK' ? { reply: 'OMNIGENT_CONNECTION_OK' } : {}),
          ...(proof.ok !== true || proof.cleanup !== 'stopped' ? { reason: proof.cleanup === 'stop_failed' ? 'SESSION_STOP_FAILED' : text(proof.reason ?? response.error?.code, 64) ?? 'NO_REPLY' } : {}),
        };
        this.state.verification[engine] = receipt; await this.save();
      }
      const complete = object(this.local.host).connected === true && installed.every(id => {
        const receipt = this.state.verification[id] as (OmnigentReceipt & { server?: string; hostId?: string }) | undefined;
        return !!receipt?.hostId && !!receipt?.cliVersion && object(raw[id]).ready === true && receipt?.ok === true && receipt.cleanup === 'stopped' && receipt.cliVersion === text(object(raw[id]).version, 64)
          && receipt.server === this.state.server && receipt.hostId === text(object(this.local.host).id ?? object(this.local.host).host_id);
      });
      if (!complete) throw new OmnigentServiceError('INCOMPLETE_CHAT_VERIFICATION');
      this.state.phase = 'ready';
    }
    if (this.state.operation) this.state.operation = { ...this.state.operation, phase: 'done', finishedAt: new Date().toISOString() };
    delete this.state.issue; await this.save();
  }
  async waitForIdle(): Promise<void> { await this.active; }
  private async executeAdapter(input: AdapterInput): Promise<AdapterResult> {
    if (this.activeAbort?.signal.aborted) return { ok: false, error: { code: 'OPERATION_SUPERSEDED' } };
    const python = this.env.OD_OMNIGENT_PYTHON ?? (existsSync('/opt/omnigent/venv/bin/python') ? '/opt/omnigent/venv/bin/python' : 'python3');
    const script = path.join(path.dirname(fileURLToPath(import.meta.url)), 'adapter.py');
    return new Promise(resolve => {
      const child = spawn(python, [script], { env: this.env, stdio: ['pipe', 'pipe', 'pipe'] });
      let output = ''; let finished = false; let termination: NodeJS.Timeout | undefined;
      const finish = (result: AdapterResult) => { if (finished) return; finished = true; clearTimeout(timer); if (termination) clearTimeout(termination); resolve(result); };
      const timer = setTimeout(() => { child.kill('SIGTERM'); termination = setTimeout(() => child.kill('SIGKILL'), 25_000); }, input.action === 'verify' ? 180_000 : 120_000);
      const signal = this.activeAbort?.signal;
      const abort = () => { child.kill('SIGTERM'); termination ??= setTimeout(() => child.kill('SIGKILL'), 25_000); };
      signal?.addEventListener('abort', abort, { once: true });
      if (signal?.aborted) abort();
      child.stdout.on('data', chunk => { if (output.length + chunk.length > 1024 * 1024) child.kill('SIGKILL'); else output += String(chunk); });
      child.stderr.resume(); child.stdin.on('error', () => {});
      child.on('error', () => finish({ ok: false, error: { code: 'DEPENDENCIES_MISSING' } }));
      child.on('close', code => {
        signal?.removeEventListener('abort', abort);
        if (code !== 0) return finish({ ok: false, error: { code: termination ? 'ADAPTER_TIMEOUT' : 'ADAPTER_FAILED' } });
        try { finish(JSON.parse(output) as AdapterResult); } catch { finish({ ok: false, error: { code: 'ADAPTER_FAILED' } }); }
      });
      child.stdin.end(JSON.stringify(input));
    });
  }
}

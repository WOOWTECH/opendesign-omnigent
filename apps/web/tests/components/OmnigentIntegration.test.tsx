// @vitest-environment jsdom
import { act, cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import type { OmnigentIntegrationStatus } from '@open-design/contracts';
import { OmnigentIntegrationProvider, OMNIGENT_POLL_MS } from '../../src/components/OmnigentIntegrationProvider';
import { OmnigentIntegrationSection } from '../../src/components/OmnigentIntegrationSection';
import { OmnigentSetupDialog } from '../../src/components/OmnigentSetupDialog';

const mocks = vi.hoisted(() => ({
  navigate: vi.fn(), upgrade: vi.fn(), open: vi.fn(), account: 0,
  workspace: { context: null, loading: false, identityChangePending: false },
}));
vi.mock('../../src/router', () => ({ navigate: mocks.navigate }));
vi.mock('../../src/providers/registry', () => ({ openExternalUrl: mocks.open }));
vi.mock('../../src/collab/useWorkspaceContext', () => ({
  currentWorkspaceAccountGeneration: () => mocks.account,
  useWorkspaceContext: () => mocks.workspace,
  workspaceIdentityCacheKey: () => 'workspace',
}));
vi.mock('../../src/state/projects', () => ({
  workspaceProjectHeaders: () => ({}), resolvedWorkspaceContextForWrite: () => null,
  resetPluginsCache: vi.fn(), upgradePlugin: mocks.upgrade,
}));

function status(patch: Partial<OmnigentIntegrationStatus> = {}): OmnigentIntegrationStatus {
  return {
    installed: true, enabled: true, phase: 'setup_required', server: null, loginConfirmed: false,
    host: { connected: false, name: 'residenthost' }, installedEngines: ['claude', 'opencode'],
    engines: { claude: { installed: true, models: [{ id: 'claude-model' }] }, opencode: { installed: true } },
    verification: {}, onboarding: { projectId: 'setup-project', conversationId: 'setup-conversation' }, ...patch,
  };
}
function json(value: unknown, code = 200) { return new Response(JSON.stringify(value), { status: code }); }
function renderIntegration() {
  return render(<OmnigentIntegrationProvider><OmnigentIntegrationSection /></OmnigentIntegrationProvider>);
}
beforeEach(() => {
  vi.clearAllMocks(); mocks.account = 0; mocks.workspace = { context: null, loading: false, identityChangePending: false };
  mocks.upgrade.mockResolvedValue({ ok: true });
});
afterEach(() => { cleanup(); vi.useRealTimers(); vi.unstubAllGlobals(); });

describe('native Omnigent integration', () => {
  it('leaves a bundled launcher idle until Configure creates a fresh setup form', async () => {
    const fetch = vi.fn().mockImplementation((_url, request) => Promise.resolve(json(
      request?.method === 'POST' ? { ok: true, status: status({ installationSource: 'bundled' }) }
        : status({ installationSource: 'bundled', onboarding: undefined }),
    )));
    vi.stubGlobal('fetch', fetch);
    renderIntegration();
    const configure = await screen.findByRole('button', { name: 'Configure' });
    expect(screen.queryByTestId('omnigent-setup-dialog')).toBeNull();
    expect(mocks.navigate).not.toHaveBeenCalled();
    expect(fetch.mock.calls.filter(([, request]) => request?.method === 'POST')).toHaveLength(0);
    fireEvent.click(configure);
    expect(await screen.findByTestId('omnigent-setup-dialog')).toBeTruthy();
    expect(mocks.navigate).toHaveBeenCalledWith({ kind: 'project', projectId: 'setup-project', conversationId: 'setup-conversation', fileName: null });
    for (const label of ['Server URL', 'Account', 'Password']) expect((screen.getByLabelText(label) as HTMLInputElement).value).toBe('');
  });

  it('updates the bundled integration runtime without asking the plugin installer to replace it', async () => {
    const current = status({ installationSource: 'bundled' });
    const fetch = vi.fn().mockImplementation((_url, request) => Promise.resolve(json(
      request?.method === 'POST' ? { ok: true, status: current } : current,
    )));
    vi.stubGlobal('fetch', fetch);
    renderIntegration();
    fireEvent.click(await screen.findByRole('button', { name: 'Update plugin' }));
    await waitFor(() => expect(fetch.mock.calls.filter(([, request]) => request?.method === 'POST')
      .map(([, request]) => JSON.parse(request.body))).toEqual([{ action: 'update' }]));
    expect(mocks.upgrade).not.toHaveBeenCalled();
  });

  it('renders no Settings section while the Omnigent plugin is not installed', async () => {
    const fetch = vi.fn().mockImplementation(() => Promise.resolve(json(status({ installed: false, enabled: false, onboarding: undefined }))));
    vi.stubGlobal('fetch', fetch);
    renderIntegration();
    await waitFor(() => expect(fetch).toHaveBeenCalled());
    expect(screen.queryByTestId('omnigent-integration-section')).toBeNull();
    expect(screen.queryByRole('dialog', { name: 'Connect Omnigent' })).toBeNull();
    expect(mocks.navigate).not.toHaveBeenCalled();
    expect(fetch.mock.calls.filter(([, request]) => request?.method === 'POST')).toHaveLength(0);
  });

  it('keeps a disabled install disabled on reload and opens a fresh native form on enable', async () => {
    const fetch = vi.fn().mockImplementation((_url, request) => Promise.resolve(
      json(request?.method === 'POST' ? { ok: true, status: status() } : status({ enabled: false })),
    ));
    vi.stubGlobal('fetch', fetch);
    renderIntegration();
    fireEvent.click(await screen.findByRole('button', { name: 'Enable and set up' }));
    expect(await screen.findByRole('dialog', { name: 'Connect Omnigent' })).toBeTruthy();
    expect(mocks.navigate).toHaveBeenCalledWith({ kind: 'project', projectId: 'setup-project', conversationId: 'setup-conversation', fileName: null });
    for (const label of ['Server URL', 'Account', 'Password']) expect((screen.getByLabelText(label) as HTMLInputElement).value).toBe('');
    expect(fetch.mock.calls.filter(([, request]) => request?.method === 'POST')).toHaveLength(1);
  });

  it('recovers an imported install without onboarding once through enable', async () => {
    const fetch = vi.fn().mockImplementation((_url, request) => Promise.resolve(
      json(request?.method === 'POST' ? { ok: true, status: status() } : status({ onboarding: undefined })),
    ));
    vi.stubGlobal('fetch', fetch);
    renderIntegration();
    expect(await screen.findByTestId('omnigent-setup-dialog')).toBeTruthy();
    expect(fetch.mock.calls.filter(([, request]) => request?.method === 'POST').map(([, request]) => JSON.parse(request.body)))
      .toEqual([{ action: 'enable' }]);
  });

  it('keeps a fresh configure form open when status retains an older successful setup', async () => {
    const old = status({ loginConfirmed: true, phase: 'ready', operation: {
      id: 'previous-setup', action: 'setup', phase: 'done', startedAt: '2026-10-12T00:00:00Z',
    } });
    const fetch = vi.fn().mockImplementation((_url, request) => Promise.resolve(json(request?.method === 'POST' ? { ok: true, status: old } : old)));
    vi.stubGlobal('fetch', fetch);
    renderIntegration();
    fireEvent.click(await screen.findByRole('button', { name: 'Configure' }));
    expect(await screen.findByTestId('omnigent-setup-dialog')).toBeTruthy();
    await act(async () => { window.dispatchEvent(new Event('focus')); });
    expect(screen.getByTestId('omnigent-setup-dialog')).toBeTruthy();
    for (const label of ['Server URL', 'Account', 'Password']) expect((screen.getByLabelText(label) as HTMLInputElement).value).toBe('');
  });

  it('locks a fresh re-enable form until asynchronous SDK preparation finishes', async () => {
    vi.useFakeTimers();
    let current = status({ enabled: false });
    const fetch = vi.fn().mockImplementation((_url, request) => {
      if (request?.method === 'POST') current = status({ operation: {
        id: 'prepare-sdk', action: 'enable', phase: 'running', startedAt: '2026-10-12T00:00:00Z',
      } });
      return Promise.resolve(json(request?.method === 'POST' ? { ok: true, status: current } : current, request?.method === 'POST' ? 202 : 200));
    });
    vi.stubGlobal('fetch', fetch);
    renderIntegration();
    await act(async () => { await Promise.resolve(); });
    fireEvent.click(screen.getByRole('button', { name: 'Enable and set up' }));
    await act(async () => { await Promise.resolve(); });
    expect(screen.getByRole('button', { name: 'Connect' })).toHaveProperty('disabled', true);
    current = status({ operation: { id: 'prepare-sdk', action: 'enable', phase: 'done', startedAt: '2026-10-12T00:00:00Z' } });
    await act(async () => { await vi.advanceTimersByTimeAsync(OMNIGENT_POLL_MS); });
    expect(screen.getByTestId('omnigent-setup-dialog')).toBeTruthy();
    expect(screen.getByRole('button', { name: 'Connect' })).toHaveProperty('disabled', false);
    for (const label of ['Server URL', 'Account', 'Password']) expect((screen.getByLabelText(label) as HTMLInputElement).value).toBe('');
  });

  it('clears private fields on rejection and never forwards secrets to navigation, status or storage', async () => {
    const onStatus = vi.fn();
    const fetch = vi.fn().mockResolvedValue(json({ error: { code: 'AUTH_FAILED', message: 'synthetic-private-password' } }, 401));
    vi.stubGlobal('fetch', fetch);
    const storage = vi.spyOn(Storage.prototype, 'setItem');
    const view = render(<OmnigentSetupDialog workspaceContext={null} busy={false} onClose={vi.fn()} onStatus={onStatus} />);
    expect(screen.getByRole('dialog').classList.contains('ph-no-capture')).toBe(true);
    fireEvent.change(screen.getByLabelText('Server URL'), { target: { value: 'https://omnigent.example' } });
    fireEvent.change(screen.getByLabelText('Account'), { target: { value: 'user' } });
    fireEvent.change(screen.getByLabelText('Password'), { target: { value: 'synthetic-private-password' } });
    fireEvent.click(screen.getByRole('button', { name: 'Connect' }));
    expect(await screen.findByRole('alert')).toHaveProperty('textContent', 'Login was rejected. Enter fresh credentials and try again.');
    for (const label of ['Server URL', 'Account', 'Password']) expect((screen.getByLabelText(label) as HTMLInputElement).value).toBe('');
    expect(onStatus).not.toHaveBeenCalled(); expect(mocks.navigate).not.toHaveBeenCalled(); expect(storage).not.toHaveBeenCalled();
    expect(view.container.textContent).not.toContain('synthetic-private-password');
    expect(document.body.textContent).not.toContain('synthetic-private-password');
    expect(fetch).toHaveBeenCalledTimes(1);
    storage.mockRestore();
  });

  it('polls a background operation and stops after the terminal status', async () => {
    vi.useFakeTimers();
    let current = status({ loginConfirmed: true, phase: 'ready' });
    const fetch = vi.fn().mockImplementation((_url, request) => {
      if (request?.method === 'POST') current = status({ loginConfirmed: true, operation: {
        id: 'job-1', action: 'verify', phase: 'running', startedAt: '2026-10-12T00:00:00Z',
      } });
      return Promise.resolve(json(request?.method === 'POST' ? { ok: true, status: current } : current));
    });
    vi.stubGlobal('fetch', fetch);
    renderIntegration();
    await act(async () => { await Promise.resolve(); });
    fireEvent.click(screen.getByRole('button', { name: 'Verify' }));
    await act(async () => { await Promise.resolve(); });
    expect(screen.getByRole('button', { name: 'Verify' })).toHaveProperty('disabled', true);
    const before = fetch.mock.calls.length;
    await act(async () => { await vi.advanceTimersByTimeAsync(OMNIGENT_POLL_MS - 1); });
    expect(fetch).toHaveBeenCalledTimes(before);
    current = status({ loginConfirmed: true, phase: 'ready', operation: {
      id: 'job-1', action: 'verify', phase: 'done', startedAt: '2026-10-12T00:00:00Z',
    } });
    await act(async () => { await vi.advanceTimersByTimeAsync(1); });
    expect(fetch).toHaveBeenCalledTimes(before + 1);
    await act(async () => { await vi.advanceTimersByTimeAsync(OMNIGENT_POLL_MS * 2); });
    expect(fetch).toHaveBeenCalledTimes(before + 1);
  });

  it('clears detached input values and ignores a credential response after closing', async () => {
    let finish!: (response: Response) => void;
    const fetch = vi.fn().mockImplementation(() => new Promise<Response>((resolve) => { finish = resolve; }));
    vi.stubGlobal('fetch', fetch);
    const onStatus = vi.fn();
    const view = render(<OmnigentSetupDialog workspaceContext={null} busy={false} onClose={vi.fn()} onStatus={onStatus} />);
    const password = screen.getByLabelText('Password') as HTMLInputElement;
    fireEvent.change(screen.getByLabelText('Server URL'), { target: { value: 'https://omnigent.example' } });
    fireEvent.change(screen.getByLabelText('Account'), { target: { value: 'user' } });
    fireEvent.change(password, { target: { value: 'synthetic-private-password' } });
    fireEvent.click(screen.getByRole('button', { name: 'Connect' }));
    expect(password.value).toBe('');
    view.unmount();
    expect(fetch.mock.calls[0]?.[1]?.signal.aborted).toBe(true);
    await act(async () => { finish(json({ ok: true, status: status({ loginConfirmed: true }) })); });
    expect(onStatus).not.toHaveBeenCalled();
    const fresh = render(<OmnigentSetupDialog workspaceContext={null} busy={false} onClose={vi.fn()} onStatus={onStatus} />);
    const unsent = screen.getByLabelText('Password') as HTMLInputElement;
    fireEvent.change(unsent, { target: { value: 'synthetic-unsent-password' } });
    fresh.unmount();
    expect(unsent.value).toBe('');
  });

  it('shows only installed agents and verifies only a selected listed model', async () => {
    const fetch = vi.fn().mockImplementation((_url, request) => Promise.resolve(json(request?.method === 'POST'
      ? { ok: true, status: status({ loginConfirmed: true }) } : status({ loginConfirmed: true }))));
    vi.stubGlobal('fetch', fetch);
    renderIntegration();
    await screen.findByRole('button', { name: 'Verify' });
    expect(screen.queryByText('Codex')).toBeNull(); expect(screen.queryByText('Pi')).toBeNull();
    fireEvent.change(screen.getByLabelText('Agent'), { target: { value: 'claude' } });
    fireEvent.change(screen.getByLabelText('Model'), { target: { value: 'claude-model' } });
    fireEvent.click(screen.getByRole('button', { name: 'Verify' }));
    await waitFor(() => expect(fetch.mock.calls.some(([, request]) => request?.body === JSON.stringify({
      action: 'verify', engine: 'claude', model: 'claude-model',
    }))).toBe(true));
  });

  it('explains vendor auth with native sign-in guidance and lists host modules', async () => {
    const onOpenAgentSettings = vi.fn();
    vi.stubGlobal('fetch', vi.fn().mockImplementation(() => Promise.resolve(json(status({
      engines: { claude: { installed: true, authStatus: 'needs-auth' }, opencode: { installed: true, authStatus: 'version-too-low' } },
      modules: [{ key: 'claude.native', name: 'Claude native' }, { key: 'opencode.native', name: 'OpenCode native' }],
    })))));
    render(<OmnigentIntegrationProvider><OmnigentIntegrationSection onOpenAgentSettings={onOpenAgentSettings} /></OmnigentIntegrationProvider>);
    expect(await screen.findByText('Authentication required')).toBeTruthy();
    expect(screen.getByText('Authenticate with the vendor CLI (sign in or add API credentials) before returning to OpenDesign.')).toBeTruthy();
    expect(screen.getByText('This agent version is unsupported. Review the native Local CLI requirements.')).toBeTruthy();
    fireEvent.click(screen.getByRole('button', { name: 'Local CLI' }));
    expect(onOpenAgentSettings).toHaveBeenCalledTimes(1);
    expect(screen.getByRole('heading', { name: 'Available modules' })).toBeTruthy();
    expect(screen.getByText('Claude native')).toBeTruthy(); expect(screen.getByText('OpenCode native')).toBeTruthy();
    expect(screen.queryByRole('button', { name: 'Install' })).toBeNull();
  });

  it('downloads through native upgrade before backend update and preserves install on download failure', async () => {
    const fetch = vi.fn().mockImplementation((_url, request) => Promise.resolve(json(request?.method === 'POST' ? { ok: true, status: status() } : status())));
    vi.stubGlobal('fetch', fetch);
    renderIntegration();
    fireEvent.click(await screen.findByRole('button', { name: 'Update plugin' }));
    await waitFor(() => expect(fetch.mock.calls.some(([, request]) => request?.body === JSON.stringify({ action: 'update' }))).toBe(true));
    expect(mocks.upgrade).toHaveBeenCalledWith('woow-omnigent', null);
    mocks.upgrade.mockResolvedValueOnce({ ok: false, message: 'synthetic-private-password' });
    await waitFor(() => expect(screen.getByRole('button', { name: 'Update plugin' })).toHaveProperty('disabled', false));
    const before = fetch.mock.calls.filter(([, request]) => request?.method === 'POST').length;
    fireEvent.click(screen.getByRole('button', { name: 'Update plugin' }));
    expect(await screen.findByRole('alert')).toHaveProperty('textContent', 'The native plugin download failed. The installed plugin is still available.');
    expect(fetch.mock.calls.filter(([, request]) => request?.method === 'POST')).toHaveLength(before);
    expect(screen.getAllByText('Claude Code').length).toBeGreaterThan(0);
  });

  it('explains failed engine probes and opens only their existing conversations on the configured server', async () => {
    vi.stubGlobal('fetch', vi.fn().mockImplementation(() => Promise.resolve(json(status({
      loginConfirmed: true, server: 'https://omnigent.example',
      verification: {
        claude: { ok: false, reason: 'OP_TIMEOUT', sessionId: 'claude-probe', url: 'https://omnigent.example/c/claude-probe', reply: 'synthetic-private-output' },
        opencode: { ok: false, reason: 'MODELS_UNAVAILABLE', sessionId: 'opencode-probe', url: 'https://omnigent.example/c/opencode-probe' },
      },
    })))));
    renderIntegration();
    expect(await screen.findByText('Engine verification timed out. Open the engine to review first-run setup, then verify again.')).toBeTruthy();
    expect(screen.getByText('The engine model catalog is unavailable. Review engine setup, refresh status, and verify again.')).toBeTruthy();
    fireEvent.click(screen.getAllByRole('button', { name: 'Open engine' })[0]!);
    expect(mocks.open).toHaveBeenCalledWith('https://omnigent.example/c/claude-probe');
    expect(document.body.textContent).not.toContain('synthetic-private-output');
  });

  it('uses native sign-in guidance when a failed receipt needs engine authentication', async () => {
    const onOpenAgentSettings = vi.fn();
    vi.stubGlobal('fetch', vi.fn().mockImplementation(() => Promise.resolve(json(status({
      loginConfirmed: true, verification: { claude: { ok: false, reason: 'NATIVE_AUTH_REQUIRED' } },
    })))));
    render(<OmnigentIntegrationProvider><OmnigentIntegrationSection onOpenAgentSettings={onOpenAgentSettings} /></OmnigentIntegrationProvider>);
    expect(await screen.findByText('Authenticate with the vendor CLI (sign in or add API credentials) before returning to OpenDesign.')).toBeTruthy();
    fireEvent.click(screen.getByRole('button', { name: 'Local CLI' }));
    expect(onOpenAgentSettings).toHaveBeenCalledTimes(1);
  });

  it('does not render raw receipt reasons or a probe link for an unapproved owner', async () => {
    vi.stubGlobal('fetch', vi.fn().mockImplementation(() => Promise.resolve(json(status({
      loginConfirmed: false, server: 'https://omnigent.example', verification: {
        claude: { ok: false, reason: 'synthetic-private-output', sessionId: 'probe', url: 'https://omnigent.example/c/probe' },
      },
    })))));
    renderIntegration();
    await screen.findByRole('button', { name: 'Configure' });
    expect(document.body.textContent).not.toContain('synthetic-private-output');
    expect(screen.queryByRole('button', { name: 'Open engine' })).toBeNull();
  });

  it('ignores an enable response from a retired workspace identity', async () => {
    let finish!: (response: Response) => void;
    const fetch = vi.fn().mockImplementation((_url, request) => request?.method === 'POST'
      ? new Promise<Response>((resolve) => { finish = resolve; }) : Promise.resolve(json(status({ enabled: false }))));
    vi.stubGlobal('fetch', fetch);
    const view = renderIntegration();
    fireEvent.click(await screen.findByRole('button', { name: 'Enable and set up' }));
    await waitFor(() => expect(finish).toBeTypeOf('function'));
    mocks.account = 1;
    view.rerender(<OmnigentIntegrationProvider><OmnigentIntegrationSection /></OmnigentIntegrationProvider>);
    await act(async () => { finish(json({ ok: true, status: status() })); });
    expect(mocks.navigate).not.toHaveBeenCalled();
    expect(screen.queryByTestId('omnigent-setup-dialog')).toBeNull();
  });
});

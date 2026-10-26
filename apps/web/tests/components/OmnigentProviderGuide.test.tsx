// @vitest-environment jsdom
import { act, cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { OmnigentProviderGuide } from '../../src/components/OmnigentProviderGuide';
import { OmnigentIssue } from '../../src/components/OmnigentIssue';
import { I18nProvider } from '../../src/i18n';
const mocks = vi.hoisted(() => ({ list: vi.fn(), copy: vi.fn(), account: 1,
  workspace: { context: null, loading: false, identityChangePending: false, failure: undefined as string | undefined },
}));
vi.mock('../../src/state/projects', () => ({ listPlugins: mocks.list }));
vi.mock('../../src/lib/copy-to-clipboard', () => ({ copyToClipboard: mocks.copy }));
vi.mock('../../src/collab/useWorkspaceContext', () => ({
  currentWorkspaceAccountGeneration: () => mocks.account,
  useWorkspaceContext: () => mocks.workspace,
  workspaceIdentityCacheKey: () => 'fixture-workspace',
}));
const record = { id: 'woow-cli-terminal', sourceKind: 'bundled', manifest: { od: { launch: { path: '/native-console/' } } } };
const props = { hostName: 'OD target', serverUrl: 'https://omni.example/tenant/', engines: ['claude', 'codex', 'pi', 'opencode'] };
async function expand() {
  const details = screen.getByTestId('omnigent-provider-guide') as HTMLDetailsElement;
  await act(async () => { details.open = true; fireEvent(details, new Event('toggle')); });
}
beforeEach(() => {
  vi.clearAllMocks(); mocks.list.mockResolvedValue([record]); mocks.copy.mockResolvedValue(true);
  mocks.account = 1; mocks.workspace = { context: null, loading: false, identityChangePending: false, failure: undefined };
});
afterEach(() => { cleanup(); });
describe('Omnigent native provider guide', () => {
  it('loads only on expansion and reuses the bundled terminal path', async () => {
    render(<OmnigentProviderGuide {...props} />);
    expect(mocks.list).not.toHaveBeenCalled();
    await expand();
    const terminal = await screen.findByRole('link', { name: 'Open CLI Terminal' });
    expect(terminal.getAttribute('href')).toBe('/native-console/');
    expect(terminal.getAttribute('target')).toBe('_blank');
    expect(terminal.getAttribute('rel')).toContain('noopener');
    expect(mocks.list).toHaveBeenCalledWith({ includeHidden: true, workspaceContext: null, accountGeneration: 1 });
    expect(screen.getByText('Use this machine: OD target')).toBeTruthy();
    expect(screen.getByRole('link', { name: 'Open Omnigent setup' }).getAttribute('href')).toBe(props.serverUrl);
    expect(document.body.textContent).not.toContain('/settings/harnesses');
  });
  it.each([{ records: [] }, { records: [{ ...record, sourceKind: 'user' }] }, { records: [{ ...record, manifest: { od: { launch: { path: 'https://other.example/' } } } }] }])(
    'offers guidance without inventing a terminal path for an absent or untrusted launcher', async ({ records }) => {
      mocks.list.mockResolvedValue(records); render(<OmnigentProviderGuide {...props} />); await expand();
      await waitFor(() => expect(mocks.list).toHaveBeenCalled());
      expect(screen.queryByRole('link', { name: 'Open CLI Terminal' })).toBeNull();
      expect(screen.getByText(/If CLI Terminal is available/)).toBeTruthy();
    },
  );
  it('shows only installed supported commands and copies without executing', async () => {
    render(<OmnigentProviderGuide {...props} engines={['codex', 'unknown']} />); await expand();
    expect(screen.getByText('codex login --device-auth')).toBeTruthy();
    expect(screen.queryByText('claude auth login')).toBeNull();
    fireEvent.click(screen.getByRole('button', { name: 'Copy command' }));
    await screen.findByText('Command copied.');
    expect(mocks.copy).toHaveBeenCalledWith('codex login --device-auth');
    mocks.copy.mockResolvedValueOnce(false);
    fireEvent.click(screen.getByRole('button', { name: 'Copy command' }));
    expect(await screen.findByText('Could not copy. Select and copy the command above.')).toBeTruthy();
  });
  it('does not use an old account response as a terminal launcher', async () => {
    let resolve!: (value: unknown) => void;
    mocks.list.mockImplementationOnce(() => new Promise(r => { resolve = r; }));
    const view = render(<OmnigentProviderGuide {...props} />); await expand();
    mocks.account = 2; mocks.workspace.identityChangePending = true;
    view.rerender(<OmnigentProviderGuide {...props} />);
    await act(async () => resolve([record]));
    expect(screen.queryByRole('link', { name: 'Open CLI Terminal' })).toBeNull();
  });
  it('uses Traditional Chinese copy and escapes the host name', async () => {
    render(<I18nProvider initial="zh-TW"><OmnigentProviderGuide {...props} hostName={'<script>private-host</script>'} /></I18nProvider>);
    await expand();
    expect(screen.getByText('設定模型供應商')).toBeTruthy();
    expect(screen.getByText('請使用這台主機：<script>private-host</script>')).toBeTruthy();
    expect(document.querySelector('script')).toBeNull();
  });
});
describe('structured provider error messages', () => {
  it.each([
    ['FREE_USAGE_LIMIT_REACHED', 'The free provider allowance has been reached.'],
    ['INSUFFICIENT_QUOTA', 'The provider reported insufficient quota.'],
    ['QUOTA_EXCEEDED', 'The provider reported insufficient quota.'],
    ['RATE_LIMITED', 'The provider is rate limiting requests.'],
    ['REPLY_TIMEOUT', 'Engine verification timed out.'],
  ])('explains %s without guessing another failure cause', (code, prefix) => {
    render(<OmnigentIssue code={code} engine />);
    expect(screen.getByRole('alert').textContent?.startsWith(prefix)).toBe(true);
    expect(document.body.textContent).not.toContain(code);
  });
});

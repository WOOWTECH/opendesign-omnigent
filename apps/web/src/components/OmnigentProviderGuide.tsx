import { useEffect, useState } from 'react';
import { builtInLaunchPath } from '@open-design/contracts';
import { Button } from '@open-design/components';
import { currentWorkspaceAccountGeneration, useWorkspaceContext, workspaceIdentityCacheKey } from '../collab/useWorkspaceContext';
import { useT } from '../i18n';
import { copyToClipboard } from '../lib/copy-to-clipboard';
import { listPlugins } from '../state/projects';
import styles from './OmnigentIntegrationSection.module.css';

const LOGIN_COMMANDS: Record<string, string> = {
  claude: 'claude auth login', codex: 'codex login --device-auth',
  opencode: 'opencode auth login', pi: 'pi',
};

/** Reuse this workspace's native launcher; never guess a terminal URL. */
export function OmnigentProviderGuide({ hostName, serverUrl, engines }: {
  hostName: string; serverUrl: string; engines: string[];
}) {
  const t = useT();
  const workspace = useWorkspaceContext();
  const account = currentWorkspaceAccountGeneration();
  const identity = JSON.stringify([account, workspaceIdentityCacheKey(workspace.context)]);
  const [open, setOpen] = useState(false);
  const [launcher, setLauncher] = useState<{ identity: string; path: string | null } | null>(null);
  const [copyResult, setCopyResult] = useState<string | null>(null);
  const ready = !workspace.loading && !workspace.identityChangePending
    && workspace.failure !== 'unavailable' && workspace.failure !== 'reauth-required';
  const terminal = ready && launcher?.identity === identity ? launcher.path : null;
  useEffect(() => {
    if (!open || !ready) return;
    let disposed = false;
    void listPlugins({ includeHidden: true, workspaceContext: workspace.context, accountGeneration: account })
      .then(plugins => {
        if (disposed || currentWorkspaceAccountGeneration() !== account) return;
        const plugin = plugins.find(item => item.id === 'woow-cli-terminal');
        setLauncher({ identity, path: plugin ? builtInLaunchPath(plugin) : null });
      }).catch(() => { if (!disposed) setLauncher({ identity, path: null }); });
    return () => { disposed = true; };
  }, [open, ready, identity, account, workspace.context]);
  return <details className={styles.providerGuide} data-testid="omnigent-provider-guide"
    onToggle={event => setOpen(event.currentTarget.open)}>
    <summary>{t('omnigent.providerGuide')}</summary>
    {open ? <>
    <p>{t('omnigent.providerHost', { host: hostName })}</p>
    <p>{t('omnigent.providerNativeHint')}</p>
    <a className="btn" href={serverUrl} target="_blank" rel="noopener noreferrer"
      data-testid="omnigent-provider-server">{t('omnigent.providerNativeOpen')}</a>
    <p>{t('omnigent.providerTerminalHint')}</p>
    {terminal ? <a className="btn" href={terminal} target="_blank" rel="noopener noreferrer"
      data-testid="omnigent-provider-terminal">{t('omnigent.providerTerminalOpen')}</a>
      : <p className={styles.hint}>{t('omnigent.providerTerminalFallback')}</p>}
    <ul className={styles.loginCommands}>{engines.filter(id => LOGIN_COMMANDS[id]).map(id => <li key={id}>
      <strong>{id === 'claude' ? 'Claude Code' : id === 'codex' ? 'Codex' : id === 'pi' ? 'Pi' : 'OpenCode'}</strong>
      <code>{LOGIN_COMMANDS[id]}</code>
      <Button size="sm" onClick={() => { void copyToClipboard(LOGIN_COMMANDS[id]!).then(ok =>
        setCopyResult(t(ok ? 'omnigent.providerCopied' : 'omnigent.providerCopyFailed'))); }}>
        {t('omnigent.providerCopy')}
      </Button>
      {id === 'pi' ? <span className={styles.hint}>{t('omnigent.providerPiHint')}</span> : null}
    </li>)}</ul>
    {copyResult ? <p aria-live="polite">{copyResult}</p> : null}
    <p>{t('omnigent.providerFinish')}</p>
    </> : null}
  </details>;
}

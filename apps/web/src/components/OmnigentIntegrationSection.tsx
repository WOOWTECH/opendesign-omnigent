import { useState } from 'react';
import { Button } from '@open-design/components';
import { useT } from '../i18n';
import { openExternalUrl } from '../providers/registry';
import { omnigentPublicUrl } from '../providers/omnigent';
import { useOmnigentIntegration } from './OmnigentIntegrationProvider';
import { OmnigentIssue } from './OmnigentIssue';
import { AgentIcon } from './AgentIcon';
import { Icon } from './Icon';
import styles from './OmnigentIntegrationSection.module.css';

export function OmnigentIntegrationSection({ onOpenAgentSettings }: { onOpenAgentSettings?: () => void }) {
  const t = useT();
  const integration = useOmnigentIntegration();
  const [engine, setEngine] = useState('');
  const [model, setModel] = useState('');
  const status = integration?.status;
  const installed = status?.installedEngines.filter((id) => status.engines[id]?.installed) ?? [];
  const selectedEngine = installed.includes(engine) ? engine : '';
  const models = selectedEngine ? status?.engines[selectedEngine]?.models ?? [] : [];
  const selectedModel = models.some((item) => item.id === model) ? model : '';
  const busy = integration?.busy ?? false;
  const serverUrl = omnigentPublicUrl(status?.server);
  const issue = integration?.errorCode ?? status?.operation?.issue?.code ?? status?.issue?.code;
  const stateLabel = !status ? t('common.loading')
    : !status.installed ? t('omnigent.notInstalled')
    : !status.enabled ? t('omnigent.disabled')
    : busy ? t('omnigent.running')
    : status.phase === 'ready' ? t('omnigent.ready') : t('omnigent.needsSetup');

  return <section className={`settings-section ${styles.section}`} data-testid="omnigent-integration-section">
    <div className={styles.heading}>
      <div><h3>Omnigent</h3><p className={styles.hint}>{t('omnigent.description')}</p></div>
      <span role="status" aria-live="polite" className={styles.status}>
        {busy ? <Icon name="spinner" size={14} className="icon-spin" /> : null}{stateLabel}
      </span>
    </div>
    <OmnigentIssue code={issue} />
    {status?.installed ? <>
      <p className={styles.hint}>{status.host.connected ? t('omnigent.hostOnline') : t('omnigent.hostOffline')}
        {status.host.name ? ` · ${status.host.name}` : ''}</p>
      <h4>{t('omnigent.engines')}</h4>
      {installed.length === 0 ? <p className={styles.hint}>{t('omnigent.noEngines')}</p> :
        <ul className={styles.engines}>{installed.map((id) => <li key={id}>
          <AgentIcon id={id} size={20} /><strong>{id === 'claude' ? 'Claude Code' : id === 'opencode' ? 'OpenCode' : id}</strong>
          {status.engines[id]?.version ? <span className={styles.hint}>{status.engines[id].version}</span> : null}
          <span className={styles.hint}>{status.verification[id]?.ok ? t('omnigent.verified') : t('omnigent.unverified')}</span>
          {status.engines[id]?.authStatus === 'needs-auth' ? <div className={styles.engineAdvice}>
            <strong>{t('settings.agentAuthRequired')}</strong>
            <p>{t('settings.agentInstall.stepAuth')}</p>
            <Button size="sm" disabled={!onOpenAgentSettings} onClick={onOpenAgentSettings}>{t('settings.localCli')}</Button>
          </div> : status.engines[id]?.authStatus === 'binary-missing' ? <p className={styles.engineAdvice}>
            {t('settings.testAgentMissing', { agentName: id === 'claude' ? 'Claude Code' : id === 'opencode' ? 'OpenCode' : id })}
          </p> : status.engines[id]?.authStatus === 'version-too-low' ? <p className={styles.engineAdvice}>
            {t('omnigent.engineVersionOld')}
          </p> : null}
        </li>)}</ul>}
      {status.modules?.length ? <>
        <h4>{t('omnigent.modules')}</h4>
        <ul className={styles.modules}>{status.modules.map((module) => <li key={module.key}>
          <strong>{module.name}</strong><span className={styles.hint}>{module.key}</span>
        </li>)}</ul>
      </> : null}
      <div className={styles.selectors}>
        <label>{t('omnigent.engine')}<select value={selectedEngine} disabled={busy || !status.enabled}
          onChange={(event) => { setEngine(event.target.value); setModel(''); }}>
          <option value="">{t('omnigent.allEngines')}</option>
          {installed.map((id) => <option key={id} value={id}>{id === 'claude' ? 'Claude Code' : id === 'opencode' ? 'OpenCode' : id}</option>)}
        </select></label>
        {selectedEngine ? <label>{t('omnigent.model')}<select value={selectedModel} disabled={busy || !status.enabled}
          onChange={(event) => setModel(event.target.value)}>
          <option value="">{t('omnigent.defaultModel')}</option>
          {models.map((item) => <option key={item.id} value={item.id}>{item.displayName ?? item.id}</option>)}
        </select></label> : null}
      </div>
      <div className={styles.actions}>
        <Button variant="primary" disabled={busy} onClick={() => void integration?.activate()}>
          {status.enabled ? t('omnigent.configure') : t('omnigent.enable')}
        </Button>
        <Button disabled={busy || !status.enabled || !status.loginConfirmed || installed.length === 0}
          onClick={() => void integration?.run('verify', selectedEngine ? { engine: selectedEngine, ...(selectedModel ? { model: selectedModel } : {}) } : undefined)}>{t('omnigent.verify')}</Button>
        <Button disabled={!serverUrl} onClick={() => { if (serverUrl) void openExternalUrl(serverUrl); }}>{t('omnigent.open')}</Button>
        <Button disabled={busy || !status.enabled || !status.loginConfirmed} onClick={() => void integration?.run('repair')}>{t('omnigent.repair')}</Button>
        <Button disabled={busy || !status.enabled} onClick={() => void integration?.run('disconnect')}>{t('omnigent.disconnect')}</Button>
        <Button disabled={busy || !status.enabled} onClick={() => void integration?.run('disable')}>{t('omnigent.disable')}</Button>
        <Button disabled={busy || !status.enabled} onClick={() => void integration?.run('update')}>{t('omnigent.update')}</Button>
      </div>
    </> : <p className={styles.hint}>{t('omnigent.installHint')}</p>}
    <Button variant="ghost" disabled={busy} onClick={() => void integration?.refresh()}>{t('omnigent.refresh')}</Button>
  </section>;
}

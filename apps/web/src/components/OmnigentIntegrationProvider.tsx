import { createContext, useCallback, useContext, useEffect, useRef, useState, type ReactNode } from 'react';
import { OMNIGENT_PLUGIN_ID, type OmnigentAction, type OmnigentIntegrationStatus } from '@open-design/contracts';
import {
  currentWorkspaceAccountGeneration, useWorkspaceContext, workspaceIdentityCacheKey,
} from '../collab/useWorkspaceContext';
import { fetchOmnigentStatus, OmnigentRequestError, runOmnigentAction } from '../providers/omnigent';
import { resolvedWorkspaceContextForWrite, resetPluginsCache, upgradePlugin } from '../state/projects';
import { navigate } from '../router';
import { OmnigentSetupDialog } from './OmnigentSetupDialog';
import { useOmnigentIssueMessage } from './OmnigentIssue';
import { Toast } from './Toast';

export const OMNIGENT_POLL_MS = 1500;
interface IntegrationController {
  status: OmnigentIntegrationStatus | null;
  busy: boolean;
  errorCode: string | null;
  activate: () => Promise<void>;
  refresh: () => Promise<void>;
  run: (action: Exclude<OmnigentAction, 'setup' | 'enable'>, verification?: { engine?: string; model?: string }) => Promise<void>;
}
const IntegrationContext = createContext<IntegrationController | null>(null);
export function useOmnigentIntegration(): IntegrationController | null { return useContext(IntegrationContext); }

/** App-owned so the dialog and jobs survive the library → conversation route. */
export function OmnigentIntegrationProvider({ children }: { children: ReactNode }) {
  const workspace = useWorkspaceContext();
  const account = currentWorkspaceAccountGeneration();
  const identity = JSON.stringify([account, workspaceIdentityCacheKey(workspace.context), !!workspace.identityChangePending]);
  const ready = !workspace.loading && !workspace.identityChangePending
    && workspace.failure !== 'unavailable' && workspace.failure !== 'reauth-required';
  const identityRef = useRef(identity);
  identityRef.current = identity;
  const workspaceRef = useRef(workspace);
  workspaceRef.current = workspace;
  const readyRef = useRef(ready);
  readyRef.current = ready;
  const statusRef = useRef<OmnigentIntegrationStatus | null>(null);
  const controller = useRef<AbortController | null>(null);
  const mounted = useRef(true);
  const revision = useRef(0);
  const writePending = useRef(false);
  const [status, setStatus] = useState<OmnigentIntegrationStatus | null>(null);
  const [loadedIdentity, setLoadedIdentity] = useState<string | null>(null);
  const [pending, setPending] = useState(false);
  const [errorCode, setErrorCode] = useState<string | null>(null);
  const [dialog, setDialog] = useState<{ identity: string; key: number } | null>(null);
  const dialogSequence = useRef(0);
  const submittedSetupOperation = useRef<string | null>(null);
  const visibleStatus = loadedIdentity === identity && ready ? status : null;
  statusRef.current = visibleStatus;
  const busy = pending || visibleStatus?.operation?.phase === 'running';
  const errorMessage = useOmnigentIssueMessage(errorCode);

  const isCurrent = useCallback((issuedIdentity: string, issuedAccount: number) =>
    mounted.current && readyRef.current && identityRef.current === issuedIdentity
    && currentWorkspaceAccountGeneration() === issuedAccount, []);

  const acceptStatus = useCallback((next: OmnigentIntegrationStatus) => {
    revision.current += 1;
    setStatus(next);
    setLoadedIdentity(identityRef.current);
    setErrorCode(null);
    if (!next.installed || !next.enabled
      || (next.loginConfirmed && next.operation && next.operation.id === submittedSetupOperation.current
        && next.operation.action === 'setup' && next.operation.phase === 'done')) setDialog(null);
  }, []);

  const activate = useCallback(async () => {
    if (!readyRef.current || writePending.current) return;
    if (statusRef.current?.operation?.phase === 'running') { setErrorCode('OPERATION_BUSY'); return; }
    const issuedIdentity = identityRef.current;
    const issuedAccount = currentWorkspaceAccountGeneration();
    writePending.current = true;
    submittedSetupOperation.current = null;
    setPending(true);
    setErrorCode(null);
    revision.current += 1;
    try {
      const result = await runOmnigentAction('enable', {
        workspaceContext: resolvedWorkspaceContextForWrite(workspaceRef.current), signal: controller.current?.signal,
      });
      if (!isCurrent(issuedIdentity, issuedAccount)) return;
      if (!result.ok || !result.status.installed || !result.status.enabled || !result.status.onboarding)
        throw new OmnigentRequestError('REQUEST_FAILED');
      acceptStatus(result.status);
      navigate({ kind: 'project', projectId: result.status.onboarding.projectId, conversationId: result.status.onboarding.conversationId, fileName: null });
      setDialog({ identity: issuedIdentity, key: ++dialogSequence.current });
    } catch (error) {
      if (isCurrent(issuedIdentity, issuedAccount)) setErrorCode(error instanceof OmnigentRequestError ? error.code : 'REQUEST_FAILED');
    } finally {
      if (identityRef.current === issuedIdentity) { writePending.current = false; setPending(false); }
    }
  }, [acceptStatus, isCurrent]);

  const refresh = useCallback(async () => {
    if (!readyRef.current) return;
    const issuedIdentity = identityRef.current;
    const issuedAccount = currentWorkspaceAccountGeneration();
    const issuedRevision = ++revision.current;
    try {
      const next = await fetchOmnigentStatus({ workspaceContext: workspaceRef.current.context, signal: controller.current?.signal });
      if (!isCurrent(issuedIdentity, issuedAccount) || revision.current !== issuedRevision) return;
      acceptStatus(next);
      // A completed import can outlive its view; recover from daemon-owned state.
      // Existing onboarding prevents re-enabling an intentionally disabled plugin.
      if (next.installed && !next.onboarding && !writePending.current) await activate();
    } catch (error) {
      if (isCurrent(issuedIdentity, issuedAccount) && revision.current === issuedRevision)
        setErrorCode(error instanceof OmnigentRequestError ? error.code : 'REQUEST_FAILED');
    }
  }, [acceptStatus, activate, isCurrent]);

  const run = useCallback(async (action: Exclude<OmnigentAction, 'setup' | 'enable'>, verification?: { engine?: string; model?: string }) => {
    if (!readyRef.current || writePending.current || status?.operation?.phase === 'running') return;
    if (action === 'update' && !statusRef.current?.enabled) { setErrorCode('PLUGIN_DISABLED'); return; }
    const issuedIdentity = identityRef.current;
    const issuedAccount = currentWorkspaceAccountGeneration();
    writePending.current = true;
    setPending(true);
    setErrorCode(null);
    revision.current += 1;
    try {
      const options = { workspaceContext: resolvedWorkspaceContextForWrite(workspaceRef.current), signal: controller.current?.signal };
      if (action === 'update') {
        const upgrade = await upgradePlugin(OMNIGENT_PLUGIN_ID, options.workspaceContext);
        if (!isCurrent(issuedIdentity, issuedAccount)) return;
        if (!upgrade.ok) throw new OmnigentRequestError('UPDATE_FAILED');
        resetPluginsCache();
        window.dispatchEvent(new Event('open-design:plugins-changed'));
      }
      const result = await runOmnigentAction(action, options, verification);
      if (!isCurrent(issuedIdentity, issuedAccount)) return;
      if (!result.ok) throw new OmnigentRequestError('REQUEST_FAILED');
      acceptStatus(result.status);
    } catch (error) {
      if (isCurrent(issuedIdentity, issuedAccount)) setErrorCode(error instanceof OmnigentRequestError ? error.code : 'REQUEST_FAILED');
    } finally {
      if (identityRef.current === issuedIdentity) { writePending.current = false; setPending(false); }
    }
  }, [acceptStatus, isCurrent, status?.operation?.phase]);

  useEffect(() => {
    mounted.current = true;
    controller.current?.abort();
    controller.current = new AbortController();
    revision.current += 1;
    writePending.current = false;
    setPending(false);
    setStatus(null);
    setLoadedIdentity(null);
    setErrorCode(null);
    setDialog(null);
    submittedSetupOperation.current = null;
    if (ready) void refresh();
    return () => { mounted.current = false; controller.current?.abort(); };
  }, [identity, ready, refresh]);

  useEffect(() => {
    const listener = () => { void refresh(); };
    window.addEventListener('open-design:plugins-changed', listener);
    window.addEventListener('focus', listener);
    return () => {
      window.removeEventListener('open-design:plugins-changed', listener);
      window.removeEventListener('focus', listener);
    };
  }, [refresh]);

  useEffect(() => {
    if (visibleStatus?.operation?.phase !== 'running') return;
    let cancelled = false;
    let timer: ReturnType<typeof setTimeout>;
    const poll = async () => {
      await refresh();
      if (!cancelled) timer = setTimeout(() => { void poll(); }, OMNIGENT_POLL_MS);
    };
    timer = setTimeout(() => { void poll(); }, OMNIGENT_POLL_MS);
    return () => { cancelled = true; clearTimeout(timer); };
  }, [visibleStatus?.operation?.id, visibleStatus?.operation?.phase, refresh]);

  return <IntegrationContext.Provider value={{ status: visibleStatus, busy, errorCode, activate, refresh, run }}>
    {children}
    {errorMessage && visibleStatus?.installed ? <Toast message={errorMessage} tone="error" portalToBody
      onDismiss={() => setErrorCode(null)} /> : null}
    {dialog?.identity === identity && ready ? <OmnigentSetupDialog key={`${identity}:${dialog.key}`}
      workspaceContext={workspace.context} busy={busy}
      issueCode={visibleStatus?.operation?.issue?.code ?? visibleStatus?.issue?.code}
      onClose={() => { submittedSetupOperation.current = null; setDialog(null); }}
      onStatus={(next) => {
        if (!isCurrent(identity, account)) return;
        submittedSetupOperation.current = next.operation?.action === 'setup' ? next.operation.id : null;
        acceptStatus(next);
      }} /> : null}
  </IntegrationContext.Provider>;
}

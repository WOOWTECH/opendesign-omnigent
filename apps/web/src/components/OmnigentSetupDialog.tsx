import { useEffect, useId, useRef, useState, type FormEvent } from 'react';
import { Button, Dialog, DialogDescription, DialogFooter, DialogTitle } from '@open-design/components';
import type { OmnigentIntegrationStatus, WorkspaceCollabContext } from '@open-design/contracts';
import { useT } from '../i18n';
import { OmnigentRequestError, setupOmnigent } from '../providers/omnigent';
import { OmnigentIssue } from './OmnigentIssue';
import { Icon } from './Icon';
import styles from './OmnigentSetupDialog.module.css';

interface Props {
  workspaceContext: WorkspaceCollabContext | null;
  busy: boolean;
  issueCode?: string | null;
  onClose: () => void;
  onStatus: (status: OmnigentIntegrationStatus) => void;
}

/** Credentials are private to this mounted form, never props or shared state. */
export function OmnigentSetupDialog({ workspaceContext, busy, issueCode, onClose, onStatus }: Props) {
  const t = useT();
  const titleId = useId();
  const descriptionId = useId();
  const formRef = useRef<HTMLFormElement>(null);
  const firstInput = useRef<HTMLInputElement>(null);
  const mounted = useRef(true);
  const controller = useRef<AbortController | null>(null);
  const submittingRef = useRef(false);
  const [submitting, setSubmitting] = useState(false);
  const [errorCode, setErrorCode] = useState<string | null>(null);

  useEffect(() => {
    mounted.current = true;
    const previousFocus = document.activeElement as HTMLElement | null;
    const form = formRef.current;
    return () => {
      mounted.current = false;
      controller.current?.abort();
      form?.reset();
      previousFocus?.focus();
    };
  }, []);

  useEffect(() => {
    // The dialog can mount while another setup operation is busy, which disables
    // the server field and makes an autofocus attempt a no-op. Focus it once the
    // form becomes available, and again after a rejected submit resets the form.
    if (!busy && !submitting) firstInput.current?.focus();
  }, [busy, submitting]);

  async function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (busy || submittingRef.current) return;
    const form = event.currentTarget;
    const values = new FormData(form);
    const server = String(values.get('server') ?? '').trim();
    const username = String(values.get('username') ?? '').trim();
    const password = String(values.get('password') ?? '');
    // Drop the DOM copy before awaiting; rejected login requires fresh entry.
    form.reset();
    values.delete('password');
    submittingRef.current = true;
    setSubmitting(true);
    setErrorCode(null);
    controller.current = new AbortController();
    try {
      const result = await setupOmnigent({ server, username, password }, {
        workspaceContext, signal: controller.current.signal,
      });
      if (!mounted.current) return;
      if (!result.ok) {
        setErrorCode('REQUEST_FAILED');
        return;
      }
      onStatus(result.status);
      if (result.status.loginConfirmed && result.status.operation?.phase !== 'running') onClose();
    } catch (error) {
      if (mounted.current) setErrorCode(error instanceof OmnigentRequestError ? error.code : 'REQUEST_FAILED');
    } finally {
      submittingRef.current = false;
      if (mounted.current) setSubmitting(false);
    }
  }

  const locked = busy || submitting;
  return (
    <Dialog className={`${styles.dialog} ph-no-capture`} ariaLabelledBy={titleId} ariaDescribedBy={descriptionId}
      onClose={onClose} closeOnEscape data-testid="omnigent-setup-dialog">
      <DialogTitle id={titleId}>{t('omnigent.setupTitle')}</DialogTitle>
      <DialogDescription id={descriptionId}>{t('omnigent.privateHint')}</DialogDescription>
      <form ref={formRef} onSubmit={(event) => void submit(event)} className={styles.form}
        onKeyDown={(event) => {
          if (event.key !== 'Tab') return;
          const controls = Array.from(event.currentTarget.querySelectorAll<HTMLElement>('input:not(:disabled),button:not(:disabled)'));
          const first = controls[0];
          const last = controls[controls.length - 1];
          if (event.shiftKey && document.activeElement === first) { event.preventDefault(); last?.focus(); }
          else if (!event.shiftKey && document.activeElement === last) { event.preventDefault(); first?.focus(); }
        }}>
        <label className={styles.field}>
          <span>{t('omnigent.server')}</span>
          <input ref={firstInput} name="server" type="url" autoComplete="off" required disabled={locked}
            placeholder="https://" spellCheck={false} />
        </label>
        <label className={styles.field}>
          <span>{t('omnigent.username')}</span>
          <input name="username" autoComplete="off" required disabled={locked} spellCheck={false} />
        </label>
        <label className={styles.field}>
          <span>{t('omnigent.password')}</span>
          <input name="password" type="password" autoComplete="new-password" required disabled={locked} />
        </label>
        {locked ? <p role="status" aria-live="polite" className={styles.progress}>
          <Icon name="spinner" size={14} className="icon-spin" />{t('omnigent.running')}
        </p> : null}
        <OmnigentIssue code={errorCode ?? issueCode} />
        <DialogFooter>
          <Button onClick={() => { formRef.current?.reset(); onClose(); }}>{t('common.close')}</Button>
          <Button type="submit" variant="primary" disabled={locked} aria-busy={locked}>
            {t('omnigent.connect')}
          </Button>
        </DialogFooter>
      </form>
    </Dialog>
  );
}

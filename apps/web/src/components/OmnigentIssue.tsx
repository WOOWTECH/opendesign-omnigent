import { useT } from '../i18n';
import { safeOmnigentIssueCode } from '../providers/omnigent';
import styles from './OmnigentIntegrationSection.module.css';

export function useOmnigentIssueMessage(code?: string | null): string | null {
  const t = useT();
  if (!code) return null;
  const safe = safeOmnigentIssueCode(code);
  const message = safe === 'OPERATION_BUSY' ? t('omnigent.errorBusy')
    : ['MISSING_DEPENDENCY', 'DEPENDENCY_MISSING', 'DEPENDENCIES_MISSING', 'ENGINE_UNAVAILABLE', 'NO_AVAILABLE_ENGINE'].includes(safe) ? t('omnigent.errorDependency')
    : ['AUTH_FAILED', 'LOGIN_FAILED', 'INVALID_CREDENTIALS', 'UNAUTHORIZED', 'FRESH_LOGIN_REQUIRED', 'NEEDS_LOGIN'].includes(safe) ? t('omnigent.errorAuth')
    : ['NEEDS_BROWSER_AUTH', 'UNSUPPORTED_AUTH'].includes(safe) ? t('omnigent.errorBrowserAuth')
    : ['INVALID_URL', 'INVALID_SERVER'].includes(safe) ? t('omnigent.errorUrl')
    : safe === 'SERVER_UNREACHABLE' ? t('omnigent.errorNetwork')
    : ['INCOMPLETE_CHAT_VERIFICATION', 'SESSION_STOP_FAILED'].includes(safe) ? t('omnigent.errorVerify')
    : ['INVALID_ENGINE', 'INVALID_MODEL'].includes(safe) ? t('omnigent.errorModel')
    : safe === 'UPDATE_FAILED' ? t('omnigent.errorUpdate') : t('omnigent.errorGeneric');
  return message;
}

export function OmnigentIssue({ code }: { code?: string | null }) {
  const message = useOmnigentIssueMessage(code);
  return message ? <p className={styles.issue} role="alert">{message}</p> : null;
}

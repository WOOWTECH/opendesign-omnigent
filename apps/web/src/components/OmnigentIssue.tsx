import { useT } from '../i18n';
import { safeOmnigentIssueCode } from '../providers/omnigent';
import styles from './OmnigentIntegrationSection.module.css';

export function useOmnigentIssueMessage(code?: string | null, engine = false): string | null {
  const t = useT();
  if (!code) return null;
  const safe = safeOmnigentIssueCode(code);
  const message = engine && ['OP_TIMEOUT', 'REPLY_TIMEOUT', 'NATIVE_START_TIMEOUT', 'TIMEOUT', 'ADAPTER_TIMEOUT'].includes(safe) ? t('omnigent.errorEngineTimeout')
    : engine && ['MODELS_UNAVAILABLE', 'MODEL_UNAVAILABLE'].includes(safe) ? t('omnigent.errorModelCatalog')
    : safe === 'FREE_USAGE_LIMIT_REACHED' ? t('omnigent.errorFreeQuota')
    : ['INSUFFICIENT_QUOTA', 'QUOTA_EXCEEDED'].includes(safe) ? t('omnigent.errorQuota')
    : safe === 'RATE_LIMITED' ? t('omnigent.errorRateLimit')
    : safe === 'ENGINE_NOT_READY' ? t('omnigent.errorEngineNotReady')
    : safe === 'NEEDS_AUTH' ? t('settings.agentAuthRequired')
    : safe === 'OPERATION_BUSY' ? t('omnigent.errorBusy')
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

export function OmnigentIssue({ code, engine = false }: { code?: string | null; engine?: boolean }) {
  const message = useOmnigentIssueMessage(code, engine);
  return message ? <p className={styles.issue} role="alert">{message}</p> : null;
}

import {
  OMNIGENT_PLUGIN_ID,
  type OmnigentAction,
  type OmnigentActionRequest,
  type OmnigentActionResponse,
  type OmnigentIntegrationStatus,
  type WorkspaceCollabContext,
} from '@open-design/contracts';
import { workspaceProjectHeaders } from '../state/projects';

const BASE = '/api/integrations/omnigent';
const SAFE_CODES = new Set([
  'OPERATION_BUSY', 'MISSING_DEPENDENCY', 'DEPENDENCY_MISSING', 'AUTH_FAILED',
  'LOGIN_FAILED', 'INVALID_CREDENTIALS', 'INVALID_URL', 'BAD_REQUEST',
  'FORBIDDEN', 'UNAUTHORIZED', 'NOT_INSTALLED', 'NOT_ENABLED',
  'ENGINE_UNAVAILABLE', 'MODEL_UNAVAILABLE', 'HOST_OFFLINE', 'VERIFY_FAILED',
  'TIMEOUT', 'UPDATE_FAILED', 'REQUEST_FAILED',
  'PLUGIN_NOT_INSTALLED', 'PLUGIN_DISABLED', 'INVALID_SERVER', 'FRESH_LOGIN_REQUIRED',
  'NEEDS_LOGIN', 'NEEDS_BROWSER_AUTH', 'UNSUPPORTED_AUTH', 'SERVER_UNREACHABLE',
  'DEPENDENCIES_MISSING', 'ADAPTER_TIMEOUT', 'ADAPTER_FAILED', 'OPERATION_INTERRUPTED',
  'INCOMPLETE_CHAT_VERIFICATION', 'SESSION_STOP_FAILED', 'NO_AVAILABLE_ENGINE',
  'INVALID_ENGINE', 'INVALID_MODEL', 'STATE_UNREADABLE',
]);

export function safeOmnigentIssueCode(code: unknown): string {
  return typeof code === 'string' && SAFE_CODES.has(code) ? code : 'REQUEST_FAILED';
}

export class OmnigentRequestError extends Error {
  constructor(readonly code: string) {
    super(safeOmnigentIssueCode(code));
    this.code = safeOmnigentIssueCode(code);
  }
}

export interface OmnigentRequestOptions {
  workspaceContext?: WorkspaceCollabContext | null;
  signal?: AbortSignal;
}

async function request<T>(path: string, options: OmnigentRequestOptions, body?: OmnigentActionRequest): Promise<T> {
  try {
    const response = await fetch(`${BASE}${path}`, {
      method: body ? 'POST' : 'GET',
      cache: 'no-store',
      credentials: 'same-origin',
      signal: options.signal,
      headers: {
        ...(body ? { 'Content-Type': 'application/json' } : {}),
        ...(options.workspaceContext ? workspaceProjectHeaders(options.workspaceContext) : {}),
      },
      ...(body ? { body: JSON.stringify(body) } : {}),
    });
    if (!response.ok) {
      let code: unknown;
      try {
        const error = await response.json() as { error?: { code?: unknown } };
        code = error.error?.code;
      } catch { /* Never surface response text or remote messages. */ }
      throw new OmnigentRequestError(safeOmnigentIssueCode(code));
    }
    return await response.json() as T;
  } catch (error) {
    if (error instanceof OmnigentRequestError) throw error;
    throw new OmnigentRequestError('REQUEST_FAILED');
  }
}

export function fetchOmnigentStatus(options: OmnigentRequestOptions = {}): Promise<OmnigentIntegrationStatus> {
  return request('/status', options);
}

export function runOmnigentAction(
  action: Exclude<OmnigentAction, 'setup'>,
  options: OmnigentRequestOptions = {},
  verification?: { engine?: string; model?: string },
): Promise<OmnigentActionResponse> {
  const body: OmnigentActionRequest = { action };
  if (action === 'verify') {
    if (verification?.engine) body.engine = verification.engine;
    if (verification?.model) body.model = verification.model;
  }
  return request('/actions', options, body);
}

/** Native dialog only. Never forward this request through shared app/config state. */
export function setupOmnigent(
  credentials: { server: string; username: string; password: string },
  options: OmnigentRequestOptions = {},
): Promise<OmnigentActionResponse> {
  return request('/actions', options, { action: 'setup', ...credentials });
}

export function isOmnigentPlugin(record: { id: string } | null | undefined): boolean {
  return record?.id === OMNIGENT_PLUGIN_ID;
}

export function omnigentPublicUrl(server: string | null | undefined): string | null {
  if (!server) return null;
  try {
    const url = new URL(server);
    if (!['https:', 'http:'].includes(url.protocol) || url.username || url.password || url.search || url.hash) return null;
    return url.href;
  } catch { return null; }
}

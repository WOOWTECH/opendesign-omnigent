import type { Express, Request, Response } from 'express';
import type { OmnigentAction, OmnigentActionRequest } from '@open-design/contracts';
import { guardSameOrigin, type OriginContext } from '../http/origin-guard.js';
import { resolveOptionalLocalWorkspaceRequestAuthority } from '../collab/workspace-resource-mutation.js';
import { OmnigentIntegrationService, OmnigentServiceError } from '../integrations/omnigent/service.js';

export interface RegisterOmnigentRoutesDeps {
  service: OmnigentIntegrationService;
  origin: OriginContext;
  authorize: (req: Request, res: Response) => Promise<boolean>;
}
const ACTIONS = new Set<OmnigentAction>(['enable', 'disable', 'setup', 'verify', 'disconnect', 'repair', 'update']);
function scope(req: Request, res: Response): string | undefined {
  const authority = resolveOptionalLocalWorkspaceRequestAuthority(req);
  if (!authority.ok) { res.status(authority.status).json({ error: { code: authority.code, message: authority.message } }); return; }
  return authority.context ? `${authority.context.workspaceId}:${authority.context.workspaceMemberId}` : 'local';
}
function failure(res: Response, error: unknown): void {
  const known = error instanceof OmnigentServiceError;
  res.status(known ? error.status : 500).json({ error: { code: known ? error.code : 'ADAPTER_FAILED', message: known ? error.message : 'Omnigent operation did not finish.' } });
}
export function registerOmnigentRoutes(app: Express, deps: RegisterOmnigentRoutesDeps): void {
  app.get('/api/integrations/omnigent/status', async (req, res) => {
    try {
      if (!await deps.authorize(req, res)) return;
      const key = scope(req, res); if (key === undefined) return;
      res.setHeader('Cache-Control', 'no-store');
      res.json(await deps.service.status(key));
    } catch (error) { failure(res, error); }
  });
  app.post('/api/integrations/omnigent/actions', async (req, res) => {
    try {
      const origin = guardSameOrigin(req, deps.origin);
      if (!origin.ok) { res.status(403).json({ error: { code: 'FORBIDDEN', message: 'Cross-origin request rejected.' } }); return; }
      if (!await deps.authorize(req, res)) return;
      const key = scope(req, res); if (key === undefined) return;
      const body = req.body as Record<string, unknown> | undefined;
      const action = body?.action as OmnigentAction;
      if (!ACTIONS.has(action)) { res.status(400).json({ error: { code: 'BAD_REQUEST', message: 'Choose a supported Omnigent action.' } }); return; }
      const input: OmnigentActionRequest = { action };
      if (action === 'setup') {
        for (const field of ['server', 'username', 'password'] as const) {
          if (typeof body?.[field] !== 'string') throw new OmnigentServiceError('INVALID_CREDENTIALS', 400);
          input[field] = body[field];
        }
      } else if (action === 'verify') {
        for (const field of ['engine', 'model'] as const) {
          if (body?.[field] !== undefined) {
            if (typeof body[field] !== 'string' || body[field].length > 256) throw new OmnigentServiceError(field === 'engine' ? 'INVALID_ENGINE' : 'INVALID_MODEL', 400);
            input[field] = body[field];
          }
        }
      }
      const status = await deps.service.action(input, key);
      res.setHeader('Cache-Control', 'no-store');
      res.status(status.operation?.phase === 'running' ? 202 : 200).json({ ok: true, status });
    } catch (error) { failure(res, error); }
  });
}

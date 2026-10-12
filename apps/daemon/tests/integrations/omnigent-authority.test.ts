import type { Request, Response } from 'express';
import type Database from 'better-sqlite3';
import { describe, expect, it, vi } from 'vitest';
import { apiTokenAuthorizationMatches } from '../../src/api-token-auth.js';
import { createAuthorizeOmnigentRequest, createOmnigentOnboarding } from '../../src/integrations/omnigent/onboarding.js';

function request(extra: Record<string, string> = {}): Request {
  const headers = { host: '127.0.0.1:7456', authorization: 'Bearer native-fixture',
    'x-od-workspace-id': 'workspace-a', 'x-od-workspace-member-id': 'member-a', ...extra };
  return { method: 'POST', headers, get: (name: string) => headers[name as keyof typeof headers] } as unknown as Request;
}

function fixture() {
  // These authority tests never create a project or execute a DB statement.
  const store = createOmnigentOnboarding({ db: {} as Database.Database, projectsDir: '/unused' });
  const getPlugin = vi.fn(async () => ({ id: 'woow-omnigent' }));
  const authorize = createAuthorizeOmnigentRequest({
    origin: { resolvedPortRef: { current: 7456 } }, onboarding: store, getPlugin,
    authorizeNativeRequest: req => apiTokenAuthorizationMatches(req.get('authorization'), 'native-fixture'),
  });
  const response = { status: vi.fn().mockReturnThis(), json: vi.fn().mockReturnThis(), setHeader: vi.fn() } as unknown as Response;
  return { store, getPlugin, authorize, response };
}

describe('Omnigent request authority without persistence', () => {
  it('uses native Bearer and Basic token validation and captures only the exact request workspace/member', async () => {
    const { store, getPlugin, authorize, response } = fixture();
    expect(await authorize(request(), response)).toBe(true);
    expect(store.context('workspace-a:member-a')).toMatchObject({ workspaceId: 'workspace-a', workspaceMemberId: 'member-a' });
    expect(getPlugin).toHaveBeenCalledWith(expect.objectContaining({ workspaceId: 'workspace-a', workspaceMemberId: 'member-a' }));
    const basic = `Basic ${Buffer.from('open-design:native-fixture').toString('base64')}`;
    expect(await authorize(request({ authorization: basic }), response)).toBe(true);
    expect(await authorize(request({ authorization: 'Bearer wrong-fixture-token' }), response)).toBe(false);
    expect(response.status).toHaveBeenLastCalledWith(401);
    expect(JSON.stringify(vi.mocked(response.json).mock.calls)).not.toContain('wrong-fixture-token');
  });

  it('rejects cross-origin and partial identities before querying the installed plugin catalog', async () => {
    const { getPlugin, authorize, response } = fixture();
    expect(await authorize(request({ origin: 'https://unrelated.example' }), response)).toBe(false);
    expect(response.status).toHaveBeenCalledWith(403);
    expect(getPlugin).not.toHaveBeenCalled();
    expect(await authorize(request({ 'x-od-workspace-member-id': '' }), response)).toBe(false);
    expect(response.status).toHaveBeenLastCalledWith(400);
    expect(response.json).toHaveBeenLastCalledWith({ error: expect.objectContaining({ code: 'WORKSPACE_CONTEXT_INCOMPLETE' }) });
    expect(getPlugin).not.toHaveBeenCalled();
  });

  it('cannot substitute a different installed plugin or mutate without the canonical scoped installation', async () => {
    const { getPlugin, authorize, response } = fixture();
    getPlugin.mockResolvedValue({ id: 'different-plugin' });
    expect(await authorize(request(), response)).toBe(false);
    expect(response.status).toHaveBeenCalledWith(409);
    expect(response.json).toHaveBeenCalledWith({ error: expect.objectContaining({ code: 'PLUGIN_NOT_INSTALLED' }) });
  });
});

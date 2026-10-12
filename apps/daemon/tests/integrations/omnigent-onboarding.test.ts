import { mkdtemp, readdir, rm, stat } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import path from 'node:path';
import type { Request, Response } from 'express';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import {
  closeDatabase, deleteConversation, deleteProject, getConversation, getProject,
  getWorkspaceProjectByProjectId, listConversations, listMessages, listProjects,
  openDatabase, rebindWorkspaceProject, updateWorkspaceProject,
} from '../../src/db.js';
import { createOmnigentOnboarding } from '../../src/integrations/omnigent/onboarding.js';

let root: string;
let db: ReturnType<typeof openDatabase>;
let store: ReturnType<typeof createOmnigentOnboarding>;

beforeEach(async () => {
  root = await mkdtemp(path.join(tmpdir(), 'od-omnigent-onboarding-'));
  db = openDatabase(root, { dataDir: root });
  store = createOmnigentOnboarding({ db, projectsDir: path.join(root, 'projects') });
});

afterEach(async () => {
  closeDatabase();
  await rm(root, { recursive: true, force: true });
});

function capture(workspaceId = 'workspace-a', memberId = 'member-a'): string {
  const headers: Record<string, string> = {
    'x-od-workspace-id': workspaceId,
    'x-od-workspace-member-id': memberId,
    'x-od-workspace-type': 'team',
    'x-od-workspace-role': 'owner',
  };
  const req = { get: (name: string) => headers[name] } as Request;
  const key = store.captureScope(req, {} as Response);
  expect(key).toBe(`${workspaceId}:${memberId}`);
  return key!;
}

describe('Omnigent native onboarding', () => {
  it('creates one private project and chat conversation with a static guide and no run or prompt', async () => {
    const scope = capture();
    const ids = await store.create(scope);
    expect(getProject(db, ids.projectId)).toMatchObject({
      name: 'Omnigent connection setup',
      metadata: { skipDiscoveryBrief: true, omnigentOnboardingScope: scope },
    });
    expect(getProject(db, ids.projectId)?.pendingPrompt).toBeUndefined();
    expect(db.prepare('SELECT pending_prompt AS pendingPrompt FROM projects WHERE id = ?').get(ids.projectId)).toMatchObject({ pendingPrompt: null });
    expect(getConversation(db, ids.conversationId)).toMatchObject({ projectId: ids.projectId, sessionMode: 'chat' });
    expect(getWorkspaceProjectByProjectId(db, ids.projectId)).toMatchObject({
      workspaceId: 'workspace-a', visibility: 'personal', resourceState: 'active',
      createdByWorkspaceMemberId: 'member-a', updatedByWorkspaceMemberId: 'member-a',
      syncState: 'local_only', resourceHubResourceId: null,
    });
    const messages = listMessages(db, ids.conversationId);
    expect(messages).toHaveLength(1);
    expect(messages[0]).toMatchObject({ role: 'assistant' });
    expect(messages[0]?.runId).toBeUndefined();
    expect(messages[0]?.content).toContain('connection dialog');
    expect(db.prepare('SELECT COUNT(*) AS count FROM messages WHERE run_id IS NOT NULL').get()).toMatchObject({ count: 0 });
    expect((await stat(path.join(root, 'projects', ids.projectId))).isDirectory()).toBe(true);
  });

  it('is idempotent through concurrent calls, cached IDs, and a new store after restart', async () => {
    const scope = capture();
    const [first, concurrent] = await Promise.all([store.create(scope), store.create(scope)]);
    expect(concurrent).toEqual(first);
    expect(await store.create(scope, first)).toEqual(first);
    store = createOmnigentOnboarding({ db, projectsDir: path.join(root, 'projects') });
    capture();
    expect(await store.create(scope)).toEqual(first);
    expect(listProjects(db)).toHaveLength(1);
    expect(listConversations(db, first.projectId)).toHaveLength(1);
    expect(listMessages(db, first.conversationId)).toHaveLength(1);
  });

  it('never reuses another workspace or another member project even when handed their IDs', async () => {
    const a = await store.create(capture());
    const bScope = capture('workspace-b', 'member-b');
    expect(store.owns(bScope, a)).toBe(false);
    const b = await store.create(bScope, a);
    expect(b.projectId).not.toBe(a.projectId);
    const otherMemberScope = capture('workspace-a', 'member-b');
    expect(store.owns(otherMemberScope, a)).toBe(false);
    const member = await store.create(otherMemberScope, a);
    expect(member.projectId).not.toBe(a.projectId);
    expect(getWorkspaceProjectByProjectId(db, a.projectId)?.createdByWorkspaceMemberId).toBe('member-a');
    expect(listProjects(db)).toHaveLength(3);
  });

  it('never leaks workspace-bound IDs through the headerless local lane', async () => {
    const scoped = await store.create(capture());
    expect(store.owns('local', scoped)).toBe(false);
    const local = await store.create('local', scoped);
    expect(local.projectId).not.toBe(scoped.projectId);
    expect(getWorkspaceProjectByProjectId(db, local.projectId)).toBeUndefined();
  });

  it('repairs a deleted conversation inside the same owned project', async () => {
    const scope = capture();
    const first = await store.create(scope);
    deleteConversation(db, first.conversationId);
    expect(store.owns(scope, first)).toBe(false);
    const repaired = await store.create(scope, first);
    expect(repaired.projectId).toBe(first.projectId);
    expect(repaired.conversationId).not.toBe(first.conversationId);
    expect(listProjects(db)).toHaveLength(1);
    expect(listMessages(db, repaired.conversationId)).toHaveLength(1);
  });

  it('recreates deleted projects and does not reuse moved or frozen projects', async () => {
    const scope = capture();
    const first = await store.create(scope);
    rebindWorkspaceProject(db, first.projectId, { workspaceId: 'workspace-b', createdByWorkspaceMemberId: 'member-b' });
    expect(store.owns(scope, first)).toBe(false);
    const second = await store.create(scope, first);
    updateWorkspaceProject(db, 'workspace-a', second.projectId, { resourceState: 'frozen' });
    const third = await store.create(scope, second);
    expect(new Set([first.projectId, second.projectId, third.projectId]).size).toBe(3);
    deleteProject(db, third.projectId);
    const fourth = await store.create(scope, third);
    expect(fourth.projectId).not.toBe(third.projectId);
  });

  it('rejects a conversation whose project does not match the cached pair', async () => {
    const scope = capture();
    const first = await store.create(scope);
    const second = await store.create(capture('workspace-b', 'member-b'));
    expect(store.owns(scope, { projectId: first.projectId, conversationId: second.conversationId })).toBe(false);
    expect(await store.create(scope, { projectId: first.projectId, conversationId: second.conversationId })).toEqual(first);
  });

  it('rejects partial or ambiguous workspace scope and never creates an unbound fallback', async () => {
    const response = { status: vi.fn().mockReturnThis(), json: vi.fn().mockReturnThis() } as unknown as Response;
    const req = { get: (name: string) => name === 'x-od-workspace-id' ? 'workspace-a' : undefined } as Request;
    expect(store.captureScope(req, response)).toBeNull();
    expect(response.status).toHaveBeenCalledWith(400);
    expect(listProjects(db)).toHaveLength(0);
    capture('a:b', 'c');
    const headers: Record<string, string> = { 'x-od-workspace-id': 'a', 'x-od-workspace-member-id': 'b:c' };
    expect(store.captureScope({ get: (name: string) => headers[name] } as Request, response)).toBeNull();
    expect(response.json).toHaveBeenLastCalledWith(expect.objectContaining({ error: expect.objectContaining({ code: 'WORKSPACE_CONTEXT_CONFLICT' }) }));
    await expect(store.create('unseen:member')).rejects.toMatchObject({ code: 'WORKSPACE_CONTEXT_REQUIRED' });
  });

  it('rolls back project, conversation, and binding together and removes only its new folder on failure', async () => {
    const scope = capture();
    db.exec(`CREATE TRIGGER fail_onboarding_binding BEFORE INSERT ON workspace_projects
      BEGIN SELECT RAISE(ABORT, 'synthetic binding failure'); END`);
    await expect(store.create(scope)).rejects.toThrow('synthetic binding failure');
    expect(listProjects(db)).toHaveLength(0);
    expect(db.prepare('SELECT COUNT(*) AS count FROM conversations').get()).toMatchObject({ count: 0 });
    expect(db.prepare('SELECT COUNT(*) AS count FROM messages').get()).toMatchObject({ count: 0 });
    expect(await readdir(path.join(root, 'projects'))).toEqual([]);
    db.exec('DROP TRIGGER fail_onboarding_binding');
    expect(await store.create(scope)).toHaveProperty('conversationId');
  });
});

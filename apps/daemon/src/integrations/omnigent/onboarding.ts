import { randomUUID } from 'node:crypto';
import { stat } from 'node:fs/promises';
import path from 'node:path';
import type Database from 'better-sqlite3';
import type { Request, Response } from 'express';
import type { InstalledPluginRecord, OmnigentIntegrationStatus, OmnigentOnboarding } from '@open-design/contracts';
import { bindCreatedProjectToWorkspace, localProjectWorkspaceAttribution } from '../../collab/created-project-workspace.js';
import {
  resolveOptionalLocalWorkspaceRequestAuthority,
  type WorkspaceResourceContext,
} from '../../collab/workspace-resource-mutation.js';
import {
  ensureWorkspaceProject,
  getConversation,
  getProject,
  getWorkspaceProjectByProjectId,
  insertConversation,
  insertProject,
  updateProject,
  upsertMessage,
} from '../../db.js';
import { guardSameOrigin, type OriginContext } from '../../http/origin-guard.js';
import { ensureProject, removeProjectDir } from '../../projects.js';
import { OMNIGENT_PLUGIN_ID, OmnigentServiceError } from './service.js';

const TITLE = 'Omnigent connection setup';
const GUIDANCE = 'Use the Omnigent connection dialog to sign in. Keep your account and password out of this conversation. After connecting, verify the installed engines and choose an available model.';

export interface OmnigentOnboardingOptions {
  db: Database.Database;
  projectsDir: string;
}

export interface OmnigentOnboardingStore {
  captureScope(req: Request, res: Response): string | null;
  context(scope: string): WorkspaceResourceContext | null;
  create(scope: string, existing?: OmnigentOnboarding): Promise<OmnigentOnboarding>;
  owns(scope: string, existing: OmnigentOnboarding): boolean;
}

/**
 * Reuse only a dedicated managed project owned by the exact Workspace/member.
 * The legacy local lane reuses only unbound projects. Native binding, the
 * conversation, and its static guide are committed together; no Run is started.
 */
export function createOmnigentOnboarding(options: OmnigentOnboardingOptions): OmnigentOnboardingStore {
  const { db, projectsDir } = options;
  const scopes = new Map<string, WorkspaceResourceContext | null>([['local', null]]);
  const pending = new Map<string, Promise<OmnigentOnboarding>>();

  function context(scope: string): WorkspaceResourceContext | null {
    if (!scopes.has(scope)) throw new OmnigentServiceError('WORKSPACE_CONTEXT_REQUIRED', 400);
    return scopes.get(scope) ?? null;
  }

  function captureScope(req: Request, res: Response): string | null {
    const resolved = resolveOptionalLocalWorkspaceRequestAuthority(req);
    if (!resolved.ok) {
      res.status(resolved.status).json({ error: { code: resolved.code, message: resolved.message } });
      return null;
    }
    const attribution = localProjectWorkspaceAttribution(req);
    const key = attribution ? `${attribution.workspaceId}:${attribution.workspaceMemberId}` : 'local';
    const existing = scopes.get(key);
    if (existing && attribution && (existing.workspaceId !== attribution.workspaceId
      || existing.workspaceMemberId !== attribution.workspaceMemberId)) {
      res.status(400).json({ error: { code: 'WORKSPACE_CONTEXT_CONFLICT', message: 'Workspace and member scope is ambiguous.' } });
      return null;
    }
    scopes.set(key, attribution);
    return key;
  }

  function ownedProject(scope: string, projectId: string): ReturnType<typeof getProject> {
    const attribution = context(scope);
    const project = getProject(db, projectId);
    if (!project || project.metadata?.omnigentOnboardingScope !== scope
      || project.metadata?.omnigentOnboardingPluginId !== OMNIGENT_PLUGIN_ID
      || project.metadata?.baseDir) return null;
    const binding = getWorkspaceProjectByProjectId(db, projectId);
    if (!attribution) return binding ? null : project;
    return binding?.workspaceId === attribution.workspaceId
      && binding.createdByWorkspaceMemberId === attribution.workspaceMemberId
      && binding.visibility === 'personal'
      && binding.resourceState === 'active'
      && binding.syncState === 'local_only' ? project : null;
  }

  function owns(scope: string, existing: OmnigentOnboarding): boolean {
    if (!existing || typeof existing.projectId !== 'string' || typeof existing.conversationId !== 'string') return false;
    const project = ownedProject(scope, existing.projectId);
    return !!project && project.metadata?.omnigentOnboardingConversationId === existing.conversationId
      && getConversation(db, existing.conversationId)?.projectId === existing.projectId;
  }

  async function createOwned(scope: string, existing?: OmnigentOnboarding): Promise<OmnigentOnboarding> {
    context(scope);
    if (existing && owns(scope, existing)) return existing;
    const candidates = db.prepare(`
      SELECT id FROM projects
       WHERE json_valid(metadata_json)
         AND json_extract(metadata_json, '$.omnigentOnboardingPluginId') = ?
         AND json_extract(metadata_json, '$.omnigentOnboardingScope') = ?
       ORDER BY created_at ASC, id ASC
    `).all(OMNIGENT_PLUGIN_ID, scope) as Array<{ id: string }>;
    const project = candidates.map(candidate => ownedProject(scope, candidate.id)).find(Boolean);
    if (project) {
      const conversationId = project.metadata?.omnigentOnboardingConversationId;
      if (typeof conversationId === 'string' && owns(scope, { projectId: project.id, conversationId })) {
        return { projectId: project.id, conversationId };
      }
    }
    const projectId = project?.id ?? randomUUID();
    const conversationId = randomUUID();
    const now = Date.now();
    await ensureProject(projectsDir, projectId);
    try {
      return db.transaction(() => {
        const metadata = {
          ...(project?.metadata ?? {}),
          kind: 'prototype',
          templateId: `plugin:${OMNIGENT_PLUGIN_ID}`,
          templateLabel: TITLE,
          skipDiscoveryBrief: true,
          omnigentOnboardingPluginId: OMNIGENT_PLUGIN_ID,
          omnigentOnboardingScope: scope,
          omnigentOnboardingConversationId: conversationId,
        };
        if (project) updateProject(db, projectId, { metadata });
        else insertProject(db, {
          id: projectId, name: TITLE, skillId: null, designSystemId: null,
          pendingPrompt: null, metadata, createdAt: now, updatedAt: now,
        });
        insertConversation(db, { id: conversationId, projectId, title: TITLE, sessionMode: 'chat', createdAt: now, updatedAt: now });
        bindCreatedProjectToWorkspace(input => ensureWorkspaceProject(db, input), context(scope), projectId, now);
        upsertMessage(db, conversationId, { id: randomUUID(), role: 'assistant', content: GUIDANCE, createdAt: now });
        return { projectId, conversationId };
      })();
    } catch (error) {
      if (!project) await removeProjectDir(projectsDir, projectId).catch(() => undefined);
      throw error;
    }
  }

  function create(scope: string, existing?: OmnigentOnboarding): Promise<OmnigentOnboarding> {
    let work = pending.get(scope);
    if (!work) {
      work = createOwned(scope, existing).finally(() => { pending.delete(scope); });
      pending.set(scope, work);
    }
    return work;
  }

  return { captureScope, context, create, owns };
}

export interface AuthorizeOmnigentRequestOptions {
  origin: OriginContext;
  onboarding: OmnigentOnboardingStore;
  authorizeNativeRequest(req: Request): boolean;
  getPlugin(context: WorkspaceResourceContext | null): Promise<{ id: string } | null>;
}

function safeNumber(value: unknown): number | null {
  return typeof value === 'number' && Number.isFinite(value) && value >= 0 && value <= Number.MAX_SAFE_INTEGER ? value : null;
}

/** Resolve one canonical installation identity for both route access and service fences. */
export function createOmnigentInstallationLookup(
  resolve: (context: WorkspaceResourceContext | null) => Promise<InstalledPluginRecord | null>,
): (context: WorkspaceResourceContext | null) => Promise<InstalledPluginRecord | null> {
  return async context => {
    const record = await resolve(context);
    if (!record || record.id !== OMNIGENT_PLUGIN_ID) return null;
    let installedAt = safeNumber(record.installedAt);
    let updatedAt = safeNumber(record.updatedAt);
    if (record.source.startsWith('team:plugin:')) {
      // Native Team folder resolution stamps Date.now() on every read. The
      // durable materialization marker's birth/mtime instead identifies the
      // installed generation, and changes when a new staged copy replaces it.
      try {
        const marker = await stat(path.join(record.fsPath, '.od-team-resource.json'));
        installedAt = safeNumber(marker.birthtimeMs > 0 ? marker.birthtimeMs : marker.mtimeMs);
        updatedAt = safeNumber(marker.mtimeMs);
      } catch { return null; }
    }
    if (installedAt === null || updatedAt === null) return null;
    return { ...record, installedAt, updatedAt };
  };
}

/** Use native API authentication and the same installed-plugin catalog scope as Plugin Remix. */
export function createAuthorizeOmnigentRequest(options: AuthorizeOmnigentRequestOptions): (req: Request, res: Response) => Promise<boolean> {
  return async (req, res) => {
    if (!options.authorizeNativeRequest(req)) {
      res.status(401).json({ error: { code: 'API_TOKEN_REQUIRED', message: 'OpenDesign authentication is required.' } });
      return false;
    }
    if (!guardSameOrigin(req, options.origin).ok) {
      res.status(403).json({ error: { code: 'FORBIDDEN', message: 'Cross-origin request rejected.' } });
      return false;
    }
    const scope = options.onboarding.captureScope(req, res);
    if (scope === null) return false;
    const plugin = await options.getPlugin(options.onboarding.context(scope));
    if (plugin?.id === OMNIGENT_PLUGIN_ID) return true;
    if (req.method === 'GET') {
      const absent: OmnigentIntegrationStatus = {
        installed: false, enabled: false, phase: 'not-installed', server: null,
        loginConfirmed: false, host: { connected: false }, installedEngines: [], engines: {}, verification: {},
      };
      res.setHeader('Cache-Control', 'no-store');
      res.json(absent);
    } else res.status(409).json({ error: { code: 'PLUGIN_NOT_INSTALLED', message: 'Install the Omnigent plugin in this workspace first.' } });
    return false;
  };
}

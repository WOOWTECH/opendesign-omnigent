import { mkdtemp, mkdir, writeFile, rm, symlink } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import path from 'node:path';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { ensureOmnigentDependencies, type DependencyCommandRunner } from '../../../src/integrations/omnigent/dependencies.js';

// The production image path is simulated so these tests also run on developer machines without that image.
vi.mock('node:fs/promises', async importOriginal => {
  const fs = await importOriginal<typeof import('node:fs/promises')>();
  return { ...fs,
    access: async (...args: Parameters<typeof fs.access>) => args[0] === '/opt/omnigent/venv/bin/python' ? undefined : fs.access(...args),
    stat: async (...args: Parameters<typeof fs.stat>) => args[0] === '/opt/omnigent/venv/bin/python' ? { isFile: () => true } : fs.stat(...args),
  };
});

describe('private Omnigent dependencies', () => {
  let root: string;
  let stateDir: string;
  let env: NodeJS.ProcessEnv;
  let installed: Set<string>;
  let run: ReturnType<typeof vi.fn<DependencyCommandRunner>>;
  const imagePython = '/opt/omnigent/venv/bin/python';
  const executable = async (file: string) => {
    await mkdir(path.dirname(file), { recursive: true });
    await writeFile(file, '#!/bin/sh\nexit 0\n', { mode: 0o700 });
    return file;
  };
  beforeEach(async () => {
    root = await mkdtemp(path.join(tmpdir(), 'od-omnigent-deps-'));
    stateDir = path.join(root, 'integration');
    env = { PATH: path.join(root, 'system-bin'), HOME: root };
    installed = new Set();
    run = vi.fn<DependencyCommandRunner>(async (command, args) => {
      if (args.includes('-c')) return { code: installed.has(command) ? 0 : 1, stdout: installed.has(command) ? '0.16.0\n' : '' };
      if (args[0] === '--version') return { code: 0, stdout: 'uv 0.12.10\n' };
      if (args.includes('uv==0.12.10')) await executable(path.join(stateDir, 'deps', 'bootstrap', 'bin', 'uv'));
      if (args.includes('omnigent==0.16.0')) installed.add(await executable(path.join(stateDir, 'deps', 'uv', 'tools', 'omnigent', 'bin', 'python')));
      return { code: 0, stdout: '' };
    });
  });
  afterEach(async () => { await rm(root, { recursive: true, force: true }); });

  it('reuses the image SDK by importing its package version without installing anything', async () => {
    installed.add(imagePython);
    const result = await ensureOmnigentDependencies({ stateDir, env, run });
    expect(result.python).toBe(imagePython);
    expect(run).toHaveBeenCalledOnce();
    expect(run.mock.calls[0]?.[1]).toEqual(['-I', '-c', expect.stringContaining('version("omnigent")')]);
    expect(env.PATH).toBe(path.join(root, 'system-bin'));
  });
  it('does not update the global image SDK when update is requested', async () => {
    installed.add(imagePython);
    expect((await ensureOmnigentDependencies({ stateDir, env, run, update: true })).python).toBe(imagePython);
    expect(run.mock.calls.every(([, args]) => args.includes('-c'))).toBe(true);
  });
  it('honors an injected SDK Python without reading process environment', async () => {
    const python = await executable(path.join(root, 'custom', 'python'));
    installed.add(python);
    const result = await ensureOmnigentDependencies({ stateDir, env: { ...env, OD_OMNIGENT_PYTHON: python }, run });
    expect(result.python).toBe(python);
    expect(run).toHaveBeenCalledOnce();
  });
  it('finds an SDK through the real location of an existing Omnigent CLI', async () => {
    const bin = path.join(root, 'external', 'bin');
    const python = await executable(path.join(bin, 'python'));
    await executable(path.join(bin, 'omnigent'));
    await mkdir(env.PATH!, { recursive: true });
    await symlink(path.join(bin, 'omnigent'), path.join(env.PATH!, 'omnigent'));
    installed.add(python);
    expect((await ensureOmnigentDependencies({ stateDir, env, run })).python).toBe(python);
    expect(run.mock.calls.every(([, args]) => args.includes('-c'))).toBe(true);
  });
  it('bootstraps pinned uv and SDK into private directories without installing engines', async () => {
    const python = await executable(path.join(env.PATH!, 'python3'));
    const result = await ensureOmnigentDependencies({ stateDir, env, run });
    const deps = path.join(stateDir, 'deps');
    expect(result.python).toBe(path.join(deps, 'uv', 'tools', 'omnigent', 'bin', 'python'));
    const pip = run.mock.calls.find(([, args]) => args.includes('uv==0.12.10'))!;
    expect(pip[0]).toBe(python);
    expect(pip[1]).toContain('--target');
    expect(pip[1]).toContain(path.join(deps, 'bootstrap'));
    const install = run.mock.calls.find(([, args]) => args.includes('omnigent==0.16.0'))!;
    expect(install[1]).toEqual(['tool', 'install', '--python', '3.12', 'omnigent==0.16.0']);
    expect(install[2].env).toMatchObject({ UV_TOOL_DIR: path.join(deps, 'uv', 'tools'), UV_TOOL_BIN_DIR: path.join(deps, 'bin'), UV_PYTHON_INSTALL_DIR: path.join(deps, 'python'), UV_CACHE_DIR: path.join(deps, 'uv-cache') });
    expect(run.mock.calls.some(([, args]) => args.some(arg => /^(claude|codex|pi|opencode)(==|$)/.test(arg)))).toBe(false);
  });
  it('reuses pinned uv from PATH and does not invoke pip', async () => {
    const uv = await executable(path.join(env.PATH!, 'uv'));
    await ensureOmnigentDependencies({ stateDir, env, run });
    expect(run.mock.calls.find(([, args]) => args.includes('omnigent==0.16.0'))?.[0]).toBe(uv);
    expect(run.mock.calls.some(([, args]) => args.includes('pip'))).toBe(false);
  });
  it('uses a private pinned bootstrap when system uv has a different version', async () => {
    const globalUv = await executable(path.join(env.PATH!, 'uv'));
    await executable(path.join(env.PATH!, 'python3'));
    const base = run.getMockImplementation()!;
    run.mockImplementation(async (command, args, options) => command === globalUv && args[0] === '--version' ? { code: 0, stdout: 'uv 9.0.0\n' } : base(command, args, options));
    await ensureOmnigentDependencies({ stateDir, env, run });
    expect(run.mock.calls.find(([, args]) => args.includes('omnigent==0.16.0'))?.[0]).toBe(path.join(stateDir, 'deps', 'bootstrap', 'bin', 'uv'));
    expect(run.mock.calls.filter(([command]) => command === globalUv)).toHaveLength(1);
  });
  it('updates only a managed SDK, leaving the image SDK alone', async () => {
    const python = await executable(path.join(stateDir, 'deps', 'uv', 'tools', 'omnigent', 'bin', 'python'));
    installed.add(python); installed.add(imagePython);
    await executable(path.join(env.PATH!, 'uv'));
    expect((await ensureOmnigentDependencies({ stateDir, env, run, update: true })).python).toBe(python);
    expect(run.mock.calls.find(([, args]) => args.includes('omnigent==0.16.0'))?.[1]).toContain('--force');
    expect(run.mock.calls.some(([command, args]) => command === imagePython && !args.includes('-c'))).toBe(false);
  });
  it('refuses a dependency directory that redirects writes outside daemon state', async () => {
    await mkdir(stateDir, { recursive: true });
    const external = path.join(root, 'external');
    await mkdir(external);
    await symlink(external, path.join(stateDir, 'deps'));
    await expect(ensureOmnigentDependencies({ stateDir, env, run })).rejects.toThrow('must be private');
    expect(run.mock.calls.every(([, args]) => args.includes('-c'))).toBe(true);
  });
  it('reports missing bootstrap prerequisites without leaking subprocess output', async () => {
    await executable(path.join(env.PATH!, 'python3'));
    run.mockResolvedValue({ code: 1, stdout: 'private-registry-token' });
    await expect(ensureOmnigentDependencies({ stateDir, env, run })).rejects.toThrow('Python pip is required');
  });
  it('requires a successful SDK import after installation', async () => {
    await executable(path.join(env.PATH!, 'uv'));
    const base = run.getMockImplementation()!;
    run.mockImplementation(async (command, args, options) => args.includes('-c') ? { code: 1, stdout: 'private error' } : base(command, args, options));
    await expect(ensureOmnigentDependencies({ stateDir, env, run })).rejects.toThrow('SDK is unavailable after installation');
  });
  it('refuses a nested tool directory that redirects package updates outside managed state', async () => {
    const external = path.join(root, 'external');
    await mkdir(external);
    await mkdir(path.join(stateDir, 'deps', 'uv'), { recursive: true });
    await symlink(external, path.join(stateDir, 'deps', 'uv', 'tools'));
    await expect(ensureOmnigentDependencies({ stateDir, env, run })).rejects.toThrow('must be private');
    expect(run.mock.calls.every(([, args]) => args.includes('-c'))).toBe(true);
  });
  it('requires the exact pinned uv version after bootstrap', async () => {
    await executable(path.join(env.PATH!, 'python3'));
    const base = run.getMockImplementation()!;
    run.mockImplementation(async (command, args, options) => args[0] === '--version' ? { code: 0, stdout: 'uv 9.0.0\n' } : base(command, args, options));
    await expect(ensureOmnigentDependencies({ stateDir, env, run })).rejects.toThrow('does not match the pinned version');
    expect(run.mock.calls.some(([, args]) => args.includes('omnigent==0.16.0'))).toBe(false);
  });
});

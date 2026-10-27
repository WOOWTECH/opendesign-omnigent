import { createHash } from 'node:crypto';
import { mkdtemp, mkdir, readFile, writeFile, rm, symlink } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import path from 'node:path';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { ensureOmnigentDependencies, type DependencyCommandRunner } from '../../../src/integrations/omnigent/dependencies.js';

const patchFixture = vi.hoisted(() => ({ original: 'original SDK fixture\n', patched: 'patched SDK fixture\n', quotaOriginal: 'quota baseline fixture\n', quotaPatched: 'quota notice fixture\n', interruptOriginal: 'interrupt baseline fixture\n', interruptPatched: 'native abort fixture\n', piOriginal: 'Pi picker baseline fixture\n', piPatched: 'Pi custom picker fixture\n', imageRoot: '' }));
// The production image path is simulated so these tests also run on developer machines without that image.
vi.mock('node:fs/promises', async importOriginal => {
  const fs = await importOriginal<typeof import('node:fs/promises')>();
  const { createHash } = await import('node:crypto');
  return { ...fs,
    access: async (...args: Parameters<typeof fs.access>) => args[0] === '/opt/omnigent/venv/bin/python' ? undefined : fs.access(...args),
    stat: async (...args: Parameters<typeof fs.stat>) => args[0] === '/opt/omnigent/venv/bin/python' ? { isFile: () => true } : fs.stat(...args),
    realpath: async (...args: Parameters<typeof fs.realpath>) => args[0] === '/opt/omnigent/venv' ? patchFixture.imageRoot : fs.realpath(...args),
    // Exercise hash validation with small fixtures; the public SDK source is not vendored into tests.
    readFile: async (...args: Parameters<typeof fs.readFile>) => {
      const contents = await fs.readFile(...args);
      if (!String(args[0]).endsWith('/sdk-patches/manifest.json')) return contents;
      const manifest = JSON.parse(String(contents));
      for (const entry of manifest.patches) {
        const quota = entry.patch === 'opencode-quota-error.patch';
        const interrupt = entry.patch === 'opencode-native-interrupt.patch';
        const pi = entry.patch === 'pi-custom-model-options.patch';
        entry.files[0].baseSha256 = createHash('sha256').update(quota ? patchFixture.quotaOriginal : interrupt ? patchFixture.interruptOriginal : pi ? patchFixture.piOriginal : patchFixture.original).digest('hex');
        entry.files[0].replacementSha256 = createHash('sha256').update(quota ? patchFixture.quotaPatched : interrupt ? patchFixture.interruptPatched : pi ? patchFixture.piPatched : patchFixture.patched).digest('hex');
      }
      const result = JSON.stringify(manifest);
      return typeof contents === 'string' ? result : Buffer.from(result);
    },
  };
});

describe('private Omnigent dependencies', () => {
  let root: string;
  let stateDir: string;
  let env: NodeJS.ProcessEnv;
  let installed: Set<string>;
  let sdkRoot: string;
  let sdkPackage: string;
  let sdkTarget: string;
  let quotaTarget: string;
  let interruptTarget: string;
  let piTarget: string;
  let imagePackage: string;
  let externalPackages: Map<string, string>;
  const seedSdk = async (sdkPackage: string, patched: boolean) => {
    for (const [relative, contents] of [
      ['host/connect.py', patched ? patchFixture.patched : patchFixture.original],
      ['harnesses/opencode_native/forwarder.py', patched ? patchFixture.quotaPatched : patchFixture.quotaOriginal],
      ['runner/native/interrupt.py', patched ? patchFixture.interruptPatched : patchFixture.interruptOriginal],
      ['harnesses/pi_native/credentials.py', patched ? patchFixture.piPatched : patchFixture.piOriginal],
    ]) {
      const target = path.join(sdkPackage, relative!);
      await mkdir(path.dirname(target), { recursive: true });
      await writeFile(target, contents!);
    }
  };
  let sdkVersion: string;
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
    sdkRoot = path.join(stateDir, 'deps', 'uv', 'tools', 'omnigent');
    sdkPackage = path.join(sdkRoot, 'lib', 'python3.12', 'site-packages', 'omnigent');
    sdkTarget = path.join(sdkPackage, 'host', 'connect.py');
    quotaTarget = path.join(sdkPackage, 'harnesses/opencode_native/forwarder.py');
    interruptTarget = path.join(sdkPackage, 'runner/native/interrupt.py');
    piTarget = path.join(sdkPackage, 'harnesses/pi_native/credentials.py');
    patchFixture.imageRoot = path.join(root, 'image-sdk');
    imagePackage = path.join(patchFixture.imageRoot, 'lib/python3.12/site-packages/omnigent');
    await seedSdk(imagePackage, true);
    externalPackages = new Map([[imagePython, imagePackage]]);
    sdkVersion = '0.16.0';
    installed = new Set();
    run = vi.fn<DependencyCommandRunner>(async (command, args) => {
      if (args.includes('-c') && args.some(arg => arg.includes('packageRoot'))) return { code: 0, stdout: JSON.stringify({ version: sdkVersion, packageRoot: externalPackages.get(command) ?? sdkPackage }) };
      if (args.includes('-c')) return { code: installed.has(command) ? 0 : 1, stdout: installed.has(command) ? '0.16.0\n' : '' };
      if (args.some(arg => arg.endsWith('/sdk-patches/apply.py'))) {
        await writeFile(sdkTarget, patchFixture.patched);
        await writeFile(quotaTarget, patchFixture.quotaPatched);
        await writeFile(interruptTarget, patchFixture.interruptPatched);
        await writeFile(piTarget, patchFixture.piPatched);
        return { code: 0, stdout: 'SDK_PATCH_APPLIED\n' };
      }
      if (args[0] === '--version') return { code: 0, stdout: 'uv 0.12.10\n' };
      if (args.includes('uv==0.12.10')) await executable(path.join(stateDir, 'deps', 'bootstrap', 'bin', 'uv'));
      if (args.includes('omnigent==0.16.0')) {
        installed.add(await executable(path.join(sdkRoot, 'bin', 'python')));
        await seedSdk(sdkPackage, false);
      }
      return { code: 0, stdout: '' };
    });
  });
  afterEach(async () => { await rm(root, { recursive: true, force: true }); });

  it('reuses the image SDK only after verifying all four patched files without installing anything', async () => {
    installed.add(imagePython);
    const result = await ensureOmnigentDependencies({ stateDir, env, run });
    expect(result.python).toBe(imagePython);
    expect(run).toHaveBeenCalledTimes(2);
    expect(run.mock.calls[0]?.[1]).toEqual(['-I', '-B', '-c', expect.stringContaining('version("omnigent")')]);
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
    const pkg = path.join(root, 'custom/lib/omnigent'); await seedSdk(pkg, true); externalPackages.set(python, pkg);
    const result = await ensureOmnigentDependencies({ stateDir, env: { ...env, OD_OMNIGENT_PYTHON: python }, run });
    expect(result.python).toBe(python);
    expect(run).toHaveBeenCalledTimes(2);
  });
  it('finds an SDK through the real location of an existing Omnigent CLI', async () => {
    const bin = path.join(root, 'external', 'bin');
    const python = await executable(path.join(bin, 'python'));
    await executable(path.join(bin, 'omnigent'));
    await mkdir(env.PATH!, { recursive: true });
    await symlink(path.join(bin, 'omnigent'), path.join(env.PATH!, 'omnigent'));
    installed.add(python);
    const pkg = path.join(root, 'external/lib/omnigent'); await seedSdk(pkg, true); externalPackages.set(python, pkg);
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
    expect(await readFile(sdkTarget, 'utf8')).toBe(patchFixture.patched);
    expect(await readFile(quotaTarget, 'utf8')).toBe(patchFixture.quotaPatched);
    const patch = run.mock.calls.find(([, args]) => args.some(arg => arg.endsWith('/sdk-patches/apply.py')))!;
    expect(patch[0]).toBe(result.python);
    expect(patch[1]).toEqual(['-I', '-B', expect.stringMatching(/\/sdk-patches\/apply\.py$/), sdkPackage, expect.stringMatching(/\/sdk-patches\/manifest\.json$/)]);
  });
  it('falls back to a private install when an image has only the old connect patch', async () => {
    installed.add(imagePython);
    const target = path.join(imagePackage, 'harnesses/opencode_native/forwarder.py');
    await writeFile(target, patchFixture.quotaOriginal);
    await executable(path.join(env.PATH!, 'uv'));
    expect((await ensureOmnigentDependencies({ stateDir, env, run })).python).toBe(path.join(sdkRoot, 'bin/python'));
    expect(await readFile(target, 'utf8')).toBe(patchFixture.quotaOriginal);
    expect(run.mock.calls.some(([command, args]) => command === imagePython && !args.includes('-c'))).toBe(false);
    expect(await readFile(quotaTarget, 'utf8')).toBe(patchFixture.quotaPatched);
  });
  it('rejects an unknown second target before patching the valid first target', async () => {
    installed.add(await executable(path.join(sdkRoot, 'bin/python')));
    await seedSdk(sdkPackage, false);
    await writeFile(quotaTarget, 'unknown quota source');
    await expect(ensureOmnigentDependencies({ stateDir, env, run })).rejects.toThrow('patch verification failed');
    expect(await readFile(sdkTarget, 'utf8')).toBe(patchFixture.original);
    expect(run.mock.calls.some(([, args]) => args.some(arg => arg.endsWith('/sdk-patches/apply.py')))).toBe(false);
  });
  it('uses a private SDK when the image has host and quota patches but lacks native interrupt', async () => {
    installed.add(imagePython);
    const target = path.join(imagePackage, 'runner/native/interrupt.py');
    await writeFile(target, patchFixture.interruptOriginal);
    await executable(path.join(env.PATH!, 'uv'));
    expect((await ensureOmnigentDependencies({ stateDir, env, run })).python).toBe(path.join(sdkRoot, 'bin/python'));
    expect(await readFile(target, 'utf8')).toBe(patchFixture.interruptOriginal);
    expect(run.mock.calls.some(([command, args]) => command === imagePython && !args.includes('-c'))).toBe(false);
    expect(await readFile(interruptTarget, 'utf8')).toBe(patchFixture.interruptPatched);
  });
  it('rejects an unknown interrupt target before modifying either existing patch target', async () => {
    installed.add(await executable(path.join(sdkRoot, 'bin/python')));
    await seedSdk(sdkPackage, false);
    await writeFile(interruptTarget, 'unknown interrupt source');
    await expect(ensureOmnigentDependencies({ stateDir, env, run })).rejects.toThrow('patch verification failed');
    expect(await readFile(sdkTarget, 'utf8')).toBe(patchFixture.original);
    expect(await readFile(quotaTarget, 'utf8')).toBe(patchFixture.quotaOriginal);
    expect(run.mock.calls.some(([, args]) => args.some(arg => arg.endsWith('/sdk-patches/apply.py')))).toBe(false);
  });
  it('upgrades a private SDK with old patches and then reuses all four without repatching', async () => {
    const python = await executable(path.join(sdkRoot, 'bin/python'));
    installed.add(python);
    await seedSdk(sdkPackage, true);
    await writeFile(interruptTarget, patchFixture.interruptOriginal);
    expect((await ensureOmnigentDependencies({ stateDir, env, run })).python).toBe(python);
    expect(await readFile(sdkTarget, 'utf8')).toBe(patchFixture.patched);
    expect(await readFile(quotaTarget, 'utf8')).toBe(patchFixture.quotaPatched);
    expect(await readFile(interruptTarget, 'utf8')).toBe(patchFixture.interruptPatched);
    expect(run.mock.calls.some(([, args]) => args.some(arg => arg.endsWith('/sdk-patches/apply.py')))).toBe(true);
    expect(run.mock.calls.some(([, args]) => args.includes('omnigent==0.16.0'))).toBe(false);
    run.mockClear();
    expect((await ensureOmnigentDependencies({ stateDir, env, run })).python).toBe(python);
    expect(run.mock.calls.every(([, args]) => args.includes('-c'))).toBe(true);
  });
  it('finishes a partial managed patch set while preserving the existing connect patch', async () => {
    installed.add(await executable(path.join(sdkRoot, 'bin/python')));
    await seedSdk(sdkPackage, false);
    await writeFile(sdkTarget, patchFixture.patched);
    await ensureOmnigentDependencies({ stateDir, env, run });
    expect(await readFile(sdkTarget, 'utf8')).toBe(patchFixture.patched);
    expect(await readFile(quotaTarget, 'utf8')).toBe(patchFixture.quotaPatched);
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
    expect(await readFile(sdkTarget, 'utf8')).toBe(patchFixture.patched);
    expect(await readFile(quotaTarget, 'utf8')).toBe(patchFixture.quotaPatched);
  });
  it('patches a previously installed private SDK on reuse and verifies it again without repatching', async () => {
    const python = await executable(path.join(sdkRoot, 'bin', 'python'));
    installed.add(python);
    await seedSdk(sdkPackage, false);
    expect((await ensureOmnigentDependencies({ stateDir, env, run })).python).toBe(python);
    expect(await readFile(sdkTarget, 'utf8')).toBe(patchFixture.patched);
    expect(await readFile(quotaTarget, 'utf8')).toBe(patchFixture.quotaPatched);
    run.mockClear();
    expect((await ensureOmnigentDependencies({ stateDir, env, run })).python).toBe(python);
    expect(run.mock.calls.some(([, args]) => args.some(arg => arg.includes('packageRoot')))).toBe(true);
    expect(run.mock.calls.every(([, args]) => args.includes('-c'))).toBe(true);
    await writeFile(sdkTarget, 'unknown updated SDK source');
    await expect(ensureOmnigentDependencies({ stateDir, env, run })).rejects.toThrow('Private Omnigent SDK patch verification failed.');
    expect(await readFile(sdkTarget, 'utf8')).toBe('unknown updated SDK source');
  });
  it('rejects a different managed SDK version before modifying its source', async () => {
    installed.add(await executable(path.join(sdkRoot, 'bin', 'python')));
    await seedSdk(sdkPackage, false); sdkVersion = '0.17.0';
    await expect(ensureOmnigentDependencies({ stateDir, env, run })).rejects.toThrow('Private Omnigent SDK patch verification failed.');
    expect(await readFile(sdkTarget, 'utf8')).toBe(patchFixture.original);
    expect(run.mock.calls.every(([, args]) => args.includes('-c'))).toBe(true);
  });
  it('rejects an unknown source from a fresh pinned installation without invoking the patch installer', async () => {
    await executable(path.join(env.PATH!, 'uv'));
    const base = run.getMockImplementation()!;
    run.mockImplementation(async (command, args, options) => {
      const result = await base(command, args, options);
      if (args.includes('omnigent==0.16.0')) await writeFile(sdkTarget, 'unknown package source');
      return result;
    });
    await expect(ensureOmnigentDependencies({ stateDir, env, run })).rejects.toThrow('Private Omnigent SDK patch verification failed.');
    expect(await readFile(sdkTarget, 'utf8')).toBe('unknown package source');
    expect(run.mock.calls.some(([, args]) => args.some(arg => arg.endsWith('/sdk-patches/apply.py')))).toBe(false);
  });
  it.each(['exit', 'throw', 'unchanged'] as const)('fails closed with a fixed diagnostic when the SDK patch runner returns %s', async failure => {
    await executable(path.join(env.PATH!, 'uv'));
    const base = run.getMockImplementation()!;
    run.mockImplementation(async (command, args, options) => {
      if (!args.some(arg => arg.endsWith('/sdk-patches/apply.py'))) return base(command, args, options);
      if (failure === 'throw') throw new Error('private runner detail');
      return { code: failure === 'exit' ? 1 : 0, stdout: 'private runner detail' };
    });
    await expect(ensureOmnigentDependencies({ stateDir, env, run })).rejects.toThrow(/^Private Omnigent SDK patch verification failed\.$/);
    expect(createHash('sha256').update(await readFile(sdkTarget)).digest('hex')).toBe(createHash('sha256').update(patchFixture.original).digest('hex'));
  });
  it('refuses to patch an SDK imported outside the private environment', async () => {
    installed.add(await executable(path.join(sdkRoot, 'bin', 'python')));
    sdkPackage = path.join(root, 'external', 'omnigent'); sdkTarget = path.join(sdkPackage, 'host', 'connect.py');
    await mkdir(path.dirname(sdkTarget), { recursive: true }); await writeFile(sdkTarget, patchFixture.original);
    await expect(ensureOmnigentDependencies({ stateDir, env, run })).rejects.toThrow('Private Omnigent SDK patch verification failed.');
    expect(await readFile(sdkTarget, 'utf8')).toBe(patchFixture.original);
    expect(run.mock.calls.every(([, args]) => args.includes('-c'))).toBe(true);
  });
  it.each(['file', 'directory'] as const)('refuses a reused private SDK target whose %s is a symlink outside its environment', async kind => {
    installed.add(await executable(path.join(sdkRoot, 'bin', 'python')));
    const externalDir = path.join(root, 'external'); await mkdir(externalDir);
    const external = path.join(externalDir, 'connect.py'); await writeFile(external, patchFixture.original);
    await mkdir(sdkPackage, { recursive: true });
    if (kind === 'directory') await symlink(externalDir, path.dirname(sdkTarget));
    else { await mkdir(path.dirname(sdkTarget)); await symlink(external, sdkTarget); }
    await expect(ensureOmnigentDependencies({ stateDir, env, run })).rejects.toThrow('Private Omnigent SDK patch verification failed.');
    expect(await readFile(external, 'utf8')).toBe(patchFixture.original);
    expect(run.mock.calls.every(([, args]) => args.includes('-c'))).toBe(true);
  });
  it('rejects a populated dependency root symlink before patching a reused SDK', async () => {
    await mkdir(stateDir, { recursive: true });
    const external = path.join(root, 'external'); await mkdir(external);
    await symlink(external, path.join(stateDir, 'deps'));
    installed.add(await executable(path.join(sdkRoot, 'bin', 'python')));
    await mkdir(path.dirname(sdkTarget), { recursive: true }); await writeFile(sdkTarget, patchFixture.original);
    await expect(ensureOmnigentDependencies({ stateDir, env, run })).rejects.toThrow('Private Omnigent SDK patch verification failed.');
    expect(await readFile(sdkTarget, 'utf8')).toBe(patchFixture.original);
    expect(run.mock.calls.every(([, args]) => args.includes('-c'))).toBe(true);
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

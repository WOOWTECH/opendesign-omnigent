import { execFile } from 'node:child_process';
import { chmod, chown, copyFile, link, mkdir, mkdtemp, readFile, readdir, rename, rm, stat, symlink, writeFile } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import path from 'node:path';
import { pathToFileURL } from 'node:url';
import { promisify } from 'node:util';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { restoreOmnigentPrivacy } from '../../../src/integrations/omnigent/permissions.js';

const exec = promisify(execFile);
const helperUrl = new URL('../../../src/integrations/omnigent/permissions.ts', import.meta.url).href;
const roots: string[] = [];
afterEach(async () => { await Promise.all(roots.splice(0).map(root => rm(root, { recursive: true, force: true }))); });

async function restoredFixture(temporaryRoot = tmpdir()) {
  const root = await mkdtemp(path.join(temporaryRoot, 'od-omnigent-privacy-'));
  roots.push(root);
  const data = path.join(root, 'data');
  const state = path.join(data, 'integrations', 'omnigent');
  const host = path.join(state, 'host');
  await mkdir(host, { recursive: true });
  await chmod(data, 0o750);
  for (const directory of [path.dirname(state), state, host]) await chmod(directory, 0o755);
  const files = [path.join(state, 'state.json'), path.join(state, 'host-owner.json'),
    path.join(host, 'auth_tokens.json'), path.join(host, 'config.yaml')];
  for (const file of files) {
    // Synthetic content only. Disabled integrations must retain privacy too.
    await writeFile(file, file.endsWith('state.json')
      ? JSON.stringify({ schema: 1, enabled: false, loginConfirmed: false, phase: 'disabled',
        installedGeneration: 1, installationScope: 'local', approvalScope: 'local',
        server: 'https://synthetic.invalid', verification: {}, onboarding: {} }) + '\n'
      : 'synthetic-private-fixture\n');
    await chmod(file, 0o644);
  }
  return { root, data, state, host, files };
}

async function mode(file: string) { return (await stat(file)).mode & 0o7777; }

async function isolatedHelper(root: string): Promise<string> {
  // The UID reader/repairer must not need access to the runner's checkout.
  // This module has built-in Node dependencies only; Node 24 strips its types.
  const target = path.join(root, 'permissions.mts');
  await copyFile(new URL(helperUrl), target);
  await chmod(target, 0o644);
  return pathToFileURL(target).href;
}

describe.skipIf(process.platform !== 'linux')('Omnigent restore privacy', () => {
  it('repairs restored modes even when disabled, without changing bytes, ownership or inode', async () => {
    const f = await restoredFixture();
    const before = await Promise.all(f.files.map(async file => ({ bytes: await readFile(file), info: await stat(file) })));
    expect(await restoreOmnigentPrivacy(f.state)).toBe('restored');
    expect(await mode(f.state)).toBe(0o700);
    expect(await mode(f.host)).toBe(0o700);
    for (const [i, file] of f.files.entries()) {
      expect(await mode(file)).toBe(0o600);
      expect(await readFile(file)).toEqual(before[i]!.bytes);
      const info = await stat(file);
      expect([info.uid, info.gid, info.ino, info.size, info.mtimeMs]).toEqual(
        [before[i]!.info.uid, before[i]!.info.gid, before[i]!.info.ino, before[i]!.info.size, before[i]!.info.mtimeMs]);
    }
    expect(await mode(f.data)).toBe(0o750);
    expect(await mode(path.dirname(f.state))).toBe(0o755);
    expect(await restoreOmnigentPrivacy(f.state)).toBe('restored');
    expect(await Promise.all(f.files.map(file => readFile(file)))).toEqual(before.map(entry => entry.bytes));
  });

  it('rejects an integrations ancestor symlink without chmod of outside directories or secrets', async () => {
    const f = await restoredFixture();
    const outside = path.join(f.root, 'outside');
    await rename(path.dirname(f.state), outside);
    await symlink(outside, path.dirname(f.state));
    const outsideState = path.join(outside, 'omnigent');
    const targets = [outside, outsideState, path.join(outsideState, 'host'),
      ...f.files.map(file => path.join(outsideState, path.relative(f.state, file)))];
    const before = await Promise.all(targets.map(mode));
    const bytes = await Promise.all(targets.slice(3).map(file => readFile(file)));
    const result = await restoreOmnigentPrivacy(f.state).catch(error => error);
    expect(await Promise.all(targets.map(mode))).toEqual(before);
    expect(await Promise.all(targets.slice(3).map(file => readFile(file)))).toEqual(bytes);
    expect(result).toBeInstanceOf(Error);
  });

  it.each(['data', 'state', 'host'] as const)('rejects a %s directory symlink without changing its target', async key => {
    const f = await restoredFixture();
    const outside = path.join(f.root, 'outside');
    const target = f[key];
    const outsideFiles = f.files.filter(file => file.startsWith(target + path.sep))
      .map(file => path.join(outside, path.relative(target, file)));
    await rename(target, outside);
    await symlink(outside, target);
    const before = await Promise.all([outside, ...outsideFiles].map(mode));
    const bytes = await Promise.all(outsideFiles.map(file => readFile(file)));
    await expect(restoreOmnigentPrivacy(f.state)).rejects.toThrow('Unable to restore private Omnigent permissions.');
    expect(await Promise.all([outside, ...outsideFiles].map(mode))).toEqual(before);
    expect(await Promise.all(outsideFiles.map(file => readFile(file)))).toEqual(bytes);
  });

  for (const kind of ['symlink', 'hardlink'] as const) {
    it.each(['state.json', 'host-owner.json', 'host/auth_tokens.json', 'host/config.yaml'])(
      `rejects a ${kind} at %s without chmod or rewriting its outside inode`, async name => {
        const f = await restoredFixture();
        const outside = path.join(f.root, 'outside-secret');
        const target = path.join(f.state, name);
        await writeFile(outside, 'synthetic-outside-fixture\n'); await chmod(outside, 0o644);
        await rm(target);
        if (kind === 'symlink') await symlink(outside, target);
        else await link(outside, target);
        const before = await stat(outside);
        await expect(restoreOmnigentPrivacy(f.state)).rejects.toThrow('Unable to restore private Omnigent permissions.');
        const after = await stat(outside);
        expect([after.mode, after.uid, after.gid, after.ino, after.mtimeMs, after.nlink]).toEqual(
          [before.mode, before.uid, before.gid, before.ino, before.mtimeMs, before.nlink]);
        expect(await readFile(outside, 'utf8')).toBe('synthetic-outside-fixture\n');
      });
  }

  it('does not create a missing integration or alter its ancestors', async () => {
    const f = await restoredFixture();
    await rm(f.state, { recursive: true });
    expect(await restoreOmnigentPrivacy(f.state)).toBe('missing');
    expect(await readdir(path.dirname(f.state))).toEqual([]);
    expect(await mode(f.data)).toBe(0o750);
    expect(await mode(path.dirname(f.state))).toBe(0o755);
  });

  it('does not create missing host/login files or modify unknown files', async () => {
    const f = await restoredFixture();
    await rm(f.host, { recursive: true });
    await Promise.all(f.files.slice(0, 2).map(file => rm(file)));
    const unrelated = path.join(f.state, 'unrelated.json');
    await writeFile(unrelated, 'unchanged\n'); await chmod(unrelated, 0o644);
    expect(await restoreOmnigentPrivacy(f.state)).toBe('restored');
    expect(await mode(f.state)).toBe(0o700);
    expect(await readdir(f.state)).toEqual(['unrelated.json']);
    expect(await readFile(unrelated, 'utf8')).toBe('unchanged\n');
    expect(await mode(unrelated)).toBe(0o644);
    await mkdir(f.host); await chmod(f.host, 0o755);
    await restoreOmnigentPrivacy(f.state);
    expect(await mode(f.host)).toBe(0o700);
    expect(await readdir(f.host)).toEqual([]);
  });

  it('rejects a dangling private symlink instead of treating it as missing', async () => {
    const f = await restoredFixture();
    await rm(f.files[0]!);
    await symlink(path.join(f.root, 'does-not-exist'), f.files[0]!);
    await expect(restoreOmnigentPrivacy(f.state)).rejects.toThrow('Unable to restore private Omnigent permissions.');
    await expect(stat(path.join(f.root, 'does-not-exist'))).rejects.toMatchObject({ code: 'ENOENT' });
  });

  it('rejects a directory where a private regular file is expected', async () => {
    const f = await restoredFixture();
    await rm(f.files[0]!); await mkdir(f.files[0]!); await chmod(f.files[0]!, 0o755);
    await expect(restoreOmnigentPrivacy(f.state)).rejects.toThrow('Unable to restore private Omnigent permissions.');
    expect(await mode(f.files[0]!)).toBe(0o755);
  });

  it('rejects a private FIFO without blocking startup', async () => {
    const f = await restoredFixture();
    await rm(f.files[0]!);
    await exec('mkfifo', [f.files[0]!], { timeout: 2_000 });
    const code = `import { restoreOmnigentPrivacy } from ${JSON.stringify(await isolatedHelper(f.root))};
try { await restoreOmnigentPrivacy(process.argv[1]); process.exitCode = 1; }
catch (error) { console.log(error.message); }`;
    const child = await exec(process.execPath, ['--input-type=module', '-e', code, f.state], { timeout: 5_000 });
    expect(child.stdout.trim()).toBe('Unable to restore private Omnigent permissions.');
    expect((await stat(f.files[0]!)).isFIFO()).toBe(true);
  });

  it.each(['relative/state', '/', '/tmp/../outside'])('rejects unsafe root argument %s', async target => {
    await expect(restoreOmnigentPrivacy(target)).rejects.toThrow('Unable to restore private Omnigent permissions.');
  });

  // Gitea's Linux container runs these daemon tests as root. Local non-root
  // shells cannot impersonate another UID; do not substitute mode arithmetic.
  it.skipIf(process.getuid?.() !== 0)('denies UID 1002 in shared GID 1001 after UID 1001 repairs the restored state', async () => {
    const f = await restoredFixture('/tmp');
    await chmod(f.root, 0o755);
    const childHelperUrl = await isolatedHelper(f.root);
    for (const file of [f.data, path.dirname(f.state), f.state, f.host, ...f.files]) await chown(file, 1001, 1001);
    const reader = `import { readFile } from 'node:fs/promises';
const results = await Promise.all(JSON.parse(process.argv[1]).map(async file => {
  try { await readFile(file); return 'readable'; } catch (error) { return error.code; }
})); console.log(JSON.stringify({uid:process.getuid(), gid:process.getgid(), results}));`;
    const readAsPeer = async () => JSON.parse((await exec(process.execPath,
      ['--input-type=module', '-e', reader, JSON.stringify(f.files)],
      { uid: 1002, gid: 1001, cwd: '/tmp', env: {}, timeout: 5_000 })).stdout);
    expect(await readAsPeer()).toEqual({ uid: 1002, gid: 1001, results: Array(4).fill('readable') });
    const repair = `import { restoreOmnigentPrivacy } from ${JSON.stringify(childHelperUrl)};
console.log(await restoreOmnigentPrivacy(process.argv[1]));`;
    const child = await exec(process.execPath, ['--input-type=module', '-e', repair, f.state],
      { uid: 1001, gid: 1001, cwd: '/tmp', env: {}, timeout: 5_000 });
    expect(child.stdout.trim()).toBe('restored');
    expect(await readAsPeer()).toEqual({ uid: 1002, gid: 1001, results: Array(4).fill('EACCES') });
    for (const file of [f.state, f.host, ...f.files]) {
      const info = await stat(file);
      expect([info.uid, info.gid]).toEqual([1001, 1001]);
    }
  });
});

describe('Omnigent privacy platform compatibility', () => {
  it.each(['darwin', 'win32'])('reports unsupported on %s without changing existing behavior', async platform => {
    const f = await restoredFixture();
    const before = await Promise.all([f.state, ...f.files].map(mode));
    const original = Object.getOwnPropertyDescriptor(process, 'platform')!;
    let result: ReturnType<typeof restoreOmnigentPrivacy>;
    try {
      Object.defineProperty(process, 'platform', { value: platform });
      result = restoreOmnigentPrivacy(f.state);
    } finally { Object.defineProperty(process, 'platform', original); }
    expect(await result).toBe('unsupported-platform');
    expect(await Promise.all([f.state, ...f.files].map(mode))).toEqual(before);
  });
});

describe.skipIf(process.platform !== 'linux')('Omnigent service privacy integration', () => {
  it('restores disabled state on resumeLocalHost and status without touching the adapter or content', async () => {
    const { OmnigentIntegrationService } = await import('../../../src/integrations/omnigent/service.js');
    const f = await restoredFixture();
    const bytes = await Promise.all(f.files.map(file => readFile(file)));
    const adapter = vi.fn(async () => { throw new Error('Disabled service must not invoke an adapter'); });
    const onboarding = vi.fn(async () => { throw new Error('Must not create onboarding'); });
    const service = new OmnigentIntegrationService({ dataDir: f.data,
      getPlugin: () => ({ id: 'woow-omnigent', installedAt: 1, sourceKind: 'bundled' }),
      createOnboarding: onboarding, adapter, env: { HOME: f.root } });
    await service.resumeLocalHost();
    expect((await service.status('local', true)).enabled).toBe(false);
    expect(adapter).not.toHaveBeenCalled();
    expect(onboarding).not.toHaveBeenCalled();
    expect(await mode(f.state)).toBe(0o700);
    expect(await mode(f.host)).toBe(0o700);
    expect(await Promise.all(f.files.map(mode))).toEqual(Array(4).fill(0o600));
    expect(await Promise.all(f.files.map(file => readFile(file)))).toEqual(bytes);
  });
});

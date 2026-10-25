import { spawn } from 'node:child_process';
import { createHash } from 'node:crypto';
import { constants } from 'node:fs';
import { access, lstat, mkdir, readFile, realpath, stat } from 'node:fs/promises';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

export const OMNIGENT_DEPENDENCY_VERSIONS = Object.freeze({ uv: '0.12.10', omnigent: '0.16.0' });
const IMAGE_PYTHON = '/opt/omnigent/venv/bin/python';
const SDK_PROBE = 'import omnigent; from importlib.metadata import version; print(version("omnigent"))';
const SDK_PATCH_PROBE = 'import json; from importlib.metadata import version; from importlib.util import find_spec; print(json.dumps({"version": version("omnigent"), "packageRoot": list(find_spec("omnigent").submodule_search_locations)[0]}))';
const moduleDir = path.dirname(fileURLToPath(import.meta.url));
// Source execution and builds share the reviewed deploy assets; packaged daemons carry a build copy.
const SDK_PATCH_ASSETS = path.basename(path.resolve(moduleDir, '../..')) === 'src'
  ? path.resolve(moduleDir, '../../../../../deploy/omnigent/sdk-patches')
  : path.join(moduleDir, 'sdk-patches');

export interface DependencyCommandOptions { env: NodeJS.ProcessEnv; cwd?: string; timeoutMs: number; }
export interface DependencyCommandResult { code: number | null; stdout: string; }
export type DependencyCommandRunner = (command: string, args: string[], options: DependencyCommandOptions) => Promise<DependencyCommandResult>;
export interface OmnigentDependencyOptions {
  /** The integration directory derived from the daemon's resolved data root. */
  stateDir: string;
  env?: NodeJS.ProcessEnv;
  update?: boolean;
  run?: DependencyCommandRunner;
}
export interface OmnigentDependencies { python: string; env?: NodeJS.ProcessEnv; }

/** Argv-only execution with bounded output; installation errors never expose subprocess output. */
export const runDependencyCommand: DependencyCommandRunner = (command, args, options) => new Promise(resolve => {
  const child = spawn(command, args, { env: options.env, ...(options.cwd ? { cwd: options.cwd } : {}), shell: false, stdio: ['ignore', 'pipe', 'ignore'] });
  let stdout = '';
  let finished = false;
  let killTimer: NodeJS.Timeout | undefined;
  const finish = (code: number | null) => {
    if (finished) return;
    finished = true;
    clearTimeout(timer);
    if (killTimer) clearTimeout(killTimer);
    resolve({ code, stdout });
  };
  const timer = setTimeout(() => {
    child.kill('SIGTERM');
    killTimer = setTimeout(() => { child.kill('SIGKILL'); finish(null); }, 2_000);
  }, options.timeoutMs);
  child.stdout.on('data', (chunk: Buffer) => { stdout += chunk.toString('utf8').slice(0, Math.max(0, 65_536 - stdout.length)); });
  child.on('error', () => finish(null));
  child.on('close', code => finish(code));
});

async function executable(candidate: string): Promise<boolean> {
  try { await access(candidate, constants.X_OK); return (await stat(candidate)).isFile(); }
  catch { return false; }
}
async function locate(name: string, env: NodeJS.ProcessEnv): Promise<string | undefined> {
  for (const directory of (env.PATH ?? '').split(path.delimiter).filter(Boolean)) {
    const candidate = path.resolve(directory, name);
    if (await executable(candidate)) return candidate;
  }
  return undefined;
}
async function probe(python: string, env: NodeJS.ProcessEnv, run: DependencyCommandRunner): Promise<boolean> {
  if (!await executable(python)) return false;
  try {
    const result = await run(python, ['-I', '-B', '-c', SDK_PROBE], { env, timeoutMs: 5_000 });
    return result.code === 0 && /^\d+\.\d+\.\d+(?:[^\s]*)?$/.test(result.stdout.trim());
  } catch { return false; }
}
async function pinnedUv(command: string, env: NodeJS.ProcessEnv, run: DependencyCommandRunner): Promise<boolean> {
  try {
    const version = await run(command, ['--version'], { env, timeoutMs: 5_000 });
    return version.code === 0 && /^uv 0\.12\.10(?:\s|$)/.test(version.stdout.trim());
  } catch { return false; }
}
function inside(root: string, candidate: string): boolean {
  const relative = path.relative(root, candidate);
  return relative !== '' && relative !== '..' && !relative.startsWith(`..${path.sep}`) && !path.isAbsolute(relative);
}
async function managed(python: string, deps: string): Promise<boolean> {
  try { return inside(await realpath(deps), await realpath(path.dirname(python))); }
  catch { return false; }
}

interface SdkPatchTarget { sdkRelativePath: string; baseSha256: string; replacementSha256: string; }
const PATCH_TARGETS: Record<string, string> = {
  'opencode-host-model-options.patch': 'host/connect.py',
  'opencode-quota-error.patch': 'harnesses/opencode_native/forwarder.py',
  'opencode-native-interrupt.patch': 'runner/native/interrupt.py',
};

/** External SDKs are read-only; only the daemon's private install can be patched. */
async function ensureSdkPatch(python: string, env: NodeJS.ProcessEnv, run: DependencyCommandRunner, deps?: string): Promise<void> {
  try {
    const sdkRoot = await realpath(path.resolve(path.dirname(python), '..'));
    if (deps) {
      const root = await realpath(deps);
      if (root !== path.join(await realpath(path.dirname(deps)), 'deps') || !inside(root, sdkRoot)) throw new Error();
    }
    const manifest = JSON.parse(await readFile(path.join(SDK_PATCH_ASSETS, 'manifest.json'), 'utf8'));
    if (manifest.schemaVersion !== 2 || manifest.sdkVersion !== OMNIGENT_DEPENDENCY_VERSIONS.omnigent
      || !Array.isArray(manifest.patches) || manifest.patches.length !== Object.keys(PATCH_TARGETS).length) throw new Error();
    const names = new Set<string>();
    const files: SdkPatchTarget[] = [];
    for (const entry of manifest.patches) {
      if (!Object.hasOwn(PATCH_TARGETS, entry.patch) || names.has(entry.patch)
        || !/^[a-f0-9]{64}$/.test(entry.patchSha256) || !Array.isArray(entry.files) || entry.files.length !== 1) throw new Error();
      names.add(entry.patch);
      const file = entry.files[0];
      if (file.sdkRelativePath !== PATCH_TARGETS[entry.patch] || !/^[a-f0-9]{64}$/.test(file.baseSha256)
        || !/^[a-f0-9]{64}$/.test(file.replacementSha256) || file.baseSha256 === file.replacementSha256) throw new Error();
      const patchPath = path.join(SDK_PATCH_ASSETS, entry.patch);
      if (!(await lstat(patchPath)).isFile()
        || createHash('sha256').update(await readFile(patchPath)).digest('hex') !== entry.patchSha256) throw new Error();
      files.push(file);
    }
    const info = await run(python, ['-I', '-B', '-c', SDK_PATCH_PROBE], { env, timeoutMs: 5_000 });
    if (info.code !== 0) throw new Error();
    const sdk = JSON.parse(info.stdout);
    if (sdk.version !== OMNIGENT_DEPENDENCY_VERSIONS.omnigent || typeof sdk.packageRoot !== 'string'
      || !path.isAbsolute(sdk.packageRoot) || !inside(sdkRoot, await realpath(sdk.packageRoot))) throw new Error();
    const targets: { path: string; expected: string }[] = [];
    let needsPatch = false;
    for (const file of files) {
      let target = sdk.packageRoot;
      for (const part of file.sdkRelativePath.split('/')) {
        target = path.join(target, part);
        if ((await lstat(target)).isSymbolicLink()) throw new Error();
      }
      if (!(await lstat(target)).isFile() || !inside(sdkRoot, await realpath(target))) throw new Error();
      const before = createHash('sha256').update(await readFile(target)).digest('hex');
      if (before !== file.replacementSha256) {
        if (!deps || before !== file.baseSha256) throw new Error();
        needsPatch = true;
      }
      targets.push({ path: target, expected: file.replacementSha256 });
    }
    if (!needsPatch) return;
    if (!deps) throw new Error();
    const patched = await run(python, ['-I', '-B', path.join(SDK_PATCH_ASSETS, 'apply.py'), sdk.packageRoot,
      path.join(SDK_PATCH_ASSETS, 'manifest.json')], { env, cwd: deps, timeoutMs: 10_000 });
    if (patched.code !== 0) throw new Error();
    for (const target of targets) {
      if (createHash('sha256').update(await readFile(target.path)).digest('hex') !== target.expected) throw new Error();
    }
  } catch {
    // Neither imported paths, package source nor subprocess diagnostics cross the service boundary.
    throw new Error('Private Omnigent SDK patch verification failed.');
  }
}

/** Reuse installed SDKs. Install/update only private Omnigent/uv; never install agent engines. */
export async function ensureOmnigentDependencies(options: OmnigentDependencyOptions): Promise<OmnigentDependencies> {
  const env = { ...(options.env ?? process.env) };
  const run = options.run ?? runDependencyCommand;
  const deps = path.resolve(options.stateDir, 'deps');
  const managedPython = path.join(deps, 'uv', 'tools', 'omnigent', 'bin', 'python');
  const managedBin = path.join(deps, 'bin');
  const bootstrapBin = path.join(deps, 'bootstrap', 'bin');
  const runtimeEnv: NodeJS.ProcessEnv = { ...env, PATH: [managedBin, bootstrapBin, env.PATH ?? ''].filter(Boolean).join(path.delimiter) };
  const candidates = [env.OD_OMNIGENT_PYTHON, IMAGE_PYTHON, managedPython];
  const cli = await locate('omnigent', env);
  if (cli) {
    try { candidates.push(path.join(path.dirname(await realpath(cli)), 'python')); } catch { /* Fall through to Python discovery. */ }
  }
  candidates.push(await locate('python3', env), await locate('python', env));
  // An explicit update refreshes a previously managed install, even when an image SDK is present.
  if (options.update && await probe(managedPython, runtimeEnv, run)) candidates.unshift(managedPython);
  for (const python of [...new Set(candidates.filter((value): value is string => Boolean(value)))]) {
    if (!await probe(python, runtimeEnv, run)) continue;
    if (!await managed(python, deps)) {
      try {
        await ensureSdkPatch(python, runtimeEnv, run);
        return { python, env: runtimeEnv };
      } catch { continue; }
    }
    if (!options.update) {
      await ensureSdkPatch(python, runtimeEnv, run, deps);
      return { python, env: runtimeEnv };
    }
    break;
  }

  if (process.platform !== 'linux' || !['x64', 'arm64'].includes(process.arch)) {
    throw new Error('Omnigent dependency installation requires Linux x64 or arm64.');
  }
  await mkdir(deps, { recursive: true, mode: 0o700 });
  // A managed directory must not redirect package-manager writes into an external installation.
  const root = await realpath(deps);
  if (root !== path.join(await realpath(options.stateDir), 'deps')) throw new Error('Omnigent dependency directory must be private.');
  for (const directory of ['bootstrap', 'bootstrap/bin', 'bin', 'uv', 'uv/tools', 'uv-cache', 'python']) {
    const target = path.join(deps, directory);
    await mkdir(target, { recursive: true, mode: 0o700 });
    if (!inside(root, await realpath(target))) throw new Error('Omnigent dependency directory must be private.');
  }
  const installEnv: NodeJS.ProcessEnv = {
    ...runtimeEnv, PIP_NO_INPUT: '1', PIP_DISABLE_PIP_VERSION_CHECK: '1',
    UV_CACHE_DIR: path.join(deps, 'uv-cache'), UV_TOOL_DIR: path.join(deps, 'uv', 'tools'),
    UV_TOOL_BIN_DIR: managedBin, UV_PYTHON_INSTALL_DIR: path.join(deps, 'python'),
  };
  const checkedRun = async (command: string, args: string[], timeoutMs: number, message: string) => {
    try { if ((await run(command, args, { env: installEnv, cwd: deps, timeoutMs })).code === 0) return; }
    catch { /* Only the fixed diagnostic crosses this boundary. */ }
    throw new Error(message);
  };
  let uv = await locate('uv', runtimeEnv);
  if (uv && !await pinnedUv(uv, installEnv, run)) uv = undefined;
  if (!uv) {
    const python = await locate('python3', env) ?? await locate('python', env);
    if (!python) throw new Error('Python with pip is required to bootstrap the private Omnigent dependency.');
    await checkedRun(python, ['-m', 'pip', '--version'], 10_000, 'Python pip is required to bootstrap the private Omnigent dependency.');
    await checkedRun(python, ['-m', 'pip', 'install', '--target', path.join(deps, 'bootstrap'), '--upgrade', '--no-cache-dir', '--disable-pip-version-check', `uv==${OMNIGENT_DEPENDENCY_VERSIONS.uv}`], 600_000, 'Private uv installation failed. Check package registry access and retry.');
    uv = path.join(bootstrapBin, 'uv');
    if (!await executable(uv)) throw new Error('The private uv executable is unavailable after installation.');
    if (!await pinnedUv(uv, installEnv, run)) throw new Error('The private uv executable does not match the pinned version.');
  }
  const args = ['tool', 'install', '--python', '3.12'];
  if (options.update) args.push('--force');
  args.push(`omnigent==${OMNIGENT_DEPENDENCY_VERSIONS.omnigent}`);
  await checkedRun(uv, args, 600_000, 'Private Omnigent installation failed. Check package registry access and retry.');
  if (!await probe(managedPython, runtimeEnv, run)) throw new Error('The private Omnigent SDK is unavailable after installation.');
  await ensureSdkPatch(managedPython, runtimeEnv, run, deps);
  return { python: managedPython, env: runtimeEnv };
}

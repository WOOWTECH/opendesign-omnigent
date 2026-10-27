import { execFile } from 'node:child_process';
import { copyFile, mkdir, mkdtemp, readFile, readdir, rm, writeFile } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { promisify } from 'node:util';
import { afterEach, beforeEach, describe, expect, it } from 'vitest';

const execFileAsync = promisify(execFile);
const daemonRoot = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '../../..');
const canonical = path.resolve(daemonRoot, '../../deploy/omnigent/sdk-patches');
const assetNames = ['apply.py', 'manifest.json', 'opencode-host-model-options.patch', 'opencode-quota-error.patch', 'opencode-native-interrupt.patch', 'pi-custom-model-options.patch', 'opencode-user-config-merge.patch'];
const hostKitCanonical = path.join(daemonRoot, 'src/integrations/omnigent/host-kit');
const hostKitAssetNames = ['supervisor.sh', 'og_helper.py', 'manifest.json'];

describe('Omnigent build assets', () => {
  let root: string;
  let destination: string;
  let patchSource: string;
  let hostKitSource: string;
  let builder: string;
  beforeEach(async () => {
    root = await mkdtemp(path.join(tmpdir(), 'od-omnigent-assets-'));
    destination = path.join(root, 'apps/daemon/dist/integrations/omnigent');
    const source = path.join(root, 'apps/daemon/src/integrations/omnigent');
    patchSource = path.join(root, 'deploy/omnigent/sdk-patches');
    hostKitSource = path.join(source, 'host-kit');
    await Promise.all([mkdir(destination, { recursive: true }), mkdir(hostKitSource, { recursive: true }), mkdir(patchSource, { recursive: true })]);
    await writeFile(path.join(source, 'adapter.py'), '# adapter fixture\n');
    for (const name of assetNames) await copyFile(path.join(canonical, name), path.join(patchSource, name));
    for (const name of hostKitAssetNames) await copyFile(path.join(hostKitCanonical, name), path.join(hostKitSource, name));
    // The asset builder has no TypeScript-only syntax; run its exact source in an isolated build tree.
    builder = path.join(destination, 'build-assets.mjs');
    await copyFile(path.join(daemonRoot, 'src/integrations/omnigent/build-assets.ts'), builder);
  });
  afterEach(async () => { await rm(root, { recursive: true, force: true }); });

  it('packages the exact canonical SDK patch and host kit alongside the adapter', async () => {
    await execFileAsync(process.execPath, [builder]);
    expect(await readFile(path.join(destination, 'adapter.py'), 'utf8')).toBe('# adapter fixture\n');
    expect((await readdir(path.join(destination, 'sdk-patches'))).sort()).toEqual([...assetNames].sort());
    for (const name of assetNames) {
      expect(await readFile(path.join(destination, 'sdk-patches', name))).toEqual(await readFile(path.join(canonical, name)));
    }
    expect((await readdir(path.join(destination, 'host-kit'))).sort()).toEqual([...hostKitAssetNames].sort());
    for (const name of hostKitAssetNames) {
      expect(await readFile(path.join(destination, 'host-kit', name))).toEqual(await readFile(path.join(hostKitCanonical, name)));
    }
  });

  it.each(['opencode-host-model-options.patch', 'opencode-quota-error.patch', 'opencode-native-interrupt.patch', 'pi-custom-model-options.patch', 'opencode-user-config-merge.patch'])('fails the build when %s is missing', async name => {
    await rm(path.join(patchSource, name));
    await expect(execFileAsync(process.execPath, [builder])).rejects.toMatchObject({ code: 1 });
  });

  it.each(hostKitAssetNames)('fails the build when host-kit/%s is missing', async (name) => {
    await rm(path.join(hostKitSource, name));
    await expect(execFileAsync(process.execPath, [builder])).rejects.toMatchObject({ code: 1 });
  });
});

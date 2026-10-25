import { copyFile, mkdir } from 'node:fs/promises';
import { fileURLToPath } from 'node:url';
import path from 'node:path';

const destination = path.dirname(fileURLToPath(import.meta.url));
const source = path.resolve(destination, '../../../src/integrations/omnigent');
await mkdir(destination, { recursive: true });
await copyFile(path.join(source, 'adapter.py'), path.join(destination, 'adapter.py'));

// Keep deploy/omnigent/sdk-patches as the single reviewed source for image and private SDK installs.
const patchSource = path.resolve(source, '../../../../../deploy/omnigent/sdk-patches');
const patchDestination = path.join(destination, 'sdk-patches');
await mkdir(patchDestination, { recursive: true });
for (const name of ['apply.py', 'manifest.json', 'opencode-host-model-options.patch', 'opencode-quota-error.patch', 'opencode-native-interrupt.patch']) {
  await copyFile(path.join(patchSource, name), path.join(patchDestination, name));
}

const hostKitSource = path.join(source, 'host-kit');
const hostKitDestination = path.join(destination, 'host-kit');
await mkdir(hostKitDestination, { recursive: true });
for (const name of ['supervisor.sh', 'og_helper.py', 'manifest.json']) {
  await copyFile(path.join(hostKitSource, name), path.join(hostKitDestination, name));
}

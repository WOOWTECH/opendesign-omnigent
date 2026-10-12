import { copyFile, mkdir } from 'node:fs/promises';
import { fileURLToPath } from 'node:url';
import path from 'node:path';

const destination = path.dirname(fileURLToPath(import.meta.url));
const source = path.resolve(destination, '../../../src/integrations/omnigent/adapter.py');
await mkdir(destination, { recursive: true });
await copyFile(source, path.join(destination, 'adapter.py'));

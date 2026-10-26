import { execFile } from 'node:child_process';
import { createHash } from 'node:crypto';
import { mkdtemp, mkdir, writeFile, rm } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { promisify } from 'node:util';
import { afterEach, beforeEach, describe, expect, it } from 'vitest';
const execute = promisify(execFile);
const script = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '../../../../../deploy/omnigent/verify-overlay.py');
const hash = (value: string) => createHash('sha256').update(value).digest('hex');
describe('current-base daemon overlay guard', () => {
  let root: string;
  let manifest: { baseImage: string; files: Record<string, string> };
  const contract = { baseImage: 'registry.example/od@sha256:fixture', daemonOverlayFiles: ['integrations/omnigent/adapter.py'],
    baseDaemonOverrides: { 'mcp-config.js': hash('new MCP'), 'routes/mcp-servers.js': hash('new route') } };
  beforeEach(async () => {
    root = await mkdtemp(path.join(tmpdir(), 'od-overlay-test-'));
    await mkdir(path.join(root, 'daemon/routes'), { recursive: true });
    await writeFile(path.join(root, 'daemon/server.js'), 'unchanged');
    await writeFile(path.join(root, 'daemon/mcp-config.js'), 'new MCP');
    await writeFile(path.join(root, 'daemon/routes/mcp-servers.js'), 'new route');
    manifest = { baseImage: contract.baseImage, files: { 'daemon-dist/server.js': hash('unchanged'),
      'daemon-dist/mcp-config.js': hash('old MCP'), 'daemon-dist/integrations/omnigent/adapter.py': hash('new adapter') } };
  });
  afterEach(async () => { await rm(root, { recursive: true, force: true }); });
  async function check() {
    await writeFile(path.join(root, 'manifest.json'), JSON.stringify(manifest));
    await writeFile(path.join(root, 'contract.json'), JSON.stringify(contract));
    return execute('python3', ['-B', script, path.join(root, 'manifest.json'), path.join(root, 'contract.json'), path.join(root, 'daemon')]);
  }
  it('accepts only the reviewed adapter delta while preserving newer MCP units', async () => {
    expect((await check()).stdout).toContain('native MCP additions preserved');
  });
  it('rejects another compiled backend change that would otherwise be omitted', async () => {
    manifest.files['daemon-dist/server.js'] = hash('unshipped new server');
    await expect(check()).rejects.toMatchObject({ code: 1, stderr: expect.stringContaining('Unexpected daemon change: server.js') });
  });
  it('rejects any change to the preserved native MCP bytes', async () => {
    await writeFile(path.join(root, 'daemon/mcp-config.js'), 'old MCP');
    await expect(check()).rejects.toMatchObject({ code: 1, stderr: expect.stringContaining('Base MCP asset changed: mcp-config.js') });
  });
  it('rejects a different base image', async () => {
    manifest.baseImage = 'registry.example/od:older';
    await expect(check()).rejects.toMatchObject({ code: 1 });
  });
});

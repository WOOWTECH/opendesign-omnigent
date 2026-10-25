import express from 'express';
import type { AddressInfo } from 'node:net';
import { mkdtemp, readFile, rm, writeFile } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import path from 'node:path';
import { afterEach, beforeEach, describe, expect, it } from 'vitest';
import type { McpServerUpsertResponse } from '@open-design/contracts';
import { readMcpConfig, writeMcpConfig, type McpServerConfig } from '../src/mcp-config.js';
import { isLocalSameOrigin } from '../src/origin-validation.js';
import { registerMcpServerRoutes, type RegisterMcpServerRoutesDeps } from '../src/routes/mcp-servers.js';

let dataDir: string;
let baseUrl: string;
let server: ReturnType<express.Express['listen']>;

const owned: McpServerConfig = {
  id: 'test-managed', transport: 'stdio', command: '/usr/bin/node',
  args: ['/managed/mcp.mjs'], enabled: true,
};
const peer: McpServerConfig = {
  id: 'peer', transport: 'http', url: 'https://example.test/mcp',
  authMode: 'none', headers: { 'X-Project': 'fixture' }, enabled: false,
};

beforeEach(async () => {
  dataDir = await mkdtemp(path.join(tmpdir(), 'od-mcp-upsert-'));
  const app = express();
  app.use(express.json());
  const resolvedPortRef = { current: 0 };
  registerMcpServerRoutes(app, {
    http: {
      isLocalSameOrigin,
      resolvedPortRef,
      sendApiError: (res: express.Response, status: number, code: string, message: string) =>
        res.status(status).json({ error: { code, message } }),
    },
    paths: { RUNTIME_DATA_DIR: dataDir },
  } as unknown as RegisterMcpServerRoutesDeps);
  server = app.listen(0, '127.0.0.1');
  await new Promise<void>((resolve, reject) => {
    server.once('listening', resolve);
    server.once('error', reject);
  });
  resolvedPortRef.current = (server.address() as AddressInfo).port;
  baseUrl = `http://127.0.0.1:${resolvedPortRef.current}`;
});

afterEach(async () => {
  await new Promise<void>((resolve, reject) => server.close((error) => error ? reject(error) : resolve()));
  await rm(dataDir, { recursive: true, force: true });
});

function put(id: string, body: unknown, origin = baseUrl) {
  return fetch(`${baseUrl}/api/mcp/servers/${id}`, {
    method: 'PUT',
    headers: { 'content-type': 'application/json', origin },
    body: JSON.stringify(body),
  });
}

describe('atomic MCP server mutations', () => {
  it('creates only the requested entry, preserves peers and returns no peer data', async () => {
    await writeMcpConfig(dataDir, { servers: [peer] });
    const response = await put(owned.id, { server: owned, expected: null });
    expect(response.status).toBe(200);
    expect(await response.json()).toEqual({ server: owned });
    expect(await readMcpConfig(dataDir)).toEqual({ servers: [peer, owned] });
  });

  it.each([true, false])('updates by full-entry CAS while preserving enabled=%s', async (enabled) => {
    const current = { ...owned, enabled };
    await writeMcpConfig(dataDir, { servers: [peer, current] });
    const desired = { ...owned, label: 'Updated', command: '/new/node', args: ['/new/mcp.mjs'] };
    const response = await put(owned.id, { server: desired, expected: current });
    expect(response.status).toBe(200);
    expect(await response.json()).toEqual({ server: { ...desired, enabled } });
    expect(await readMcpConfig(dataDir)).toEqual({ servers: [peer, { ...desired, enabled }] });
  });

  it('supports HTTP and SSE entries using the same single-entry contract', async () => {
    await writeMcpConfig(dataDir, { servers: [owned] });
    const added = await put(peer.id, { server: peer, expected: null });
    expect(added.status).toBe(200);
    const desired = { ...peer, transport: 'sse', url: 'https://example.test/events' };
    const updated = await put(peer.id, { server: desired, expected: peer });
    expect(updated.status).toBe(200);
    expect(await readMcpConfig(dataDir)).toEqual({ servers: [owned, desired] });
  });

  it('deletes only the selected entry after full-entry CAS', async () => {
    const current = { ...owned, enabled: false };
    await writeMcpConfig(dataDir, { servers: [peer, current] });
    const response = await put(owned.id, { server: null, expected: current });
    expect(response.status).toBe(200);
    expect(await response.json()).toEqual({ server: null });
    expect(await readMcpConfig(dataDir)).toEqual({ servers: [peer] });
  });

  it('accepts null/null only when the selected entry is absent', async () => {
    await writeMcpConfig(dataDir, { servers: [peer] });
    const response = await put(owned.id, { server: null, expected: null });
    expect(response.status).toBe(200);
    expect(await response.json()).toEqual({ server: null });
    expect((await put(peer.id, { server: null, expected: null })).status).toBe(409);
    expect(await readMcpConfig(dataDir)).toEqual({ servers: [peer] });
  });

  it('rejects a stale delete without changing either entry', async () => {
    const current = { ...owned, command: '/new-owner/node' };
    await writeMcpConfig(dataDir, { servers: [peer, current] });
    const response = await put(owned.id, { server: null, expected: owned });
    expect(response.status).toBe(409);
    expect(await readMcpConfig(dataDir)).toEqual({ servers: [peer, current] });
  });

  it.each([
    ['create collision', null],
    ['command ownership changed', { ...owned, command: '/foreign/node' }],
    ['argument ownership changed', { ...owned, args: ['/foreign/mcp.mjs'] }],
    ['user toggle changed', { ...owned, enabled: false }],
    ['other fields changed', { ...owned, env: { WORKSPACE: 'changed' } }],
  ])('returns a safe 409 without writing when %s', async (_name, expected) => {
    await writeMcpConfig(dataDir, { servers: [peer, owned] });
    const before = await readFile(path.join(dataDir, 'mcp-config.json'), 'utf8');
    const response = await put(owned.id, { server: { ...owned, label: 'new' }, expected });
    expect(response.status).toBe(409);
    expect(await response.json()).toEqual({ error: {
      code: 'MCP_SERVER_CONFLICT', message: 'server changed; expected entry does not match',
    } });
    expect(await readFile(path.join(dataDir, 'mcp-config.json'), 'utf8')).toBe(before);
  });

  it('rejects an expected entry that has been removed', async () => {
    await writeMcpConfig(dataDir, { servers: [peer] });
    const response = await put(owned.id, { server: owned, expected: owned });
    expect(response.status).toBe(409);
    expect(await readMcpConfig(dataDir)).toEqual({ servers: [peer] });
  });

  it.each([
    ['missing expected', { server: owned }],
    ['missing server', { expected: owned }],
    ['invalid body', []],
    ['arbitrary peer mutation', { server: owned, expected: null, servers: [] }],
    ['URL/server id mismatch', { server: { ...owned, id: 'different' }, expected: null }],
    ['URL/expected id mismatch', { server: owned, expected: { ...owned, id: 'different' } }],
    ['delete URL/expected id mismatch', { server: null, expected: { ...owned, id: 'different' } }],
    ['unknown server field', { server: { ...owned, arbitrary: true }, expected: null }],
    ['unknown expected field', { server: owned, expected: { ...owned, arbitrary: true } }],
    ['stdio with HTTP fields', { server: { ...owned, url: peer.url }, expected: null }],
    ['HTTP with stdio fields', { server: { ...peer, id: owned.id, command: owned.command }, expected: null }],
    ['invalid args', { server: { ...owned, args: [42] }, expected: null }],
    ['invalid enabled', { server: { ...owned, enabled: 'false' }, expected: null }],
    ['missing command', { server: { id: owned.id, transport: 'stdio', enabled: true }, expected: null }],
    ['invalid environment', { server: { ...owned, env: { EXAMPLE: 42 } }, expected: null }],
    ['invalid transport', { server: { ...owned, transport: 'udp' }, expected: null }],
    ['invalid URL', { server: { ...peer, id: owned.id, url: 'not-a-url' }, expected: null }],
    ['unsupported URL scheme', { server: { ...peer, id: owned.id, url: 'file:///tmp/example' }, expected: null }],
  ])('rejects %s without changing the config', async (_name, body) => {
    await writeMcpConfig(dataDir, { servers: [peer] });
    const response = await put(owned.id, body);
    expect(response.status).toBe(400);
    expect(await response.json()).toMatchObject({ error: { code: 'MCP_SERVER_INVALID_REQUEST' } });
    expect(await readMcpConfig(dataDir)).toEqual({ servers: [peer] });
  });

  it('enforces the native same-origin guard before writing', async () => {
    await writeMcpConfig(dataDir, { servers: [peer] });
    const response = await put(owned.id, { server: owned, expected: null }, 'https://foreign.example');
    expect(response.status).toBe(403);
    expect(await readMcpConfig(dataDir)).toEqual({ servers: [peer] });
  });

  it('validates the URL id even when both entries are null', async () => {
    const response = await put('invalid.id', { server: null, expected: null });
    expect(response.status).toBe(400);
    expect(await readMcpConfig(dataDir)).toEqual({ servers: [] });
  });

  it('preserves concurrent updates of different entries', async () => {
    await writeMcpConfig(dataDir, { servers: [peer] });
    const other = { ...owned, id: 'other' };
    const responses = await Promise.all([
      put(owned.id, { server: owned, expected: null }),
      put(other.id, { server: other, expected: null }),
    ]);
    expect(responses.map((response) => response.status)).toEqual([200, 200]);
    const config = await readMcpConfig(dataDir);
    expect(config.servers).toHaveLength(3);
    expect(config.servers).toEqual(expect.arrayContaining([peer, owned, other]));
  });

  it('allows only one concurrent replacement of the same expected entry', async () => {
    await writeMcpConfig(dataDir, { servers: [peer, owned] });
    const responses = await Promise.all(['first', 'second'].map((label) =>
      put(owned.id, { server: { ...owned, label }, expected: owned })));
    expect(responses.map((response) => response.status).sort()).toEqual([200, 409]);
    const winner = await responses.find((response) => response.status === 200)!.json() as McpServerUpsertResponse;
    expect(await readMcpConfig(dataDir)).toEqual({ servers: [peer, winner.server] });
  });

  it('serializes a delete racing with a replacement of the same entry', async () => {
    await writeMcpConfig(dataDir, { servers: [peer, owned] });
    const responses = await Promise.all([
      put(owned.id, { server: null, expected: owned }),
      put(owned.id, { server: { ...owned, label: 'Updated' }, expected: owned }),
    ]);
    expect(responses.map((response) => response.status).sort()).toEqual([200, 409]);
    const winner = await responses.find((response) => response.status === 200)!.json() as McpServerUpsertResponse;
    expect(await readMcpConfig(dataDir)).toEqual({ servers: winner.server ? [peer, winner.server] : [peer] });
  });

  it.each([
    '{broken',
    JSON.stringify({ servers: [peer, { id: 'invalid-peer' }] }),
    JSON.stringify({ servers: [peer, owned, owned] }),
    JSON.stringify({ servers: [peer, { ...peer, enabled: true }] }),
  ])(
    'fails closed without overwriting unreadable or invalid peer configuration (%#)', async (raw) => {
      const file = path.join(dataDir, 'mcp-config.json');
      await writeFile(file, raw);
      const response = await put(owned.id, { server: owned, expected: null });
      expect(response.status).toBe(500);
      expect(await response.json()).toEqual({ error: {
        code: 'MCP_SERVER_WRITE_FAILED', message: 'could not update MCP server',
      } });
      expect(await readFile(file, 'utf8')).toBe(raw);
    },
  );
});

import type { Express } from 'express';
import { isDeepStrictEqual } from 'node:util';
import type { McpServerConfig, McpServerUpsertRequest, McpServerUpsertResponse } from '@open-design/contracts';
import { sanitizeMcpServer, writeMcpConfig } from '../mcp-config.js';
import type { RouteDeps } from '../server-context.js';

export interface RegisterMcpServerRoutesDeps extends RouteDeps<'http' | 'paths'> {}

class McpServerConflict extends Error {}

const SERVER_ID_PATTERN = /^[a-z0-9][a-z0-9_-]{0,63}$/i;

function isObject(value: unknown): value is Record<string, unknown> {
  return value !== null && typeof value === 'object' && !Array.isArray(value);
}

function parseServer(value: unknown): McpServerConfig | null {
  if (!isObject(value) || typeof value.id !== 'string'
    || !SERVER_ID_PATTERN.test(value.id) || typeof value.enabled !== 'boolean') return null;
  const fields = ['id', 'label', 'templateId', 'transport', 'enabled'];
  if (value.transport === 'stdio') fields.push('command', 'args', 'env');
  else if (value.transport === 'http' || value.transport === 'sse') fields.push('url', 'authMode', 'headers');
  else return null;
  if (Object.keys(value).some((key) => !fields.includes(key))) return null;
  for (const field of ['label', 'templateId', 'command', 'url']) {
    const entry = value[field];
    if (Object.hasOwn(value, field) && (typeof entry !== 'string' || !entry.trim())) return null;
  }
  if (Object.hasOwn(value, 'args') && (!Array.isArray(value.args) || value.args.some((arg) => typeof arg !== 'string'))) return null;
  if (Object.hasOwn(value, 'authMode') && value.authMode !== 'none' && value.authMode !== 'oauth') return null;
  for (const field of ['env', 'headers']) {
    if (!Object.hasOwn(value, field)) continue;
    const map = value[field];
    if (!isObject(map) || Object.entries(map).some(([key, entry]) =>
      !key.trim() || key === '__proto__' || key === 'constructor' || typeof entry !== 'string' || !entry.trim())) return null;
  }
  return sanitizeMcpServer(value);
}

function parseRequest(value: unknown): McpServerUpsertRequest | null {
  if (!isObject(value) || !Object.hasOwn(value, 'server') || !Object.hasOwn(value, 'expected')
    || Object.keys(value).some((key) => key !== 'server' && key !== 'expected')) return null;
  const server = value.server === null ? null : parseServer(value.server);
  const expected = value.expected === null ? null : parseServer(value.expected);
  if ((value.server !== null && !server) || (value.expected !== null && !expected)) return null;
  return { server, expected };
}

/** Atomically change one entry without accepting a caller-owned peer list. */
export function registerMcpServerRoutes(app: Express, ctx: RegisterMcpServerRoutesDeps): void {
  const { isLocalSameOrigin, resolvedPortRef, sendApiError } = ctx.http;
  const { RUNTIME_DATA_DIR } = ctx.paths;

  app.put('/api/mcp/servers/:id', async (req, res) => {
    if (!isLocalSameOrigin(req, resolvedPortRef.current)) {
      return sendApiError(res, 403, 'MCP_SERVER_FORBIDDEN', 'cross-origin request rejected');
    }
    const parsed = parseRequest(req.body);
    const id = req.params.id;
    if (!parsed || !SERVER_ID_PATTERN.test(id) || (parsed.server !== null && parsed.server.id !== id)
      || (parsed.expected !== null && parsed.expected.id !== id)) {
      return sendApiError(res, 400, 'MCP_SERVER_INVALID_REQUEST', 'invalid single-server update');
    }
    const { server, expected } = parsed;
    try {
      const config = await writeMcpConfig(RUNTIME_DATA_DIR, (previous) => {
        const current = previous.servers.find((entry) => entry.id === id) ?? null;
        if (!isDeepStrictEqual(current, expected)) throw new McpServerConflict();
        if (server === null) {
          return { ...previous, servers: previous.servers.filter((entry) => entry.id !== id) };
        }
        const next = { ...server, enabled: current?.enabled === false ? false : server.enabled };
        return {
          ...previous,
          servers: current
            ? previous.servers.map((entry) => entry.id === id ? next : entry)
            : [...previous.servers, next],
        };
      });
      const response: McpServerUpsertResponse = { server: config.servers.find((entry) => entry.id === id) ?? null };
      return res.json(response);
    } catch (error) {
      if (error instanceof McpServerConflict) {
        return sendApiError(res, 409, 'MCP_SERVER_CONFLICT', 'server changed; expected entry does not match');
      }
      return sendApiError(res, 500, 'MCP_SERVER_WRITE_FAILED', 'could not update MCP server');
    }
  });
}

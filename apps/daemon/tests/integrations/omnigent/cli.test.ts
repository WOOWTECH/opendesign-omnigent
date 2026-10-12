import { spawn } from 'node:child_process';
import { createServer } from 'node:http';
import path from 'node:path';
import { Readable } from 'node:stream';
import { fileURLToPath } from 'node:url';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { isLocalSameOrigin } from '../../../src/origin-validation.js';
import { runOmnigentCli, type OmnigentCliOptions } from '../../../src/integrations/omnigent/cli.js';

describe('native Omnigent CLI', () => {
  let output: string[];
  let errors: string[];
  let request: ReturnType<typeof vi.fn<typeof fetch>>;
  let options: OmnigentCliOptions;
  let exitCode: typeof process.exitCode;
  beforeEach(() => {
    exitCode = process.exitCode;
    output = []; errors = [];
    request = vi.fn<typeof fetch>(async () => new Response(JSON.stringify({ phase: 'needs-login', installed: true }), { status: 200 }));
    options = { env: { OD_DAEMON_URL: 'http://127.0.0.1:7456/base/', OD_API_TOKEN: 'fixture-api-token' }, fetch: request, stdout: value => { output.push(value); }, stderr: value => { errors.push(value); } };
  });
  afterEach(() => { process.exitCode = exitCode; });
  it('sends authenticated status with a valid native loopback origin', async () => {
    expect(await runOmnigentCli(['status', '--json'], options)).toEqual({ exitCode: 0 });
    expect(request).toHaveBeenCalledWith('http://127.0.0.1:7456/base/api/integrations/omnigent/status', expect.objectContaining({ method: 'GET', headers: expect.objectContaining({ Authorization: 'Bearer fixture-api-token', Origin: 'http://127.0.0.1:7456' }), redirect: 'error' }));
    const headers = request.mock.calls[0]?.[1]?.headers as Record<string, string>;
    expect(isLocalSameOrigin({ headers: { host: '127.0.0.1:7456', origin: headers.Origin } }, 7456, {})).toBe(true);
    expect(JSON.parse(output.join(''))).toMatchObject({ phase: 'needs-login' });
    expect(errors).toEqual([]);
  });
  it.each(['enable', 'disable', 'verify', 'disconnect', 'repair', 'update'])('forwards the %s action to the native API', async action => {
    expect((await runOmnigentCli([action], options)).exitCode).toBe(0);
    expect(request.mock.calls[0]?.[0]).toBe('http://127.0.0.1:7456/base/api/integrations/omnigent/actions');
    expect(JSON.parse(request.mock.calls[0]?.[1]?.body as string)).toEqual({ action });
  });
  it('accepts setup credentials only from JSON stdin', async () => {
    options.stdin = Readable.from([JSON.stringify({ server: 'https://server.example', username: 'fixture-account', password: 'fixture-password' })]);
    expect((await runOmnigentCli(['setup', '--json'], options)).exitCode).toBe(0);
    const [url, init] = request.mock.calls[0]!;
    expect(url).not.toContain('fixture-password');
    expect(JSON.parse(init?.body as string)).toEqual({ action: 'setup', server: 'https://server.example', username: 'fixture-account', password: 'fixture-password' });
    expect(output.join('') + errors.join('')).not.toContain('fixture-password');
  });
  it.each([
    { args: ['setup', '--password', 'fixture-password'] },
    { args: ['setup', '--password=fixture-password'] },
    { args: ['setup', 'fixture-password'] },
  ])('rejects credentials in argv without echoing them', async ({ args }) => {
    expect((await runOmnigentCli(args, options)).exitCode).toBe(2);
    expect(request).not.toHaveBeenCalled();
    expect(output.join('') + errors.join('')).not.toContain('fixture-password');
  });
  it.each(['{"password":"fixture-password",', '{"server":"x","username":"u","password":"p","extra":true}', '[]', '{"server":"x","username":"u","password":""}'])('rejects invalid setup JSON without forwarding or exposing it', async input => {
    options.stdin = Readable.from([input]);
    expect((await runOmnigentCli(['setup'], options)).exitCode).toBe(2);
    expect(request).not.toHaveBeenCalled();
    expect(errors.join('')).not.toContain('fixture-password');
  });
  it('bounds setup input size and refuses an interactive terminal', async () => {
    options.stdin = Readable.from(['x'.repeat(16_385)]);
    expect((await runOmnigentCli(['setup'], options)).exitCode).toBe(2);
    options.stdin = Object.assign(Readable.from([]), { isTTY: true });
    expect((await runOmnigentCli(['setup'], options)).exitCode).toBe(2);
    expect(request).not.toHaveBeenCalled();
  });
  it('passes workspace authority and verification selection using native headers', async () => {
    await runOmnigentCli(['verify', '--workspace=w1', '--workspace-member', 'm1', '--engine', 'codex', '--model=example'], options);
    expect(request.mock.calls[0]?.[1]?.headers).toMatchObject({ 'x-od-workspace-id': 'w1', 'x-od-workspace-member-id': 'm1' });
    expect(JSON.parse(request.mock.calls[0]?.[1]?.body as string)).toEqual({ action: 'verify', engine: 'codex', model: 'example' });
  });
  it('rejects incomplete workspace authority before making a request', async () => {
    expect((await runOmnigentCli(['status', '--workspace', 'w1'], options)).exitCode).toBe(2);
    expect(request).not.toHaveBeenCalled();
  });
  it('prefers an explicit daemon URL over the environment', async () => {
    await runOmnigentCli(['status', '--daemon-url=http://localhost:7457'], options);
    expect(request.mock.calls[0]?.[0]).toBe('http://localhost:7457/api/integrations/omnigent/status');
  });
  it.each(['ftp://127.0.0.1', 'http://user:fixture-password@127.0.0.1', 'http://127.0.0.1/?token=fixture-password'])('rejects unsafe daemon URLs without echoing them', async url => {
    expect((await runOmnigentCli(['status', '--daemon-url', url], options)).exitCode).toBe(2);
    expect(request).not.toHaveBeenCalled();
    expect(errors.join('')).not.toContain('fixture-password');
  });
  it('never prints a raw HTTP error body, including with --json', async () => {
    request.mockResolvedValue(new Response(JSON.stringify({ error: { message: 'fixture-password', code: 'secret-code' } }), { status: 403 }));
    expect((await runOmnigentCli(['status', '--json'], options)).exitCode).toBe(1);
    expect(errors.join('')).toContain('HTTP 403');
    expect(errors.join('')).not.toMatch(/fixture-password|secret-code/);
    expect(output).toEqual([]);
    expect(process.exitCode).toBe(1);
  });
  it('does not expose thrown fetch errors or follow redirects carrying credentials', async () => {
    request.mockRejectedValue(new Error('fixture-password'));
    expect((await runOmnigentCli(['status'], options)).exitCode).toBe(3);
    expect(errors.join('')).not.toContain('fixture-password');
    expect(request.mock.calls[0]?.[1]?.redirect).toBe('error');
  });
  it.each(['not-json fixture-password', JSON.stringify({ ok: false, error: { message: 'fixture-password' } })])('rejects malformed or failed success envelopes without printing their contents', async body => {
    request.mockResolvedValue(new Response(body, { status: 200 }));
    expect((await runOmnigentCli(['status', '--json'], options)).exitCode).toBe(1);
    expect(errors.join('')).not.toContain('fixture-password');
    expect(output).toEqual([]);
  });
  it('prints help without discovery or an API request', async () => {
    expect((await runOmnigentCli(['--help'], options)).exitCode).toBe(0);
    expect(output.join('')).toContain('JSON from stdin');
    expect(request).not.toHaveBeenCalled();
  });

  it('runs the built native od dispatcher against a live loopback HTTP fixture', async () => {
    const received: { method?: string; url?: string; token?: string; origin?: string; body: string }[] = [];
    const server = createServer(async (req, res) => {
      let body = '';
      for await (const chunk of req) body += String(chunk);
      received.push({ ...(req.method ? { method: req.method } : {}), ...(req.url ? { url: req.url } : {}), ...(req.headers.authorization ? { token: req.headers.authorization } : {}), ...(req.headers.origin ? { origin: req.headers.origin } : {}), body });
      res.setHeader('Content-Type', 'application/json');
      res.end(JSON.stringify(req.method === 'POST' ? { ok: true, status: { phase: 'connecting' } } : { installed: true, phase: 'needs-login' }));
    });
    await new Promise<void>(resolve => server.listen(0, '127.0.0.1', resolve));
    try {
      const address = server.address();
      if (!address || typeof address === 'string') throw new Error('Expected loopback address');
      const url = `http://127.0.0.1:${address.port}`;
      const launcher = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '../../../bin/od.mjs');
      const launch = (action: string, input = '') => new Promise<{ code: number | null; output: string; errors: string }>((resolve, reject) => {
        const child = spawn(process.execPath, [launcher, 'omnigent', action, '--json', '--daemon-url', url], { env: { ...process.env, OD_API_TOKEN: 'fixture-api-token' }, stdio: ['pipe', 'pipe', 'pipe'] });
        let output = ''; let errors = '';
        child.stdout.on('data', chunk => { output += String(chunk); });
        child.stderr.on('data', chunk => { errors += String(chunk); });
        child.on('error', reject);
        child.on('close', code => resolve({ code, output, errors }));
        child.stdin.end(input);
      });
      const status = await launch('status');
      expect(status.code).toBe(0);
      expect(JSON.parse(status.output)).toMatchObject({ phase: 'needs-login' });
      const setup = await launch('setup', JSON.stringify({ server: 'https://server.example', username: 'fixture-account', password: 'fixture-password' }));
      expect(setup.code).toBe(0);
      expect(received.map(req => [req.method, req.url])).toEqual([['GET', '/api/integrations/omnigent/status'], ['POST', '/api/integrations/omnigent/actions']]);
      expect(received.every(req => req.token === 'Bearer fixture-api-token' && req.origin === url)).toBe(true);
      expect(JSON.parse(received[1]!.body)).toMatchObject({ action: 'setup', password: 'fixture-password' });
      expect(status.output + status.errors + setup.output + setup.errors).not.toContain('fixture-password');
    } finally { await new Promise<void>((resolve, reject) => server.close(error => error ? reject(error) : resolve())); }
  });
});

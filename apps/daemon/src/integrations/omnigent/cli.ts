import type { OmnigentAction, OmnigentActionRequest } from '@open-design/contracts';
import { resolveDaemonUrl } from '../../daemon-url.js';

const ACTIONS = new Set<string>(['status', 'enable', 'disable', 'setup', 'verify', 'disconnect', 'repair', 'update']);
const STRING_FLAGS = new Set(['daemon-url', 'workspace', 'workspace-member', 'engine', 'model']);
const USAGE = `Usage: od omnigent <status|enable|disable|setup|verify|disconnect|repair|update> [options]

Options:
  --json                         Emit the daemon JSON response.
  --daemon-url <url>              OpenDesign daemon HTTP base.
  --workspace <id>                Workspace id; requires --workspace-member.
  --workspace-member <id>         Workspace member id; requires --workspace.
  --engine <id> --model <id>      Select an engine/model for verify.
  --help                         Show this help.

Setup reads only JSON from stdin: {"server":"https://server.example","username":"account","password":"..."}
Supply OD_API_TOKEN through the environment when daemon authentication is enabled.
`;
export interface OmnigentCliOptions {
  env?: NodeJS.ProcessEnv;
  stdin?: AsyncIterable<string | Buffer> & { isTTY?: boolean };
  stdout?: (text: string) => void;
  stderr?: (text: string) => void;
  fetch?: typeof fetch;
}
class CliError extends Error {
  constructor(public readonly code: string, message: string, public readonly exitCode = 2) { super(message); }
}
function parse(args: string[]): { command?: string; flags: Record<string, string>; json: boolean; help: boolean } {
  let command: string | undefined;
  const flags: Record<string, string> = {};
  let json = false;
  let help = false;
  for (let i = 0; i < args.length; i++) {
    const arg = args[i]!;
    if (arg === '--help' || arg === '-h') { help = true; continue; }
    if (arg === '--json') { json = true; continue; }
    if (arg.startsWith('--')) {
      const equals = arg.indexOf('=');
      const name = arg.slice(2, equals < 0 ? undefined : equals);
      // Never echo arguments: rejected flags/positionals can contain credentials.
      if (!STRING_FLAGS.has(name)) throw new CliError('INVALID_ARGUMENTS', 'Unsupported Omnigent option. Use --help; setup credentials must be supplied as JSON on stdin.');
      const value = equals < 0 ? args[++i] : arg.slice(equals + 1);
      if (!value || value.startsWith('--') || /[\x00-\x1f\x7f]/.test(value)) throw new CliError('INVALID_ARGUMENTS', 'An Omnigent option requires a nonempty value.');
      flags[name] = value;
    } else {
      if (command || !ACTIONS.has(arg)) throw new CliError('INVALID_ARGUMENTS', 'Choose a supported Omnigent command. Use --help.');
      command = arg;
    }
  }
  if (command !== 'verify' && (flags.engine || flags.model)) throw new CliError('INVALID_ARGUMENTS', '--engine and --model are supported only by verify.');
  return { ...(command ? { command } : {}), flags, json, help };
}
async function setupInput(stdin: NonNullable<OmnigentCliOptions['stdin']>): Promise<Pick<OmnigentActionRequest, 'server' | 'username' | 'password'>> {
  if (stdin.isTTY) throw new CliError('STDIN_REQUIRED', 'Pipe setup JSON containing server, username and password to stdin.');
  const chunks: Buffer[] = [];
  let size = 0;
  for await (const chunk of stdin) {
    const buffer = Buffer.isBuffer(chunk) ? chunk : Buffer.from(chunk);
    size += buffer.length;
    if (size > 16_384) throw new CliError('INVALID_INPUT', 'Setup JSON exceeds the input limit.');
    chunks.push(buffer);
  }
  let value: unknown;
  try { value = JSON.parse(Buffer.concat(chunks).toString('utf8')); }
  catch { throw new CliError('INVALID_INPUT', 'Setup stdin must contain valid JSON.'); }
  if (!value || typeof value !== 'object' || Array.isArray(value)) throw new CliError('INVALID_INPUT', 'Setup JSON must contain server, username and password.');
  const data = value as Record<string, unknown>;
  if (Object.keys(data).some(key => !['server', 'username', 'password'].includes(key)) ||
    typeof data.server !== 'string' || !data.server.trim() || data.server.length > 2_048 ||
    typeof data.username !== 'string' || !data.username.trim() || data.username.length > 254 ||
    typeof data.password !== 'string' || !data.password || data.password.length > 4_096) {
    throw new CliError('INVALID_INPUT', 'Setup JSON must contain only nonempty server, username and password strings.');
  }
  return { server: data.server, username: data.username, password: data.password };
}

/** Thin native API client. Passwords are accepted only over stdin; failures use fixed diagnostics. */
export async function runOmnigentCli(args: string[], options: OmnigentCliOptions = {}): Promise<{ exitCode: number }> {
  const env = options.env ?? process.env;
  const stdout = options.stdout ?? ((text: string) => { process.stdout.write(text); });
  const stderr = options.stderr ?? ((text: string) => { process.stderr.write(text); });
  const finish = (exitCode: number) => { process.exitCode = exitCode; return { exitCode }; };
  try {
    const { command, flags, json, help } = parse(args);
    if (help || !command) { stdout(USAGE); return finish(0); }
    const workspace = flags.workspace?.trim();
    const member = flags['workspace-member']?.trim();
    if (Boolean(workspace) !== Boolean(member) || (flags.workspace !== undefined && !workspace) || (flags['workspace-member'] !== undefined && !member)) {
      throw new CliError('WORKSPACE_CONTEXT_REQUIRED', 'Pass --workspace and --workspace-member together.');
    }
    const body: OmnigentActionRequest | undefined = command === 'status' ? undefined : { action: command as OmnigentAction };
    if (command === 'setup') Object.assign(body!, await setupInput(options.stdin ?? process.stdin));
    if (command === 'verify') {
      for (const key of ['engine', 'model'] as const) {
        if (flags[key]) {
          if (flags[key].length > 256) throw new CliError('INVALID_ARGUMENTS', 'Engine and model ids must be at most 256 characters.');
          body![key] = flags[key];
        }
      }
    }
    let base: URL;
    try {
      base = new URL(await resolveDaemonUrl({ env, ...(flags['daemon-url'] ? { flagUrl: flags['daemon-url'] } : {}) }));
      if (!['http:', 'https:'].includes(base.protocol) || base.username || base.password || base.search || base.hash) throw new Error();
    } catch { throw new CliError('INVALID_DAEMON_URL', 'Resolve a valid daemon HTTP URL without credentials, query parameters or fragments.'); }
    const headers: Record<string, string> = { Accept: 'application/json', Origin: base.origin };
    if (env.OD_API_TOKEN) headers.Authorization = `Bearer ${env.OD_API_TOKEN}`;
    if (workspace && member) { headers['x-od-workspace-id'] = workspace; headers['x-od-workspace-member-id'] = member; }
    if (body) headers['Content-Type'] = 'application/json';
    const endpoint = `${base.href.replace(/\/+$/, '')}/api/integrations/omnigent/${body ? 'actions' : 'status'}`;
    let response: Response;
    try {
      response = await (options.fetch ?? fetch)(endpoint, {
        method: body ? 'POST' : 'GET', headers, redirect: 'error', signal: AbortSignal.timeout(30_000),
        ...(body ? { body: JSON.stringify(body) } : {}),
      });
    } catch { throw new CliError('DAEMON_UNREACHABLE', 'The daemon request could not finish. Check its URL, authentication and availability.', 3); }
    if (!response.ok) {
      // Do not parse or print the raw error body, even with --json.
      await response.body?.cancel().catch(() => undefined);
      throw new CliError('HTTP_ERROR', `Omnigent request failed (HTTP ${response.status}). Check daemon authentication and integration status.`, 1);
    }
    let result: unknown;
    try { result = await response.json(); }
    catch { throw new CliError('INVALID_RESPONSE', 'The daemon returned an invalid Omnigent response.', 1); }
    if (!result || typeof result !== 'object' || Array.isArray(result) || 'error' in result || ('ok' in result && result.ok !== true)) {
      throw new CliError('INVALID_RESPONSE', 'The daemon returned an invalid Omnigent response.', 1);
    }
    if (json) stdout(`${JSON.stringify(result)}\n`);
    else {
      const status = ('status' in result ? result.status : result) as Record<string, unknown> | null;
      const phase = typeof status?.phase === 'string' ? status.phase.replace(/[\x00-\x1f\x7f]/g, '').slice(0, 64) : 'accepted';
      stdout(`[omnigent] ${command}: ${phase}\n`);
    }
    return finish(0);
  } catch (error) {
    const known = error instanceof CliError ? error : new CliError('CLI_FAILED', 'The Omnigent command did not finish.', 1);
    stderr(`${JSON.stringify({ ok: false, error: { code: known.code, message: known.message } })}\n`);
    return finish(known.exitCode);
  }
}

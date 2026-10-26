import { execFile } from 'node:child_process';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { promisify } from 'node:util';
import { describe, expect, it } from 'vitest';

const exec = promisify(execFile);
const adapter = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '../../../src/integrations/omnigent/adapter.py');

describe('Omnigent managed host', () => {
  it('drains the owned host through the built daemon runtime stop hook', async () => {
    const script = `
import assert from 'node:assert/strict';
import { spawn } from 'node:child_process';
import { once } from 'node:events';
import { mkdtemp, mkdir, readFile, rm, writeFile } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import path from 'node:path';
import { pathToFileURL } from 'node:url';
const root = await mkdtemp(path.join(tmpdir(), 'od-omnigent-daemon-stop-'));
const state = path.join(root, 'data/integrations/omnigent');
const home = path.join(state, 'host');
await mkdir(home, { recursive: true });
const owner = 'e'.repeat(64);
await writeFile(path.join(state, 'host-owner.json'), JSON.stringify({ schema: 1, id: owner }), { mode: 0o600 });
const receipt = path.join(root, 'stopped');
process.env.OD_DATA_DIR = path.join(root, 'data');
process.env.HOME = root;
process.env.OPEN_DESIGN_VELA_TELEMETRY = 'off';
const child = spawn(process.execPath, ['-e', "const fs=require('node:fs'); process.on('SIGTERM',()=>{fs.writeFileSync(process.argv[1],'TERM');process.exit(0)});console.log('ready');setInterval(()=>{},1000)", receipt], {
  env: { ...process.env, OD_OMNIGENT_HOST_OWNER: owner, OD_OMNIGENT_HOST_HOME: home }, stdio: ['ignore', 'pipe', 'pipe'], detached: true,
});
const exited = once(child, 'exit');
let runtime;
try {
  await once(child.stdout, 'data');
  const { startDaemonRuntime } = await import(pathToFileURL(process.argv[1]).href);
  runtime = await startDaemonRuntime({ host: '127.0.0.1', port: 0, openBrowser: false, logListening: false });
  await Promise.all([runtime.stop(), runtime.stop()]);
  assert.equal(await readFile(receipt, 'utf8'), 'TERM');
  assert.deepEqual(await exited, [0, null]);
  console.log('owned host stopped through daemon runtime');
} finally {
  if (child.exitCode === null && child.signalCode === null) child.kill('SIGKILL');
  await exited;
  await runtime?.stop();
  await rm(root, { recursive: true, force: true });
}
`;
    const runtime = path.resolve(path.dirname(adapter), '../../../dist/daemon-startup.js');
    const result = await exec(process.execPath, ['--input-type=module', '-e', script, runtime], { timeout: 25_000, maxBuffer: 1024 * 1024 });
    expect(result.stdout).toContain('owned host stopped through daemon runtime');
  }, 30_000);
  it('requires explicit authorization and successful login before starting the native supervisor', async () => {
    const script = `
import importlib.util, json, pathlib, sys, tempfile
from unittest.mock import Mock, patch
spec = importlib.util.spec_from_file_location('adapter', sys.argv[1])
m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m)
with tempfile.TemporaryDirectory() as root:
    request = dict(server='https://omnigent.example', stateDir=root, configHome=root, dataDir=root, workspace=root)
    a = m.Adapter(request, auth=Mock(), client_factory=Mock())
    a.local_host = Mock(return_value=dict(managerRunning=False))
    with patch.object(m.subprocess, 'Popen') as spawn:
        try: a.ensure_host(request['server'])
        except m.AdapterError as e: assert e.code == 'KIT_NOT_RUNNING'
        else: raise AssertionError('unapproved host started')
        spawn.assert_not_called()
    a.request['allowManagedHost'] = True
    events = []
    a.login = lambda: events.append('login')
    a.merge_config = lambda _: events.append('merge')
    original = a.ensure_host
    a.ensure_host = lambda _: (events.append('start') or 'kit')
    a.catalog = lambda **_: dict(host=dict(connected=True, id='owned-host'))
    a.recover_probes = Mock()
    a.connect()
    assert events == ['login', 'merge', 'start'], events
    events.clear()
    a.login = Mock(side_effect=m.AdapterError('needs_login', 'rejected'))
    try: a.connect()
    except m.AdapterError: pass
    assert events == []
    a.ensure_host = original
    a.local_host = Mock(side_effect=[dict(managerRunning=False), dict(managerRunning=True)])
    with patch.object(pathlib.Path, 'is_file', return_value=True), patch.object(m.subprocess, 'Popen') as spawn:
        assert a.ensure_host(request['server']) == 'kit'
        args, kwargs = spawn.call_args
        assert args[0][0] == '/bin/sh' and args[0][1].endswith('/host-kit/supervisor.sh')
        assert kwargs['start_new_session'] is True
        assert kwargs['env']['OG_CONFIG_HOME'] == root
        assert 'OD_API_TOKEN' in kwargs['env']['OG_UNSET_ENV'].split(',')
        assert kwargs['env']['OG_BIN'] == str(pathlib.Path(sys.executable).parent / 'omnigent')
print(json.dumps({'authorization':True,'loginBeforeStart':True,'nativeKit':True}))
`;
    const result = await exec('python3', ['-B', '-c', script, adapter], { timeout: 15_000 });
    expect(JSON.parse(result.stdout)).toEqual({ authorization: true, loginBeforeStart: true, nativeKit: true });
  });
});

import { execFile } from 'node:child_process';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { promisify } from 'node:util';
import { describe, expect, it } from 'vitest';

const exec = promisify(execFile);
const adapter = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '../../../src/integrations/omnigent/adapter.py');

describe('Omnigent managed host', () => {
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

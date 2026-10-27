"""Exercise the SDK's real config merge function without importing its server dependencies."""
import ast
import json
import logging
from pathlib import Path
import sys
import tempfile
import types


source = Path(sys.argv[1])
module = ast.parse(source.read_text())
function = next(node for node in module.body
                if isinstance(node, ast.FunctionDef) and node.name == 'maybe_merge_user_provider_config')

with tempfile.TemporaryDirectory() as directory:
    config_dir = Path(directory) / 'opencode'
    config_dir.mkdir()
    jsonc = config_dir / 'opencode.jsonc'
    jsonc.write_text('{"$schema":"https://opencode.ai/config.json"}')
    (config_dir / 'opencode.json').write_text(json.dumps({
        'provider': {'litellm': {'models': {'chatgpt/gpt-5.6-sol': {'name': 'ChatGPT'}}}}
    }))
    scope = {
        'json': json,
        '_logger': logging.getLogger('dual-config-check'),
        '_strip_jsonc_comments': lambda value: value,
        '_strip_trailing_commas': lambda value: value,
        'user_opencode_config_path': lambda: jsonc,
    }
    for name in ('omnigent', 'omnigent.harnesses', 'omnigent.harnesses.opencode_native'):
        sys.modules[name] = types.ModuleType(name)
    bridge = types.ModuleType('omnigent.harnesses.opencode_native.bridge')
    bridge.user_opencode_config_path = lambda: jsonc
    sys.modules[bridge.__name__] = bridge
    code = compile(ast.Module(body=[function], type_ignores=[]), str(source), 'exec')
    exec(code, scope)
    result = scope['maybe_merge_user_provider_config']({})
    assert 'chatgpt/gpt-5.6-sol' in result.get('provider', {}).get('litellm', {}).get('models', {}), \
        'DUAL_CONFIG_PROVIDER_MISSING'
    print('DUAL_CONFIG_PROVIDER_OK')

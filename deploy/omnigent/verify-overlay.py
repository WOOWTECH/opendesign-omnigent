#!/usr/bin/env python3
"""Refuse a partial daemon overlay that would omit another compiled change."""
import hashlib
import json
from pathlib import Path
import sys

manifest = json.loads(Path(sys.argv[1]).read_text())
contract = json.loads(Path(sys.argv[2]).read_text())
root = Path(sys.argv[3])
assert manifest['baseImage'] == contract['baseImage']
allowed = set(contract['daemonOverlayFiles'])
preserved = contract['baseDaemonOverrides']
for relative, expected in preserved.items():
    assert hashlib.sha256((root / relative).read_bytes()).hexdigest() == expected, 'Base MCP asset changed: ' + relative
for name, expected in manifest['files'].items():
    if not name.startswith('daemon-dist/'):
        continue
    relative = name.removeprefix('daemon-dist/')
    if relative in allowed or relative in preserved:
        continue
    assert hashlib.sha256((root / relative).read_bytes()).hexdigest() == expected, 'Unexpected daemon change: ' + relative
print('Daemon overlay checked; native MCP additions preserved')

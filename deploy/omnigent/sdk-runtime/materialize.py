"""Copy the pinned SDK's runtime closure, refusing to replace different files."""
import hashlib
import json
from pathlib import Path
import shutil
import sys

mode, manifest_path, bundle_path = sys.argv[1:]
assert mode in ('collect', 'install')
manifest = json.loads(Path(manifest_path).read_text())
bundle = Path(bundle_path)
bundle.mkdir(parents=True, exist_ok=True)
for absolute, expected in manifest['runtimeFiles'].items():
    target = Path(absolute)
    assert target.parent == Path('/usr/lib') and target.name.startswith(('libre2.', 'libabsl_', 'libicu'))
    source, destination = (target, bundle / target.name) if mode == 'collect' else (bundle / target.name, target)
    assert hashlib.sha256(source.read_bytes()).hexdigest() == expected['sha256'], 'SDK library hash mismatch'
    if expected['kind'] == 'symlink':
        assert source.is_symlink() and source.readlink().as_posix() == expected['target'], 'SDK symlink mismatch'
    if destination.exists() or destination.is_symlink():
        assert hashlib.sha256(destination.read_bytes()).hexdigest() == expected['sha256'], 'Base library conflict'
        continue
    shutil.copy2(source, destination, follow_symlinks=False)
print('SDK runtime closure verified')

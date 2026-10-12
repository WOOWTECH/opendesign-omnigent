#!/usr/bin/env python3
"""Package already-built native outputs and the public pinned SDK patch."""
import gzip
import hashlib
import io
import json
from pathlib import Path
import subprocess
import sys
import tarfile

root = Path(__file__).resolve().parents[2]
if len(sys.argv) != 2:
    sys.exit('Usage: build-context.py OUTPUT_TAR_GZ')
artifacts = {'daemon-dist': root / 'apps/daemon/dist', 'web-out': root / 'apps/web/out'}
if not (artifacts['daemon-dist'] / 'cli.js').is_file() or not (artifacts['web-out'] / 'index.html').is_file():
    sys.exit('Build daemon and web outputs before packaging.')
if subprocess.check_output(['git', 'status', '--porcelain', '--untracked-files=normal'], cwd=root):
    sys.exit('Commit source changes before packaging a deployable image.')
revision = subprocess.check_output(['git', 'rev-parse', 'HEAD'], cwd=root, text=True).strip()
if any(p.is_symlink() for base in artifacts.values() for p in base.rglob('*')):
    sys.exit('Build artifacts must contain regular files only.')
entries = [(name + '/' + p.relative_to(base).as_posix(), p.read_bytes())
           for name, base in artifacts.items() for p in sorted(base.rglob('*')) if p.is_file()]
for name in ('Dockerfile', 'sdk-patches/apply.py', 'sdk-patches/opencode-host-model-options.patch'):
    entries.append((name, (root / 'deploy/omnigent' / name).read_bytes()))
manifest = {'schemaVersion': 1, 'sourceRevision': revision,
            'upstreamRevision': '89e64d813bb1c7a11519b3f668f011f7017637d7',
            'runtimeDependencyClosure': 'unchanged base image',
            'lockfileSha256': hashlib.sha256((root / 'pnpm-lock.yaml').read_bytes()).hexdigest(),
            'files': {name: hashlib.sha256(data).hexdigest() for name, data in entries},
            'sdkPatches': json.loads((root / 'deploy/omnigent/sdk-patches/manifest.json').read_text())}
entries.append(('manifest.json', (json.dumps(manifest, sort_keys=True, indent=2) + '\n').encode()))
output = Path(sys.argv[1]).resolve()
with output.open('xb') as stream, gzip.GzipFile(filename='', fileobj=stream, mode='wb', mtime=0) as compressed, tarfile.open(fileobj=compressed, mode='w', format=tarfile.PAX_FORMAT) as archive:
    for name, data in sorted(entries):
        entry = tarfile.TarInfo(name)
        entry.size = len(data)
        entry.uid = entry.gid = 1001
        entry.mode = 0o644
        entry.mtime = 0
        archive.addfile(entry, io.BytesIO(data))
print(json.dumps({'sourceRevision': revision, 'fileCount': len(entries), 'archiveBytes': output.stat().st_size}))

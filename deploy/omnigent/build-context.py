#!/usr/bin/env python3
"""Package existing build outputs; never build, install, publish or deploy."""
import argparse
import gzip
import hashlib
import io
import json
from pathlib import Path
import stat
import subprocess
import tarfile


BASE_IMAGE = 'jcr-prod.woowtech.io/woow-paas-docker-local/open-design@sha256:daf04fb889bd0545b3af14b2422305f86d2e5b77346498f6e0ca3a6c05e82a0b'
UPSTREAM_REVISION = '89e64d813bb1c7a11519b3f668f011f7017637d7'


def sha256(data):
    return hashlib.sha256(data).hexdigest()


def regular_files(base):
    if not base.is_dir() or base.is_symlink():
        raise ValueError(f'Missing or unsafe artifact directory: {base}')
    result = []
    for item in sorted(base.rglob('*')):
        mode = item.lstat().st_mode
        if stat.S_ISREG(mode):
            result.append(item)
        elif not stat.S_ISDIR(mode):
            raise ValueError(f'Artifact must be a regular file or directory: {item}')
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('output', type=Path, help='New archive outside the source tree')
    parser.add_argument('--expected-revision', help='Require this full committed source revision')
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[2]
    output = args.output.resolve()
    if output.is_relative_to(root):
        raise ValueError('Write the build context outside the source tree.')
    if output.exists():
        raise ValueError('Output already exists; choose a new archive path.')

    def git(*command):
        return subprocess.check_output(['git', *command], cwd=root, text=True).strip()

    revision = git('rev-parse', 'HEAD')
    if args.expected_revision and args.expected_revision != revision:
        raise ValueError('Source revision does not match --expected-revision.')
    if git('status', '--porcelain', '--untracked-files=normal'):
        raise ValueError('Commit source changes before packaging a deployable image.')

    artifacts = {
        'daemon-dist': root / 'apps/daemon/dist',
        'bundled-omnigent': root / 'plugins/_official/woow/woow-omnigent',
    }
    required = {
        'daemon-dist': ['cli.js', 'integrations/omnigent/service.js',
                        'integrations/omnigent/dependencies.js',
                         'integrations/omnigent/dependencies.js.map',
                         'integrations/omnigent/build-assets.js',
                         'integrations/omnigent/build-assets.js.map',
                        'integrations/omnigent/adapter.py',
                        'integrations/omnigent/sdk-patches/apply.py',
                        'integrations/omnigent/sdk-patches/manifest.json',
                        'integrations/omnigent/sdk-patches/opencode-host-model-options.patch',
                         'integrations/omnigent/sdk-patches/opencode-quota-error.patch',
                         'integrations/omnigent/sdk-patches/opencode-native-interrupt.patch',
                         'integrations/omnigent/sdk-patches/pi-custom-model-options.patch'],
        'bundled-omnigent': ['open-design.json'],
    }
    entries = {}
    modes = {}
    for prefix, base in artifacts.items():
        files = regular_files(base)
        for relative in required[prefix]:
            if not (base / relative).is_file() or not (base / relative).stat().st_size:
                raise ValueError(f'Missing build asset: {prefix}/{relative}. Rebuild before packaging.')
        for item in files:
            name = prefix + '/' + item.relative_to(base).as_posix()
            entries[name] = item.read_bytes()
            modes[name] = 0o755 if item.stat().st_mode & 0o111 else 0o644

    # The daemon owns the private host runtime. Refuse a UI-only image while
    # leaving the kit's directory layout to the daemon's build-assets contract.
    integration_prefix = 'daemon-dist/integrations/omnigent/'
    host_assets = [name for name in entries if name.startswith(integration_prefix)
                   and Path(name).name in ('supervisor.sh', 'og_helper.py')]
    for basename in ('supervisor.sh', 'og_helper.py'):
        if not any(Path(name).name == basename and entries[name] for name in host_assets):
            raise ValueError(f'Missing daemon host-kit asset: {basename}. Rebuild after host integration.')

    sdk_prefix = integration_prefix + 'sdk-patches/'
    sdk_manifest = json.loads(entries[sdk_prefix + 'manifest.json'])
    expected_patches = {'opencode-host-model-options.patch', 'opencode-quota-error.patch',
                        'opencode-native-interrupt.patch', 'pi-custom-model-options.patch'}
    patches = sdk_manifest.get('patches', [])
    if (sdk_manifest.get('schemaVersion') != 2 or sdk_manifest.get('sdkVersion') != '0.16.0'
            or len(patches) != len(expected_patches) or {p.get('patch') for p in patches} != expected_patches):
        raise ValueError('Unexpected SDK patch contract; review deployment packaging.')
    for patch in patches:
        if sha256(entries[sdk_prefix + patch['patch']]) != patch['patchSha256']:
            raise ValueError('SDK patch checksum mismatch.')
    for name in ('apply.py', 'manifest.json', *sorted(expected_patches)):
        canonical = root / 'deploy/omnigent/sdk-patches' / name
        if entries[sdk_prefix + name] != canonical.read_bytes():
            raise ValueError(f'Stale daemon SDK patch asset: {name}. Rebuild daemon.')

    plugin = json.loads(entries['bundled-omnigent/open-design.json'])
    if (plugin.get('name') != 'woow-omnigent'
            or plugin.get('od', {}).get('launch', {}).get('path') != '/settings/omnigent'):
        raise ValueError('Unexpected bundled Omnigent launcher manifest.')
    dockerfile = (root / 'deploy/omnigent/Dockerfile').read_bytes()
    if [line for line in dockerfile.decode().splitlines() if line.startswith('FROM ')][-1] != 'FROM ' + BASE_IMAGE:
        raise ValueError('Dockerfile must inherit the pinned complete PaaS image.')
    entries['Dockerfile'] = dockerfile
    modes['Dockerfile'] = 0o644
    overlay = json.loads((root / 'deploy/omnigent/base-overlay.json').read_text())
    if overlay['baseImage'] != BASE_IMAGE:
        raise ValueError('Overlay contract must match the pinned current base.')
    # Base-owned MCP and branding units must not hide a future source edit.
    protected_sources = ['apps/daemon/src/' + name.removesuffix('.js') + '.ts'
                         for name in overlay['baseDaemonOverrides']]
    changed = git('diff', '--name-only', 'b3270069b93548bbfced8cb17fa90039fd88422a', revision,
                  '--', *protected_sources)
    if changed:
        raise ValueError('Protected source changed; integrate the base overrides before packaging.')
    for name in ('base-overlay.json', 'verify-overlay.py'):
        entries[name] = (root / 'deploy/omnigent' / name).read_bytes()
        modes[name] = 0o644
    if git('diff', '--name-only', 'b3270069b93548bbfced8cb17fa90039fd88422a', revision,
           '--', 'apps/web', 'packages/contracts'):
        raise ValueError('Host-only release cannot include web or contracts source changes.')
    manifest = {
        'schemaVersion': 2,
        'sourceRevision': revision,
        'upstreamRevision': UPSTREAM_REVISION,
        'baseImage': BASE_IMAGE,
        'baseSourceRevision': 'b3270069b93548bbfced8cb17fa90039fd88422a',
        'runtimeDependencyClosure': 'Inherited current complete Paseo/MCP/Omnigent image; reviewed host SDK patches only',
        'webRuntime': 'Inherited unchanged from base image; no web assets or branding rebuild',
        'daemonOverlay': overlay,
        'sdkRuntime': {'omnigent': '0.16.0', 'dependencies': 'Inherited unchanged from base image'},
        'contractsRuntime': 'Inherited unchanged; source equality guarded against baseSourceRevision',
        'lockfileSha256': sha256((root / 'pnpm-lock.yaml').read_bytes()),
        'bundledPlugin': {'name': plugin['name'], 'version': plugin['version']},
        'sdkPatches': sdk_manifest,
        'hostKitAssets': sorted(host_assets),
        'fileHashStage': 'Exact host overlay inputs; web and existing branding inherited unchanged',
        'files': {name: sha256(data) for name, data in sorted(entries.items())},
        'fileModes': {name: format(mode, '04o') for name, mode in sorted(modes.items())},
    }
    manifest_bytes = (json.dumps(manifest, sort_keys=True, indent=2) + '\n').encode()
    entries['manifest.json'] = manifest_bytes
    modes['manifest.json'] = 0o644
    if git('rev-parse', 'HEAD') != revision or git('status', '--porcelain', '--untracked-files=normal'):
        raise ValueError('Source changed during packaging; retry after the final commit/build.')
    with output.open('xb') as stream:
        with gzip.GzipFile(filename='', fileobj=stream, mode='wb', mtime=0) as compressed:
            with tarfile.open(fileobj=compressed, mode='w', format=tarfile.PAX_FORMAT) as archive:
                for name, data in sorted(entries.items()):
                    entry = tarfile.TarInfo(name)
                    entry.size = len(data)
                    entry.uid = entry.gid = 1001
                    entry.mode = modes[name]
                    entry.mtime = 0
                    archive.addfile(entry, io.BytesIO(data))
    with output.open('rb') as stream:
        archive_sha256 = hashlib.file_digest(stream, 'sha256').hexdigest()
    print(json.dumps({'sourceRevision': revision, 'baseImage': BASE_IMAGE,
                      'manifestSha256': sha256(manifest_bytes),
                      'archiveSha256': archive_sha256, 'archive': str(output),
                      'fileCount': len(entries), 'archiveBytes': output.stat().st_size}))


if __name__ == '__main__':
    try:
        main()
    except (ValueError, OSError, subprocess.CalledProcessError) as error:
        raise SystemExit(str(error)) from None

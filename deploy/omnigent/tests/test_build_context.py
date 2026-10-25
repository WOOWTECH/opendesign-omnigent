"""Exercise the actual host-only packager in an isolated, synthetic checkout."""
import contextlib
import importlib.util
import io
import json
from pathlib import Path
import shutil
import tarfile
import tempfile
import unittest
from unittest.mock import patch

DEPLOY = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location('build_context', DEPLOY / 'build-context.py')
PACKAGER = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(PACKAGER)
REVISION = 'a' * 40


class DeliveryTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.directory = Path(temporary.name)
        self.root = self.directory / 'checkout'
        self.deploy = self.root / 'deploy/omnigent'
        self.sdk = self.root / 'apps/daemon/dist/integrations/omnigent/sdk-patches'
        self.sdk.mkdir(parents=True)
        self.deploy.mkdir(parents=True)
        self.source = self.deploy / 'sdk-patches'
        shutil.copytree(DEPLOY / 'sdk-patches', self.source, ignore=shutil.ignore_patterns('tests', '__pycache__'))
        for p in self.source.iterdir():
            if p.is_file(): shutil.copyfile(p, self.sdk / p.name)
        for name in ['Dockerfile', 'base-overlay.json', 'verify-overlay.py']:
            shutil.copyfile(DEPLOY / name, self.deploy / name)
        for relative in ['cli.js', 'integrations/omnigent/service.js', 'integrations/omnigent/dependencies.js', 'integrations/omnigent/dependencies.js.map', 'integrations/omnigent/build-assets.js', 'integrations/omnigent/build-assets.js.map', 'integrations/omnigent/adapter.py', 'integrations/omnigent/host-kit/supervisor.sh', 'integrations/omnigent/host-kit/og_helper.py']:
            p = self.root / 'apps/daemon/dist' / relative
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text('fixture\n')
        plugin = self.root / 'plugins/_official/woow/woow-omnigent/open-design.json'
        plugin.parent.mkdir(parents=True)
        plugin.write_text(json.dumps({'name': 'woow-omnigent', 'version': '0.1.6', 'od': {'launch': {'path': '/settings/omnigent'}}}))
        (self.root / 'pnpm-lock.yaml').write_text('fixture-lock\n')
        self.diff = ''

    def package(self, output):
        def git(args, **kwargs):
            if args[1] == 'rev-parse': return REVISION + '\n'
            if args[1] == 'status': return ''
            if args[1] == 'diff': return self.diff if 'apps/web' in args else ''
            self.fail('Unexpected git command')
        with patch.object(PACKAGER, '__file__', str(self.deploy / 'build-context.py')), patch.object(PACKAGER.subprocess, 'check_output', side_effect=git), patch('sys.argv', ['build-context.py', str(output), '--expected-revision', REVISION]), contextlib.redirect_stdout(io.StringIO()):
            PACKAGER.main()

    def test_host_only_context_requires_no_web_and_preserves_patch_bytes(self):
        output = self.directory / 'context.tar.gz'
        self.package(output)
        with tarfile.open(output) as archive:
            self.assertFalse(any(n.startswith('web-out/') for n in archive.getnames()))
            manifest = json.load(archive.extractfile('manifest.json'))
            self.assertEqual(manifest['baseImage'], PACKAGER.BASE_IMAGE)
            self.assertEqual(manifest['baseSourceRevision'], 'b3270069b93548bbfced8cb17fa90039fd88422a')
            self.assertIn('Inherited unchanged', manifest['webRuntime'])
            self.assertEqual({entry['patch'] for entry in manifest['sdkPatches']['patches']},
                             {'opencode-host-model-options.patch', 'opencode-quota-error.patch', 'opencode-native-interrupt.patch'})
            for entry in manifest['sdkPatches']['patches']:
                name = 'daemon-dist/integrations/omnigent/sdk-patches/' + entry['patch']
                self.assertEqual(archive.extractfile(name).read(), (self.source / entry['patch']).read_bytes())

    def test_missing_quota_patch_is_rejected(self):
        (self.sdk / 'opencode-quota-error.patch').unlink()
        with self.assertRaisesRegex(ValueError, 'Missing build asset'):
            self.package(self.directory / 'context.tar.gz')

    def test_missing_interrupt_patch_is_rejected(self):
        (self.sdk / 'opencode-native-interrupt.patch').unlink()
        with self.assertRaisesRegex(ValueError, 'Missing build asset'):
            self.package(self.directory / 'context.tar.gz')

    def test_omitted_interrupt_manifest_entry_is_rejected(self):
        path = self.sdk / 'manifest.json'
        manifest = json.loads(path.read_text())
        manifest['patches'] = [entry for entry in manifest['patches']
                               if entry['patch'] != 'opencode-native-interrupt.patch']
        path.write_text(json.dumps(manifest))
        with self.assertRaisesRegex(ValueError, 'Unexpected SDK patch contract'):
            self.package(self.directory / 'context.tar.gz')

    def test_stale_canonical_applier_is_rejected(self):
        (self.sdk / 'apply.py').write_text('stale applier')
        with self.assertRaisesRegex(ValueError, 'Stale daemon SDK patch asset'):
            self.package(self.directory / 'context.tar.gz')

    def test_web_or_contract_source_change_is_rejected(self):
        for name in ['apps/web/src/App.tsx', 'packages/contracts/src/index.ts']:
            with self.subTest(name=name):
                self.diff = name + '\n'
                with self.assertRaisesRegex(ValueError, 'Host-only release'):
                    self.package(self.directory / 'context.tar.gz')

    def test_docker_preserves_web_and_pins_complete_live_base(self):
        docker = (DEPLOY / 'Dockerfile').read_text()
        self.assertTrue(docker.startswith('FROM ' + PACKAGER.BASE_IMAGE + '\n'))
        self.assertIn('daf04fb889bd0545b3af14b2422305f86d2e5b77346498f6e0ca3a6c05e82a0b', PACKAGER.BASE_IMAGE)
        self.assertNotIn('web-out', docker)
        self.assertNotIn('/app/apps/web/out', docker)
        self.assertNotIn('/usr/local/share/woow/patches/', docker)
        self.assertIn('manifest.json --check', docker)
        self.assertIn('io.woow.opendesign.omnigent-integration="0.1.6"', docker)


if __name__ == '__main__':
    unittest.main()

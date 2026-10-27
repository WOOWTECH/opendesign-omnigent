"""Offline installer tests; fixtures never import or modify an installed SDK."""
import difflib
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import stat
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location('sdk_patch_apply', ROOT / 'apply.py')
INSTALLER = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(INSTALLER)


def digest(data):
    return hashlib.sha256(data).hexdigest()


class InstallerTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.package = self.root / 'omnigent'
        self.assets = self.root / 'assets'
        self.assets.mkdir()
        self.manifest_path = self.assets / 'manifest.json'
        self.manifest = {'schemaVersion': 2, 'sdkVersion': '0.16.0', 'patches': []}
        self.originals, self.results = {}, {}
        for name, relative in INSTALLER.PATCH_TARGETS.items():
            original = ''.join(f'line_{i} = {i}\n' for i in range(32)).encode()
            result = original
            for i in ([2, 10, 18, 26] if name == 'opencode-quota-error.patch' else [2]):
                result = result.replace(f'line_{i} = {i}\n'.encode(), f'line_{i} = {i + 100}\n'.encode())
            delta = ''.join(difflib.unified_diff(original.decode().splitlines(True), result.decode().splitlines(True), fromfile='a/' + relative, tofile='b/' + relative, n=1)).encode()
            (self.assets / name).write_bytes(delta)
            target = self.package / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(original)
            self.originals[relative], self.results[relative] = original, result
            self.manifest['patches'].append({'patch': name, 'patchSha256': digest(delta), 'files': [{'sdkRelativePath': relative, 'baseSha256': digest(original), 'replacementSha256': digest(result)}]})
        self.save_manifest()

    def save_manifest(self):
        self.manifest_path.write_text(json.dumps(self.manifest))

    def apply(self, **kwargs):
        INSTALLER.apply_manifest(self.package, self.manifest_path, '0.16.0', **kwargs)

    def assert_originals(self):
        for relative, original in self.originals.items():
            self.assertEqual((self.package / relative).read_bytes(), original)
        self.assertEqual(list(self.package.rglob('.sdk-patch-*')), [])

    def test_five_patches_four_quota_hunks_and_idempotent_reuse(self):
        quota = self.assets / 'opencode-quota-error.patch'
        self.assertEqual(len(self.manifest['patches']), 5)
        self.assertEqual(sum(line.startswith(b'@@ ') for line in quota.read_bytes().splitlines()), 4)
        self.apply()
        for relative, result in self.results.items():
            self.assertEqual((self.package / relative).read_bytes(), result)
        before = {rel: (self.package / rel).stat() for rel in self.results}
        self.apply()
        self.apply(check=True)
        for relative in self.results:
            self.assertEqual((self.package / relative).stat().st_ino, before[relative].st_ino)
            self.assertEqual((self.package / relative).stat().st_mtime_ns, before[relative].st_mtime_ns)

    def test_read_only_verification_never_patches_old_sdk(self):
        with self.assertRaisesRegex(ValueError, 'BASELINE_MISMATCH'):
            self.apply(check=True)
        self.assert_originals()

    def test_partial_previous_install_is_completed_without_rewriting_first_file(self):
        first = self.package / 'host/connect.py'
        first.write_bytes(self.results['host/connect.py'])
        before = first.stat()
        self.apply()
        self.assertEqual(first.stat().st_ino, before.st_ino)
        self.assertEqual((self.package / 'harnesses/opencode_native/forwarder.py').read_bytes(), self.results['harnesses/opencode_native/forwarder.py'])

    def test_existing_host_and_quota_patches_gain_interrupt_without_rewriting_either(self):
        previous = ['host/connect.py', 'harnesses/opencode_native/forwarder.py']
        for relative in previous:
            (self.package / relative).write_bytes(self.results[relative])
        before = {relative: (self.package / relative).stat() for relative in previous}
        with self.assertRaisesRegex(ValueError, 'BASELINE_MISMATCH'):
            self.apply(check=True)
        self.assertEqual((self.package / 'runner/native/interrupt.py').read_bytes(),
                         self.originals['runner/native/interrupt.py'])
        self.apply()
        self.apply(check=True)
        for relative, metadata in before.items():
            after = (self.package / relative).stat()
            self.assertEqual((after.st_ino, after.st_mtime_ns), (metadata.st_ino, metadata.st_mtime_ns))
        self.assertEqual((self.package / 'runner/native/interrupt.py').read_bytes(),
                         self.results['runner/native/interrupt.py'])

    def test_unknown_interrupt_baseline_rejects_before_any_write(self):
        target = self.package / 'runner/native/interrupt.py'
        target.write_text('unknown interrupt source\n')
        with self.assertRaisesRegex(ValueError, 'BASELINE_MISMATCH'):
            self.apply()
        for relative in ['host/connect.py', 'harnesses/opencode_native/forwarder.py']:
            self.assertEqual((self.package / relative).read_bytes(), self.originals[relative])
        self.assertEqual(target.read_text(), 'unknown interrupt source\n')

    def test_corrupt_interrupt_patch_rejects_before_any_write(self):
        (self.assets / 'opencode-native-interrupt.patch').write_text('corrupted')
        with self.assertRaisesRegex(ValueError, 'PATCH_HASH_MISMATCH'):
            self.apply()
        self.assert_originals()

    def test_unknown_second_baseline_rejects_entire_set_before_writes(self):
        target = self.package / 'harnesses/opencode_native/forwarder.py'
        target.write_text('unknown source\n')
        with self.assertRaisesRegex(ValueError, 'BASELINE_MISMATCH'):
            self.apply()
        self.assertEqual((self.package / 'host/connect.py').read_bytes(), self.originals['host/connect.py'])
        self.assertEqual(target.read_text(), 'unknown source\n')

    def test_corrupt_second_patch_hash_rejects_before_first_write(self):
        (self.assets / 'opencode-quota-error.patch').write_text('corrupted')
        with self.assertRaisesRegex(ValueError, 'PATCH_HASH_MISMATCH'):
            self.apply()
        self.assert_originals()

    def test_result_hash_context_and_hunk_counts_fail_before_writes(self):
        entry = self.manifest['patches'][1]
        path = self.assets / entry['patch']
        original = path.read_bytes()
        for delta, code in [(original.replace(b' line_1 = 1\n', b' line_1 = 999\n'), 'CONTEXT_MISMATCH'),
                            (original.replace(b'+2,3', b'+2,4'), 'INVALID_PATCH')]:
            with self.subTest(code=code):
                path.write_bytes(delta)
                entry['patchSha256'] = digest(delta)
                self.save_manifest()
                with self.assertRaisesRegex(ValueError, code):
                    self.apply()
                self.assert_originals()
        path.write_bytes(original)
        entry['patchSha256'] = digest(original)
        entry['files'][0]['replacementSha256'] = digest(b'not approved')
        self.save_manifest()
        with self.assertRaisesRegex(ValueError, 'RESULT_MISMATCH'):
            self.apply()
        self.assert_originals()

    def test_exact_version_is_required(self):
        with self.assertRaisesRegex(ValueError, 'SDK_VERSION_MISMATCH'):
            INSTALLER.apply_manifest(self.package, self.manifest_path, '0.17.0')
        self.assert_originals()

    def test_manifest_cannot_omit_or_redirect_any_patch(self):
        original = json.dumps(self.manifest)
        count = len(self.manifest['patches'])
        for index in range(count):
            for mode in ['omitted', 'duplicate', 'traversal', 'overlapping-target', 'wrong-version']:
                with self.subTest(index=index, mode=mode):
                    self.manifest = json.loads(original)
                    if mode == 'omitted': self.manifest['patches'].pop(index)
                    if mode == 'duplicate': self.manifest['patches'][index] = self.manifest['patches'][(index + 1) % count]
                    if mode == 'traversal': self.manifest['patches'][index]['files'][0]['sdkRelativePath'] = '../outside.py'
                    if mode == 'overlapping-target':
                        self.manifest['patches'][index]['files'][0]['sdkRelativePath'] = self.manifest['patches'][(index + 1) % count]['files'][0]['sdkRelativePath']
                    if mode == 'wrong-version': self.manifest['sdkVersion'] = '0.17.0'
                    self.save_manifest()
                    with self.assertRaisesRegex(ValueError, 'INVALID_MANIFEST'): self.apply()
                    self.assert_originals()

    def test_symlink_file_and_parent_are_rejected(self):
        target = self.package / 'harnesses/pi_native/credentials.py'
        external = self.root / 'outside'
        external.mkdir()
        outside = external / 'credentials.py'
        outside.write_bytes(target.read_bytes())
        target.unlink()
        target.symlink_to(outside)
        with self.assertRaisesRegex(ValueError, 'INVALID_TARGET'): self.apply()
        target.unlink()
        target.parent.rmdir()
        target.parent.symlink_to(external)
        with self.assertRaisesRegex(ValueError, 'INVALID_TARGET'): self.apply()
        self.assertEqual(outside.read_bytes(), self.originals['harnesses/pi_native/credentials.py'])
        self.assertEqual((self.package / 'host/connect.py').read_bytes(), self.originals['host/connect.py'])

    def test_permissions_and_concurrent_change_guard(self):
        target = self.package / 'host/connect.py'
        target.chmod(0o640)
        before = target.stat()
        self.apply()
        after = target.stat()
        self.assertEqual((stat.S_IMODE(after.st_mode), after.st_uid, after.st_gid), (stat.S_IMODE(before.st_mode), before.st_uid, before.st_gid))
        with self.assertRaisesRegex(ValueError, 'TARGET_CHANGED'):
            INSTALLER.replace_checked(target, before, self.originals['host/connect.py'], self.results['host/connect.py'])
        self.assertEqual(list(target.parent.glob('.sdk-patch-*')), [])

    def test_cli_error_never_discloses_paths_or_source(self):
        result = subprocess.run([sys.executable, '-I', '-B', str(ROOT / 'apply.py'), str(self.package), str(self.manifest_path)], capture_output=True)
        self.assertEqual(result.returncode, 1)
        self.assertEqual(result.stdout, b'')
        self.assertEqual(result.stderr, b'SDK_PATCH_REJECTED\n')
        self.assert_originals()

    def test_public_assets_match_manifest_and_exact_declared_targets(self):
        manifest = INSTALLER.load_manifest(ROOT / 'manifest.json')
        for entry in manifest['patches']:
            data = (ROOT / entry['patch']).read_bytes()
            self.assertEqual(digest(data), entry['patchSha256'])
            self.assertEqual(set(INSTALLER.patch_sections(data)), {f['sdkRelativePath'] for f in entry['files']})

    def test_public_patches_against_provided_real_sdk(self):
        source = os.environ.get('OMNIGENT_PATCH_PACKAGE_ROOT')
        if not source:
            self.skipTest('Set OMNIGENT_PATCH_PACKAGE_ROOT to a read-only original SDK tree')
        for relative in self.originals:
            (self.package / relative).write_bytes((Path(source) / relative).read_bytes())
        INSTALLER.apply_manifest(self.package, ROOT / 'manifest.json', '0.16.0')
        INSTALLER.apply_manifest(self.package, ROOT / 'manifest.json', '0.16.0', check=True)
        check = subprocess.run([sys.executable, '-B', str(Path(__file__).with_name('check_provider_merge.py')),
                                str(self.package / 'harnesses/opencode_native/provider.py')],
                               capture_output=True, text=True)
        self.assertEqual(check.returncode, 0, check.stderr)
        self.assertEqual(check.stdout.strip(), 'DUAL_CONFIG_PROVIDER_OK')


if __name__ == '__main__':
    unittest.main()

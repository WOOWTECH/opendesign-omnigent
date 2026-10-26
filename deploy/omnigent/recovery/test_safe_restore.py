"""Portable public API tests; fixtures need no external archives or private paths.

Every fixture and temporary destination stays beside this file. Canonical happy
fixtures have no '.'/ './' root-directory member; directory entries are optional.
"""
import hashlib
import gzip
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tarfile
import tempfile
import time
import unittest
from concurrent.futures import ThreadPoolExecutor

from safe_restore import RestoreError, restore_archive

HERE = Path(__file__).resolve().parent


class RestoreTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='.restore-test-', dir=HERE)
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.active = self.root / 'active'
        self.active.mkdir()
        (self.active / 'sentinel').write_bytes(b'active remains unchanged\n')
        self.outside = self.root / 'outside'
        self.outside.mkdir()
        (self.outside / 'sentinel').write_bytes(b'outside remains unchanged\n')

    def archive(self, entries):
        archive = self.root / 'fixture.tar.gz'
        with tarfile.open(archive, 'w:gz') as package:
            for name, data in entries:
                member = name if isinstance(name, tarfile.TarInfo) else tarfile.TarInfo(name)
                member.size = len(data)
                member.mode = 0o6777  # Modes/ownership must never be restored.
                member.uid = 98765
                member.gid = 98765
                package.addfile(member, io.BytesIO(data))
        return archive, hashlib.sha256(archive.read_bytes()).hexdigest()

    def check_sentinels(self):
        self.assertEqual((self.active / 'sentinel').read_bytes(), b'active remains unchanged\n')
        self.assertEqual((self.outside / 'sentinel').read_bytes(), b'outside remains unchanged\n')
        self.assertFalse(list(self.root.glob('.safe-restore-*')))

    def refuse(self, archive, digest, code=None, destination=None):
        destination = destination or self.root / 'refused'
        existed = os.path.lexists(destination)
        with self.assertRaisesRegex(RestoreError, code or '.'):
            restore_archive(archive, digest, destination)
        if not existed:
            self.assertFalse(os.path.lexists(destination))
        self.check_sentinels()

    def raw_gzip(self, raw):
        archive = self.root / 'fixture.tar.gz'
        archive.write_bytes(gzip.compress(raw))
        return archive, hashlib.sha256(archive.read_bytes()).hexdigest()

    def test_valid_tree_has_verified_inventory_and_preserves_active(self):
        archive, digest = self.archive([
            ('index.html', b'<html>restored</html>'), ('assets/app.js', b'let x = 1;')])
        with tarfile.open(archive, 'r:gz') as package:
            self.assertEqual(package.getnames(), ['index.html', 'assets/app.js'])
            self.assertEqual(sum(member.isdir() for member in package.getmembers()), 0)
        result = restore_archive(archive, digest, self.root / 'restored')
        self.assertEqual(result['file_count'], 2)
        self.assertEqual(result['expanded_bytes'], 31)
        self.assertEqual(result['inventory']['index.html']['sha256'],
                         hashlib.sha256(b'<html>restored</html>').hexdigest())
        self.assertEqual((self.root / 'restored/assets/app.js').read_bytes(), b'let x = 1;')
        self.assertEqual((self.root / 'restored/index.html').stat().st_mode & 0o7777, 0o644)
        self.assertEqual((self.active / 'sentinel').read_bytes(), b'active remains unchanged\n')
        self.assertFalse(list(self.root.glob('.safe-restore-*')))

    def test_wrong_digest_refuses_before_publication(self):
        archive, _ = self.archive([('index.html', b'UI')])
        self.refuse(archive, '0' * 64, 'ARCHIVE_SHA256_MISMATCH')

    def test_invalid_digest_format(self):
        archive, _ = self.archive([('index.html', b'UI')])
        self.refuse(archive, 'not-a-digest', 'INVALID_EXPECTED_SHA256')

    def test_truncated_gzip_with_matching_digest(self):
        archive, _ = self.archive([('index.html', b'UI')])
        archive.write_bytes(archive.read_bytes()[:-8])
        self.refuse(archive, hashlib.sha256(archive.read_bytes()).hexdigest(), 'TRUNCATED_GZIP')

    def test_invalid_deflate_has_explicit_restore_error(self):
        archive = self.root / 'bad-deflate.gz'
        archive.write_bytes(b'\x1f\x8b\x08\x00' + b'\0' * 6 + b'\xff' * 20)
        self.refuse(archive, hashlib.sha256(archive.read_bytes()).hexdigest())

    def test_truncated_tar_with_valid_gzip_and_digest(self):
        member = tarfile.TarInfo('index.html')
        member.size = 100
        archive, digest = self.raw_gzip(member.tobuf() + b'only three words')
        self.refuse(archive, digest)

    def test_paths_traversal_aliases_controls_and_drive_names(self):
        for name in ['../outside/sentinel', '/absolute', 'a/../../outside/sentinel',
                     './alias', 'a//b', 'a/./b', 'C:/escape', 'a\\b', '', '.',
                     'a\nb', 'a\x7fb', 'a/../b']:
            with self.subTest(name=repr(name)):
                archive, digest = self.archive([('index.html', b'UI'), (name, b'bad')])
                self.refuse(archive, digest)

    def test_pax_path_override_cannot_escape(self):
        member = tarfile.TarInfo('apparently-safe')
        member.pax_headers = {'path': '../outside/sentinel'}
        self.refuse(*self.archive([('index.html', b'UI'), (member, b'bad')]))

    def test_path_depth_and_implicit_directory_bounds(self):
        deep = '/'.join(['a'] * 65)
        self.refuse(*self.archive([('index.html', b'UI'), (deep, b'')]))
        entries = [('index.html', b'UI')] + [(f'a{i}/b{i}/c{i}/file', b'') for i in range(700)]
        self.refuse(*self.archive(entries), code='DIRECTORY_COUNT_LIMIT')

    def test_links_and_special_members_are_refused(self):
        for kind in [tarfile.SYMTYPE, tarfile.LNKTYPE, tarfile.CHRTYPE, tarfile.BLKTYPE,
                     tarfile.FIFOTYPE, tarfile.GNUTYPE_SPARSE, tarfile.CONTTYPE]:
            with self.subTest(type=kind):
                member = tarfile.TarInfo('escape')
                member.type = kind
                member.linkname = str(self.outside / 'sentinel')
                self.refuse(*self.archive([('index.html', b'UI'), (member, b'')]))

    def test_duplicate_members_are_refused(self):
        self.refuse(*self.archive([('index.html', b'UI'), ('index.html', b'bad')]),
                    code='DUPLICATE_MEMBER')

    def test_duplicate_directories_are_refused(self):
        directory = tarfile.TarInfo('assets')
        directory.type = tarfile.DIRTYPE
        self.refuse(*self.archive([('index.html', b'UI'), (directory, b''), (directory, b'')]),
                    code='DUPLICATE_MEMBER')

    def test_file_ancestor_conflicts_in_either_order(self):
        for entries in [[('a', b'file'), ('a/b', b'child')],
                        [('a/b', b'child'), ('a', b'file')]]:
            with self.subTest(entries=entries):
                self.refuse(*self.archive([('index.html', b'UI')] + entries),
                            code='FILE_DIRECTORY_CONFLICT')

    def test_directory_index_and_missing_index(self):
        member = tarfile.TarInfo('index.html')
        member.type = tarfile.DIRTYPE
        for entries in [[(member, b'')], [('assets/app.js', b'JS')]]:
            with self.subTest(entries=entries):
                self.refuse(*self.archive(entries), code='REGULAR_INDEX_REQUIRED')

    def test_nonempty_directory_payload(self):
        member = tarfile.TarInfo('assets')
        member.type = tarfile.DIRTYPE
        self.refuse(*self.archive([('index.html', b'UI'), (member, b'bad')]),
                    code='INVALID_MEMBER_SIZE')

    def test_trailing_nonzero_tar_data(self):
        archive, _ = self.archive([('index.html', b'UI')])
        self.refuse(*self.raw_gzip(gzip.decompress(archive.read_bytes()) + b'concealed-data'),
                    code='INVALID_TAR_END_OR_TRAILING_DATA')

    def test_compressed_limit(self):
        archive = self.root / 'oversized.tar.gz'
        with archive.open('wb') as output:
            output.seek(32 * 1024 * 1024)
            output.write(b'x')
        self.refuse(archive, '0' * 64, 'COMPRESSED_LIMIT')

    def test_declared_file_limit_before_body_allocation(self):
        member = tarfile.TarInfo('index.html')
        member.size = 16 * 1024 * 1024 + 1
        self.refuse(*self.raw_gzip(member.tobuf() + b'\0' * 1024), code='FILE_SIZE_LIMIT')

    def test_exact_file_limit_is_accepted(self):
        archive, digest = self.archive([('index.html', b'x' * (16 * 1024 * 1024))])
        receipt = restore_archive(archive, digest, self.root / 'restored')
        self.assertEqual(receipt['expanded_bytes'], 16 * 1024 * 1024)
        self.check_sentinels()

    def test_expanded_limit_with_individually_valid_files(self):
        block = b'x' * (16 * 1024 * 1024)
        entries = [('index.html', b'UI')] + [(f'asset-{i}', block) for i in range(6)]
        self.refuse(*self.archive(entries), code='EXPANDED_SIZE_LIMIT')

    def test_member_count_limit(self):
        entries = [('index.html', b'UI')] + [(f'asset-{i}', b'') for i in range(2000)]
        self.refuse(*self.archive(entries), code='MEMBER_COUNT_LIMIT')

    def test_zero_padding_decompression_bomb_is_bounded(self):
        archive = self.root / 'padding-bomb.gz'
        with gzip.open(archive, 'wb') as output:
            for _ in range(106):
                output.write(b'\0' * (1024 * 1024))
        self.refuse(archive, hashlib.sha256(archive.read_bytes()).hexdigest(), 'TAR_STREAM_LIMIT')

    def test_existing_empty_and_populated_destinations_refused(self):
        archive, digest = self.archive([('index.html', b'UI')])
        empty = self.root / 'empty'
        empty.mkdir()
        self.refuse(archive, digest, 'DESTINATION_EXISTS', empty)
        self.refuse(archive, digest, 'DESTINATION_EXISTS', self.active)
        self.assertEqual(list(empty.iterdir()), [])

    def test_destination_symlinks_including_dangling_refused(self):
        archive, digest = self.archive([('index.html', b'UI')])
        for i, target in enumerate([self.active, self.root / 'absent']):
            link = self.root / f'link-{i}'
            link.symlink_to(target, target_is_directory=True)
            self.refuse(archive, digest, 'DESTINATION_EXISTS', link)
            self.assertTrue(link.is_symlink())

    def test_symlink_ancestor_refused(self):
        archive, digest = self.archive([('index.html', b'UI')])
        link = self.root / 'linked-parent'
        link.symlink_to(self.outside, target_is_directory=True)
        self.refuse(archive, digest, destination=link / 'restored')
        self.assertEqual([p.name for p in self.outside.iterdir()], ['sentinel'])

    def test_missing_parent_refused(self):
        self.refuse(*self.archive([('index.html', b'UI')]),
                    destination=self.root / 'missing-parent' / 'restored')
        self.assertFalse((self.root / 'missing-parent').exists())

    def test_source_symlink_refused(self):
        archive, digest = self.archive([('index.html', b'UI')])
        link = self.root / 'source-link'
        link.symlink_to(archive)
        self.refuse(link, digest)

    def test_concurrent_publication_has_exactly_one_winner(self):
        archive, digest = self.archive([('index.html', b'UI'), ('app', b'x' * 2_000_000)])
        destination = self.root / 'winner'
        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = [pool.submit(restore_archive, archive, digest, destination) for _ in range(2)]
        successes, failures = [], []
        for future in futures:
            try:
                successes.append(future.result())
            except RestoreError as exc:
                failures.append(str(exc))
        self.assertEqual(len(successes), 1)
        self.assertEqual(failures, ['DESTINATION_EXISTS'])
        self.assertEqual((destination / 'index.html').read_bytes(), b'UI')
        self.check_sentinels()

    def test_empty_destination_created_during_staging_is_never_replaced(self):
        archive, digest = self.archive([('index.html', b'UI')] +
                                      [(f'asset-{i}', b'x' * 512_000) for i in range(24)])
        destination = self.root / 'contested'
        with ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(restore_archive, archive, digest, destination)
            deadline = time.monotonic() + 10
            while not list(self.root.glob('.safe-restore-*')):
                if future.done() or time.monotonic() > deadline:
                    self.fail('Could not inject destination race during staging')
                time.sleep(.001)
            destination.mkdir()
            original_inode = destination.stat().st_ino
            with self.assertRaisesRegex(RestoreError, 'DESTINATION_EXISTS'):
                future.result()
        self.assertEqual(destination.stat().st_ino, original_inode)
        self.assertEqual(list(destination.iterdir()), [])
        self.check_sentinels()

    def test_explicit_directories_and_pax_mtime_are_supported(self):
        directory = tarfile.TarInfo('assets')
        directory.type = tarfile.DIRTYPE
        index = tarfile.TarInfo('index.html')
        index.pax_headers = {'mtime': '1700000000.125'}
        archive, digest = self.archive([(directory, b''), (index, b'UI'), ('assets/a.js', b'JS')])
        result = restore_archive(archive, digest, self.root / 'restored')
        self.assertEqual(result['file_count'], 2)
        self.assertEqual((self.root / 'restored/assets').stat().st_mode & 0o7777, 0o755)
        self.check_sentinels()

    def test_cli_contract_and_error_exit(self):
        archive, digest = self.archive([('index.html', b'UI')])
        cli = [sys.executable, '-B', str(HERE / 'safe_restore.py'), str(archive)]
        failed = subprocess.run(cli + ['0' * 64, str(self.root / 'failed')], capture_output=True)
        self.assertEqual(failed.returncode, 1)
        self.assertEqual(failed.stdout, b'')
        self.assertIn(b'ARCHIVE_SHA256_MISMATCH', failed.stderr)
        success = subprocess.run(cli + [digest, str(self.root / 'restored')], capture_output=True)
        self.assertEqual(success.returncode, 0, success.stderr)
        receipt = json.loads(success.stdout)
        self.assertEqual(receipt['file_count'], 1)
        self.assertNotIn('inventory', receipt)
        expanded = subprocess.run(cli + [digest, str(self.root / 'restored-2'), '--inventory'],
                                  capture_output=True)
        self.assertEqual(expanded.returncode, 0, expanded.stderr)
        self.assertEqual(json.loads(expanded.stdout)['inventory']['index.html']['bytes'], 2)
        self.check_sentinels()

if __name__ == '__main__':
    unittest.main()

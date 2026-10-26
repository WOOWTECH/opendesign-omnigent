"""Synthetic fixtures only. All generated files stay under this directory."""
import dataclasses
import hashlib
import io
import json
import os
from pathlib import Path
import socket
import sqlite3
import stat
import subprocess
import sys
import tarfile
import tempfile
import threading
import time
import unittest
import uuid
from unittest import mock

sys.dont_write_bytecode = True
import od_volume as v

HERE = Path(__file__).resolve().parent


class VolumeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.key_tmp = tempfile.TemporaryDirectory(prefix=".test-keys-", dir=HERE)
        cls.keybase = Path(cls.key_tmp.name)
        (cls.keybase / "backup").mkdir()
        v.crypto.generate_keypair(cls.keybase / "keys", cls.keybase / "backup")
        cls.public = v.crypto.read_public_key(cls.keybase / "keys/public.pem")
        cls.private = (cls.keybase / "keys/private.pem").read_bytes()
        cls.context = v.crypto.Context("synthetic-od", "od-volumes", "unit-fixture")

    @classmethod
    def tearDownClass(cls):
        cls.key_tmp.cleanup()

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix=".test-volume-", dir=HERE)
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.od, self.home, self.scratch, self.out = [self.base / n for n in
                                                    ("od", "home", "scratch", "out")]
        for path in (self.od, self.home, self.scratch, self.out):
            path.mkdir(mode=0o700)
        self.target = self.out / "capture.enc"
        self.policy = v.Policy(max_bytes=32 * 1024**2, max_entries=1000, sqlite_timeout=3)

    def capture(self, policy=None):
        return v.backup(self.od, self.home, self.target, self.scratch, self.public,
                        self.context, policy or self.policy)

    def destinations(self):
        roots = (self.base / "restore-od", self.base / "restore-home")
        for path in roots:
            path.mkdir()
        return roots

    def restore(self, roots=None, source=None, policy=None):
        roots = roots or self.destinations()
        receipt = v.restore(source or self.target, *roots, self.scratch, self.private,
                            self.context, policy or self.policy)
        return roots, receipt

    def plaintext(self):
        data, _ = v.crypto.decrypt_to_memory(io.BytesIO(self.target.read_bytes()), self.private,
            self.context, v.crypto.Limits(max_plaintext_bytes=16 * 1024**2))
        return data

    def manifest(self):
        with tarfile.open(fileobj=io.BytesIO(self.plaintext()), mode="r:") as archive:
            return json.load(archive.extractfile(v.MANIFEST))

    def encrypted_tar(self, members):
        data = io.BytesIO()
        with tarfile.open(fileobj=data, mode="w", format=tarfile.PAX_FORMAT) as archive:
            for info, payload in members:
                archive.addfile(info, io.BytesIO(payload) if payload is not None else None)
        target = self.out / "malicious.enc"
        with target.open("wb") as output:
            v.crypto.encrypt_stream(io.BytesIO(data.getvalue()), output, self.public,
                                    self.context, self.policy.envelope_limits())
        return target

    def assert_no_partial_capture(self):
        self.assertFalse(self.target.exists())
        self.assertEqual(list(self.out.iterdir()), [])
        self.assertEqual(list(self.scratch.iterdir()), [])

    def test_roundtrip_modes_links_specials_and_binary(self):
        (self.od / "private").mkdir(mode=0o750)
        (self.od / "private/config").write_bytes(b"synthetic-private-config\x00" * 80)
        (self.od / "private/config").chmod(0o640)
        (self.home / "empty").write_bytes(b"")
        (self.home / "home-absolute").symlink_to("/home/opendesign/.config/example")
        (self.home / "relative").symlink_to("../od/private/config")
        (self.home / "broken").symlink_to("missing")
        (self.home / "cycle").symlink_to("cycle")
        os.mkfifo(self.od / "runtime.fifo")
        unix = socket.socket(socket.AF_UNIX)
        self.addCleanup(unix.close)
        # Unix-domain path length stays below 108 bytes even for long workspaces.
        parent = os.open(self.od, v.DIR_FLAGS)
        self.addCleanup(os.close, parent)
        unix.bind(f"/proc/self/fd/{parent}/runtime.sock")
        first = self.capture()
        roots, restored = self.restore()
        self.assertEqual(first, restored)
        self.assertEqual(first["counts"]["skippedSockets"], 1)
        self.assertEqual(first["counts"]["skippedFifos"], 1)
        self.assertEqual(first["counts"]["symlinks"], 4)
        self.assertEqual(stat.S_IMODE(self.target.stat().st_mode), 0o600)
        self.assertFalse((roots[0] / "runtime.fifo").exists())
        self.assertFalse((roots[0] / "runtime.sock").exists())
        for relative in ("private", "private/config"):
            before, after = (self.od / relative).stat(), (roots[0] / relative).stat()
            self.assertEqual((before.st_uid, before.st_gid, before.st_mode, before.st_mtime_ns),
                             (after.st_uid, after.st_gid, after.st_mode, after.st_mtime_ns))
        for name in ("home-absolute", "relative", "broken", "cycle"):
            self.assertEqual(os.readlink(self.home / name), os.readlink(roots[1] / name))
        self.assertEqual((roots[0] / "private/config").read_bytes(),
                         (self.od / "private/config").read_bytes())
        self.assertEqual(list(self.scratch.iterdir()), [])

    def test_sqlite_live_wal_header_detection_and_readonly_source(self):
        db = self.od / "extensionless-store"
        connection = sqlite3.connect(db)
        self.addCleanup(connection.close)
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA wal_autocheckpoint=0")
        connection.execute("CREATE TABLE data(value TEXT)")
        connection.executemany("INSERT INTO data VALUES (?)", [(f"row-{i}",) for i in range(500)])
        connection.commit()
        self.assertGreater(Path(str(db) + "-wal").stat().st_size, 0)
        # ro permissions validate that native snapshot does not need source writes.
        db.chmod(0o440)
        for suffix in ("-wal", "-shm"):
            Path(str(db) + suffix).chmod(0o440)
        before = {p.name: p.read_bytes() for p in self.od.iterdir()}
        receipt = self.capture()
        self.assertEqual(receipt["counts"]["sqlite"], 1)
        self.assertEqual(receipt["counts"]["skippedSqliteSidecars"], 2)
        self.assertEqual(before, {p.name: p.read_bytes() for p in self.od.iterdir()})
        roots, _ = self.restore()
        restored_db = roots[0] / db.name
        checked = sqlite3.connect(f"file:{restored_db}?mode=ro&immutable=1", uri=True)
        self.addCleanup(checked.close)
        self.assertEqual(checked.execute("SELECT count(*) FROM data").fetchone()[0], 500)
        self.assertEqual(checked.execute("PRAGMA quick_check").fetchone(), ("ok",))
        self.assertEqual(checked.execute("PRAGMA journal_mode").fetchone(), ("delete",))
        self.assertEqual(sorted(p.name for p in roots[0].iterdir()), [db.name])

    def test_sqlite_writer_uncommitted_data_excluded(self):
        db = self.od / "live.cache"
        writer = sqlite3.connect(db)
        self.addCleanup(writer.close)
        writer.execute("PRAGMA journal_mode=WAL")
        writer.execute("CREATE TABLE data(value INTEGER)")
        writer.execute("INSERT INTO data VALUES (1)")
        writer.commit()
        writer.execute("INSERT INTO data VALUES (2)")
        self.capture()
        roots, _ = self.restore()
        with sqlite3.connect(roots[0] / db.name) as restored:
            self.assertEqual(restored.execute("SELECT value FROM data").fetchall(), [(1,)])

    def test_non_sqlite_extension_and_orphan_wal_preserved(self):
        (self.od / "not-a-db.sqlite").write_bytes(b"ordinary config content")
        (self.od / "orphan-wal").write_bytes(b"keep me")
        receipt = self.capture()
        self.assertEqual(receipt["counts"]["sqlite"], 0)
        roots, _ = self.restore()
        self.assertEqual((roots[0] / "orphan-wal").read_bytes(), b"keep me")

    def test_sqlite_busy_timeout_is_bounded_and_cleans_scratch(self):
        db = self.od / "locked"
        # POSIX file locks are per-process: test writer must be another process.
        program = """import sqlite3,sys
c=sqlite3.connect(sys.argv[1]); c.execute('CREATE TABLE data(value TEXT)'); c.commit()
c.execute('BEGIN EXCLUSIVE'); print('ready',flush=True); sys.stdin.read(1); c.close()
"""
        process = subprocess.Popen([sys.executable, "-B", "-c", program, str(db)],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
        try:
            self.assertEqual(process.stdout.readline(), b"ready\n")
            start = time.monotonic()
            with self.assertRaises(v.VolumeError):
                self.capture(dataclasses.replace(self.policy, sqlite_timeout=0.2))
            self.assertLess(time.monotonic() - start, 2.5)
        finally:
            process.communicate(b"x", timeout=3)
        self.assertEqual(process.returncode, 0)
        self.assert_no_partial_capture()

    def test_separate_live_wal_writer_snapshot_is_transaction_consistent(self):
        db = self.od / "active-store"
        program = """import sqlite3,sys,time
c=sqlite3.connect(sys.argv[1]); c.execute('PRAGMA journal_mode=WAL')
c.execute('PRAGMA wal_autocheckpoint=0')
c.execute('CREATE TABLE data(value BLOB)'); c.execute('CREATE TABLE state(n INTEGER)')
c.executemany('INSERT INTO data VALUES (?)', [(b'x'*512,) for _ in range(4000)])
c.execute('INSERT INTO state VALUES (4000)'); c.commit(); print('ready',flush=True)
for i in range(80):
 c.execute('INSERT INTO data VALUES (?)',(b'y'*512,)); c.execute('UPDATE state SET n=n+1')
 c.commit(); time.sleep(.003)
sys.stdin.read(1); c.close()
"""
        process = subprocess.Popen([sys.executable, "-B", "-c", program, str(db)],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
        try:
            self.assertEqual(process.stdout.readline(), b"ready\n")
            self.capture()
        finally:
            process.communicate(b"x", timeout=3)
        self.assertEqual(process.returncode, 0)
        roots, _ = self.restore()
        with sqlite3.connect(roots[0] / db.name) as restored:
            observed = restored.execute('SELECT count(*) FROM data').fetchone()[0]
            self.assertEqual(observed, restored.execute('SELECT n FROM state').fetchone()[0])
            self.assertGreaterEqual(observed, 4000)
            self.assertLessEqual(observed, 4080)

    def test_corrupt_sqlite_header_is_not_backed_up_as_regular_file(self):
        (self.od / "bad").write_bytes(v.SQLITE_HEADER + b"broken" * 300)
        with self.assertRaises(v.VolumeError):
            self.capture()
        self.assert_no_partial_capture()

    def test_append_prefix_snapshot_not_full_consistency(self):
        log = self.home / "events.jsonl"
        prefix = b'{"item":"original"}\n' * 100
        log.write_bytes(prefix)
        original = v.Capture.add_bytes

        def append(capture, fd, size, entry):
            if entry["path"] == "home/events.jsonl":
                with log.open("ab") as output:
                    output.write(b'{"item":"new"}\n')
            return original(capture, fd, size, entry)

        policy = dataclasses.replace(self.policy, append_globs=("home/events.jsonl",))
        with mock.patch.object(v.Capture, "add_bytes", append):
            receipt = self.capture(policy)
        self.assertEqual(receipt["counts"]["appendPrefixes"], 1)
        self.assertEqual(self.manifest()["consistency"], "per-file-and-per-sqlite-not-global")
        roots, _ = self.restore()
        self.assertEqual((roots[1] / log.name).read_bytes(), prefix)
        self.assertGreater(log.stat().st_size, len(prefix))

    def test_append_prefix_rewrite_rejected(self):
        log = self.home / "events.jsonl"
        log.write_bytes(b"old-prefix-contents\n" * 100)
        original = v.Capture.add_bytes

        def mutate(capture, fd, size, entry):
            result = original(capture, fd, size, entry)
            with log.open("r+b") as output:
                output.write(b"NEW")
            return result

        with mock.patch.object(v.Capture, "add_bytes", mutate), self.assertRaises(v.VolumeError):
            self.capture(dataclasses.replace(self.policy, append_globs=("home/events.jsonl",)))
        self.assert_no_partial_capture()

    def test_default_jsonl_is_stable_not_implicitly_append(self):
        config = self.home / "credentials.jsonl"
        config.write_bytes(b"secret-configuration\n" * 100)
        original = v.Capture.add_bytes

        def mutate(capture, fd, size, entry):
            result = original(capture, fd, size, entry)
            with config.open("ab") as output:
                output.write(b"new-value")
            return result

        with mock.patch.object(v.Capture, "add_bytes", mutate), self.assertRaises(v.VolumeError):
            self.capture()
        self.assert_no_partial_capture()

    def test_same_size_config_rewrite_and_replace_rejected(self):
        config = self.od / "config"
        config.write_bytes(b"original contents" * 100)
        original = v.Capture.add_bytes

        def mutate(capture, fd, size, entry):
            result = original(capture, fd, size, entry)
            old = config.stat()
            config.write_bytes(b"different content" * 100)
            os.utime(config, ns=(old.st_atime_ns, old.st_mtime_ns))
            return result

        with mock.patch.object(v.Capture, "add_bytes", mutate), self.assertRaises(v.VolumeError):
            self.capture()
        self.assert_no_partial_capture()

    def test_permission_error_fails_instead_of_skipping(self):
        private = self.od / "private"
        private.write_bytes(b"private content")
        original = v.os.open

        def deny(path, *args, **kwargs):
            if path == "private":
                raise PermissionError("sensitive path must not be logged")
            return original(path, *args, **kwargs)

        with mock.patch.object(v.os, "open", deny), self.assertRaises(PermissionError):
            self.capture()
        self.assert_no_partial_capture()

    def test_auth_failure_and_truncation_leave_destinations_untouched(self):
        (self.od / "config").write_bytes(b"synthetic-sensitive-payload")
        self.capture()
        original = self.target.read_bytes()
        roots = self.destinations()
        for data in (original[:-1], original[:-100], original + b"x",
                     original[:-20] + bytes([original[-20] ^ 1]) + original[-19:]):
            with self.subTest(length=len(data)):
                self.target.write_bytes(data)
                with self.assertRaises(v.crypto.EnvelopeError):
                    self.restore(roots)
                self.assertEqual([list(p.iterdir()) for p in roots], [[], []])
                self.assertEqual(list(self.scratch.iterdir()), [])

    def test_existing_targets_and_symlink_parents_refused(self):
        self.capture()
        roots = self.destinations()
        (roots[1] / "sentinel").write_bytes(b"do not overwrite")
        with self.assertRaises(v.VolumeError):
            self.restore(roots)
        self.assertEqual((roots[1] / "sentinel").read_bytes(), b"do not overwrite")
        self.assertEqual(list(roots[0].iterdir()), [])
        link = self.base / "symlink-target"
        link.symlink_to(roots[0], target_is_directory=True)
        with self.assertRaises(OSError):
            self.restore((link, roots[1]))

    def test_existing_backup_never_overwritten(self):
        self.target.write_bytes(b"keep this")
        with self.assertRaises(v.VolumeError):
            self.capture()
        self.assertEqual(self.target.read_bytes(), b"keep this")

    def test_byte_entry_limits_cleanup(self):
        (self.od / "large").write_bytes(b"x" * 100)
        with self.assertRaises(v.VolumeError):
            self.capture(dataclasses.replace(self.policy, max_bytes=50))
        self.assert_no_partial_capture()
        with self.assertRaises(v.VolumeError):
            self.capture(dataclasses.replace(self.policy, max_entries=2))
        self.assert_no_partial_capture()

    def test_authenticated_path_traversal_and_hardlink_rejected(self):
        roots = self.destinations()
        for name, kind, link in (("od/../../escape", tarfile.REGTYPE, ""),
                                 ("/absolute", tarfile.REGTYPE, ""),
                                 ("od/link", tarfile.LNKTYPE, "/etc/passwd"),
                                 ("od/device", tarfile.CHRTYPE, "")):
            with self.subTest(name=name):
                info = tarfile.TarInfo(name)
                info.type, info.linkname = kind, link
                target = self.encrypted_tar([(info, b"" if info.isfile() else None)])
                with self.assertRaises(v.VolumeError):
                    self.restore(roots, target)
                self.assertEqual([list(p.iterdir()) for p in roots], [[], []])
                target.unlink()

    def test_authenticated_symlink_ancestor_rejected(self):
        entries = []
        members = []
        s = self.od.stat()
        for name, kind in (("od", "dir"), ("home", "dir"), ("od/link", "symlink"),
                           ("od/link/escape", "file")):
            entry = v.metadata(name, kind, s)
            if kind == "symlink":
                entry.update(mode=0o777, target="/tmp")
            if kind == "file":
                entry.update(size=1, sha256=hashlib.sha256(b"x").hexdigest(), consistency="stable-file")
            entries.append(entry)
            members.append((v.tar_info(entry), b"x" if kind == "file" else None))
        payload = json.dumps({"version": 1, "consistency": "per-file-and-per-sqlite-not-global",
                              "bytes": 1, "entries": entries, "skipped": []}).encode()
        info = tarfile.TarInfo(v.MANIFEST)
        info.size = len(payload)
        members.append((info, payload))
        target = self.encrypted_tar(members)
        roots = self.destinations()
        with self.assertRaisesRegex(v.VolumeError, "UNSAFE_MEMBER_ANCESTOR"):
            self.restore(roots, target)
        self.assertEqual([list(p.iterdir()) for p in roots], [[], []])

    def test_manifest_hash_mismatch_rejected_before_extract(self):
        (self.od / "config").write_bytes(b"original")
        self.capture()
        members = []
        with tarfile.open(fileobj=io.BytesIO(self.plaintext()), mode="r:") as archive:
            for member in archive:
                data = archive.extractfile(member).read() if member.isfile() else None
                if member.name == "od/config":
                    data = b"tampered"
                    member.size = len(data)
                members.append((member, data))
        target = self.encrypted_tar(members)
        roots = self.destinations()
        with self.assertRaisesRegex(v.VolumeError, "MANIFEST_HASH_MISMATCH"):
            self.restore(roots, target)
        self.assertEqual([list(p.iterdir()) for p in roots], [[], []])

    def test_long_unicode_paths_pax_roundtrip(self):
        name = "測試" * 35
        folder = self.od / name
        folder.mkdir()
        (folder / name).write_bytes(b"long path")
        self.capture()
        roots, _ = self.restore()
        self.assertEqual((roots[0] / name / name).read_bytes(), b"long path")

    def test_oversized_pax_header_rejected_without_allocation(self):
        header = tarfile.TarInfo("extended")
        header.type, header.size = tarfile.XHDTYPE, 2 * 1024**3
        path = self.out / "oversized.tar"
        path.write_bytes(header.tobuf() + b"\x00" * 1024)
        with self.assertRaisesRegex(v.VolumeError, "EXTENDED_HEADER_LIMIT"):
            v.preflight(path, self.policy)

    def test_restore_sqlite_failure_rolls_back_created_entries(self):
        db = self.od / "database"
        with sqlite3.connect(db) as connection:
            connection.execute("CREATE TABLE data(value TEXT)")
        self.capture()
        roots = self.destinations()
        with mock.patch.object(v, "sqlite_operation", side_effect=v.VolumeError("TEST_CHECK_FAILED")):
            with self.assertRaises(v.VolumeError):
                self.restore(roots)
        self.assertEqual([list(p.iterdir()) for p in roots], [[], []])
        self.assertEqual(list(self.scratch.iterdir()), [])

    def test_cli_errors_do_not_echo_sensitive_names(self):
        result = subprocess.run([sys.executable, "-B", str(HERE / "od_volume.py"), "backup",
                                 "--sensitive-secret-do-not-echo"], capture_output=True, text=True)
        self.assertEqual(result.returncode, 2)
        self.assertNotIn("sensitive-secret", result.stdout + result.stderr)
        self.assertEqual(result.stdout, "")

    def sqlite_fixture(self, wal=False):
        database = self.od / "store"
        connection = sqlite3.connect(database)
        if wal:
            connection.execute("PRAGMA journal_mode=WAL")
            connection.execute("PRAGMA wal_autocheckpoint=0")
        connection.execute("CREATE TABLE data(value TEXT)")
        connection.execute("INSERT INTO data VALUES ('snapshot-value')")
        connection.commit()
        self.addCleanup(connection.close)
        return database, connection

    def after_database(self, action):
        original = v.Capture.add_regular

        def patched(capture, parent, name, full, header):
            result = original(capture, parent, name, full, header)
            if full == "od/store":
                action()
            return result
        return mock.patch.object(v.Capture, "add_regular", patched)

    def test_known_sqlite_sidecars_created_after_snapshot_are_audited(self):
        database, connection = self.sqlite_fixture()
        connection.close()

        def create_sidecars():
            for suffix in ("-wal", "-shm"):
                Path(str(database) + suffix).write_bytes(b"runtime-sidecar")

        with self.after_database(create_sidecars):
            receipt = self.capture()
        self.assertEqual(receipt["counts"]["directoriesWithSqliteSidecarChurn"], 1)
        self.assertEqual(receipt["counts"]["skippedSqliteSidecars"], 2)
        audit = self.manifest()["directoryObservations"]
        self.assertEqual(audit[0]["path"], "od")
        self.assertTrue(all(x["lifecycleChangeObserved"] for x in audit[0]["sidecars"]))
        roots, _ = self.restore()
        self.assertEqual([p.name for p in roots[0].iterdir()], ["store"])
        with sqlite3.connect(roots[0] / "store") as restored:
            self.assertEqual(restored.execute("SELECT value FROM data").fetchone()[0], "snapshot-value")

    def test_known_live_wal_sidecars_removed_after_snapshot_are_audited(self):
        database, writer = self.sqlite_fixture(wal=True)
        self.assertTrue(Path(str(database) + "-wal").exists())
        with self.after_database(writer.close):
            receipt = self.capture()
        self.assertEqual(receipt["counts"]["directoriesWithSqliteSidecarChurn"], 1)
        audit = self.manifest()["directoryObservations"][0]["sidecars"]
        self.assertTrue(all(not x["presentAtEnd"] for x in audit))
        self.assertEqual(receipt["counts"]["skippedSqliteSidecars"], 2)
        roots, _ = self.restore()
        self.assertEqual([p.name for p in roots[0].iterdir()], ["store"])

    def test_sidecar_disappears_during_initial_scan_is_allowed(self):
        database, writer = self.sqlite_fixture(wal=True)
        real_stat = v.os.stat
        closed = False

        def disappear(path, *args, **kwargs):
            nonlocal closed
            if path == "store-shm" and kwargs.get("dir_fd") is not None and not closed:
                writer.close()
                closed = True
            return real_stat(path, *args, **kwargs)

        with mock.patch.object(v.os, "stat", disappear):
            receipt = self.capture()
        self.assertTrue(closed)
        self.assertEqual(receipt["counts"]["directoriesWithSqliteSidecarChurn"], 1)
        self.restore()

    def test_unrelated_change_still_rejected_and_error_context_is_private(self):
        database, writer = self.sqlite_fixture(wal=True)

        def change_both():
            writer.close()
            (self.od / "new-secret-config").write_bytes(b"synthetic-secret")

        with self.after_database(change_both), self.assertRaises(v.VolumeError) as caught:
            self.capture()
        self.assertEqual(str(caught.exception), "DIRECTORY_CONTENT_CHANGED")
        self.assertEqual(caught.exception.private_context,
                         {"scope": "source-volume", "root": "od", "relativePath": "."})
        self.assertNotIn("secret", str(caught.exception))
        self.assert_no_partial_capture()

    def test_rotating_log_not_hidden_by_sqlite_sidecar_churn(self):
        database, writer = self.sqlite_fixture(wal=True)
        log = self.od / "runtime.log"
        log.write_bytes(b"runtime-log-original" * 20)

        def rotate():
            writer.close()
            log.rename(self.od / "runtime.log.1")
            log.write_bytes(b"runtime-log-new")

        with self.after_database(rotate), self.assertRaises(v.VolumeError):
            self.capture(dataclasses.replace(self.policy, append_globs=("od/runtime.log",)))
        self.assert_no_partial_capture()

    def test_unexplained_transient_entry_change_remains_rejected(self):
        database, connection = self.sqlite_fixture()
        connection.close()

        def unrelated_transient():
            transient = self.od / "unrelated.tmp"
            transient.write_bytes(b"temporary")
            transient.unlink()

        with self.after_database(unrelated_transient), self.assertRaisesRegex(v.VolumeError, "DIRECTORY_CHANGED"):
            self.capture()
        self.assert_no_partial_capture()

    def test_new_sidecar_symlink_rejected(self):
        database, connection = self.sqlite_fixture()
        connection.close()
        with self.after_database(lambda: Path(str(database) + "-wal").symlink_to("/etc/passwd")):
            with self.assertRaisesRegex(v.VolumeError, "SQLITE_SIDECAR_TYPE"):
                self.capture()
        self.assert_no_partial_capture()

    def test_nested_directory_error_keeps_nearest_private_scope(self):
        folder = self.home / "nested"
        folder.mkdir()
        (folder / "file").write_bytes(b"stable")
        original = v.Capture.add_bytes

        def mutate(capture, fd, size, entry):
            result = original(capture, fd, size, entry)
            (folder / "new").write_bytes(b"change")
            return result

        with mock.patch.object(v.Capture, "add_bytes", mutate), self.assertRaises(v.VolumeError) as caught:
            self.capture()
        self.assertEqual(caught.exception.private_context,
                         {"scope": "source-volume", "root": "home", "relativePath": "nested"})
        self.assertNotIn("nested", str(caught.exception))
        self.assert_no_partial_capture()

    def paseo_fixture(self):
        folder = self.od / "paseo"
        folder.mkdir(mode=0o700)
        folder.chmod(0o700)
        (folder / "a-anchor").write_bytes(b"unchanging-anchor-content")
        self.atomic_state(folder, 1)
        return folder

    def atomic_state(self, folder, generation):
        temporary = folder / f"state.json.{os.getpid()}.{uuid.uuid4()}.tmp"
        fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w") as output:
            json.dump({"generation": generation, "desiredEnabled": True}, output)
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, folder / "state.json")
        (folder / "state.json").chmod(0o600)

    def after_paseo_anchor(self, action):
        original = v.Capture.add_bytes

        def patched(capture, fd, size, entry):
            result = original(capture, fd, size, entry)
            if entry["path"] == "od/paseo/a-anchor":
                action()
            return result
        return mock.patch.object(v.Capture, "add_bytes", patched)

    def test_paseo_atomic_replace_requires_opt_in_and_preserves_pinned_version(self):
        folder = self.paseo_fixture()
        with self.after_paseo_anchor(lambda: self.atomic_state(folder, 2)):
            with self.assertRaises(v.VolumeError):
                self.capture()
        self.assert_no_partial_capture()
        self.atomic_state(folder, 1)
        with self.after_paseo_anchor(lambda: self.atomic_state(folder, 2)):
            receipt = self.capture(dataclasses.replace(self.policy, paseo_runtime=True))
        self.assertEqual(receipt["counts"]["pinnedAtomicJson"], 1)
        self.assertEqual(receipt["counts"]["directoriesWithPaseoRuntimeChurn"], 1)
        self.assertEqual(json.loads((folder / "state.json").read_text())["generation"], 2)
        roots, _ = self.restore()
        self.assertEqual(json.loads((roots[0] / "paseo/state.json").read_text())["generation"], 1)
        self.assertEqual(self.manifest()["directoryObservations"][0]["atomicReplacements"],
                         ["od/paseo/state.json"])

    def test_paseo_unlink_during_pinned_read_is_allowed(self):
        folder = self.paseo_fixture()
        original = v.hash_fd
        changed = False

        def replace_during_hash(fd, size):
            nonlocal changed
            if not changed:
                changed = True
                self.atomic_state(folder, 2)
            return original(fd, size)

        with mock.patch.object(v, "hash_fd", replace_during_hash):
            self.capture(dataclasses.replace(self.policy, paseo_runtime=True))
        roots, _ = self.restore()
        self.assertEqual(json.loads((roots[0] / "paseo/state.json").read_text())["generation"], 1)

    def test_paseo_in_place_content_change_is_rejected(self):
        folder = self.paseo_fixture()
        original = v.hash_fd

        def rewrite(fd, size):
            (folder / "state.json").write_text('{"generation": 2, "desiredEnabled": true}')
            return original(fd, size)

        with mock.patch.object(v, "hash_fd", rewrite), self.assertRaises(v.VolumeError):
            self.capture(dataclasses.replace(self.policy, paseo_runtime=True))
        self.assert_no_partial_capture()

    def test_paseo_exact_native_temps_are_audited_not_restored(self):
        folder = self.paseo_fixture()
        old = folder / f"state.json.100.{uuid.uuid4()}.tmp"
        new = folder / f"state.json.200.{uuid.uuid4()}.tmp"
        old.write_bytes(b"incomplete writer buffer")
        old.chmod(0o600)

        def churn():
            old.unlink()
            self.atomic_state(folder, 2)
            new.write_bytes(b"partial-next-write")
            new.chmod(0o600)

        with self.after_paseo_anchor(churn):
            receipt = self.capture(dataclasses.replace(self.policy, paseo_runtime=True))
        self.assertEqual(receipt["counts"]["skippedPaseoAtomicTemps"], 2)
        roots, _ = self.restore()
        self.assertEqual(sorted(p.name for p in (roots[0] / "paseo").iterdir()), ["a-anchor", "state.json"])

    def test_paseo_new_persisted_file_not_hidden_by_atomic_replacement(self):
        folder = self.paseo_fixture()

        def add_config():
            self.atomic_state(folder, 2)
            (folder / "new-config.json").write_bytes(b'{"persist":true}')

        with self.after_paseo_anchor(add_config), self.assertRaisesRegex(v.VolumeError, "DIRECTORY_CONTENT_CHANGED"):
            self.capture(dataclasses.replace(self.policy, paseo_runtime=True))
        self.assert_no_partial_capture()

    def test_paseo_bad_temp_pattern_is_not_ignored(self):
        folder = self.paseo_fixture()

        def wrong_pattern():
            self.atomic_state(folder, 2)
            (folder / "state.json.arbitrary.tmp").write_bytes(b"not a native temp")

        with self.after_paseo_anchor(wrong_pattern), self.assertRaises(v.VolumeError):
            self.capture(dataclasses.replace(self.policy, paseo_runtime=True))
        self.assert_no_partial_capture()

    def test_paseo_credentials_atomic_replacement_is_still_rejected(self):
        folder = self.paseo_fixture()
        secret = folder / "pairing.private.json"
        secret.write_bytes(b'{"secret":"synthetic-original"}')
        secret.chmod(0o600)

        def change_secret():
            temporary = folder / "replacement"
            temporary.write_bytes(b'{"secret":"synthetic-new"}')
            temporary.chmod(0o600)
            os.replace(temporary, secret)
            self.atomic_state(folder, 2)

        with self.after_paseo_anchor(change_secret), self.assertRaises(v.VolumeError):
            self.capture(dataclasses.replace(self.policy, paseo_runtime=True))
        self.assert_no_partial_capture()

    def test_paseo_known_locks_audited_and_guards_preserved(self):
        folder = self.paseo_fixture()
        guard = folder / "state.lock.guard"
        guard.write_bytes(b"")
        guard.chmod(0o600)
        lock = folder / "state.lock"
        lock.write_bytes(b'{"pid":100}')
        lock.chmod(0o600)
        bridge = folder / "bridge-lifecycle.lock"
        bridge.mkdir(mode=0o700)
        (bridge / "owner.json").write_bytes(b'{"pid":200}')
        (bridge / "owner.json").chmod(0o600)

        def change():
            lock.unlink()
            (bridge / "owner.json").unlink()
            bridge.rmdir()
            self.atomic_state(folder, 2)

        with self.after_paseo_anchor(change):
            receipt = self.capture(dataclasses.replace(self.policy, paseo_runtime=True))
        self.assertEqual(receipt["counts"]["skippedPaseoRuntimeLocks"], 2)
        roots, _ = self.restore()
        self.assertTrue((roots[0] / "paseo/state.lock.guard").is_file())
        self.assertFalse((roots[0] / "paseo/state.lock").exists())
        self.assertFalse((roots[0] / "paseo/bridge-lifecycle.lock").exists())

    def test_paseo_lock_directory_with_unexpected_file_is_rejected(self):
        folder = self.paseo_fixture()
        lock = folder / "bridge-lifecycle.lock"
        lock.mkdir(mode=0o700)
        (lock / "persisted-config.json").write_bytes(b"must-not-be-ignored")
        with self.assertRaisesRegex(v.VolumeError, "UNKNOWN_TRANSIENT_CHILD"):
            self.capture(dataclasses.replace(self.policy, paseo_runtime=True))
        self.assert_no_partial_capture()

    def test_paseo_temp_symlink_is_rejected(self):
        folder = self.paseo_fixture()
        (folder / f"state.json.100.{uuid.uuid4()}.tmp").symlink_to("/etc/passwd")
        with self.assertRaises(v.VolumeError):
            self.capture(dataclasses.replace(self.policy, paseo_runtime=True))
        self.assert_no_partial_capture()


    def idle_wal_fixture(self):
        database, connection = self.sqlite_fixture(wal=True)
        connection.close()
        self.assertEqual(database.read_bytes()[18:20], b"\x02\x02")
        self.assertFalse(Path(str(database) + "-wal").exists())
        return database

    def readonly_sqlite_failure(self, number=14, name="SQLITE_CANTOPEN"):
        native = sqlite3.OperationalError("sensitive SQL /path credential never emit")
        native.sqlite_errorcode, native.sqlite_errorname = number, name
        error = v.VolumeError("SQLITE_OPERATION_FAILED")
        error.sqlite_diagnostic = v.sqlite_report(native, "backup")
        return error

    def simulated_readonly_failure(self, error=None):
        operation = v.sqlite_operation
        error = error or self.readonly_sqlite_failure()

        def wrapped(parent, name, output, timeout, **kwargs):
            if name == "store":
                raise error
            return operation(parent, name, output, timeout, **kwargs)
        return mock.patch.object(v, "sqlite_operation", side_effect=wrapped)

    def readonly_mount_flag(self):
        # Synthetic fixtures cannot mount PVCs; real RO behavior is independently
        # verified by the native reader. Native SQLite still runs on the shadow.
        return mock.patch.object(v.os, "fstatvfs", return_value=mock.Mock(f_flag=os.ST_RDONLY))

    def test_idle_wal_fallback_roundtrip_and_encrypted_audit(self):
        self.idle_wal_fixture()
        with self.simulated_readonly_failure(), self.readonly_mount_flag():
            receipt = self.capture()
        self.assertEqual(receipt["counts"]["sqliteIdleWalScratchCopies"], 1)
        entry = next(e for e in self.manifest()["entries"] if e["path"] == "od/store")
        self.assertEqual(entry["snapshotMethod"], "guarded-idle-wal-scratch-copy")
        self.assertEqual(entry["sourceReadonlyError"]["source_errorcode"], 14)
        roots, restored = self.restore()
        self.assertEqual(receipt, restored)
        with sqlite3.connect(roots[0] / "store") as connection:
            self.assertEqual(connection.execute("PRAGMA quick_check").fetchall(), [("ok",)])
            self.assertEqual(connection.execute("SELECT value FROM data").fetchall(), [("snapshot-value",)])
        self.assertFalse((roots[0] / "store-wal").exists())
        self.assertFalse((roots[0] / "store-shm").exists())
        self.assertEqual(list(self.scratch.iterdir()), [])

    def test_idle_wal_readonly_cantinit_also_supported(self):
        self.idle_wal_fixture()
        failure = self.readonly_sqlite_failure(1288, "SQLITE_READONLY_CANTINIT")
        with self.simulated_readonly_failure(failure), self.readonly_mount_flag():
            self.assertEqual(self.capture()["counts"]["sqliteIdleWalScratchCopies"], 1)

    def test_idle_wal_fallback_requires_kernel_readonly_mount(self):
        self.idle_wal_fixture()
        with self.simulated_readonly_failure():
            with self.assertRaises(v.VolumeError) as caught:
                self.capture()
        self.assertEqual(caught.exception.sqlite_diagnostic["sqlite_errorcode"], 14)
        self.assertEqual(caught.exception.private_context,
                         {"scope": "source-volume", "root": "od", "relativePath": "store"})
        self.assert_no_partial_capture()

    def test_idle_wal_fallback_rejects_preexisting_sidecars(self):
        database = self.idle_wal_fixture()
        for suffix in ("-wal", "-shm", "-journal"):
            with self.subTest(suffix=suffix):
                sidecar = Path(str(database) + suffix)
                sidecar.write_bytes(b"")
                with self.simulated_readonly_failure(), self.readonly_mount_flag():
                    with self.assertRaisesRegex(v.VolumeError, "SQLITE_FALLBACK_SIDECAR_PRESENT"):
                        self.capture()
                sidecar.unlink()
                self.assert_no_partial_capture()

    def mutate_during_sqlite_copy(self, action):
        original = os.pread
        changed = False

        def read(fd, size, offset):
            nonlocal changed
            result = original(fd, size, offset)
            if not changed and offset == 0 and size > 16:
                changed = True
                action()
            return result
        return mock.patch.object(v.os, "pread", side_effect=read)

    def test_idle_wal_fallback_rejects_concurrent_wal_appearance(self):
        database = self.idle_wal_fixture()
        for suffix in ("-wal", "-shm", "-journal"):
            with self.subTest(suffix=suffix):
                sidecar = Path(str(database) + suffix)
                with self.simulated_readonly_failure(), self.readonly_mount_flag(), \
                        self.mutate_during_sqlite_copy(lambda: sidecar.write_bytes(b"")):
                    with self.assertRaisesRegex(v.VolumeError, "SQLITE_FALLBACK_SIDECAR_PRESENT"):
                        self.capture()
                sidecar.unlink()
                self.assert_no_partial_capture()

    def test_idle_wal_fallback_rejects_transient_wal_create_remove(self):
        database = self.idle_wal_fixture()

        def transient():
            sidecar = Path(str(database) + "-wal")
            sidecar.write_bytes(b"")
            sidecar.unlink()

        with self.simulated_readonly_failure(), self.readonly_mount_flag(), \
                self.mutate_during_sqlite_copy(transient):
            with self.assertRaisesRegex(v.VolumeError, "SQLITE_FALLBACK_PARENT_CHANGED"):
                self.capture()
        self.assert_no_partial_capture()

    def test_idle_wal_fallback_rejects_mainfile_mutation(self):
        database = self.idle_wal_fixture()

        def change():
            with database.open("r+b") as file:
                file.seek(100)
                file.write(b"changed!")

        with self.simulated_readonly_failure(), self.readonly_mount_flag(), \
                self.mutate_during_sqlite_copy(change):
            with self.assertRaisesRegex(v.VolumeError, "SQLITE_FALLBACK_SOURCE_CHANGED"):
                self.capture()
        self.assert_no_partial_capture()

    def test_idle_wal_fallback_rejects_mainfile_replacement(self):
        database = self.idle_wal_fixture()
        replacement = self.home / "replacement"
        replacement.write_bytes(database.read_bytes())
        with self.simulated_readonly_failure(), self.readonly_mount_flag(), \
                self.mutate_during_sqlite_copy(lambda: os.replace(replacement, database)):
            with self.assertRaisesRegex(v.VolumeError, "SQLITE_FALLBACK_SOURCE_CHANGED"):
                self.capture()
        self.assert_no_partial_capture()

    def test_idle_wal_fallback_rejects_non_wal_database(self):
        _, connection = self.sqlite_fixture()
        connection.close()
        with self.simulated_readonly_failure(), self.readonly_mount_flag():
            with self.assertRaisesRegex(v.VolumeError, "SQLITE_FALLBACK_NOT_IDLE_WAL"):
                self.capture()
        self.assert_no_partial_capture()

    def test_idle_wal_fallback_never_hides_schema_busy_or_generic_readonly(self):
        self.idle_wal_fixture()
        for number, name in ((1, "SQLITE_ERROR"), (5, "SQLITE_BUSY"), (8, "SQLITE_READONLY")):
            with self.subTest(code=number):
                error = self.readonly_sqlite_failure(number, name)
                with self.simulated_readonly_failure(error), self.readonly_mount_flag():
                    with self.assertRaises(v.VolumeError) as caught:
                        self.capture()
                self.assertIs(caught.exception, error)
                self.assert_no_partial_capture()

    def test_sqlite_diagnostic_retains_safe_code_and_filename_only(self):
        folder = self.home / ".codex"
        folder.mkdir()
        (folder / "broken.sqlite").write_bytes(v.SQLITE_HEADER + b"not a database" * 100)
        with self.assertRaises(v.VolumeError) as caught:
            self.capture()
        error = caught.exception
        self.assertEqual(error.private_context,
            {"scope": "source-volume", "root": "home", "relativePath": ".codex/broken.sqlite"})
        self.assertEqual(error.sqlite_diagnostic["sqlite_errorcode"], sqlite3.SQLITE_NOTADB)
        self.assertEqual(error.sqlite_diagnostic["sqlite_errorname"], "SQLITE_NOTADB")
        self.assertEqual(error.sqlite_diagnostic["sqlite_version"], sqlite3.sqlite_version)
        self.assertIn(error.sqlite_diagnostic["stage"], v.SQLITE_STAGES)
        self.assertNotIn("broken.sqlite", str(error))
        report = self.readonly_sqlite_failure().sqlite_diagnostic
        self.assertNotIn("credential", json.dumps(report))
        self.assertEqual(set(report), {"ok", "code", "stage", "sqlite_version",
                                     "sqlite_errorcode", "sqlite_errorname"})
        self.assert_no_partial_capture()


if __name__ == "__main__":
    unittest.main(verbosity=2)

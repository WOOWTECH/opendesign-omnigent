"""Independent black-box regressions for the controller-owned inotify fix.

Run: python -B -m unittest -v test_directory_events
No production access; all fixtures stay in od-volume. Controller contract:
PaseoJournal(fd), events() -> list[{name, mask}]. Integration tests use real
inotify; consumer tests inject through events(); overflow/lost-watch tests
inject raw Linux records at os.read. Missing journal API is a test failure.
"""
import dataclasses
import json
import os
import struct
import unittest
import uuid
from unittest import mock

import test_od_volume as fixtures

v = fixtures.v


class DirectoryEventTests(unittest.TestCase):
    # Reuse fixture methods without inheriting/rerunning the existing 52 tests.
    setUpClass = classmethod(fixtures.VolumeTests.setUpClass.__func__)
    tearDownClass = classmethod(fixtures.VolumeTests.tearDownClass.__func__)
    setUp = fixtures.VolumeTests.setUp
    capture = fixtures.VolumeTests.capture
    restore = fixtures.VolumeTests.restore
    destinations = fixtures.VolumeTests.destinations
    plaintext = fixtures.VolumeTests.plaintext
    manifest = fixtures.VolumeTests.manifest
    assert_no_partial_capture = fixtures.VolumeTests.assert_no_partial_capture
    paseo_fixture = fixtures.VolumeTests.paseo_fixture
    atomic_state = fixtures.VolumeTests.atomic_state
    after_paseo_anchor = fixtures.VolumeTests.after_paseo_anchor

    def cycle(self, path, payload=b"transient"):
        fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        with os.fdopen(fd, "wb") as output:
            output.write(payload)
        path.unlink()

    def allowed_capture(self, action):
        with self.after_paseo_anchor(action):
            receipt = self.capture(dataclasses.replace(self.policy, paseo_runtime=True))
        self.assertEqual(receipt["counts"]["pinnedAtomicJson"], 1)
        roots, _ = self.restore()
        self.assertEqual(json.loads((roots[0] / "paseo/state.json").read_text())["generation"], 1)
        return roots

    def test_state_lease_entire_lifetime_between_lists_is_accepted(self):
        folder = self.paseo_fixture()
        roots = self.allowed_capture(lambda: self.cycle(folder / "state.lock"))
        self.assertFalse((roots[0] / "paseo/state.lock").exists())

    def test_exact_state_temp_entire_lifetime_between_lists_is_accepted(self):
        folder = self.paseo_fixture()
        name = f"state.json.{os.getpid()}.{uuid.uuid4()}.tmp"
        roots = self.allowed_capture(lambda: self.cycle(folder / name))
        self.assertFalse((roots[0] / "paseo" / name).exists())

    def test_bridge_lease_directory_entire_lifetime_is_accepted(self):
        folder = self.paseo_fixture()

        def churn():
            lease = folder / "bridge-lifecycle.lock"
            lease.mkdir(mode=0o700)
            owner = lease / "owner.json"
            owner.write_text('{}')
            owner.chmod(0o600)
            owner.unlink()
            lease.rmdir()

        roots = self.allowed_capture(churn)
        self.assertFalse((roots[0] / "paseo/bridge-lifecycle.lock").exists())

    def test_unknown_transient_cannot_hide_behind_allowed_atomic_replacement(self):
        folder = self.paseo_fixture()

        def churn():
            self.atomic_state(folder, 2)
            self.cycle(folder / "unknown-persisted-config.json")

        with self.after_paseo_anchor(churn), self.assertRaises(v.VolumeError):
            self.capture(dataclasses.replace(self.policy, paseo_runtime=True))
        self.assert_no_partial_capture()

    def test_wrong_temp_pattern_cannot_hide_behind_atomic_replacement(self):
        folder = self.paseo_fixture()

        def churn():
            self.atomic_state(folder, 2)
            self.cycle(folder / "state.json.123.not-a-uuid.tmp")

        with self.after_paseo_anchor(churn), self.assertRaises(v.VolumeError):
            self.capture(dataclasses.replace(self.policy, paseo_runtime=True))
        self.assert_no_partial_capture()

    def test_other_json_temp_cannot_hide_behind_atomic_replacement(self):
        folder = self.paseo_fixture()

        def churn():
            self.atomic_state(folder, 2)
            self.cycle(folder / f"pairing.private.json.{os.getpid()}.{uuid.uuid4()}.tmp")

        with self.after_paseo_anchor(churn), self.assertRaises(v.VolumeError):
            self.capture(dataclasses.replace(self.policy, paseo_runtime=True))
        self.assert_no_partial_capture()

    def test_no_opt_in_still_rejects_native_transient_cycle(self):
        folder = self.paseo_fixture()
        with self.after_paseo_anchor(lambda: self.cycle(folder / "state.lock")), \
                self.assertRaises(v.VolumeError):
            self.capture()
        self.assert_no_partial_capture()

    def test_watch_covers_mutation_immediately_before_initial_list(self):
        folder = self.paseo_fixture()
        original = os.listdir
        inode = folder.stat().st_ino
        fired = False

        def listing(fd):
            nonlocal fired
            if isinstance(fd, int) and os.fstat(fd).st_ino == inode and not fired:
                fired = True
                self.cycle(folder / "state.lock")
            return original(fd)

        with mock.patch.object(v.os, "listdir", side_effect=listing):
            self.capture(dataclasses.replace(self.policy, paseo_runtime=True))
        self.assertTrue(fired)

    def test_unknown_event_after_final_stat_is_drained_and_rejected(self):
        folder = self.paseo_fixture()
        inode = folder.stat().st_ino
        original = os.fstat
        state = {"armed": False, "fired": False}

        def final_stat(fd):
            value = original(fd)
            if value.st_ino == inode and state["armed"] and not state["fired"]:
                state["fired"] = True
                self.cycle(folder / "late-unknown-config.json")
            return value

        def arm():
            state["armed"] = True

        with mock.patch.object(v.os, "fstat", side_effect=final_stat), \
                self.after_paseo_anchor(arm), self.assertRaises(v.VolumeError):
            self.capture(dataclasses.replace(self.policy, paseo_runtime=True))
        self.assertTrue(state["fired"])
        self.assert_no_partial_capture()

    def test_native_chmod_with_lease_keeps_guard_and_root_metadata(self):
        folder = self.paseo_fixture()
        guard = folder / "state.lock.guard"
        guard.write_bytes(b"")
        guard.chmod(0o600)

        def churn():
            folder.chmod(0o700)  # Native privateDirectory(): self ATTRIB event.
            guard.chmod(0o600)   # Native withLockTransition(): named ATTRIB.
            self.cycle(folder / "state.lock")

        roots = self.allowed_capture(churn)
        self.assertTrue((roots[0] / "paseo/state.lock.guard").is_file())
        self.assertEqual((roots[0] / "paseo").stat().st_mode & 0o777, 0o700)
        self.assertEqual((roots[0] / "paseo/state.lock.guard").stat().st_mode & 0o777, 0o600)

    def test_unknown_named_attrib_rejected_even_with_state_replacement(self):
        folder = self.paseo_fixture()
        config = folder / "z-config.json"
        config.write_text('{}')
        config.chmod(0o600)

        def churn():
            self.atomic_state(folder, 2)
            config.chmod(0o600)

        with self.after_paseo_anchor(churn), self.assertRaises(v.VolumeError):
            self.capture(dataclasses.replace(self.policy, paseo_runtime=True))
        self.assert_no_partial_capture()

    def test_guard_hardlink_added_after_scan_is_rejected(self):
        folder = self.paseo_fixture()
        guard = folder / "state.lock.guard"
        guard.write_bytes(b"")
        guard.chmod(0o600)

        def churn():
            os.link(guard, self.scratch / "unexpected-guard-link")
            self.cycle(folder / "state.lock")

        with self.after_paseo_anchor(churn), self.assertRaises(v.VolumeError):
            self.capture(dataclasses.replace(self.policy, paseo_runtime=True))
        (self.scratch / "unexpected-guard-link").unlink()
        self.assert_no_partial_capture()

    def journal_class(self):
        self.assertTrue(hasattr(v, "PaseoJournal"), "controller PaseoJournal(fd) API is not present yet")
        return v.PaseoJournal

    def test_events_api_unknown_name_rejected_without_metadata_change(self):
        self.paseo_fixture()
        journal = self.journal_class()
        events = [{"name": "unknown-persisted-config.json", "mask": 0x100},
                  {"name": "unknown-persisted-config.json", "mask": 0x200}]
        with mock.patch.object(journal, "events", return_value=events) as drain:
            with self.assertRaises(v.VolumeError) as caught:
                self.capture(dataclasses.replace(self.policy, paseo_runtime=True))
        self.assertGreaterEqual(drain.call_count, 1)
        self.assertEqual(caught.exception.private_context,
                         {"scope": "source-volume", "root": "od", "relativePath": "paseo"})
        self.assertNotIn("unknown-persisted-config", str(caught.exception))
        self.assert_no_partial_capture()

    def test_events_api_unknown_mixed_with_allowed_events_still_rejected(self):
        folder = self.paseo_fixture()
        journal = self.journal_class()
        events = [{"name": "state.lock", "mask": 0x100},
                  {"name": "unknown-new-file", "mask": 0x100},
                  {"name": "unknown-new-file", "mask": 0x200},
                  {"name": "state.lock", "mask": 0x200}]
        with self.after_paseo_anchor(lambda: self.cycle(folder / "state.lock")), \
                mock.patch.object(journal, "events", return_value=events) as drain:
            with self.assertRaises(v.VolumeError):
                self.capture(dataclasses.replace(self.policy, paseo_runtime=True))
        self.assertGreaterEqual(drain.call_count, 1)
        self.assert_no_partial_capture()

    def test_events_api_empty_journal_cannot_explain_directory_mtime_change(self):
        folder = self.paseo_fixture()
        journal = self.journal_class()
        with self.after_paseo_anchor(lambda: self.cycle(folder / "state.lock")), \
                mock.patch.object(journal, "events", return_value=[]):
            with self.assertRaises(v.VolumeError):
                self.capture(dataclasses.replace(self.policy, paseo_runtime=True))
        self.assert_no_partial_capture()

    def test_real_journal_contract_observes_cycle_and_drains_to_empty(self):
        folder = self.paseo_fixture()
        journal = self.journal_class()
        original = journal.events
        batches = []

        def collect(watcher):
            batch = original(watcher)
            self.assertIsInstance(batch, list)
            for event in batch:
                self.assertIsInstance(event["name"], str)
                self.assertIs(type(event["mask"]), int)
            batches.append(batch)
            self.assertEqual(original(watcher), [])
            return batch

        with mock.patch.object(journal, "events", collect):
            self.allowed_capture(lambda: self.cycle(folder / "state.lock"))
        events = [event for batch in batches for event in batch if event["name"] == "state.lock"]
        self.assertTrue(any(event["mask"] & 0x100 for event in events), "missing CREATE")
        self.assertTrue(any(event["mask"] & 0x200 for event in events), "missing DELETE")

    def test_watch_ready_before_initial_capture_fstat(self):
        folder = self.paseo_fixture()
        journal = self.journal_class()
        original_init = journal.__init__
        directory = v.Capture._directory
        original_stat = os.fstat
        inode = folder.stat().st_ino
        state = {"inside": False, "constructing": False, "watched": False, "fired": False}

        def watch(watcher, fd):
            state["constructing"] = True
            try:
                original_init(watcher, fd)
            finally:
                state["constructing"] = False
            if original_stat(fd).st_ino == inode:
                state["watched"] = True

        def enter(capture, fd, prefix, *args, **kwargs):
            previous = state["inside"]
            state["inside"] = prefix == "od/paseo"
            try:
                return directory(capture, fd, prefix, *args, **kwargs)
            finally:
                state["inside"] = previous

        def first_stat(fd):
            value = original_stat(fd)
            if state["inside"] and not state["constructing"] and value.st_ino == inode and not state["fired"]:
                state["fired"] = True
                self.assertTrue(state["watched"], "watch must exist before initial capture fstat")
                self.cycle(folder / "unknown-at-initial-stat.json")
            return value

        with mock.patch.object(journal, "__init__", watch), \
                mock.patch.object(v.Capture, "_directory", enter), \
                mock.patch.object(v.os, "fstat", side_effect=first_stat), self.assertRaises(v.VolumeError):
            self.capture(dataclasses.replace(self.policy, paseo_runtime=True))
        self.assertTrue(state["fired"])
        self.assert_no_partial_capture()

    def assert_strict_outside_pinned_paseo(self, folder, archive_path):
        original = v.Capture.add_bytes
        fired = False

        def after_anchor(capture, fd, size, entry):
            nonlocal fired
            result = original(capture, fd, size, entry)
            if entry["path"] == archive_path + "/a-anchor":
                fired = True
                self.cycle(folder / "state.lock")
            return result

        with mock.patch.object(v.Capture, "add_bytes", after_anchor), self.assertRaises(v.VolumeError):
            self.capture(dataclasses.replace(self.policy, paseo_runtime=True))
        self.assertTrue(fired)
        self.assert_no_partial_capture()

    def test_native_names_in_home_paseo_do_not_enable_exception(self):
        folder = self.paseo_fixture()
        moved = self.home / "paseo"
        folder.rename(moved)
        self.assert_strict_outside_pinned_paseo(moved, "home/paseo")

    def test_native_names_in_nested_paseo_do_not_enable_exception(self):
        folder = self.paseo_fixture()
        (self.od / "nested").mkdir()
        moved = self.od / "nested/paseo"
        folder.rename(moved)
        self.assert_strict_outside_pinned_paseo(moved, "od/nested/paseo")

    def test_paseo_without_pinned_state_keeps_strict_directory_guard(self):
        folder = self.paseo_fixture()
        (folder / "state.json").unlink()
        self.assert_strict_outside_pinned_paseo(folder, "od/paseo")

    def test_overflow_or_lost_watch_fails_even_without_directory_mtime_change(self):
        self.paseo_fixture()
        original = os.read
        for mask in (0x00004000, 0x00008000, 0x00002000):  # OVERFLOW, IGNORED, UNMOUNT
            with self.subTest(mask=mask):
                # A deliberately broken implementation might publish output;
                # isolate subtests so that does not mask the next fault.
                self.target.unlink(missing_ok=True)
                fired = False

                def read(fd, size):
                    nonlocal fired
                    if not fired and os.readlink(f"/proc/self/fd/{fd}") == "anon_inode:inotify":
                        fired = True
                        return struct.pack("iIII", -1, mask, 0, 0)
                    return original(fd, size)

                with mock.patch.object(v.os, "read", side_effect=read), self.assertRaises(v.VolumeError):
                    self.capture(dataclasses.replace(self.policy, paseo_runtime=True))
                self.assertTrue(fired, "test requires the real inotify os.read boundary")
                self.assert_no_partial_capture()


if __name__ == "__main__":
    unittest.main(verbosity=2)

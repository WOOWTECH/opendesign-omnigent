"""Offline installer tests. The actual SDK stays outside the public repository."""
import hashlib
import importlib.util
import os
from pathlib import Path
import stat
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("sdk_patch_apply", ROOT / "apply.py")
INSTALLER = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(INSTALLER)
PUBLIC_PATCH = ROOT / "opencode-host-model-options.patch"

class InstallerTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.target = Path(self.directory.name) / "host" / "connect.py"
        self.target.parent.mkdir()
        self.delta = Path(self.directory.name) / "delta.patch"
        self.original = b"before\nanchor\nafter\n"
        self.result = b"before\nanchor\nadded\nafter\n"
        self.target.write_bytes(self.original)
        self.delta.write_bytes(b"--- a/host/connect.py\n+++ b/host/connect.py\n@@ -2,1 +2,2 @@\n anchor\n+added\n")

    def pins(self, result=None):
        return patch.multiple(INSTALLER, BASE_SHA256=hashlib.sha256(self.original).hexdigest(), RESULT_SHA256=hashlib.sha256(self.result if result is None else result).hexdigest())

    def test_public_patch_reproduces_exact_pinned_sdk(self):
        baseline = Path(os.environ.get("OMNIGENT_PATCH_BASELINE", "/opt/omnigent/venv/lib/python3.12/site-packages/omnigent/host/connect.py"))
        if not baseline.is_file():
            self.skipTest("Provide OMNIGENT_PATCH_BASELINE or the original installed SDK")
        contents = baseline.read_bytes()
        self.assertEqual(hashlib.sha256(contents).hexdigest(), INSTALLER.BASE_SHA256)
        self.target.write_bytes(contents)
        INSTALLER.apply(self.target, PUBLIC_PATCH)
        self.assertEqual(hashlib.sha256(self.target.read_bytes()).hexdigest(), INSTALLER.RESULT_SHA256)
        expected = os.environ.get("OMNIGENT_PATCH_EXPECTED")
        if expected:
            self.assertEqual(self.target.read_bytes(), Path(expected).read_bytes())

    def test_unknown_baseline_is_unchanged_and_cli_output_is_safe(self):
        before = self.target.stat()
        with self.assertRaisesRegex(ValueError, "BASELINE_MISMATCH"):
            INSTALLER.apply(self.target, PUBLIC_PATCH)
        command = subprocess.run([sys.executable, "-B", str(ROOT / "apply.py"), str(self.target), str(PUBLIC_PATCH)], capture_output=True, check=False)
        self.assertEqual(command.returncode, 1)
        self.assertEqual(command.stdout, b"")
        self.assertEqual(command.stderr, b"SDK_PATCH_REJECTED\n")
        self.assertEqual(self.target.read_bytes(), self.original)
        after = self.target.stat()
        self.assertEqual((after.st_ino, after.st_mode, after.st_uid, after.st_gid, after.st_mtime_ns, after.st_ctime_ns), (before.st_ino, before.st_mode, before.st_uid, before.st_gid, before.st_mtime_ns, before.st_ctime_ns))

    def test_corrupt_context_and_hunk_counts_are_rejected(self):
        valid = self.delta.read_bytes()
        for corrupt, error in ((valid.replace(b" anchor\n", b" wrong-context\n"), "CONTEXT_MISMATCH"), (valid.replace(b"+2,2", b"+2,3"), "INVALID_PATCH")):
            with self.subTest(error=error), self.pins():
                self.delta.write_bytes(corrupt)
                with self.assertRaisesRegex(ValueError, error):
                    INSTALLER.apply(self.target, self.delta)
                self.assertEqual(self.target.read_bytes(), self.original)
                self.assertEqual(list(self.target.parent.glob(".sdk-patch-*")), [])

    def test_unapproved_result_is_rejected_before_writing(self):
        with self.pins(result=b"unapproved-result"), self.assertRaisesRegex(ValueError, "RESULT_MISMATCH"):
            INSTALLER.apply(self.target, self.delta)
        self.assertEqual(self.target.read_bytes(), self.original)
        self.assertEqual(list(self.target.parent.glob(".sdk-patch-*")), [])

    def test_success_preserves_mode_uid_gid_and_rejects_symlinks(self):
        self.target.chmod(0o640)
        before = self.target.stat()
        with self.pins():
            INSTALLER.apply(self.target, self.delta)
        after = self.target.stat()
        self.assertEqual(self.target.read_bytes(), self.result)
        self.assertEqual((stat.S_IMODE(after.st_mode), after.st_uid, after.st_gid), (stat.S_IMODE(before.st_mode), before.st_uid, before.st_gid))
        self.target.unlink()
        self.target.symlink_to(self.delta)
        with self.assertRaisesRegex(ValueError, "INVALID_TARGET"):
            INSTALLER.apply(self.target, self.delta)

if __name__ == "__main__":
    unittest.main()

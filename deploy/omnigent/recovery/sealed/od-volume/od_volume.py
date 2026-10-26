#!/usr/bin/env python3
"""Offline OD volume capture/restore. Linux; no network or production defaults."""
from __future__ import annotations

import argparse
import ctypes
from collections import Counter
from contextlib import contextmanager
from dataclasses import dataclass
import fnmatch
import hashlib
import io
import json
import os
from pathlib import Path
import re
import secrets
import sqlite3
import stat
import struct
import subprocess
import sys
import tarfile
import tempfile
import time
from urllib.parse import quote

# The shared module is read-only, including its bytecode cache.
sys.dont_write_bytecode = True
try:
    import backup_envelope as crypto
except ModuleNotFoundError as exc:
    if exc.name != "backup_envelope":
        raise
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "crypto"))
    import backup_envelope as crypto

SQLITE_HEADER = b"SQLite format 3\x00"
MANIFEST = "__od_volume_manifest__.json"
BLOCK = 1024 * 1024
DIR_FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
FILE_FLAGS = os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK
PASEO_ATOMIC_PATH = "od/paseo/state.json"
PASEO_GUARDS = frozenset(("state.lock.guard", "bridge-lifecycle.lock.guard", "runtime.lock.guard"))
NATIVE_JSON_TEMP = re.compile(r"([1-9][0-9]{0,9})\.[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}\.tmp\Z")


def native_temp(name, base):
    return name.startswith(base + ".") and bool(NATIVE_JSON_TEMP.fullmatch(name[len(base) + 1:]))


def paseo_transient(prefix, name, enabled):
    if not enabled or prefix != "od/paseo":
        return None
    if native_temp(name, "state.json"):
        return "paseo-atomic-temp"
    if name in ("state.lock", "bridge-lifecycle.lock"):
        return "paseo-runtime-lock"
    return None


class VolumeError(Exception):
    """Only fixed error codes; never embed paths, data, or exception messages."""

    def __init__(self, code):
        super().__init__(code)
        self.code = code
        self.private_context = None
        self.sqlite_diagnostic = None

    def at_source(self, archive_path):
        """Private operations evidence only. str(error) remains a fixed code."""
        if self.private_context is None:
            parts = safe_path(archive_path)
            self.private_context = {"scope": "source-volume", "root": parts[0],
                                    "relativePath": "/".join(parts[1:]) or "."}
        return self


def require(condition, code):
    if not condition:
        raise VolumeError(code)


class PaseoJournal:
    """Bounded Linux events for the one explicitly opted-in native directory.

    A disappearing lock may leave identical endpoint listings. Events establish
    its lifecycle; persistent file bytes and metadata still have their own guards.
    Queue overflow or an unrecognised event fails closed.
    """
    MASK = 0x00000FC6  # MODIFY, ATTRIB, MOVED_FROM/TO, CREATE/DELETE, DELETE/MOVE_SELF
    LIFECYCLE = 0x000003C0
    ISDIR = 0x40000000
    LIMIT = 4096

    def __init__(self, directory_fd):
        libc = ctypes.CDLL(None, use_errno=True)
        libc.inotify_init1.argtypes = [ctypes.c_int]
        libc.inotify_init1.restype = ctypes.c_int
        libc.inotify_add_watch.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_uint32]
        libc.inotify_add_watch.restype = ctypes.c_int
        self.fd = libc.inotify_init1(os.O_NONBLOCK | os.O_CLOEXEC)
        require(self.fd >= 0, "DIRECTORY_JOURNAL_UNAVAILABLE")
        self.watch = libc.inotify_add_watch(self.fd,
            f"/proc/self/fd/{directory_fd}".encode("ascii"), self.MASK)
        if self.watch < 0:
            self.close()
            raise VolumeError("DIRECTORY_JOURNAL_UNAVAILABLE")

    def close(self):
        if self.fd >= 0:
            os.close(self.fd)
            self.fd = -1

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()

    def events(self):
        result, total = [], 0
        while True:
            try:
                data = os.read(self.fd, 65536)
            except BlockingIOError:
                return result
            require(bool(data), "DIRECTORY_JOURNAL_CLOSED")
            total += len(data)
            require(total <= 1024 * 1024, "DIRECTORY_JOURNAL_OVERFLOW")
            offset = 0
            while offset < len(data):
                require(offset + 16 <= len(data), "DIRECTORY_JOURNAL_INVALID")
                watch, mask, cookie, size = struct.unpack_from("iIII", data, offset)
                offset += 16
                require(watch == self.watch and not mask & 0x0000EC00,
                        "DIRECTORY_JOURNAL_OVERFLOW_OR_INVALIDATED")
                require(0 <= size <= 4096 and offset + size <= len(data),
                        "DIRECTORY_JOURNAL_INVALID")
                raw = data[offset:offset + size].split(b"\x00", 1)[0]
                offset += size
                try:
                    name = raw.decode("utf-8", "strict")
                except UnicodeError:
                    raise VolumeError("DIRECTORY_JOURNAL_INVALID") from None
                require(len(result) < self.LIMIT, "DIRECTORY_JOURNAL_OVERFLOW")
                result.append({"name": name, "mask": mask})


def validate_paseo_events(events):
    require(type(events) is list and len(events) <= PaseoJournal.LIMIT,
            "DIRECTORY_JOURNAL_INVALID")
    for event in events:
        require(type(event) is dict and set(event) == {"name", "mask"},
                "DIRECTORY_JOURNAL_INVALID")
        name, mask = event["name"], event["mask"]
        require(type(name) is str and type(mask) is int and mask > 0 and
                not mask & ~(0x000003C6 | PaseoJournal.ISDIR), "DIRECTORY_JOURNAL_INVALID")
        if name == "":
            require(mask == (0x4 | PaseoJournal.ISDIR), "DIRECTORY_JOURNAL_INVALID")
            continue
        if name in PASEO_GUARDS:
            require(mask == 0x4, "DIRECTORY_JOURNAL_UNEXPECTED_GUARD_CHANGE")
            continue
        require(name == "state.json" or paseo_transient("od/paseo", name, True),
                "DIRECTORY_JOURNAL_UNEXPECTED_ENTRY")
        require(bool(mask & PaseoJournal.ISDIR) == (name == "bridge-lifecycle.lock"),
                "DIRECTORY_JOURNAL_UNEXPECTED_TYPE")
        require(name != "state.json" or not mask & 0x2, "ATOMIC_STATE_IN_PLACE_WRITE")
    return any(e["mask"] & PaseoJournal.LIFECYCLE for e in events)


@dataclass(frozen=True)
class Policy:
    max_bytes: int = 16 * 1024**3
    max_entries: int = 200_000
    manifest_bytes: int = 64 * 1024**2
    sqlite_timeout: float = 60.0
    # Opt-in only: filename extensions alone cannot identify a secret/config.
    append_globs: tuple[str, ...] = ()
    paseo_runtime: bool = False

    def validate(self):
        require(type(self.max_bytes) is int and 0 < self.max_bytes <= 48 * 1024**3,
                "INVALID_LIMIT")
        require(type(self.max_entries) is int and 2 <= self.max_entries <= 1_000_000,
                "INVALID_LIMIT")
        require(type(self.manifest_bytes) is int and 1024 <= self.manifest_bytes <= 128 * 1024**2,
                "INVALID_LIMIT")
        require(0 < self.sqlite_timeout <= 3600, "INVALID_TIMEOUT")
        require(all(isinstance(p, str) and p.startswith(("od/", "home/"))
                    and len(p) <= 4096 for p in self.append_globs), "INVALID_APPEND_PATTERN")
        require(type(self.paseo_runtime) is bool, "INVALID_PASEO_POLICY")

    def envelope_limits(self):
        # Tar headers/padding plus the bounded encrypted manifest.
        return crypto.Limits(max_plaintext_bytes=min(crypto.HARD_MAX_PLAINTEXT_BYTES,
            self.max_bytes + self.max_entries * 4096 + self.manifest_bytes + 10240))


def open_directory(path):
    """Anchor every path component, refusing symlinks even in ancestors."""
    fd = os.open("/", DIR_FLAGS)
    try:
        for part in Path(os.path.abspath(path)).parts[1:]:
            nxt = os.open(part, DIR_FLAGS, dir_fd=fd)
            os.close(fd)
            fd = nxt
        return fd
    except BaseException:
        os.close(fd)
        raise


def signature(s):
    return (s.st_dev, s.st_ino, s.st_mode, s.st_uid, s.st_gid,
            s.st_size, s.st_mtime_ns, s.st_ctime_ns)


def identity(s):
    return (s.st_dev, s.st_ino, s.st_mode, s.st_uid, s.st_gid)


def metadata(name, kind, s):
    return {"path": name, "type": kind, "mode": stat.S_IMODE(s.st_mode),
            "uid": s.st_uid, "gid": s.st_gid, "mtimeNs": s.st_mtime_ns}


def safe_path(name):
    require(isinstance(name, str) and 0 < len(name.encode("utf-8")) <= 4096,
            "UNSAFE_MEMBER_PATH")
    require("\x00" not in name and "\\" not in name and
            all(x not in ("", ".", "..") for x in name.split("/")), "UNSAFE_MEMBER_PATH")
    require(name.split("/")[0] in ("od", "home"), "UNSAFE_MEMBER_ROOT")
    return name.split("/")


def _uri(parent_fd, name, immutable=False):
    suffix = "?mode=ro" + ("&immutable=1" if immutable else "")
    return "file:" + quote(f"/proc/self/fd/{parent_fd}/{name}", safe="/") + suffix


SQLITE_STAGES = frozenset(("connect-source", "configure-source", "connect-destination", "backup",
    "journal-mode", "quick-check", "close-destination", "close-source", "complete",
    "worker-timeout", "worker-protocol"))
SQLITE_CODES = frozenset(("SQLITE_OK", "SQLITE_OPERATION_FAILED", "SQLITE_TIMEOUT",
    "SQLITE_JOURNAL_MODE", "SQLITE_QUICK_CHECK", "SQLITE_WORKER_PROTOCOL"))


def sqlite_report(error, stage):
    """Allowlisted fields only: no exception text, SQL, paths or quick_check rows."""
    number = getattr(error, "sqlite_errorcode", None)
    name = getattr(error, "sqlite_errorname", None)
    return {"ok": error is None,
        "code": "SQLITE_OK" if error is None else error.code if isinstance(error, VolumeError)
                and error.code in SQLITE_CODES else "SQLITE_OPERATION_FAILED",
        "stage": stage if stage in SQLITE_STAGES else "worker-protocol",
        "sqlite_version": sqlite3.sqlite_version,
        "sqlite_errorcode": number if type(number) is int and 0 <= number < 2**31 else None,
        "sqlite_errorname": name if isinstance(name, str) and
                re.fullmatch(r"SQLITE_[A-Z0-9_]{1,64}", name) else None}


def _sqlite_worker(parent_fd, name, output, timeout, live_source_check=False):
    """Fresh interpreter: never inherit the caller's SQLite connections/maps."""
    source = destination = None
    stage = "connect-source"
    result = None
    try:
        deadline = time.monotonic() + timeout

        def progress(*_):
            require(time.monotonic() < deadline, "SQLITE_TIMEOUT")

        source = sqlite3.connect(_uri(parent_fd, name, output is None and not live_source_check), uri=True,
                                 timeout=min(timeout, 0.1))
        stage = "configure-source"
        source.execute("PRAGMA query_only=ON")
        source.execute("PRAGMA busy_timeout=100")
        source.set_progress_handler(lambda: int(time.monotonic() >= deadline), 1000)
        if output is not None:
            # All plaintext scratch is private helper emptyDir, never source PVC.
            os.umask(0o077)
            stage = "connect-destination"
            destination = sqlite3.connect(output, timeout=0.1)
            stage = "backup"
            source.backup(destination, pages=128, progress=progress, sleep=0.02)
            destination.set_progress_handler(lambda: int(time.monotonic() >= deadline), 1000)
            stage = "journal-mode"
            require(destination.execute("PRAGMA journal_mode=DELETE").fetchone()[0] == "delete",
                    "SQLITE_JOURNAL_MODE")
            checked = destination
        else:
            checked = source
        stage = "quick-check"
        rows = checked.execute("PRAGMA quick_check").fetchmany(2)
        require(rows == [("ok",)], "SQLITE_QUICK_CHECK")
        progress()
        result = sqlite_report(None, "complete")
    except BaseException as error:
        result = sqlite_report(error, stage)
    finally:
        for connection, closing_stage in ((destination, "close-destination"), (source, "close-source")):
            if connection is not None:
                try:
                    connection.close()
                except BaseException as error:
                    if result is None or result["ok"]:
                        result = sqlite_report(error, closing_stage)
    return result


def sqlite_operation(parent_fd, name, output, timeout, *, live_source_check=False):
    """Bound busy/large database work by wall clock, including quick_check."""
    arguments = json.dumps([parent_fd, name, output, timeout, live_source_check]).encode("utf-8")
    process = subprocess.Popen([sys.executable, "-B", os.path.abspath(__file__), "_sqlite_worker"],
                               stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                               stderr=subprocess.DEVNULL, pass_fds=(parent_fd,))
    try:
        try:
            payload, _ = process.communicate(arguments, timeout=timeout + 0.25)
        except subprocess.TimeoutExpired:
            error = VolumeError("SQLITE_TIMEOUT")
            error.sqlite_diagnostic = sqlite_report(error, "worker-timeout")
            raise error from None
        try:
            require(len(payload) <= 4096, "SQLITE_WORKER_PROTOCOL")
            report = json.loads(payload)
            require(type(report) is dict and set(report) == {"ok", "code", "stage", "sqlite_version",
                    "sqlite_errorcode", "sqlite_errorname"}, "SQLITE_WORKER_PROTOCOL")
            require(type(report["ok"]) is bool and report["code"] in SQLITE_CODES and
                    report["stage"] in SQLITE_STAGES and
                    isinstance(report["sqlite_version"], str) and
                    re.fullmatch(r"[0-9]+\.[0-9]+\.[0-9]+", report["sqlite_version"]) and
                    (report["sqlite_errorcode"] is None or type(report["sqlite_errorcode"]) is int and
                     0 <= report["sqlite_errorcode"] < 2**31) and
                    (report["sqlite_errorname"] is None or isinstance(report["sqlite_errorname"], str)
                     and re.fullmatch(r"SQLITE_[A-Z0-9_]{1,64}", report["sqlite_errorname"])),
                    "SQLITE_WORKER_PROTOCOL")
        except Exception:
            error = VolumeError("SQLITE_WORKER_PROTOCOL")
            error.sqlite_diagnostic = sqlite_report(error, "worker-protocol")
            raise error from None
        if process.returncode != 0 or not report["ok"]:
            error = VolumeError(report["code"] if report["code"] != "SQLITE_OK" else "SQLITE_OPERATION_FAILED")
            error.sqlite_diagnostic = report
            raise error
        return report
    finally:
        if process.poll() is None:
            process.terminate()
            try:
                process.wait(0.5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(0.5)
        if process.stdin is not None:
            process.stdin.close()
        if process.stdout is not None:
            process.stdout.close()


def sqlite_snapshot(parent_fd, name, source_fd, output, timeout):
    """Native online backup, with a narrowly guarded idle-WAL readonly fallback."""
    deadline = time.monotonic() + timeout
    try:
        report = sqlite_operation(parent_fd, name, output, timeout)
        return {**report, "snapshot_method": "native-online-backup"}
    except VolumeError as original:
        detail = original.sqlite_diagnostic
        # Observed source failure: SQLite cannot create idle WAL sidecars on RO
        # mounts. Never use this fallback for schema errors, busy DBs or live WAL.
        if not detail or detail.get("sqlite_errorcode") not in (14, 1288) or detail.get("stage") not in (
                "connect-source", "configure-source", "backup"):
            raise
        if not (os.fstatvfs(source_fd).f_flag & os.ST_RDONLY
                and os.fstatvfs(parent_fd).f_flag & os.ST_RDONLY):
            raise
        source_error = dict(detail)
    before = os.fstat(source_fd)
    parent_before = os.fstat(parent_fd)

    def guard():
        require(time.monotonic() < deadline, "SQLITE_TIMEOUT")
        require(stat.S_ISREG(before.st_mode) and signature(before) == signature(os.fstat(source_fd))
                == signature(os.stat(name, dir_fd=parent_fd, follow_symlinks=False)),
                "SQLITE_FALLBACK_SOURCE_CHANGED")
        for suffix in ("-wal", "-shm", "-journal"):
            try:
                os.stat(name + suffix, dir_fd=parent_fd, follow_symlinks=False)
            except FileNotFoundError:
                continue
            raise VolumeError("SQLITE_FALLBACK_SIDECAR_PRESENT")
        # Also reject create-then-remove sidecars between observations. Unrelated
        # directory churn is conservatively rejected for this fallback only.
        require(signature(parent_before) == signature(os.fstat(parent_fd)),
                "SQLITE_FALLBACK_PARENT_CHANGED")

    guard()
    require(os.pread(source_fd, 16, 0) == SQLITE_HEADER and os.pread(source_fd, 2, 18) == b"\x02\x02",
            "SQLITE_FALLBACK_NOT_IDLE_WAL")
    stage = os.path.dirname(os.path.abspath(output))
    shadow = os.path.join(stage, "idle-wal-source")
    # Remove only files belonging to our failed destination in private scratch.
    for suffix in ("", "-wal", "-shm", "-journal"):
        try:
            os.unlink(output + suffix)
        except FileNotFoundError:
            pass
    copied = hashlib.sha256()
    descriptor = os.open(shadow, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(descriptor, "wb") as destination:
        offset = 0
        while offset < before.st_size:
            guard()
            data = os.pread(source_fd, min(BLOCK, before.st_size - offset), offset)
            require(bool(data), "SQLITE_FALLBACK_SOURCE_CHANGED")
            destination.write(data)
            copied.update(data)
            offset += len(data)
        destination.flush()
        os.fsync(destination.fileno())
    guard()
    # Re-read with the same bounded guards; never silently trust a changed prefix.
    verified = hashlib.sha256()
    offset = 0
    while offset < before.st_size:
        guard()
        data = os.pread(source_fd, min(BLOCK, before.st_size - offset), offset)
        require(bool(data), "SQLITE_FALLBACK_SOURCE_CHANGED")
        verified.update(data)
        offset += len(data)
    require(verified.hexdigest() == copied.hexdigest(), "SQLITE_FALLBACK_SOURCE_CHANGED")
    guard()
    scratch_fd = open_directory(stage)
    try:
        # The shadow lives on writable helper scratch, so SQLite may create its
        # own WAL/SHM here. Source is NEVER opened immutable or made writable.
        report = sqlite_operation(scratch_fd, "idle-wal-source", output,
                                  max(0.01, deadline - time.monotonic()))
    finally:
        os.close(scratch_fd)
    guard()
    return {**report, "snapshot_method": "guarded-idle-wal-scratch-copy",
            "source_errorcode": source_error["sqlite_errorcode"],
            "source_errorname": source_error["sqlite_errorname"],
            "source_errorstage": source_error["stage"]}


class PrefixReader:
    def __init__(self, fd, size):
        self.fd, self.size, self.offset = fd, size, 0
        self.digest = hashlib.sha256()

    def read(self, count=-1):
        remaining = self.size - self.offset
        count = remaining if count < 0 else min(count, remaining)
        if count == 0:
            return b""
        result = os.pread(self.fd, count, self.offset)
        require(bool(result), "SOURCE_SHRANK")
        self.offset += len(result)
        self.digest.update(result)
        return result


def hash_fd(fd, size):
    reader = PrefixReader(fd, size)
    while reader.read(min(BLOCK, size - reader.offset)):
        pass
    return reader.digest.hexdigest()


def tar_info(entry):
    info = tarfile.TarInfo(entry["path"])
    info.mode, info.uid, info.gid = entry["mode"], entry["uid"], entry["gid"]
    info.mtime = entry["mtimeNs"] // 10**9
    if entry["type"] == "dir":
        info.type = tarfile.DIRTYPE
    elif entry["type"] == "symlink":
        info.type, info.linkname = tarfile.SYMTYPE, entry["target"]
    else:
        info.size = entry["size"]
    return info


class Capture:
    def __init__(self, archive, scratch, policy):
        self.archive, self.scratch, self.policy = archive, scratch, policy
        self.entries, self.skipped = [], []
        self.directory_observations = []
        self.total = 0

    def record(self, entry, skipped=False):
        safe_path(entry["path"])
        require(len(self.entries) + len(self.skipped) < self.policy.max_entries, "ENTRY_LIMIT")
        (self.skipped if skipped else self.entries).append(entry)

    def add_atomic_json(self, parent, name, full, fd):
        """Only the reviewed Paseo state file; retain the already pinned inode."""
        require(self.policy.paseo_runtime and full == PASEO_ATOMIC_PATH, "INVALID_ATOMIC_PATH")
        before = os.fstat(fd)
        require(stat.S_ISREG(before.st_mode) and 0 < before.st_size <= 4 * 1024**2
                and stat.S_IMODE(before.st_mode) == 0o600 and before.st_nlink in (0, 1),
                "INVALID_ATOMIC_STATE")
        payload = os.pread(fd, before.st_size, 0)
        require(len(payload) == before.st_size, "ATOMIC_STATE_SHORT_READ")
        try:
            parsed = json.loads(payload, object_pairs_hook=_unique_json)
        except Exception:
            raise VolumeError("INVALID_ATOMIC_JSON") from None
        require(type(parsed) is dict, "INVALID_ATOMIC_JSON")
        digest = hashlib.sha256(payload).hexdigest()
        require(hash_fd(fd, before.st_size) == digest, "ATOMIC_STATE_CONTENT_CHANGED")
        after = os.fstat(fd)
        # unlink by atomic replacement changes ctime/nlink on the old inode;
        # size, mtime, mode, ownership and two full content reads must agree.
        require(identity(before) == identity(after) and before.st_size == after.st_size
                and before.st_mtime_ns == after.st_mtime_ns and
                (before.st_ctime_ns == after.st_ctime_ns or
                 (before.st_nlink == 1 and after.st_nlink == 0)), "ATOMIC_STATE_CONTENT_CHANGED")
        current = os.stat(name, dir_fd=parent, follow_symlinks=False)
        require(stat.S_ISREG(current.st_mode) and current.st_nlink == 1 and
                (current.st_uid, current.st_gid, stat.S_IMODE(current.st_mode)) ==
                (before.st_uid, before.st_gid, 0o600), "ATOMIC_STATE_PATH_CHANGED")
        entry = metadata(full, "file", before)
        entry.update(size=len(payload), sha256=digest, consistency="pinned-atomic-json-not-global",
                     pathReplacedAtCapture=(before.st_dev, before.st_ino) !=
                                          (current.st_dev, current.st_ino))
        require(self.total + len(payload) <= self.policy.max_bytes, "BYTE_LIMIT")
        self.total += len(payload)
        self.archive.addfile(tar_info(entry), io.BytesIO(payload))
        self.record(entry)

    def validate_transient(self, parent, name, owner):
        """Exact native names only; do not hide arbitrary lock-directory files."""
        try:
            s = os.stat(name, dir_fd=parent, follow_symlinks=False)
        except FileNotFoundError:
            return None
        require((s.st_uid, s.st_gid) == (owner.st_uid, owner.st_gid), "TRANSIENT_OWNER_CHANGED")
        if name == "bridge-lifecycle.lock":
            require(stat.S_ISDIR(s.st_mode) and stat.S_IMODE(s.st_mode) == 0o700,
                    "INVALID_TRANSIENT_TYPE")
            try:
                fd = os.open(name, DIR_FLAGS, dir_fd=parent)
            except FileNotFoundError:
                return None
            try:
                for child in os.listdir(fd):
                    require(child == "owner.json" or native_temp(child, "owner.json"),
                            "UNKNOWN_TRANSIENT_CHILD")
                    try:
                        c = os.stat(child, dir_fd=fd, follow_symlinks=False)
                    except FileNotFoundError:
                        continue
                    require(stat.S_ISREG(c.st_mode) and stat.S_IMODE(c.st_mode) == 0o600
                            and c.st_nlink == 1 and (c.st_uid, c.st_gid) == (owner.st_uid, owner.st_gid),
                            "INVALID_TRANSIENT_TYPE")
            finally:
                os.close(fd)
        else:
            require(stat.S_ISREG(s.st_mode) and stat.S_IMODE(s.st_mode) == 0o600
                    and s.st_nlink == 1, "INVALID_TRANSIENT_TYPE")
        return s

    def add_regular(self, parent, name, full, header):
        try:
            return self._add_regular(parent, name, full, header)
        except VolumeError as error:
            error.at_source(full)
            raise

    def _add_regular(self, parent, name, full, header):
        fd = os.open(name, FILE_FLAGS, dir_fd=parent)
        try:
            before = os.fstat(fd)
            require(stat.S_ISREG(before.st_mode), "SOURCE_TYPE_CHANGED")
            require(os.pread(fd, 16, 0) == header, "SOURCE_HEADER_CHANGED")
            is_db = header == SQLITE_HEADER
            entry = metadata(full, "sqlite" if is_db else "file", before)
            if is_db:
                with tempfile.TemporaryDirectory(prefix="sqlite-", dir=self.scratch) as stage:
                    output = os.path.join(stage, "snapshot")
                    diagnostic = sqlite_snapshot(parent, name, fd, output, self.policy.sqlite_timeout)
                    after = os.stat(name, dir_fd=parent, follow_symlinks=False)
                    require(identity(before) == identity(after), "SQLITE_SOURCE_REPLACED")
                    snap = os.open(output, FILE_FLAGS)
                    try:
                        entry.update(consistency="sqlite-online-backup", quickCheck="ok",
                                     snapshotMethod=diagnostic["snapshot_method"],
                                     sqliteVersion=diagnostic["sqlite_version"])
                        if "source_errorcode" in diagnostic:
                            entry["sourceReadonlyError"] = {key: diagnostic[key] for key in
                                ("source_errorcode", "source_errorname", "source_errorstage")}
                        self.add_bytes(snap, os.fstat(snap).st_size, entry)
                    finally:
                        os.close(snap)
                    require(not any(os.path.exists(output + suffix) for suffix in ("-wal", "-shm")),
                            "SQLITE_SNAPSHOT_SIDECAR")
            else:
                append = any(fnmatch.fnmatchcase(full, p) for p in self.policy.append_globs)
                entry["consistency"] = "fixed-prefix-not-atomic" if append else "stable-file"
                self.add_bytes(fd, before.st_size, entry)
                after = os.fstat(fd)
                path_after = os.stat(name, dir_fd=parent, follow_symlinks=False)
                if append:
                    require(identity(before) == identity(after) == identity(path_after)
                            and after.st_size >= before.st_size, "APPEND_SOURCE_REPLACED_OR_SHRANK")
                    require(hash_fd(fd, before.st_size) == entry["sha256"], "APPEND_PREFIX_CHANGED")
                    require(os.fstat(fd).st_size >= before.st_size, "APPEND_SOURCE_SHRANK")
                else:
                    require(signature(before) == signature(after) == signature(path_after),
                            "STABLE_SOURCE_CHANGED")
            self.record(entry)
        finally:
            os.close(fd)

    def add_bytes(self, fd, size, entry):
        require(self.total + size <= self.policy.max_bytes, "BYTE_LIMIT")
        self.total += size
        entry["size"] = size
        reader = PrefixReader(fd, size)
        self.archive.addfile(tar_info(entry), reader)
        require(reader.offset == size, "SOURCE_SHORT_READ")
        entry["sha256"] = reader.digest.hexdigest()

    def directory(self, fd, prefix):
        pinned = {}
        journal = None
        try:
            if self.policy.paseo_runtime and prefix == "od/paseo":
                journal = PaseoJournal(fd)
            return self._directory(fd, prefix, pinned, journal)
        except VolumeError as error:
            error.at_source(prefix)
            raise
        finally:
            if journal is not None:
                journal.close()
            for descriptor in pinned.values():
                os.close(descriptor)

    def _directory(self, fd, prefix, pinned, journal=None):
        initial = os.fstat(fd)
        entry = metadata(prefix, "dir", initial)
        self.record(entry)
        self.archive.addfile(tar_info(entry))
        names = sorted(os.listdir(fd))
        atomic_enabled = self.policy.paseo_runtime and prefix == "od/paseo" and "state.json" in names
        transient_names = {n for n in names if paseo_transient(prefix, n, atomic_enabled)}
        # First identify databases by bytes, including extensionless databases.
        scanned, sqlite_names, vanished_sidecars = {}, set(), set()
        # A database basename is shorter than either sidecar name, so detect it
        # first. Do not open WAL/SHM files just to read their headers.
        for name in sorted(names, key=lambda n: (len(n), n)):
            if name in transient_names:
                scanned[name] = (self.validate_transient(fd, name, initial), None)
                continue
            is_sidecar = name.endswith(("-wal", "-shm")) and name[:-4] in sqlite_names
            try:
                s = os.stat(name, dir_fd=fd, follow_symlinks=False)
            except FileNotFoundError:
                if not is_sidecar:
                    raise VolumeError("DIRECTORY_CONTENT_CHANGED") from None
                scanned[name] = (None, None)
                vanished_sidecars.add(name)
                continue
            header = None
            if is_sidecar:
                require(stat.S_ISREG(s.st_mode), "SQLITE_SIDECAR_TYPE")
            elif stat.S_ISREG(s.st_mode):
                child = os.open(name, FILE_FLAGS, dir_fd=fd)
                try:
                    if atomic_enabled and name == "state.json":
                        s = os.fstat(child)
                        require(stat.S_ISREG(s.st_mode), "INVALID_ATOMIC_STATE")
                        pinned[name] = os.dup(child)
                    else:
                        require(identity(s) == identity(os.fstat(child)), "SOURCE_REPLACED")
                    header = os.pread(child, 16, 0)
                finally:
                    os.close(child)
                if header == SQLITE_HEADER:
                    sqlite_names.add(name)
            scanned[name] = (s, header)
        sidecars = {base + suffix for base in sqlite_names for suffix in ("-wal", "-shm")}
        for name in names:
            full = prefix + "/" + name
            safe_path(full)
            s, header = scanned[name]
            if name in transient_names:
                self.record({"path": full, "type": paseo_transient(prefix, name, True)}, True)
                continue
            if name in sidecars:
                # Only a successful native database backup can reach the end of
                # this traversal. WAL/SHM are never independently restored.
                self.record({"path": full, "type": "sqlite-sidecar", "kind": name[-3:]}, True)
                continue
            try:
                current = os.stat(name, dir_fd=fd, follow_symlinks=False)
            except FileNotFoundError:
                raise VolumeError("DIRECTORY_CONTENT_CHANGED") from None
            if name in pinned:
                self.add_atomic_json(fd, name, full, pinned[name])
                continue
            require(identity(s) == identity(current), "SOURCE_REPLACED")
            if stat.S_ISDIR(s.st_mode):
                child = os.open(name, DIR_FLAGS, dir_fd=fd)
                try:
                    require(identity(s) == identity(os.fstat(child)), "SOURCE_REPLACED")
                    self.directory(child, full)
                finally:
                    os.close(child)
            elif stat.S_ISREG(s.st_mode):
                self.add_regular(fd, name, full, header)
            elif stat.S_ISLNK(s.st_mode):
                link = metadata(full, "symlink", s)
                link["target"] = os.readlink(name, dir_fd=fd)
                require(signature(s) == signature(os.stat(name, dir_fd=fd, follow_symlinks=False)),
                        "SYMLINK_CHANGED")
                self.record(link)
                self.archive.addfile(tar_info(link))
            elif stat.S_ISSOCK(s.st_mode) or stat.S_ISFIFO(s.st_mode):
                self.record({"path": full, "type": "socket" if stat.S_ISSOCK(s.st_mode) else "fifo"}, True)
            else:
                raise VolumeError("UNSUPPORTED_SPECIAL_FILE")
        # Permit only observed lifecycle changes of known, successfully backed-up
        # SQLite sidecars. Protected names and inode/ownership/mode identities
        # must match, even if the directory timestamp has been restored.
        final_names = set(os.listdir(fd))
        original_names = set(names)
        final_transients = {n for n in final_names if paseo_transient(prefix, n, atomic_enabled)}
        protected = original_names - sidecars - transient_names
        require(protected == final_names - sidecars - final_transients, "DIRECTORY_CONTENT_CHANGED")
        atomic_replacements = []
        for name in protected:
            try:
                current = os.stat(name, dir_fd=fd, follow_symlinks=False)
            except FileNotFoundError:
                raise VolumeError("DIRECTORY_CONTENT_CHANGED") from None
            if name in pinned:
                old = scanned[name][0]
                require(stat.S_ISREG(current.st_mode) and current.st_nlink == 1 and
                        (current.st_uid, current.st_gid, stat.S_IMODE(current.st_mode)) ==
                        (old.st_uid, old.st_gid, 0o600), "ATOMIC_STATE_PATH_CHANGED")
                if (old.st_dev, old.st_ino) != (current.st_dev, current.st_ino):
                    atomic_replacements.append(prefix + "/" + name)
            else:
                require(identity(scanned[name][0]) == identity(current), "DIRECTORY_CONTENT_CHANGED")
        transient_observations = []
        for name in sorted(transient_names | final_transients):
            current = self.validate_transient(fd, name, initial)
            old = scanned.get(name, (None, None))[0]
            if name not in transient_names:
                self.record({"path": prefix + "/" + name,
                             "type": paseo_transient(prefix, name, True)}, True)
            transient_observations.append({"path": prefix + "/" + name,
                "listedAtStart": name in transient_names, "presentAtEnd": current is not None,
                "lifecycleChangeObserved": (name in transient_names) != (current is not None)
                or (old is not None and current is not None and
                    (old.st_dev, old.st_ino) != (current.st_dev, current.st_ino))})
        observed = []
        for name in sorted(sidecars & (original_names | final_names)):
            old = scanned.get(name, (None, None))[0]
            try:
                new = os.stat(name, dir_fd=fd, follow_symlinks=False)
            except FileNotFoundError:
                new = None
                if name in final_names:
                    vanished_sidecars.add(name)
            require(new is None or stat.S_ISREG(new.st_mode), "SQLITE_SIDECAR_TYPE")
            if name not in original_names:
                self.record({"path": prefix + "/" + name, "type": "sqlite-sidecar",
                             "kind": name[-3:]}, True)
            changed = ((name in original_names) != (new is not None) or
                       name in vanished_sidecars or
                       (old is not None and new is not None and
                        (old.st_dev, old.st_ino) != (new.st_dev, new.st_ino)))
            observed.append({"path": prefix + "/" + name,
                             "listedAtStart": name in original_names,
                             "presentAtEnd": new is not None,
                             "lifecycleChangeObserved": bool(changed)})
        final = os.fstat(fd)
        events = journal.events() if journal is not None else []
        event_churn = validate_paseo_events(events) if atomic_enabled else False
        require(atomic_enabled or not events, "DIRECTORY_CHANGED")
        require(identity(initial) == identity(final), "DIRECTORY_METADATA_CHANGED")
        for guard in (protected & PASEO_GUARDS) if atomic_enabled else ():
            s = os.stat(guard, dir_fd=fd, follow_symlinks=False)
            require(identity(s) == identity(scanned[guard][0]),
                    "DIRECTORY_JOURNAL_UNEXPECTED_GUARD_CHANGE")
            require(stat.S_ISREG(s.st_mode) and stat.S_IMODE(s.st_mode) == 0o600 and
                    s.st_nlink == 1 and (s.st_uid, s.st_gid) == (initial.st_uid, initial.st_gid),
                    "DIRECTORY_JOURNAL_UNEXPECTED_GUARD_CHANGE")
        if signature(initial) != signature(final):
            # Identical endpoint lists with no observed sidecar churn cannot
            # prove a transient unrelated entry was safe; reject that ambiguity.
            require(any(e["lifecycleChangeObserved"] for e in observed + transient_observations)
                    or atomic_replacements or event_churn, "DIRECTORY_CHANGED")
            observation = {"path": prefix,
                "policy": "only-known-sqlite-sidecar-lifecycle",
                "protectedNamesSha256": hashlib.sha256(json.dumps(sorted(protected),
                    ensure_ascii=True, separators=(",", ":")).encode("ascii")).hexdigest(),
                "sidecars": observed}
            if atomic_replacements or transient_observations or event_churn:
                observation.update(policy="known-sqlite-sidecars-and-paseo-runtime",
                                   atomicReplacements=atomic_replacements,
                                   transients=transient_observations)
                if events:
                    observation["eventJournal"] = events
            self.directory_observations.append(observation)


def _disjoint(*paths):
    paths = [Path(os.path.abspath(p)) for p in paths]
    for i, path in enumerate(paths):
        for other in paths[i + 1:]:
            require(path != other and path not in other.parents and other not in path.parents,
                    "OVERLAPPING_PATHS")


def summary(manifest):
    counts = Counter(e["type"] for e in manifest["entries"])
    skipped = Counter(e["type"] for e in manifest["skipped"])
    return {"entries": len(manifest["entries"]), "files": counts["file"],
            "sqlite": counts["sqlite"], "directories": counts["dir"],
            "symlinks": counts["symlink"], "bytes": manifest["bytes"],
            "appendPrefixes": sum(e.get("consistency") == "fixed-prefix-not-atomic"
                                  for e in manifest["entries"]),
            "skippedSockets": skipped["socket"], "skippedFifos": skipped["fifo"],
            "skippedSqliteSidecars": skipped["sqlite-sidecar"],
            "directoriesWithSqliteSidecarChurn": sum(any(s["lifecycleChangeObserved"] for s in
                o.get("sidecars", [])) for o in manifest.get("directoryObservations", [])),
            "pinnedAtomicJson": sum(e.get("consistency") == "pinned-atomic-json-not-global"
                                    for e in manifest["entries"]),
            "skippedPaseoAtomicTemps": skipped["paseo-atomic-temp"],
            "skippedPaseoRuntimeLocks": skipped["paseo-runtime-lock"],
            "directoriesWithPaseoRuntimeChurn": sum(o.get("policy") ==
                "known-sqlite-sidecars-and-paseo-runtime" for o in manifest.get("directoryObservations", [])),
            "sqliteIdleWalScratchCopies": sum(e.get("snapshotMethod") == "guarded-idle-wal-scratch-copy"
                                               for e in manifest["entries"])}


def backup(od_root, home_root, target, scratch_root, public_key_pem, context,
           policy=Policy()):
    """Create a NEW encrypted tar. Any mutable stable-file check aborts capture.

    Sources must be read-only mounts. Scratch needs room for the largest SQLite
    snapshot; target needs room for uncompressed tar plus envelope overhead.
    """
    policy.validate()
    _disjoint(od_root, home_root, scratch_root, target)
    roots = {"od": open_directory(od_root)}
    parent = temporary = writer = None
    try:
        roots["home"] = open_directory(home_root)
        require((os.fstat(roots["od"]).st_dev, os.fstat(roots["od"]).st_ino) !=
                (os.fstat(roots["home"]).st_dev, os.fstat(roots["home"]).st_ino), "SAME_ROOT")
        scratch_fd = open_directory(scratch_root)
        os.close(scratch_fd)
        parent_path, name = os.path.split(os.path.abspath(target))
        parent = open_directory(parent_path)
        require(name not in os.listdir(parent), "TARGET_EXISTS")
        temporary = ".od-volume-" + secrets.token_hex(16)
        output_fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                            0o600, dir_fd=parent)
        with os.fdopen(output_fd, "wb") as output, tempfile.TemporaryDirectory(
                prefix="od-capture-", dir=scratch_root) as stage:
            os.chmod(stage, 0o700)  # Clear an inherited parent setgid bit.
            os.fchmod(output.fileno(), 0o600)
            writer = crypto.CipherWriter(output, public_key_pem, context, policy.envelope_limits())
            with tarfile.open(fileobj=writer, mode="w|", format=tarfile.PAX_FORMAT) as archive:
                capture = Capture(archive, stage, policy)
                for label, fd in roots.items():
                    capture.directory(fd, label)
                manifest = {"version": 1, "consistency": "per-file-and-per-sqlite-not-global",
                            "entries": capture.entries, "skipped": capture.skipped,
                            "directoryObservations": capture.directory_observations,
                            "bytes": capture.total}
                payload = json.dumps(manifest, sort_keys=True, separators=(",", ":"),
                                     ensure_ascii=True).encode("ascii")
                require(len(payload) <= policy.manifest_bytes, "MANIFEST_LIMIT")
                info = tarfile.TarInfo(MANIFEST)
                info.size, info.mode = len(payload), 0o600
                archive.addfile(info, io.BytesIO(payload))
            receipt = writer.finish()
            output.flush()
            os.fsync(output.fileno())
        os.link(temporary, name, src_dir_fd=parent, dst_dir_fd=parent, follow_symlinks=False)
        os.unlink(temporary, dir_fd=parent)
        temporary = None
        os.fsync(parent)
        return {"counts": summary(manifest), "envelopeBytes": receipt["envelopeBytes"],
                "envelopeSha256": receipt["envelopeSha256"],
                "manifestSha256": hashlib.sha256(payload).hexdigest()}
    except BaseException:
        if writer is not None:
            writer.abort()
        raise
    finally:
        if temporary is not None:
            os.unlink(temporary, dir_fd=parent)
        if parent is not None:
            os.close(parent)
        for fd in roots.values():
            os.close(fd)


def _unique_json(pairs):
    result = {}
    for key, value in pairs:
        require(key not in result, "DUPLICATE_JSON_KEY")
        result[key] = value
    return result


def guard_tar_headers(archive_path, policy):
    """Bound extended headers before tarfile can allocate their declared size."""
    with open(archive_path, "rb") as stream:
        length = os.fstat(stream.fileno()).st_size
        require(length <= policy.envelope_limits().max_plaintext_bytes, "TAR_LIMIT")
        headers = 0
        while True:
            raw = stream.read(512)
            require(len(raw) == 512, "TRUNCATED_TAR")
            if raw == b"\x00" * 512:
                require(stream.read(512) == b"\x00" * 512, "TRUNCATED_TAR")
                while chunk := stream.read(BLOCK):
                    require(not any(chunk), "TRAILING_TAR_DATA")
                return
            member = tarfile.TarInfo.frombuf(raw, "utf-8", "strict")
            headers += 1
            require(headers <= 2 * policy.max_entries + 4, "ENTRY_LIMIT")
            require(member.type in (tarfile.REGTYPE, tarfile.AREGTYPE, tarfile.DIRTYPE,
                                    tarfile.SYMTYPE, tarfile.XHDTYPE), "UNSAFE_MEMBER_TYPE")
            if member.type == tarfile.XHDTYPE:
                require(0 <= member.size <= 64 * 1024, "EXTENDED_HEADER_LIMIT")
            else:
                require(0 <= member.size <= max(policy.max_bytes, policy.manifest_bytes),
                        "BYTE_LIMIT")
            end = stream.tell() + ((member.size + 511) // 512) * 512
            require(end <= length - 1024, "TRUNCATED_TAR")
            stream.seek(end)


def preflight(archive_path, policy=Policy()):
    """Validate authenticated uncompressed tar completely; never extract."""
    policy.validate()
    guard_tar_headers(archive_path, policy)
    found, manifest, manifest_hash, total = {}, None, None, 0
    with tarfile.open(archive_path, "r:") as archive:
        for member in archive:
            require(manifest is None, "MEMBER_AFTER_MANIFEST")
            require(member.type in (tarfile.REGTYPE, tarfile.AREGTYPE, tarfile.DIRTYPE,
                                    tarfile.SYMTYPE) and member.sparse is None, "UNSAFE_MEMBER_TYPE")
            if member.name == MANIFEST:
                require(member.isfile() and 0 < member.size <= policy.manifest_bytes, "MANIFEST_LIMIT")
                stream = archive.extractfile(member)
                payload = stream.read(policy.manifest_bytes + 1)
                require(len(payload) == member.size, "MANIFEST_SHORT_READ")
                manifest = json.loads(payload, object_pairs_hook=_unique_json)
                manifest_hash = hashlib.sha256(payload).hexdigest()
                continue
            safe_path(member.name)
            require(member.name not in found and len(found) < policy.max_entries, "DUPLICATE_OR_LIMIT")
            require(type(member.size) is int and member.size >= 0 and
                    total + member.size <= policy.max_bytes, "BYTE_LIMIT")
            require(0 <= member.mode <= 0o7777 and 0 <= member.uid < 2**32 - 1
                    and 0 <= member.gid < 2**32 - 1, "INVALID_METADATA")
            record = {"member": member, "type": "dir" if member.isdir() else
                      "symlink" if member.issym() else "file"}
            if member.isfile():
                digest, size = hashlib.sha256(), 0
                stream = archive.extractfile(member)
                while chunk := stream.read(BLOCK):
                    digest.update(chunk)
                    size += len(chunk)
                require(size == member.size, "TAR_SHORT_READ")
                record["sha256"] = digest.hexdigest()
                total += size
            else:
                require(member.size == 0, "NONFILE_PAYLOAD")
            if member.issym():
                require(member.linkname and "\x00" not in member.linkname and
                        len(member.linkname.encode("utf-8")) <= 4096, "INVALID_LINK")
            found[member.name] = record
    require(type(manifest) is dict and manifest.get("version") == 1 and
            manifest.get("consistency") == "per-file-and-per-sqlite-not-global" and
            type(manifest.get("entries")) is list and type(manifest.get("skipped")) is list,
            "INVALID_MANIFEST")
    entries = {}
    require(len(manifest["entries"]) + len(manifest["skipped"]) <= policy.max_entries,
            "ENTRY_LIMIT")
    for entry in manifest["entries"]:
        require(type(entry) is dict, "INVALID_MANIFEST_ENTRY")
        name = entry.get("path")
        parts = safe_path(name)
        require(name in found and name not in entries, "MANIFEST_MEMBERS_MISMATCH")
        actual, member = found[name], found[name]["member"]
        require(entry.get("type") in ("dir", "symlink", "file", "sqlite"), "INVALID_ENTRY_TYPE")
        require(actual["type"] == ("file" if entry["type"] == "sqlite" else entry["type"]),
                "MANIFEST_TYPE_MISMATCH")
        for field in ("mode", "uid", "gid", "mtimeNs"):
            require(type(entry.get(field)) is int, "INVALID_METADATA")
        require(entry["mode"] == member.mode and entry["uid"] == member.uid and
                entry["gid"] == member.gid and entry["mtimeNs"] // 10**9 == member.mtime,
                "MANIFEST_METADATA_MISMATCH")
        if actual["type"] == "file":
            require(type(entry.get("size")) is int and entry["size"] == member.size and
                    entry.get("sha256") == actual["sha256"], "MANIFEST_HASH_MISMATCH")
            require(entry.get("consistency") in ("stable-file", "fixed-prefix-not-atomic",
                    "sqlite-online-backup", "pinned-atomic-json-not-global"), "INVALID_CONSISTENCY")
            if entry.get("consistency") == "pinned-atomic-json-not-global":
                require(name == PASEO_ATOMIC_PATH and entry["type"] == "file" and
                        entry["mode"] == 0o600 and 0 < entry["size"] <= 4 * 1024**2
                        and type(entry.get("pathReplacedAtCapture")) is bool,
                        "INVALID_ATOMIC_MANIFEST")
            if entry["type"] == "sqlite":
                require(entry.get("quickCheck") == "ok" and
                        entry["consistency"] == "sqlite-online-backup", "INVALID_SQLITE_MANIFEST")
        elif actual["type"] == "symlink":
            require(entry.get("target") == member.linkname and member.mode == 0o777,
                    "MANIFEST_LINK_MISMATCH")
        require(len(parts) > 1 or actual["type"] == "dir", "INVALID_ROOT_TYPE")
        entries[name] = entry
    require(set(entries) == set(found) and {"od", "home"} <= set(entries)
            and manifest.get("bytes") == total, "MANIFEST_MEMBERS_MISMATCH")
    seen_skipped = set()
    for skipped in manifest["skipped"]:
        require(type(skipped) is dict and skipped.get("type") in
                ("socket", "fifo", "sqlite-sidecar", "paseo-atomic-temp", "paseo-runtime-lock"),
                "INVALID_SKIPPED_ENTRY")
        safe_path(skipped.get("path"))
        require(skipped["path"] not in entries and skipped["path"] not in seen_skipped,
                "INVALID_SKIPPED_ENTRY")
        seen_skipped.add(skipped["path"])
        if skipped["type"].startswith("paseo-"):
            parent, leaf = skipped["path"].rsplit("/", 1)
            require(paseo_transient(parent, leaf, True) == skipped["type"] and
                    PASEO_ATOMIC_PATH in entries and entries[PASEO_ATOMIC_PATH].get("consistency") ==
                    "pinned-atomic-json-not-global", "INVALID_PASEO_TRANSIENT")
    for name in entries:
        parts = name.split("/")
        for i in range(1, len(parts)):
            parent = "/".join(parts[:i])
            require(parent in entries and entries[parent]["type"] == "dir", "UNSAFE_MEMBER_ANCESTOR")
    observations = manifest.get("directoryObservations", [])
    require(type(observations) is list and len(observations) <= len(entries), "INVALID_DIRECTORY_AUDIT")
    seen_observations = set()
    for observation in observations:
        require(type(observation) is dict, "INVALID_DIRECTORY_AUDIT")
        path = observation.get("path")
        require(path in entries and entries[path]["type"] == "dir" and
                path not in seen_observations and observation.get("policy") in
                ("only-known-sqlite-sidecar-lifecycle", "known-sqlite-sidecars-and-paseo-runtime"),
                "INVALID_DIRECTORY_AUDIT")
        seen_observations.add(path)
        sidecars = observation.get("sidecars")
        require(type(sidecars) is list and 0 <= len(sidecars) <= policy.max_entries,
                "INVALID_DIRECTORY_AUDIT")
        changed = False
        for sidecar in sidecars:
            require(type(sidecar) is dict and type(sidecar.get("path")) is str,
                    "INVALID_DIRECTORY_AUDIT")
            name = sidecar["path"]
            require(name.rsplit("/", 1)[0] == path and name.endswith(("-wal", "-shm"))
                    and name[:-4] in entries and entries[name[:-4]]["type"] == "sqlite"
                    and name in seen_skipped, "INVALID_DIRECTORY_AUDIT")
            require(all(type(sidecar.get(k)) is bool for k in
                        ("listedAtStart", "presentAtEnd", "lifecycleChangeObserved")),
                    "INVALID_DIRECTORY_AUDIT")
            changed = changed or sidecar["lifecycleChangeObserved"]
        if observation["policy"] == "known-sqlite-sidecars-and-paseo-runtime":
            require(path == "od/paseo" and PASEO_ATOMIC_PATH in entries and
                    entries[PASEO_ATOMIC_PATH].get("consistency") == "pinned-atomic-json-not-global",
                    "INVALID_DIRECTORY_AUDIT")
            replacements = observation.get("atomicReplacements")
            transients = observation.get("transients")
            require(type(replacements) is list and replacements in ([], [PASEO_ATOMIC_PATH]) and
                    type(transients) is list and len(transients) <= policy.max_entries,
                    "INVALID_DIRECTORY_AUDIT")
            changed = changed or bool(replacements)
            if "eventJournal" in observation:
                changed = validate_paseo_events(observation["eventJournal"]) or changed
            for item in transients:
                require(type(item) is dict and type(item.get("path")) is str,
                        "INVALID_DIRECTORY_AUDIT")
                parent, leaf = item["path"].rsplit("/", 1)
                require(paseo_transient(parent, leaf, True) is not None and item["path"] in seen_skipped
                        and all(type(item.get(k)) is bool for k in
                            ("listedAtStart", "presentAtEnd", "lifecycleChangeObserved")),
                        "INVALID_DIRECTORY_AUDIT")
                changed = changed or item["lifecycleChangeObserved"]
        require(changed, "INVALID_DIRECTORY_AUDIT")
    return manifest, found, manifest_hash


@contextmanager
def parent_at(roots, name):
    parts = safe_path(name)
    require(len(parts) > 1, "ROOT_HAS_NO_PARENT")
    fd = os.dup(roots[parts[0]])
    try:
        for component in parts[1:-1]:
            child = os.open(component, DIR_FLAGS, dir_fd=fd)
            os.close(fd)
            fd = child
        yield fd, parts[-1]
    finally:
        os.close(fd)


def set_fd_metadata(fd, entry):
    s = os.fstat(fd)
    if (s.st_uid, s.st_gid) != (entry["uid"], entry["gid"]):
        os.fchown(fd, entry["uid"], entry["gid"])
    os.fchmod(fd, entry["mode"])
    os.utime(fd, ns=(entry["mtimeNs"], entry["mtimeNs"]))
    s = os.fstat(fd)
    if (s.st_uid, s.st_gid, stat.S_IMODE(s.st_mode)) != (entry["uid"], entry["gid"], entry["mode"]):
        error=VolumeError("METADATA_RESTORE_FAILED")
        error.private_context={"scope":"restore-volume","path":entry["path"],
            "expectedMode":entry["mode"],"actualMode":stat.S_IMODE(s.st_mode)}
        raise error


def extract_verified_tar(archive_path, od_root, home_root, policy=Policy()):
    """Internal/reusable offline tar restore; caller must authenticate envelope.

    Full tar preflight occurs before touching fresh target contents. Destination
    roots must already exist and be empty; root-owned helper and exclusive PVCs
    are required to preserve arbitrary uid/gid and prevent concurrent writers.
    """
    manifest, members, manifest_hash = preflight(archive_path, policy)
    _disjoint(od_root, home_root, archive_path)
    roots, created, originals = {}, [], {}
    try:
        for label, path in (("od", od_root), ("home", home_root)):
            roots[label] = open_directory(path)
            require(not os.listdir(roots[label]), "DESTINATION_NOT_FRESH")
            originals[label] = metadata(label, "dir", os.fstat(roots[label]))
        require((os.fstat(roots["od"]).st_dev, os.fstat(roots["od"]).st_ino) !=
                (os.fstat(roots["home"]).st_dev, os.fstat(roots["home"]).st_ino), "SAME_ROOT")
        directories = sorted((e for e in manifest["entries"] if e["type"] == "dir"),
                             key=lambda e: (e["path"].count("/"), e["path"]))
        for entry in directories:
            if "/" not in entry["path"]:
                continue
            with parent_at(roots, entry["path"]) as (parent, name):
                os.mkdir(name, 0o700, dir_fd=parent)
                created.append((entry["path"], "dir"))
        with tarfile.open(archive_path, "r:") as archive:
            for entry in manifest["entries"]:
                if entry["type"] not in ("file", "sqlite"):
                    continue
                with parent_at(roots, entry["path"]) as (parent, name):
                    fd = os.open(name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                                 0o600, dir_fd=parent)
                    created.append((entry["path"], "file"))
                    with os.fdopen(fd, "wb") as output:
                        source = archive.extractfile(members[entry["path"]]["member"])
                        digest, written = hashlib.sha256(), 0
                        while chunk := source.read(BLOCK):
                            output.write(chunk)
                            digest.update(chunk)
                            written += len(chunk)
                        require(written == entry["size"] and digest.hexdigest() == entry["sha256"],
                                "RESTORE_HASH_MISMATCH")
                        output.flush()
                        os.fsync(output.fileno())
                        if entry["type"] == "sqlite":
                            sqlite_operation(parent, name, None, policy.sqlite_timeout)
                        set_fd_metadata(output.fileno(), entry)
        # Links are created last, and can never be a parent of another member.
        for entry in manifest["entries"]:
            if entry["type"] != "symlink":
                continue
            with parent_at(roots, entry["path"]) as (parent, name):
                os.symlink(entry["target"], name, dir_fd=parent)
                created.append((entry["path"], "symlink"))
                s = os.stat(name, dir_fd=parent, follow_symlinks=False)
                if (s.st_uid, s.st_gid) != (entry["uid"], entry["gid"]):
                    os.chown(name, entry["uid"], entry["gid"], dir_fd=parent, follow_symlinks=False)
                os.utime(name, ns=(entry["mtimeNs"], entry["mtimeNs"]), dir_fd=parent,
                         follow_symlinks=False)
                s = os.stat(name, dir_fd=parent, follow_symlinks=False)
                require((s.st_uid, s.st_gid, stat.S_IMODE(s.st_mode)) ==
                        (entry["uid"], entry["gid"], entry["mode"]), "METADATA_RESTORE_FAILED")
        for entry in reversed(directories):
            if "/" not in entry["path"]:
                fd = os.dup(roots[entry["path"]])
            else:
                with parent_at(roots, entry["path"]) as (parent, name):
                    fd = os.open(name, DIR_FLAGS, dir_fd=parent)
            try:
                set_fd_metadata(fd, entry)
                os.fsync(fd)
            finally:
                os.close(fd)
        return {"counts": summary(manifest), "manifestSha256": manifest_hash}
    except BaseException:
        # Best effort rollback removes ONLY entries created by this invocation.
        # On filesystem failure the caller must discard the failed fresh PVCs.
        for label, entry in originals.items():
            try:
                os.fchmod(roots[label], 0o700)
            except OSError:
                pass
        for name, kind in reversed(created):
            try:
                with parent_at(roots, name) as (parent, leaf):
                    if kind == "dir":
                        os.rmdir(leaf, dir_fd=parent)
                    else:
                        os.unlink(leaf, dir_fd=parent)
            except OSError:
                pass
        for label, entry in originals.items():
            try:
                set_fd_metadata(roots[label], entry)
            except OSError:
                pass
        raise
    finally:
        for fd in roots.values():
            os.close(fd)


def restore(source, od_root, home_root, scratch_root, private_key, context, policy=Policy()):
    """Authenticate ALL encrypted bytes before preflight or destination writes.

    private_key is protected PEM bytes or a key path accepted by backup_envelope.
    Scratch holds the full plaintext tar temporarily in a new mode-0700 directory.
    """
    policy.validate()
    _disjoint(source, od_root, home_root, scratch_root)
    scratch_fd = open_directory(scratch_root)
    os.close(scratch_fd)
    source_parent, name = os.path.split(os.path.abspath(source))
    parent = open_directory(source_parent)
    try:
        fd = os.open(name, FILE_FLAGS, dir_fd=parent)
        with os.fdopen(fd, "rb") as input_stream, tempfile.TemporaryDirectory(
                prefix="od-restore-", dir=scratch_root) as stage:
            os.chmod(stage, 0o700)
            require(stat.S_ISREG(os.fstat(input_stream.fileno()).st_mode), "INVALID_ENVELOPE_FILE")
            archive_path = os.path.join(stage, "authenticated.tar")
            receipt = crypto.decrypt_to_path(input_stream, archive_path, private_key, context,
                                             policy.envelope_limits())
            result = extract_verified_tar(archive_path, od_root, home_root, policy)
            result["envelopeSha256"] = receipt["envelopeSha256"]
            result["envelopeBytes"] = receipt["envelopeBytes"]
            return result
    finally:
        os.close(parent)


class Parser(argparse.ArgumentParser):
    def error(self, message):
        self.exit(2, "od-volume: invalid arguments\n")


def main(argv=None):
    parser = Parser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True, parser_class=Parser)
    for command in ("backup", "restore"):
        p = sub.add_parser(command)
        p.add_argument("--od-root", required=True)
        p.add_argument("--home-root", required=True)
        p.add_argument("--scratch", required=True)
        p.add_argument("--identity", required=True)
        p.add_argument("--purpose", default="od-volumes")
        p.add_argument("--run-id", required=True)
        p.add_argument("--max-bytes", type=int, default=16 * 1024**3)
        p.add_argument("--max-entries", type=int, default=200_000)
        p.add_argument("--sqlite-timeout", type=float, default=60)
        if command == "backup":
            p.add_argument("--target", required=True)
            p.add_argument("--public-key", required=True)
            p.add_argument("--append-glob", action="append", default=[])
            p.add_argument("--paseo-runtime", action="store_true")
        else:
            p.add_argument("--source", required=True)
            p.add_argument("--private-key", required=True)
    a = parser.parse_args(argv)
    try:
        policy = Policy(max_bytes=a.max_bytes, max_entries=a.max_entries,
                        sqlite_timeout=a.sqlite_timeout,
                        append_globs=tuple(getattr(a, "append_glob", ())),
                        paseo_runtime=getattr(a, "paseo_runtime", False))
        context = crypto.Context(a.identity, a.purpose, a.run_id)
        if a.command == "backup":
            result = backup(a.od_root, a.home_root, a.target, a.scratch,
                            crypto.read_public_key(a.public_key), context, policy)
        else:
            result = restore(a.source, a.od_root, a.home_root, a.scratch,
                             a.private_key, context, policy)
        print(json.dumps(result, sort_keys=True))
        return 0
    except (Exception, KeyboardInterrupt):
        sys.stderr.write("od-volume: operation failed\n")
        return 1


if __name__ == "__main__":
    if sys.argv[1:] == ["_sqlite_worker"]:
        try:
            arguments = json.loads(sys.stdin.buffer.read(16384))
            result = _sqlite_worker(*arguments)
            sys.stdout.write(json.dumps(result, sort_keys=True) + "\n")
            raise SystemExit(0 if result["ok"] else 1)
        except Exception:
            raise SystemExit(1) from None
    raise SystemExit(main())

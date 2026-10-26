#!/usr/bin/env python3
"""Guarded full UI tree restore, Python 3.11+ on Linux; no external dependencies.

    receipt = restore_archive(archive, trusted_sha256, fresh_destination)
    python3 safe_restore.py archive.tar.gz SHA256 /private/existing/parent/new-tree

The caller supplies a digest from a trusted receipt, not from untrusted input.
The existing parent must be trusted/private and must not be renamed concurrently.
No active tree is edited. No archive owners, permissions or timestamps are applied.
The receipt includes the complete independently reread file inventory; CLI output
omits that large map unless --inventory is supplied. This restores UI files only.

Limits include all logical members (directories too) and physical tar metadata.
Publication requires Linux renameat2(RENAME_NOREPLACE); unsupported systems fail
closed. Atomic visibility is guaranteed, not power-loss durability of publication.
"""
import argparse
import ctypes
import errno
import gzip
import hashlib
import io
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import stat
import sys
import tarfile
import uuid
import zlib

MAX_COMPRESSED_BYTES = 32 * 1024 * 1024
MAX_EXPANDED_BYTES = 96 * 1024 * 1024
MAX_MEMBERS = 2000
MAX_FILE_BYTES = 16 * 1024 * 1024
MAX_PATH_DEPTH = 64
# Bound headers, PAX metadata, padding and gzip bombs independently of file sizes.
MAX_TAR_BYTES = MAX_EXPANDED_BYTES + MAX_MEMBERS * 4096 + 10240
DIR_FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC


class RestoreError(Exception):
    """Restore refused; no destination was published by this call."""


class ArchiveError(RestoreError):
    pass


class DestinationError(RestoreError):
    pass


class LimitExceeded(ArchiveError):
    pass


def _verified_bytes(archive, expected):
    if not isinstance(expected, str) or not re.fullmatch(r'[0-9a-fA-F]{64}', expected):
        raise ArchiveError('INVALID_EXPECTED_SHA256')
    fd = os.open(archive, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC)
    with os.fdopen(fd, 'rb') as source:
        info = os.fstat(source.fileno())
        if not stat.S_ISREG(info.st_mode):
            raise ArchiveError('ARCHIVE_NOT_REGULAR')
        if info.st_size > MAX_COMPRESSED_BYTES:
            raise LimitExceeded('COMPRESSED_LIMIT')
        data = source.read(MAX_COMPRESSED_BYTES + 1)
    if len(data) > MAX_COMPRESSED_BYTES:
        raise LimitExceeded('COMPRESSED_LIMIT')
    if hashlib.sha256(data).hexdigest() != expected.lower():
        raise ArchiveError('ARCHIVE_SHA256_MISMATCH')
    try:
        with gzip.GzipFile(fileobj=io.BytesIO(data)) as compressed:
            raw = compressed.read(MAX_TAR_BYTES + 1)
    except (OSError, EOFError, zlib.error) as exc:
        raise ArchiveError('INVALID_OR_TRUNCATED_GZIP') from exc
    if len(raw) > MAX_TAR_BYTES:
        raise LimitExceeded('TAR_STREAM_LIMIT')
    return raw


def _canonical_name(name):
    path = PurePosixPath(name)
    if (not name or name == '.' or path.is_absolute() or '..' in path.parts
            or path.as_posix() != name or '\\' in name or ':' in name
            or any(ord(c) < 32 or ord(c) == 127 for c in name)
            or len(path.parts) > MAX_PATH_DEPTH
            or len(os.fsencode(name)) > 4096
            or any(len(os.fsencode(part)) > 255 for part in path.parts)):
        raise ArchiveError('NONCANONICAL_OR_UNSAFE_PATH')
    return path


def _inventory_archive(package, raw):
    seen, files, directories = set(), {}, set()
    total, last_end = 0, 0
    members = []
    for member in package:
        if len(seen) >= MAX_MEMBERS:
            raise LimitExceeded('MEMBER_COUNT_LIMIT')
        path = _canonical_name(member.name)
        if member.name in seen:
            raise ArchiveError('DUPLICATE_MEMBER')
        seen.add(member.name)
        if member.sparse is not None or any(k.startswith('GNU.sparse') for k in member.pax_headers):
            raise ArchiveError('SPARSE_MEMBER')
        if member.type not in (tarfile.REGTYPE, tarfile.AREGTYPE, tarfile.DIRTYPE):
            raise ArchiveError('LINK_OR_SPECIAL_MEMBER')
        if member.size < 0 or (member.isdir() and member.size != 0):
            raise ArchiveError('INVALID_MEMBER_SIZE')
        if member.size > MAX_FILE_BYTES:
            raise LimitExceeded('FILE_SIZE_LIMIT')
        total += member.size
        if total > MAX_EXPANDED_BYTES:
            raise LimitExceeded('EXPANDED_SIZE_LIMIT')
        for parent in path.parents:
            if parent != PurePosixPath('.'):
                directories.add(parent.as_posix())
        if member.isdir():
            directories.add(member.name)
        else:
            with package.extractfile(member) as stream:
                data = stream.read(MAX_FILE_BYTES + 1)
            if len(data) != member.size:
                raise ArchiveError('TRUNCATED_MEMBER')
            files[member.name] = {'bytes': len(data), 'sha256': hashlib.sha256(data).hexdigest()}
        last_end = member.offset_data + ((member.size + 511) // 512) * 512
        members.append(member)
        if len(directories) > MAX_MEMBERS:
            raise LimitExceeded('DIRECTORY_COUNT_LIMIT')
    if set(files) & directories:
        raise ArchiveError('FILE_DIRECTORY_CONFLICT')
    if 'index.html' not in files:
        raise ArchiveError('REGULAR_INDEX_REQUIRED')
    if len(raw) < last_end + 1024 or any(raw[last_end:]):
        raise ArchiveError('INVALID_TAR_END_OR_TRAILING_DATA')
    return members, files, directories, total


def _parent_fd(destination):
    supplied = Path(destination)
    if '..' in supplied.parts:
        raise DestinationError('DESTINATION_TRAVERSAL')
    absolute = Path(os.path.abspath(supplied))
    if absolute == Path('/'):
        raise DestinationError('DESTINATION_EXISTS')
    fd = os.open('/', DIR_FLAGS)
    try:
        for part in absolute.parent.parts[1:]:
            next_fd = os.open(part, DIR_FLAGS, dir_fd=fd)
            os.close(fd)
            fd = next_fd
        try:
            os.stat(absolute.name, dir_fd=fd, follow_symlinks=False)
        except FileNotFoundError:
            pass
        else:
            raise DestinationError('DESTINATION_EXISTS')
        return fd, absolute
    except BaseException:
        os.close(fd)
        raise


def _subdir(root_fd, parts):
    fd = os.dup(root_fd)
    try:
        for part in parts:
            next_fd = os.open(part, DIR_FLAGS, dir_fd=fd)
            os.close(fd)
            fd = next_fd
        return fd
    except BaseException:
        os.close(fd)
        raise


def _materialize(package, members, directories, stage_fd):
    for directory in sorted(directories, key=lambda name: (name.count('/'), name)):
        path = PurePosixPath(directory)
        parent = _subdir(stage_fd, path.parts[:-1])
        try:
            os.mkdir(path.name, mode=0o755, dir_fd=parent)
        finally:
            os.close(parent)
    for member in members:
        if member.isdir():
            continue
        path = PurePosixPath(member.name)
        parent = _subdir(stage_fd, path.parts[:-1])
        try:
            fd = os.open(path.name, os.O_WRONLY | os.O_CREAT | os.O_EXCL
                         | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600, dir_fd=parent)
            with os.fdopen(fd, 'wb') as output, package.extractfile(member) as source:
                remaining = member.size
                while remaining:
                    chunk = source.read(min(1024 * 1024, remaining))
                    if not chunk:
                        raise ArchiveError('TRUNCATED_MEMBER')
                    output.write(chunk)
                    remaining -= len(chunk)
                output.flush()
                os.fchmod(output.fileno(), 0o644)
                os.fsync(output.fileno())
        finally:
            os.close(parent)


def _inventory_disk(stage_fd):
    files, directories = {}, set()
    for path, dirs, names, fd in os.fwalk('.', dir_fd=stage_fd, follow_symlinks=False):
        relative = PurePosixPath(path)
        for name in dirs:
            info = os.stat(name, dir_fd=fd, follow_symlinks=False)
            if not stat.S_ISDIR(info.st_mode):
                raise DestinationError('STAGING_LINK_OR_SPECIAL_ENTRY')
            directories.add((relative / name).as_posix())
        for name in names:
            file_fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC, dir_fd=fd)
            with os.fdopen(file_fd, 'rb') as stream:
                info = os.fstat(stream.fileno())
                if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                    raise DestinationError('STAGING_LINK_OR_SPECIAL_ENTRY')
                data = stream.read(MAX_FILE_BYTES + 1)
            if len(data) > MAX_FILE_BYTES:
                raise DestinationError('STAGING_SIZE_CHANGED')
            files[(relative / name).as_posix()] = {
                'bytes': len(data), 'sha256': hashlib.sha256(data).hexdigest()}
        os.fchmod(fd, 0o755 if path != '.' else 0o700)
        os.fsync(fd)
    return files, directories


def _publish(parent_fd, stage_name, name):
    try:
        rename = ctypes.CDLL(None, use_errno=True).renameat2
    except AttributeError as exc:
        raise DestinationError('ATOMIC_NOREPLACE_UNAVAILABLE') from exc
    rename.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p, ctypes.c_uint]
    rename.restype = ctypes.c_int
    if rename(parent_fd, os.fsencode(stage_name), parent_fd, os.fsencode(name), 1) != 0:
        code = ctypes.get_errno()
        if code in (errno.EEXIST, errno.ENOTEMPTY):
            raise DestinationError('DESTINATION_EXISTS')
        raise DestinationError('ATOMIC_PUBLICATION_FAILED') from OSError(code, os.strerror(code))


def restore_archive(archive, expected_sha256, destination):
    """Verify, stage, reread and atomically publish into an absent destination.

    Raises RestoreError on refusal; existing paths are never replaced. The caller
    owns the parent and must keep it stable. The returned inventory uses the same
    canonical serialization as the existing serving-tree receipt.
    """
    parent_fd, stage_fd, stage_name = None, None, None
    try:
        parent_fd, absolute = _parent_fd(destination)
        raw = _verified_bytes(archive, expected_sha256)
        with tarfile.open(fileobj=io.BytesIO(raw), mode='r:') as package:
            members, expected, directories, total = _inventory_archive(package, raw)
            disk = os.fstatvfs(parent_fd)
            if disk.f_bavail * disk.f_frsize < total + 1024 * 1024:
                raise DestinationError('INSUFFICIENT_STAGING_SPACE')
            candidate = '.safe-restore-' + uuid.uuid4().hex
            os.mkdir(candidate, mode=0o700, dir_fd=parent_fd)
            stage_name = candidate  # Cleanup only a directory created by this call.
            stage_fd = os.open(stage_name, DIR_FLAGS, dir_fd=parent_fd)
            _materialize(package, members, directories, stage_fd)
            actual, actual_dirs = _inventory_disk(stage_fd)
            if actual != expected or actual_dirs != directories:
                raise DestinationError('STAGING_INVENTORY_MISMATCH')
            inventory_sha = hashlib.sha256(json.dumps(
                actual, sort_keys=True, separators=(',', ':')).encode()).hexdigest()
            receipt = {'archive_sha256': expected_sha256.lower(), 'destination': str(absolute),
                       'file_count': len(actual), 'expanded_bytes': total,
                       'inventory_sha256': inventory_sha, 'inventory': actual}
            os.fchmod(stage_fd, 0o755)
            os.fsync(stage_fd)
            _publish(parent_fd, stage_name, absolute.name)
            stage_name = None
            return receipt
    except RestoreError:
        raise
    except (OSError, EOFError, tarfile.TarError, ValueError) as exc:
        raise RestoreError('RESTORE_IO_OR_FORMAT_ERROR') from exc
    finally:
        cleanup_error = None
        try:
            if stage_fd is not None:
                os.close(stage_fd)
            if stage_name is not None and parent_fd is not None:
                shutil.rmtree(stage_name, dir_fd=parent_fd)
        except OSError as exc:
            cleanup_error = exc
        finally:
            if parent_fd is not None:
                os.close(parent_fd)
        if cleanup_error is not None:
            raise DestinationError('STAGING_CLEANUP_FAILED') from cleanup_error


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('archive', type=Path)
    parser.add_argument('expected_sha256')
    parser.add_argument('destination', type=Path)
    parser.add_argument('--inventory', action='store_true', help='include full file inventory in JSON')
    args = parser.parse_args()
    try:
        result = restore_archive(args.archive, args.expected_sha256, args.destination)
    except RestoreError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    if not args.inventory:
        result.pop('inventory')
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())

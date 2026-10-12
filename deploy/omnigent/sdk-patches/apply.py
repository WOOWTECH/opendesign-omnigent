#!/usr/bin/env python3
"""Apply the public, single-hunk SDK patch; never print source or raw errors."""
import hashlib
import os
from pathlib import Path
import re
import stat
import sys
import tempfile

BASE_SHA256 = "1093a7c89de966c39e598f683b2651073abf613152e4e875676135bd28138ad1"
RESULT_SHA256 = "2de8af76672e8008731c7fe4500eec059cd0d54b68091a8b364f631af911d75e"

def apply(target, patch):
    target, patch = Path(target), Path(patch)
    if not target.is_absolute() or target.name != "connect.py" or target.parent.name != "host":
        raise ValueError("INVALID_TARGET")
    metadata = target.lstat()
    if not stat.S_ISREG(metadata.st_mode):
        raise ValueError("INVALID_TARGET")
    original = target.read_bytes()
    if hashlib.sha256(original).hexdigest() != BASE_SHA256:
        raise ValueError("BASELINE_MISMATCH")
    lines = patch.read_bytes().splitlines(keepends=True)
    if len(lines) < 3 or lines[:2] != [b"--- a/host/connect.py\n", b"+++ b/host/connect.py\n"]:
        raise ValueError("INVALID_PATCH")
    hunk = re.fullmatch(rb"@@ -(\d+),(\d+) \+(\d+),(\d+) @@\n", lines[2])
    if not hunk:
        raise ValueError("INVALID_PATCH")
    start, old_count, new_start, new_count = map(int, hunk.groups())
    source = original.splitlines(keepends=True)
    if start < 1 or start > len(source) + 1 or new_start != start:
        raise ValueError("INVALID_PATCH")
    cursor, consumed, produced = start - 1, 0, 0
    output = source[:cursor]
    for line in lines[3:]:
        if line[:1] not in (b" ", b"+", b"-"):
            raise ValueError("INVALID_PATCH")
        if line[:1] != b"+":
            if cursor >= len(source) or source[cursor] != line[1:]:
                raise ValueError("CONTEXT_MISMATCH")
            cursor += 1
            consumed += 1
        if line[:1] != b"-":
            output.append(line[1:])
            produced += 1
    if (consumed, produced) != (old_count, new_count):
        raise ValueError("INVALID_PATCH")
    result = b"".join(output + source[cursor:])
    if hashlib.sha256(result).hexdigest() != RESULT_SHA256:
        raise ValueError("RESULT_MISMATCH")
    fd, temporary = tempfile.mkstemp(prefix=".sdk-patch-", dir=target.parent)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(result)
            stream.flush()
            os.fsync(stream.fileno())
            os.fchown(stream.fileno(), metadata.st_uid, metadata.st_gid)
            os.fchmod(stream.fileno(), stat.S_IMODE(metadata.st_mode))
        current = target.lstat()
        fields = ("st_dev", "st_ino", "st_mode", "st_uid", "st_gid", "st_size", "st_mtime_ns", "st_ctime_ns")
        if any(getattr(current, field) != getattr(metadata, field) for field in fields) or target.read_bytes() != original:
            raise ValueError("TARGET_CHANGED")
        os.replace(temporary, target)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)

if __name__ == "__main__":
    if len(sys.argv) != 3:
        sys.exit("Usage: apply.py TARGET_ABSOLUTE_PATH PATCH_PATH")
    try:
        apply(sys.argv[1], sys.argv[2])
    except (OSError, ValueError):
        sys.exit("SDK_PATCH_REJECTED")
    print("SDK_PATCH_APPLIED")

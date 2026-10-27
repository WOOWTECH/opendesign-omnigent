#!/usr/bin/env python3
"""Apply the reviewed SDK patch set, or verify it without writing any files."""
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import sys
import tempfile

SDK_VERSION = "0.16.0"
PATCH_TARGETS = {
    "opencode-host-model-options.patch": "host/connect.py",
    "opencode-quota-error.patch": "harnesses/opencode_native/forwarder.py",
    "opencode-native-interrupt.patch": "runner/native/interrupt.py",
    "pi-custom-model-options.patch": "harnesses/pi_native/credentials.py",
    "opencode-user-config-merge.patch": "harnesses/opencode_native/provider.py",
}
HASH = re.compile(r"[a-f0-9]{64}")
HUNK = re.compile(rb"@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@(?:[^\n]*)\n")


def digest(data):
    return hashlib.sha256(data).hexdigest()


def safe_relative(value):
    return (isinstance(value, str) and value.endswith(".py")
            and all(re.fullmatch(r"[A-Za-z0-9_][A-Za-z0-9_.-]*", part)
                    and part not in (".", "..") for part in value.split("/")))


def load_manifest(manifest_path):
    manifest_path = Path(manifest_path)
    if not stat.S_ISREG(manifest_path.lstat().st_mode):
        raise ValueError("INVALID_MANIFEST")
    manifest = json.loads(manifest_path.read_text())
    if manifest.get("schemaVersion") != 2 or manifest.get("sdkVersion") != SDK_VERSION:
        raise ValueError("INVALID_MANIFEST")
    patches = manifest.get("patches")
    if not isinstance(patches, list) or not patches:
        raise ValueError("INVALID_MANIFEST")
    names, targets = set(), set()
    for entry in patches:
        name = entry.get("patch")
        if name not in PATCH_TARGETS or name in names or not HASH.fullmatch(entry.get("patchSha256", "")):
            raise ValueError("INVALID_MANIFEST")
        names.add(name)
        files = entry.get("files")
        if not isinstance(files, list) or len(files) != 1 or files[0].get("sdkRelativePath") != PATCH_TARGETS[name]:
            raise ValueError("INVALID_MANIFEST")
        for item in files:
            relative = item.get("sdkRelativePath")
            if not safe_relative(relative) or relative in targets:
                raise ValueError("INVALID_MANIFEST")
            targets.add(relative)
            for key in ("baseSha256", "replacementSha256"):
                if not HASH.fullmatch(item.get(key, "")):
                    raise ValueError("INVALID_MANIFEST")
            if item["baseSha256"] == item["replacementSha256"]:
                raise ValueError("INVALID_MANIFEST")
    if names != set(PATCH_TARGETS):
        raise ValueError("INVALID_MANIFEST")
    return manifest


def patch_sections(data):
    lines = data.splitlines(keepends=True)
    sections, current, i = {}, None, 0
    while i < len(lines):
        line = lines[i]
        if line.startswith(b"diff --git ") or re.fullmatch(rb"index [a-f0-9]+\.\.[a-f0-9]+(?: 100(?:644|755))?\n", line):
            i += 1
            continue
        if line.startswith(b"--- a/"):
            relative = line[len(b"--- a/"):].rstrip(b"\n").decode("utf-8")
            if not safe_relative(relative) or relative in sections or i + 1 >= len(lines):
                raise ValueError("INVALID_PATCH")
            if lines[i + 1] != b"+++ b/" + relative.encode() + b"\n":
                raise ValueError("INVALID_PATCH")
            relative = relative.removeprefix("omnigent/")
            if relative in sections:
                raise ValueError("INVALID_PATCH")
            current = []
            sections[relative] = current
            i += 2
            continue
        if current is None or not (HUNK.fullmatch(line) or line[:1] in (b" ", b"+", b"-")):
            raise ValueError("INVALID_PATCH")
        current.append(line)
        i += 1
    return sections


def patched_bytes(original, lines):
    source = original.splitlines(keepends=True)
    output, cursor, i = [], 0, 0
    if not lines:
        raise ValueError("INVALID_PATCH")
    while i < len(lines):
        match = HUNK.fullmatch(lines[i])
        if not match:
            raise ValueError("INVALID_PATCH")
        start, old_count, new_start, new_count = (int(n) if n is not None else 1 for n in match.groups())
        offset = start - 1 if old_count else start
        if offset < cursor or offset > len(source):
            raise ValueError("INVALID_PATCH")
        output.extend(source[cursor:offset])
        cursor = offset
        if (new_start - 1 if new_count else new_start) != len(output):
            raise ValueError("INVALID_PATCH")
        consumed = produced = 0
        i += 1
        while i < len(lines) and not HUNK.fullmatch(lines[i]):
            line = lines[i]
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
            i += 1
        if (consumed, produced) != (old_count, new_count):
            raise ValueError("INVALID_PATCH")
    return b"".join(output + source[cursor:])


def checked_target(root, relative):
    target = root
    for part in relative.split("/"):
        target = target / part
        if stat.S_ISLNK(target.lstat().st_mode):
            raise ValueError("INVALID_TARGET")
    if not stat.S_ISREG(target.lstat().st_mode) or not target.resolve().is_relative_to(root):
        raise ValueError("INVALID_TARGET")
    return target


def replace_checked(target, metadata, original, result):
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


def apply_manifest(package_root, manifest_path, sdk_version, check=False):
    root, manifest_path = Path(package_root), Path(manifest_path)
    if not root.is_absolute() or root.is_symlink() or not root.is_dir() or root.resolve() != root:
        raise ValueError("INVALID_TARGET")
    manifest = load_manifest(manifest_path)
    if sdk_version != manifest["sdkVersion"]:
        raise ValueError("SDK_VERSION_MISMATCH")
    changes = []
    for entry in manifest["patches"]:
        patch_path = manifest_path.parent / entry["patch"]
        if not stat.S_ISREG(patch_path.lstat().st_mode):
            raise ValueError("INVALID_PATCH")
        data = patch_path.read_bytes()
        if digest(data) != entry["patchSha256"]:
            raise ValueError("PATCH_HASH_MISMATCH")
        sections = patch_sections(data)
        if set(sections) != {f["sdkRelativePath"] for f in entry["files"]}:
            raise ValueError("INVALID_PATCH")
        for item in entry["files"]:
            target = checked_target(root, item["sdkRelativePath"])
            metadata, original = target.lstat(), target.read_bytes()
            before = digest(original)
            if before == item["replacementSha256"]:
                continue
            if check or before != item["baseSha256"]:
                raise ValueError("BASELINE_MISMATCH")
            result = patched_bytes(original, sections[item["sdkRelativePath"]])
            if digest(result) != item["replacementSha256"]:
                raise ValueError("RESULT_MISMATCH")
            compile(result, item["sdkRelativePath"], "exec")
            changes.append((target, metadata, original, result))
    # Validate every patch and target before changing any file. Interrupted sets
    # can be retried: each target must still match a reviewed base or result.
    for args in changes:
        replace_checked(*args)


if __name__ == "__main__":
    try:
        from importlib.metadata import version
        if len(sys.argv) not in (3, 4) or (len(sys.argv) == 4 and sys.argv[3] != "--check"):
            raise ValueError("INVALID_ARGUMENTS")
        root = Path(sys.argv[1])
        if not root.resolve().is_relative_to(Path(sys.prefix).resolve()):
            raise ValueError("INVALID_TARGET")
        apply_manifest(root, sys.argv[2], version("omnigent"), check=len(sys.argv) == 4)
    except Exception:
        sys.exit("SDK_PATCH_REJECTED")
    print("SDK_PATCH_VERIFIED" if len(sys.argv) == 4 else "SDK_PATCH_APPLIED")

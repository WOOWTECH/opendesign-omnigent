#!/usr/bin/env python3
"""Bounded authenticated backup envelopes; no production access or logging.

Python 3.10+, cryptography >= 3.4.8, Linux filesystem semantics.
Only encryption accepts an arbitrary output stream. Decryption authenticates
the *whole* input before publishing a new file or returning bounded memory.
"""

from __future__ import annotations

import argparse
import base64
import dataclasses
import functools
import hashlib
import hmac
import io
import json
import os
from pathlib import Path
import re
import secrets
import stat
import struct
import sys
from typing import BinaryIO

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

MAGIC = b"ODBKUP\x00\x01"
PREFIX = struct.Struct(">8sI")
RECORD = struct.Struct(">BQI")
FINAL = struct.Struct(">QQ32s")
DATA_RECORD, FINAL_RECORD = 1, 2
ALGORITHM = "RSA-OAEP-SHA256+AES-256-GCM"
OAEP_LABEL = b"od-backup-envelope/v1/key"
AAD_LABEL = b"od-backup-envelope/v1/record\x00"
MAX_HEADER_BYTES = 8192
MAX_KEY_BYTES = 32768
MIN_CHUNK_BYTES = 4096
MAX_CHUNK_BYTES = 4 * 1024 * 1024
HARD_MAX_PLAINTEXT_BYTES = 64 * 1024**3
HARD_MAX_CHUNKS = 1024**2
MAX_MEMORY_PLAINTEXT_BYTES = 16 * 1024 * 1024
TOKEN = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:/@-]{0,127}\Z")
HEX256 = re.compile(r"[0-9a-f]{64}\Z")
HEADER_FIELDS = {
    "version", "algorithm", "chunkBytes", "identity", "purpose", "runId",
    "keyFingerprint", "noncePrefix", "wrappedKey",
}


class EnvelopeError(Exception):
    """Sanitized failure; never contains input data, keys or filesystem paths."""


def _sanitized(function):
    @functools.wraps(function)
    def wrapped(*args, **kwargs):
        try:
            return function(*args, **kwargs)
        except Exception:
            raise EnvelopeError("backup envelope operation failed") from None
    return wrapped


def _require(condition: bool) -> None:
    if not condition:
        raise EnvelopeError("backup envelope rejected")


@dataclasses.dataclass(frozen=True)
class Context:
    """Public, authenticated labels; never put credentials in these fields."""

    identity: str
    purpose: str
    run_id: str

    def _validate(self) -> None:
        for value in (self.identity, self.purpose, self.run_id):
            _require(isinstance(value, str) and TOKEN.fullmatch(value) is not None)


@dataclasses.dataclass(frozen=True)
class Limits:
    """Local policy, independent of untrusted input; all bounds are enforced."""

    chunk_bytes: int = 1024 * 1024
    max_plaintext_bytes: int = HARD_MAX_PLAINTEXT_BYTES
    max_chunks: int = HARD_MAX_CHUNKS

    def _validate(self) -> None:
        for value in (self.chunk_bytes, self.max_plaintext_bytes, self.max_chunks):
            _require(type(value) is int)
        _require(MIN_CHUNK_BYTES <= self.chunk_bytes <= MAX_CHUNK_BYTES)
        _require(0 <= self.max_plaintext_bytes <= HARD_MAX_PLAINTEXT_BYTES)
        _require(1 <= self.max_chunks <= HARD_MAX_CHUNKS)


def _canonical(value: dict) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=True, allow_nan=False).encode("ascii")


def _oaep() -> padding.OAEP:
    return padding.OAEP(mgf=padding.MGF1(hashes.SHA256()),
                        algorithm=hashes.SHA256(), label=OAEP_LABEL)


def _valid_public_key(key) -> None:
    _require(isinstance(key, rsa.RSAPublicKey))
    _require(key.key_size in (3072, 4096))
    _require(key.public_numbers().e == 65537)


def _fingerprint(key: rsa.RSAPublicKey) -> str:
    return hashlib.sha256(key.public_bytes(
        serialization.Encoding.DER,
        serialization.PublicFormat.SubjectPublicKeyInfo)).hexdigest()


def _load_public(pem: bytes) -> rsa.RSAPublicKey:
    _require(isinstance(pem, bytes) and 0 < len(pem) <= MAX_KEY_BYTES)
    key = serialization.load_pem_public_key(pem)
    _valid_public_key(key)
    return key


def _open_directory(path: str | os.PathLike, *, private: bool = False) -> int:
    """Anchor every component without following symlinks; caller owns the tree."""
    path = os.path.abspath(os.fspath(path))
    descriptor = os.open("/", os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
    try:
        for part in Path(path).parts[1:]:
            new = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW |
                          os.O_CLOEXEC, dir_fd=descriptor)
            os.close(descriptor)
            descriptor = new
        info = os.fstat(descriptor)
        if private:
            _require(info.st_uid == os.geteuid() and stat.S_IMODE(info.st_mode) == 0o700)
        return descriptor
    except BaseException:
        os.close(descriptor)
        raise


def _split_destination(path: str | os.PathLike) -> tuple[str, str]:
    absolute = os.path.abspath(os.fspath(path))
    parent, name = os.path.split(absolute)
    _require(name not in ("", ".", ".."))
    return parent, name


def _read_key_file(path: str | os.PathLike, *, private: bool) -> bytes:
    parent, name = _split_destination(path)
    parent_fd = _open_directory(parent, private=private)
    try:
        descriptor = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC |
                             os.O_NONBLOCK, dir_fd=parent_fd)
        with os.fdopen(descriptor, "rb") as stream:
            info = os.fstat(stream.fileno())
            _require(stat.S_ISREG(info.st_mode) and 0 < info.st_size <= MAX_KEY_BYTES)
            if private:
                _require(info.st_uid == os.geteuid() and info.st_nlink == 1 and
                         stat.S_IMODE(info.st_mode) == 0o600)
            value = stream.read(MAX_KEY_BYTES + 1)
            _require(0 < len(value) <= MAX_KEY_BYTES)
            return value
    finally:
        os.close(parent_fd)


@_sanitized
def read_public_key(path: str | os.PathLike) -> bytes:
    """Read a bounded, regular, non-symlink public PEM file."""
    pem = _read_key_file(path, private=False)
    _load_public(pem)
    return pem


def _write_all(stream: BinaryIO, data: bytes) -> None:
    view = memoryview(data)
    while view:
        count = stream.write(view)
        _require(type(count) is int and 0 < count <= len(view))
        view = view[count:]


def _read_up_to(stream: BinaryIO, count: int) -> bytes:
    """Fill at most one bounded chunk; handle legal short reads on pipes."""
    result = bytearray()
    while len(result) < count:
        value = stream.read(count - len(result))
        _require(isinstance(value, bytes) and len(value) <= count - len(result))
        if not value:
            break
        result.extend(value)
    return bytes(result)


def _read_exact(stream: BinaryIO, count: int) -> bytes:
    value = _read_up_to(stream, count)
    _require(len(value) == count)
    return value


class _Tracked:
    def __init__(self, stream: BinaryIO):
        self.stream = stream
        self.size = 0
        self.digest = hashlib.sha256()

    def write(self, data: bytes) -> None:
        _write_all(self.stream, data)
        self.size += len(data)
        self.digest.update(data)

    def read(self, count: int) -> bytes:
        value = _read_exact(self.stream, count)
        self.size += len(value)
        self.digest.update(value)
        return value


def _receipt(context: Context, fingerprint: str, stream: _Tracked,
             total: int, chunks: int) -> dict:
    # Plaintext SHA-256 is encrypted in the terminal record, never in receipts.
    return {"version": 1, "algorithm": ALGORITHM,
            "context": dataclasses.asdict(context), "keyFingerprint": fingerprint,
            "plaintextBytes": total, "chunks": chunks,
            "envelopeBytes": stream.size,
            "envelopeSha256": stream.digest.hexdigest()}


@_sanitized
def generate_keypair(private_key_dir: str | os.PathLike,
                     backup_dir: str | os.PathLike) -> dict:
    """Create a NEW mode-0700 keydir outside an existing backup directory.

    private.pem is PKCS8 mode 0600; public.pem and receipt.json are non-secret.
    This is local filesystem separation, not KMS or an encrypted private key.
    Both paths must have stable, trusted, existing parent directories.
    """
    key_path = Path(os.path.abspath(os.fspath(private_key_dir)))
    backup_path = Path(os.path.abspath(os.fspath(backup_dir)))
    _require(key_path != backup_path and key_path not in backup_path.parents and
             backup_path not in key_path.parents)
    backup_fd = _open_directory(backup_path)
    os.close(backup_fd)
    parent, name = _split_destination(key_path)
    parent_fd = _open_directory(parent)
    key_fd = None
    made_directory = False
    created = []
    try:
        os.mkdir(name, 0o700, dir_fd=parent_fd)  # Never reuse an existing keydir.
        made_directory = True
        key_fd = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW |
                         os.O_CLOEXEC, dir_fd=parent_fd)
        os.fchmod(key_fd, 0o700)
        key = rsa.generate_private_key(public_exponent=65537, key_size=3072)
        public = key.public_key()
        receipt = {"version": 1, "keyType": "RSA-3072", "keyFingerprint": _fingerprint(public),
                   "privateKey": str(key_path / "private.pem"),
                   "publicKey": str(key_path / "public.pem"),
                   "custody": "local-mode-0600-private-key-outside-backup-directory"}
        entries = (
            ("private.pem", key.private_bytes(serialization.Encoding.PEM,
                serialization.PrivateFormat.PKCS8, serialization.NoEncryption()), 0o600),
            ("public.pem", public.public_bytes(serialization.Encoding.PEM,
                serialization.PublicFormat.SubjectPublicKeyInfo), 0o644),
            ("receipt.json", _canonical(receipt) + b"\n", 0o644),
        )
        for filename, payload, mode in entries:
            descriptor = os.open(filename, os.O_WRONLY | os.O_CREAT | os.O_EXCL |
                                 os.O_NOFOLLOW | os.O_CLOEXEC, mode, dir_fd=key_fd)
            created.append(filename)
            with os.fdopen(descriptor, "wb") as output:
                os.fchmod(output.fileno(), mode)
                _write_all(output, payload)
                output.flush()
                os.fsync(output.fileno())
        os.fsync(key_fd)
        os.fsync(parent_fd)
        return receipt
    except BaseException:
        if key_fd is not None:
            for filename in created:
                os.unlink(filename, dir_fd=key_fd)
        if made_directory:
            os.rmdir(name, dir_fd=parent_fd)
        raise
    finally:
        if key_fd is not None:
            os.close(key_fd)
        os.close(parent_fd)


class CipherWriter:
    """Binary ciphertext writer for tarfile's ``w|gz`` mode.

    Call finish() after the upstream producer succeeds. A context manager
    finishes only on normal exit. abort() or any write/flush failure permanently
    prevents finalization. This object never closes its underlying stream.
    flush() does NOT finalize a partial chunk. Finalization is explicit.
    """

    @_sanitized
    def __init__(self, destination: BinaryIO, public_key_pem: bytes,
                 context: Context, limits: Limits = Limits()):
        context._validate()
        limits._validate()
        key = _load_public(public_key_pem)
        stream_key = AESGCM.generate_key(bit_length=256)
        self._nonce_prefix = secrets.token_bytes(4)
        self._fingerprint = _fingerprint(key)
        self._context = context
        self._limits = limits
        self._destination = destination
        self._tracked = _Tracked(destination)
        self._digest = hashlib.sha256()
        self._buffer = bytearray()
        self._cipher = AESGCM(stream_key)
        self._total = self._chunks = 0
        self._failed = self._finished = False
        self._final_receipt = None
        header = _canonical({
            "version": 1, "algorithm": ALGORITHM, "chunkBytes": limits.chunk_bytes,
            "identity": context.identity, "purpose": context.purpose, "runId": context.run_id,
            "keyFingerprint": self._fingerprint,
            "noncePrefix": base64.b64encode(self._nonce_prefix).decode("ascii"),
            "wrappedKey": base64.b64encode(key.encrypt(stream_key, _oaep())).decode("ascii"),
        })
        _require(len(header) <= MAX_HEADER_BYTES)
        prefix = PREFIX.pack(MAGIC, len(header))
        self._aad_prefix = AAD_LABEL + hashlib.sha256(prefix + header).digest()
        self._tracked.write(prefix + header)

    def _frame(self, kind: int, plaintext: bytes) -> None:
        frame = RECORD.pack(kind, self._chunks, len(plaintext) + 16)
        nonce = self._nonce_prefix + struct.pack(">Q", self._chunks)
        self._tracked.write(frame + self._cipher.encrypt(
            nonce, plaintext, self._aad_prefix + frame))

    def _drain_chunk(self) -> None:
        self._frame(DATA_RECORD, bytes(self._buffer))
        self._digest.update(self._buffer)
        self._chunks += 1
        self._buffer.clear()

    @_sanitized
    def write(self, data) -> int:
        try:
            _require(not self._failed and not self._finished)
            _require(isinstance(data, (bytes, bytearray, memoryview)))
            view = memoryview(data).cast("B")
            size = len(view)
            next_total = self._total + size
            _require(next_total <= self._limits.max_plaintext_bytes)
            needed_chunks = (next_total + self._limits.chunk_bytes - 1) // self._limits.chunk_bytes
            _require(needed_chunks <= self._limits.max_chunks)
            while view:
                count = min(len(view), self._limits.chunk_bytes - len(self._buffer))
                self._buffer.extend(view[:count])
                view = view[count:]
                if len(self._buffer) == self._limits.chunk_bytes:
                    self._drain_chunk()
            self._total = next_total
            return size
        except BaseException:
            self._failed = True
            raise

    @_sanitized
    def flush(self) -> None:
        try:
            _require(not self._failed)
            self._destination.flush()
        except BaseException:
            self._failed = True
            raise

    def tell(self) -> int:
        return self._total

    def writable(self) -> bool:
        return not self._failed and not self._finished

    def abort(self) -> None:
        self._failed = True
        self._buffer.clear()

    @_sanitized
    def finish(self) -> dict:
        try:
            _require(not self._failed)
            if self._finished:
                return dict(self._final_receipt)
            if self._buffer:
                self._drain_chunk()
            terminal = FINAL.pack(self._chunks, self._total, self._digest.digest())
            self._frame(FINAL_RECORD, terminal)
            self._destination.flush()
            self._final_receipt = _receipt(self._context, self._fingerprint,
                self._tracked, self._total, self._chunks)
            self._finished = True
            return dict(self._final_receipt)
        except BaseException:
            self._failed = True
            raise

    def __enter__(self):
        return self

    def __exit__(self, exception_type, exception, traceback):
        if exception_type is None:
            self.finish()
        else:
            self.abort()
        return False


@_sanitized
def encrypt_stream(source: BinaryIO, destination: BinaryIO,
                   public_key_pem: bytes, context: Context,
                   limits: Limits = Limits()) -> dict:
    """Stream ciphertext only, with O(chunk size) working memory.

    A failed input/output/cap check may leave an INVALID ciphertext prefix in
    destination. Require success from every producer and this function before
    declaring a backup captured; an upstream process can fail after writing an
    apparently valid prefix to its pipe. No plaintext is logged.
    """
    writer = CipherWriter(destination, public_key_pem, context, limits)
    try:
        while True:
            remaining = limits.max_plaintext_bytes - writer.tell()
            chunk = _read_up_to(source, min(limits.chunk_bytes, remaining + 1))
            if not chunk:
                break
            writer.write(chunk)
        return writer.finish()
    except BaseException:
        writer.abort()
        raise


def _decrypt_to_stream(source: BinaryIO, destination: BinaryIO,
                       private_key: rsa.RSAPrivateKey, expected: Context,
                       limits: Limits) -> dict:
    tracked = _Tracked(source)
    prefix = tracked.read(PREFIX.size)
    magic, size = PREFIX.unpack(prefix)
    _require(magic == MAGIC and 0 < size <= MAX_HEADER_BYTES)
    raw = tracked.read(size)
    header = json.loads(raw)
    _require(type(header) is dict and set(header) == HEADER_FIELDS)
    _require(_canonical(header) == raw)  # Reject aliases and duplicate JSON keys.
    _require(type(header["version"]) is int and header["version"] == 1)
    _require(header["algorithm"] == ALGORITHM)
    _require(type(header["chunkBytes"]) is int and
             MIN_CHUNK_BYTES <= header["chunkBytes"] <= limits.chunk_bytes)
    _require(header["identity"] == expected.identity and
             header["purpose"] == expected.purpose and header["runId"] == expected.run_id)
    fingerprint = _fingerprint(private_key.public_key())
    _require(isinstance(header["keyFingerprint"], str) and
             HEX256.fullmatch(header["keyFingerprint"]) is not None and
             hmac.compare_digest(header["keyFingerprint"], fingerprint))
    nonce_prefix = base64.b64decode(header["noncePrefix"], validate=True)
    wrapped = base64.b64decode(header["wrappedKey"], validate=True)
    _require(len(nonce_prefix) == 4 and len(wrapped) == private_key.key_size // 8)
    stream_key = private_key.decrypt(wrapped, _oaep())
    _require(len(stream_key) == 32)
    cipher = AESGCM(stream_key)
    aad_prefix = AAD_LABEL + hashlib.sha256(prefix + raw).digest()
    total = chunks = 0
    digest = hashlib.sha256()
    short_chunk_seen = False
    while True:
        frame = tracked.read(RECORD.size)
        kind, index, size = RECORD.unpack(frame)
        _require(index == chunks and kind in (DATA_RECORD, FINAL_RECORD))
        if kind == DATA_RECORD:
            _require(not short_chunk_seen and chunks < limits.max_chunks)
            _require(16 < size <= header["chunkBytes"] + 16)
            _require(total + size - 16 <= limits.max_plaintext_bytes)
        else:
            _require(size == FINAL.size + 16)
        nonce = nonce_prefix + struct.pack(">Q", index)
        plaintext = cipher.decrypt(nonce, tracked.read(size), aad_prefix + frame)
        if kind == DATA_RECORD:
            _write_all(destination, plaintext)
            digest.update(plaintext)
            total += len(plaintext)
            chunks += 1
            short_chunk_seen = len(plaintext) < header["chunkBytes"]
        else:
            final_chunks, final_size, final_hash = FINAL.unpack(plaintext)
            _require(final_chunks == chunks and final_size == total and
                     hmac.compare_digest(final_hash, digest.digest()))
            _require(_read_up_to(source, 1) == b"")  # No suffix/concatenation accepted.
            return _receipt(expected, fingerprint, tracked, total, chunks)


def _load_private_pem(pem: bytes) -> rsa.RSAPrivateKey:
    _require(isinstance(pem, bytes) and 0 < len(pem) <= MAX_KEY_BYTES)
    private_key = serialization.load_pem_private_key(pem, password=None)
    _require(isinstance(private_key, rsa.RSAPrivateKey))
    _valid_public_key(private_key.public_key())
    return private_key


@_sanitized
def decrypt_to_memory(source: BinaryIO, private_key_pem: bytes, expected: Context,
                      limits: Limits = Limits(max_plaintext_bytes=MAX_MEMORY_PLAINTEXT_BYTES)
                      ) -> tuple[bytes, dict]:
    """Return small plaintext only AFTER all records, final hash and EOF verify.

    Hard maximum plaintext is 16 MiB, including when a caller overrides limits.
    The private key is passed from the caller's protected memory; no secret or
    plaintext file is read/written by this function. BytesIO is internal and is
    closed on failure. Python cannot promise secure memory zeroization.
    """
    expected._validate()
    limits._validate()
    _require(limits.max_plaintext_bytes <= MAX_MEMORY_PLAINTEXT_BYTES)
    private_key = _load_private_pem(private_key_pem)
    with io.BytesIO() as output:
        receipt = _decrypt_to_stream(source, output, private_key, expected, limits)
        return output.getvalue(), receipt


@_sanitized
def decrypt_to_path(source: BinaryIO, destination: str | os.PathLike,
                    private_key_path: str | os.PathLike | bytes, expected: Context,
                    limits: Limits = Limits()) -> dict:
    """Authenticate to a mode-0600 temp file, then atomically publish NEW file.

    Destination's parent must be mode 0700, owned by this effective UID, and in
    a stable trusted directory tree. Existing files/symlinks are never replaced,
    including a concurrent destination creation. Staging is cleaned on failure.
    A private-key path has the same owner/mode requirements and is not logged.
    Alternatively pass PEM bytes from protected caller memory (e.g. a scoped
    Kubernetes Secret read by the isolated restore helper). In that case the
    caller is responsible for key custody; this function writes no key file.
    No stdout/raw-stream decrypt interface exists, because it could leak a
    truncated but individually authenticated prefix to a consumer.
    """
    expected._validate()
    limits._validate()
    pem = (private_key_path if isinstance(private_key_path, bytes)
           else _read_key_file(private_key_path, private=True))
    private_key = _load_private_pem(pem)
    parent, name = _split_destination(destination)
    parent_fd = _open_directory(parent, private=True)
    temporary = None
    owned_temporary = False
    try:
        try:
            os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
        except FileNotFoundError:
            pass
        else:
            raise EnvelopeError("backup destination already exists")
        temporary = ".od-envelope-" + secrets.token_hex(16) + ".partial"
        descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL |
                             os.O_NOFOLLOW | os.O_CLOEXEC, 0o600, dir_fd=parent_fd)
        owned_temporary = True
        with os.fdopen(descriptor, "wb") as output:
            os.fchmod(output.fileno(), 0o600)
            receipt = _decrypt_to_stream(source, output, private_key, expected, limits)
            output.flush()
            os.fsync(output.fileno())
        # link(2) is atomic and fails with EEXIST instead of overwriting a racer.
        os.link(temporary, name, src_dir_fd=parent_fd, dst_dir_fd=parent_fd,
                follow_symlinks=False)
        os.unlink(temporary, dir_fd=parent_fd)
        temporary = None
        os.fsync(parent_fd)
        return receipt
    finally:
        if temporary is not None and owned_temporary:
            os.unlink(temporary, dir_fd=parent_fd)
        os.close(parent_fd)


class _Parser(argparse.ArgumentParser):
    def error(self, message):
        # argparse's default errors may echo caller-supplied values.
        self.exit(2, "backup envelope: invalid arguments\n")


def _parser() -> argparse.ArgumentParser:
    parser = _Parser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True, parser_class=_Parser)
    keygen = sub.add_parser("keygen", help="create a separate protected key directory")
    keygen.add_argument("--private-key-dir", required=True)
    keygen.add_argument("--backup-dir", required=True)
    for command in ("encrypt", "decrypt"):
        action = sub.add_parser(command)
        action.add_argument("--identity", required=True)
        action.add_argument("--purpose", required=True)
        action.add_argument("--run-id", required=True)
        action.add_argument("--chunk-bytes", type=int, default=1024 * 1024)
        action.add_argument("--max-bytes", type=int, required=True)
        action.add_argument("--max-chunks", type=int, default=HARD_MAX_CHUNKS)
        if command == "encrypt":
            action.add_argument("--public-key", required=True)
        else:
            action.add_argument("--private-key", required=True)
            action.add_argument("--output", required=True)
    return parser


def main(argv: list[str] | None = None) -> int:
    arguments = _parser().parse_args(argv)
    try:
        if arguments.command == "keygen":
            receipt = generate_keypair(arguments.private_key_dir, arguments.backup_dir)
            output = sys.stdout
        else:
            context = Context(arguments.identity, arguments.purpose, arguments.run_id)
            limits = Limits(arguments.chunk_bytes, arguments.max_bytes, arguments.max_chunks)
            if arguments.command == "encrypt":
                public_pem = read_public_key(arguments.public_key)
                receipt = encrypt_stream(sys.stdin.buffer, sys.stdout.buffer,
                                         public_pem, context, limits)
            else:
                receipt = decrypt_to_path(sys.stdin.buffer, arguments.output,
                                          arguments.private_key, context, limits)
            output = sys.stderr
        output.write(json.dumps(receipt, sort_keys=True) + "\n")
        output.flush()
        return 0
    except (Exception, KeyboardInterrupt):
        # Never render exception text, tracebacks, PEM or input bytes.
        sys.stderr.write("backup envelope: operation failed\n")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())

"""Synthetic-only security and streaming tests; all writes stay in this directory."""

import base64
import contextlib
import hashlib
import io
import json
import os
from pathlib import Path
import stat
import struct
import subprocess
import sys
import tarfile
import tempfile
import unittest
from unittest import mock

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

import backup_envelope as env

ROOT = Path(__file__).resolve().parent
CONTEXT = env.Context("synthetic/service201", "database", "synthetic-run-20261026")
LIMITS = env.Limits(chunk_bytes=4096, max_plaintext_bytes=1024 * 1024, max_chunks=256)


def split_envelope(raw):
    magic, length = env.PREFIX.unpack(raw[:env.PREFIX.size])
    assert magic == env.MAGIC
    end = env.PREFIX.size + length
    records = []
    offset = end
    while offset < len(raw):
        _, _, size = env.RECORD.unpack(raw[offset:offset + env.RECORD.size])
        records.append(raw[offset:offset + env.RECORD.size + size])
        offset += env.RECORD.size + size
    assert offset == len(raw)
    return raw[:end], records


def rewrite_header(raw, update):
    prefix, records = split_envelope(raw)
    value = json.loads(prefix[env.PREFIX.size:])
    update(value)
    header = env._canonical(value)
    return env.PREFIX.pack(env.MAGIC, len(header)) + header + b"".join(records)


class ShortReader(io.BytesIO):
    def read(self, count=-1):
        if count < 0 or count > env.MAX_CHUNK_BYTES + 16:
            raise AssertionError("unbounded read")
        return super().read(min(count, 113))


class ShortWriter(io.BytesIO):
    def write(self, value):
        return super().write(value[:137])


class PatternReader:
    """Never materializes the whole input; controls maximum requested allocation."""

    def __init__(self, total, largest_allowed=65536):
        self.remaining = total
        self.largest_allowed = largest_allowed
        self.largest_read = 0

    def read(self, count):
        if not 0 <= count <= self.largest_allowed:
            raise AssertionError("unbounded read")
        self.largest_read = max(self.largest_read, count)
        size = min(count, self.remaining)
        self.remaining -= size
        return b"X" * size


class EnvelopeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory(prefix=".crypto-tests-", dir=ROOT)
        cls.root = Path(cls.temp.name)
        cls.backup = cls.root / "backup"
        cls.backup.mkdir(mode=0o700)
        cls.keys = cls.root / "keys"
        cls.key_receipt = env.generate_keypair(cls.keys, cls.backup)
        cls.public = env.read_public_key(cls.keys / "public.pem")
        cls.private = cls.keys / "private.pem"
        cls.other_keys = cls.root / "other-keys"
        env.generate_keypair(cls.other_keys, cls.backup)
        cls.payload = bytes(range(256)) * 32 + b"synthetic secret marker" * 13

    @classmethod
    def tearDownClass(cls):
        cls.temp.cleanup()

    def setUp(self):
        self.scratch = tempfile.TemporaryDirectory(prefix="case-", dir=self.root)
        self.work = Path(self.scratch.name)
        # This workspace inherits SGID; enforce the documented private-dir mode.
        os.chmod(self.work, 0o700)
        self.output = self.work / "restored.bin"

    def tearDown(self):
        self.scratch.cleanup()

    def encrypt(self, payload=None, context=CONTEXT, limits=LIMITS):
        output = io.BytesIO()
        receipt = env.encrypt_stream(io.BytesIO(self.payload if payload is None else payload),
                                     output, self.public, context, limits)
        return output.getvalue(), receipt

    def decrypt(self, raw, **overrides):
        args = {"source": io.BytesIO(raw), "destination": self.output,
                "private_key_path": self.private, "expected": CONTEXT, "limits": LIMITS}
        args.update(overrides)
        return env.decrypt_to_path(**args)

    def reject(self, raw, **overrides):
        with self.assertRaises(env.EnvelopeError) as caught:
            self.decrypt(raw, **overrides)
        self.assertEqual(str(caught.exception), "backup envelope operation failed")
        self.assertFalse(self.output.exists())
        self.assertEqual(list(self.work.glob(".od-envelope-*.partial")), [])

    def test_multichunk_round_trip_and_receipt(self):
        raw, encrypted = self.encrypt()
        restored = self.decrypt(raw)
        self.assertEqual(self.output.read_bytes(), self.payload)
        self.assertEqual(encrypted, restored)
        self.assertEqual(encrypted["chunks"], 3)
        self.assertEqual(encrypted["envelopeSha256"], hashlib.sha256(raw).hexdigest())
        self.assertEqual(encrypted["envelopeBytes"], len(raw))
        self.assertEqual(stat.S_IMODE(self.output.stat().st_mode), 0o600)
        self.assertNotIn("plaintextSha256", encrypted)
        self.assertNotIn(b"synthetic secret marker", raw)

    def test_empty_stream_has_authenticated_terminal(self):
        raw, receipt = self.encrypt(b"")
        self.assertEqual(receipt["chunks"], 0)
        self.decrypt(raw)
        self.assertEqual(self.output.read_bytes(), b"")
        self.assertEqual(len(split_envelope(raw)[1]), 1)

    def test_memory_decrypt_full_authentication_no_files(self):
        raw, expected_receipt = self.encrypt()
        plaintext, receipt = env.decrypt_to_memory(io.BytesIO(raw), self.private.read_bytes(),
                                                   CONTEXT, LIMITS)
        self.assertEqual(plaintext, self.payload)
        self.assertEqual(receipt, expected_receipt)
        self.assertEqual(list(self.work.iterdir()), [])

    def test_file_decrypt_accepts_protected_key_bytes(self):
        raw, receipt = self.encrypt()
        restored = self.decrypt(raw, private_key_path=self.private.read_bytes())
        self.assertEqual(self.output.read_bytes(), self.payload)
        self.assertEqual(restored, receipt)
        self.assertEqual(sorted(p.name for p in self.work.iterdir()), ["restored.bin"])

    def test_memory_decrypt_never_returns_partial_or_accepts_suffix(self):
        raw, _ = self.encrypt()
        for value in (raw[:-1], raw + b"x", raw[:-20] + b"X" * 20):
            with self.subTest(size=len(value)), self.assertRaises(env.EnvelopeError):
                env.decrypt_to_memory(io.BytesIO(value), self.private.read_bytes(), CONTEXT, LIMITS)
        self.assertEqual(list(self.work.iterdir()), [])

    def test_memory_decrypt_hard_and_caller_caps(self):
        raw, _ = self.encrypt()
        for limit in (env.Limits(4096, len(self.payload) - 1, 256),
                      env.Limits(4096, env.MAX_MEMORY_PLAINTEXT_BYTES + 1, 256)):
            with self.subTest(limit=limit), self.assertRaises(env.EnvelopeError):
                env.decrypt_to_memory(io.BytesIO(raw), self.private.read_bytes(), CONTEXT, limit)

    def test_memory_decrypt_wrong_key_and_context(self):
        raw, _ = self.encrypt()
        with self.assertRaises(env.EnvelopeError):
            env.decrypt_to_memory(io.BytesIO(raw), (self.other_keys / "private.pem").read_bytes(),
                                  CONTEXT, LIMITS)
        with self.assertRaises(env.EnvelopeError):
            env.decrypt_to_memory(io.BytesIO(raw), self.private.read_bytes(),
                                  env.Context("other", CONTEXT.purpose, CONTEXT.run_id), LIMITS)

    def test_exact_chunk_boundary(self):
        payload = b"a" * 8192
        raw, receipt = self.encrypt(payload)
        self.decrypt(raw)
        self.assertEqual(receipt["chunks"], 2)
        self.assertEqual(self.output.read_bytes(), payload)

    def test_single_byte(self):
        raw, _ = self.encrypt(b"a")
        self.decrypt(raw)
        self.assertEqual(self.output.read_bytes(), b"a")

    def test_short_pipe_reads_and_writes(self):
        output = ShortWriter()
        env.encrypt_stream(ShortReader(self.payload), output, self.public, CONTEXT, LIMITS)
        self.decrypt(output.getvalue(), source=ShortReader(output.getvalue()))
        self.assertEqual(self.output.read_bytes(), self.payload)

    def test_bounded_stream_64_mib(self):
        total = 64 * 1024 * 1024 + 17
        limits = env.Limits(65536, total, 2048)
        source = PatternReader(total)
        encrypted = self.work / "encrypted.bin"
        with encrypted.open("wb") as destination:
            first = env.encrypt_stream(source, destination, self.public, CONTEXT, limits)
        with encrypted.open("rb") as source_file:
            second = self.decrypt(b"", source=source_file, limits=limits)
        self.assertEqual(first, second)
        self.assertEqual(self.output.stat().st_size, total)
        self.assertLessEqual(source.largest_read, 65536)
        self.assertEqual(first["chunks"], 1025)
        expected = hashlib.sha256()
        actual = hashlib.sha256()
        with self.output.open("rb") as result:
            while chunk := result.read(65536):
                expected.update(b"X" * len(chunk))
                actual.update(chunk)
        self.assertEqual(expected.digest(), actual.digest())

    def test_cipherwriter_tar_gzip_stream(self):
        encrypted = io.BytesIO()
        writer = env.CipherWriter(encrypted, self.public, CONTEXT, LIMITS)
        with tarfile.open(fileobj=writer, mode="w|gz") as archive:
            info = tarfile.TarInfo("synthetic/data.bin")
            info.size = len(self.payload)
            archive.addfile(info, io.BytesIO(self.payload))
        receipt = writer.finish()
        self.decrypt(encrypted.getvalue())
        with tarfile.open(self.output, "r:gz") as archive:
            self.assertEqual(archive.getnames(), ["synthetic/data.bin"])
            self.assertEqual(archive.extractfile("synthetic/data.bin").read(), self.payload)
        self.assertEqual(writer.tell(), receipt["plaintextBytes"])
        self.assertEqual(writer.finish(), receipt)
        self.assertFalse(writer.writable())

    def test_cipherwriter_variable_writes_flushes(self):
        destination = io.BytesIO()
        with env.CipherWriter(destination, self.public, CONTEXT, LIMITS) as writer:
            for offset in range(0, len(self.payload), 71):
                chunk = memoryview(self.payload)[offset:offset + 71]
                self.assertEqual(writer.write(chunk), len(chunk))
                writer.flush()
        self.decrypt(destination.getvalue())
        self.assertEqual(self.output.read_bytes(), self.payload)

    def test_producer_exception_does_not_finalize(self):
        destination = io.BytesIO()
        with self.assertRaisesRegex(RuntimeError, "synthetic producer failure"):
            with env.CipherWriter(destination, self.public, CONTEXT, LIMITS) as writer:
                writer.write(self.payload)
                raise RuntimeError("synthetic producer failure")
        self.reject(destination.getvalue())
        with self.assertRaises(env.EnvelopeError):
            writer.finish()

    def test_writer_abort_prevents_finish(self):
        destination = io.BytesIO()
        writer = env.CipherWriter(destination, self.public, CONTEXT, LIMITS)
        writer.write(self.payload)
        writer.abort()
        with self.assertRaises(env.EnvelopeError):
            writer.finish()
        self.reject(destination.getvalue())

    def test_writer_failure_poisons_finish(self):
        destination = io.BytesIO()
        writer = env.CipherWriter(destination, self.public, CONTEXT, LIMITS)
        writer.write(self.payload)
        with self.assertRaises(env.EnvelopeError):
            writer.write(object())
        with self.assertRaises(env.EnvelopeError):
            writer.finish()
        self.reject(destination.getvalue())

    def test_writer_after_finish_rejected(self):
        destination = io.BytesIO()
        writer = env.CipherWriter(destination, self.public, CONTEXT, LIMITS)
        writer.finish()
        with self.assertRaises(env.EnvelopeError):
            writer.write(b"x")

    def test_each_stream_has_distinct_key_and_nonce(self):
        first, _ = self.encrypt()
        second, _ = self.encrypt()
        first_header = json.loads(split_envelope(first)[0][env.PREFIX.size:])
        second_header = json.loads(split_envelope(second)[0][env.PREFIX.size:])
        self.assertNotEqual(first_header["wrappedKey"], second_header["wrappedKey"])
        self.assertNotEqual(first, second)

    def test_wrong_key(self):
        raw, _ = self.encrypt()
        self.reject(raw, private_key_path=self.other_keys / "private.pem")

    def test_all_context_fields_are_pinned(self):
        raw, _ = self.encrypt()
        for context in (env.Context("other", CONTEXT.purpose, CONTEXT.run_id),
                        env.Context(CONTEXT.identity, "other", CONTEXT.run_id),
                        env.Context(CONTEXT.identity, CONTEXT.purpose, "other")):
            with self.subTest(context=context):
                self.reject(raw, expected=context)

    def test_header_modified_but_expected_context_also_modified(self):
        raw, _ = self.encrypt()
        changed = rewrite_header(raw, lambda header: header.update(purpose="other"))
        self.reject(changed, expected=env.Context(CONTEXT.identity, "other", CONTEXT.run_id))

    def test_wrapped_key_tampering(self):
        raw, _ = self.encrypt()
        def update(header):
            key = bytearray(base64.b64decode(header["wrappedKey"]))
            key[20] ^= 1
            header["wrappedKey"] = base64.b64encode(key).decode()
        self.reject(rewrite_header(raw, update))

    def test_nonce_prefix_tampering(self):
        raw, _ = self.encrypt()
        self.reject(rewrite_header(raw, lambda h: h.update(noncePrefix="AAAAAA==")))

    def test_ciphertext_and_tag_tampering(self):
        raw, _ = self.encrypt()
        header, records = split_envelope(raw)
        for index in (env.RECORD.size, len(records[0]) - 1):
            with self.subTest(index=index):
                changed = bytearray(records[0])
                changed[index] ^= 1
                self.reject(header + bytes(changed) + b"".join(records[1:]))

    def test_all_truncation_boundaries(self):
        raw, _ = self.encrypt()
        header, records = split_envelope(raw)
        cuts = [0, 1, env.PREFIX.size - 1, env.PREFIX.size,
                len(header) - 1, len(header), len(raw) - 1]
        position = len(header)
        for record in records:
            cuts.extend([position + 1, position + env.RECORD.size - 1,
                         position + env.RECORD.size, position + len(record) - 1])
            position += len(record)
            if position < len(raw):
                cuts.append(position)
        for cut in sorted(set(cuts)):
            with self.subTest(cut=cut):
                self.reject(raw[:cut])

    def test_record_reordering(self):
        raw, _ = self.encrypt()
        header, records = split_envelope(raw)
        self.reject(header + records[1] + records[0] + b"".join(records[2:]))

    def test_record_duplication(self):
        raw, _ = self.encrypt()
        header, records = split_envelope(raw)
        self.reject(header + records[0] + b"".join(records))

    def test_record_deletion(self):
        raw, _ = self.encrypt()
        header, records = split_envelope(raw)
        self.reject(header + records[0] + b"".join(records[2:]))

    def test_counter_tamper_even_if_reordered_to_match(self):
        raw, _ = self.encrypt()
        header, records = split_envelope(raw)
        kind, _, size = env.RECORD.unpack(records[1][:env.RECORD.size])
        changed = env.RECORD.pack(kind, 0, size) + records[1][env.RECORD.size:]
        self.reject(header + changed + b"".join(records[1:]))

    def test_trailing_data_and_concatenation(self):
        raw, _ = self.encrypt()
        for suffix in (b"x", raw):
            with self.subTest(suffix_length=len(suffix)):
                self.reject(raw + suffix)

    def test_no_terminal_record(self):
        raw, _ = self.encrypt()
        header, records = split_envelope(raw)
        self.reject(header + b"".join(records[:-1]))

    def test_authenticated_terminal_count_length_and_digest(self):
        raw, _ = self.encrypt()
        header_prefix, records = split_envelope(raw)
        header = json.loads(header_prefix[env.PREFIX.size:])
        key = serialization.load_pem_private_key(self.private.read_bytes(), password=None)
        stream_key = key.decrypt(base64.b64decode(header["wrappedKey"]), env._oaep())
        cipher = AESGCM(stream_key)
        frame = records[-1][:env.RECORD.size]
        _, index, _ = env.RECORD.unpack(frame)
        nonce = base64.b64decode(header["noncePrefix"]) + struct.pack(">Q", index)
        aad = env.AAD_LABEL + hashlib.sha256(header_prefix).digest() + frame
        terminal = cipher.decrypt(nonce, records[-1][env.RECORD.size:], aad)
        chunks, size, digest = env.FINAL.unpack(terminal)
        for wrong in (env.FINAL.pack(chunks + 1, size, digest),
                      env.FINAL.pack(chunks, size + 1, digest),
                      env.FINAL.pack(chunks, size, b"z" * 32)):
            with self.subTest(wrong=wrong[:16].hex()):
                changed = frame + cipher.encrypt(nonce, wrong, aad)
                self.reject(header_prefix + b"".join(records[:-1]) + changed)

    def test_authenticated_short_data_chunk_cannot_have_following_data(self):
        raw, _ = self.encrypt()
        header_prefix, _ = split_envelope(raw)
        header = json.loads(header_prefix[env.PREFIX.size:])
        key = serialization.load_pem_private_key(self.private.read_bytes(), password=None)
        cipher = AESGCM(key.decrypt(base64.b64decode(header["wrappedKey"]), env._oaep()))
        aad_prefix = env.AAD_LABEL + hashlib.sha256(header_prefix).digest()
        nonce_prefix = base64.b64decode(header["noncePrefix"])
        records = []
        for counter, data in enumerate((b"first-short", b"second-short")):
            frame = env.RECORD.pack(env.DATA_RECORD, counter, len(data) + 16)
            records.append(frame + cipher.encrypt(nonce_prefix + struct.pack(">Q", counter),
                                                   data, aad_prefix + frame))
        terminal = env.FINAL.pack(2, 23, hashlib.sha256(b"first-shortsecond-short").digest())
        frame = env.RECORD.pack(env.FINAL_RECORD, 2, len(terminal) + 16)
        records.append(frame + cipher.encrypt(nonce_prefix + struct.pack(">Q", 2),
                                               terminal, aad_prefix + frame))
        self.reject(header_prefix + b"".join(records))

    def test_writer_output_failure_poisons_finish_and_sanitizes(self):
        destination = io.BytesIO()
        writer = env.CipherWriter(destination, self.public, CONTEXT, LIMITS)
        with mock.patch.object(destination, "write", side_effect=OSError("SENSITIVE")):
            with self.assertRaises(env.EnvelopeError):
                writer.write(self.payload)
        with self.assertRaises(env.EnvelopeError):
            writer.finish()
        self.reject(destination.getvalue())

    def test_invalid_header_variants(self):
        raw, _ = self.encrypt()
        changes = [dict(version=2), dict(version=True), dict(algorithm="other"),
                   dict(chunkBytes=True), dict(chunkBytes=2**32), dict(chunkBytes=1),
                   dict(noncePrefix="!invalid!"), dict(noncePrefix=""),
                   dict(keyFingerprint="x" * 64), dict(wrappedKey=""), dict(extra=1)]
        for change in changes:
            with self.subTest(change=change):
                self.reject(rewrite_header(raw, lambda h: h.update(change)))

    def test_magic_and_header_caps(self):
        raw, _ = self.encrypt()
        self.reject(b"BADMAGIC" + raw[8:])
        self.reject(env.PREFIX.pack(env.MAGIC, env.MAX_HEADER_BYTES + 1))
        self.reject(env.PREFIX.pack(env.MAGIC, 0))

    def test_noncanonical_duplicate_and_invalid_json(self):
        raw, _ = self.encrypt()
        prefix, records = split_envelope(raw)
        header = prefix[env.PREFIX.size:]
        for changed in (b" " + header, header[:-1] + b',"version":1}',
                        b"[]", b"\xff", b"{" * 8192):
            with self.subTest(size=len(changed)):
                self.reject(env.PREFIX.pack(env.MAGIC, len(changed)) + changed + b"".join(records))

    def test_record_length_type_and_counter_caps(self):
        raw, _ = self.encrypt()
        header, records = split_envelope(raw)
        for kind, index, size in ((9, 0, 4096), (1, 0, 2**32 - 1),
                                  (1, 0, 16), (1, 2**64 - 1, 17), (2, 0, 63)):
            with self.subTest(kind=kind, index=index, size=size):
                self.reject(header + env.RECORD.pack(kind, index, size))

    def test_decrypt_plaintext_and_chunk_caps(self):
        raw, _ = self.encrypt()
        self.reject(raw, limits=env.Limits(4096, len(self.payload) - 1, 256))
        self.reject(raw, limits=env.Limits(4096, len(self.payload), 2))

    def test_encrypt_plaintext_cap_and_invalid_prefix(self):
        destination = io.BytesIO()
        with self.assertRaises(env.EnvelopeError):
            env.encrypt_stream(io.BytesIO(self.payload), destination, self.public,
                               CONTEXT, env.Limits(4096, len(self.payload) - 1, 256))
        self.reject(destination.getvalue())

    def test_encrypt_chunk_cap_and_invalid_prefix(self):
        destination = io.BytesIO()
        with self.assertRaises(env.EnvelopeError):
            env.encrypt_stream(io.BytesIO(self.payload), destination, self.public,
                               CONTEXT, env.Limits(4096, len(self.payload), 2))
        self.reject(destination.getvalue())

    def test_exact_plaintext_cap(self):
        limit = env.Limits(4096, len(self.payload), 3)
        raw, _ = self.encrypt(limits=limit)
        self.decrypt(raw, limits=limit)
        self.assertEqual(self.output.read_bytes(), self.payload)

    def test_zero_plaintext_cap(self):
        limit = env.Limits(4096, 0, 1)
        raw, _ = self.encrypt(b"", limits=limit)
        self.decrypt(raw, limits=limit)
        with self.assertRaises(env.EnvelopeError):
            self.encrypt(b"x", limits=limit)

    def test_invalid_local_limits_and_context(self):
        for limits in (env.Limits(0, 1, 1), env.Limits(4096, -1, 1),
                       env.Limits(4096, 0, 0), env.Limits(True, 1, 1),
                       env.Limits(4096, env.HARD_MAX_PLAINTEXT_BYTES + 1, 1),
                       env.Limits(4096, 1, env.HARD_MAX_CHUNKS + 1)):
            with self.subTest(limits=limits), self.assertRaises(env.EnvelopeError):
                self.encrypt(limits=limits)
        for context in (env.Context("", "data", "run"), env.Context("id", "x\ny", "run"),
                        env.Context("id", "data", "x" * 129)):
            with self.subTest(context=context), self.assertRaises(env.EnvelopeError):
                self.encrypt(context=context)

    def test_existing_destination_preserved(self):
        raw, _ = self.encrypt()
        self.output.write_bytes(b"original")
        with self.assertRaises(env.EnvelopeError):
            self.decrypt(raw)
        self.assertEqual(self.output.read_bytes(), b"original")
        self.assertEqual(list(self.work.glob(".od-envelope-*")), [])

    def test_destination_symlink_preserved(self):
        raw, _ = self.encrypt()
        target = self.work / "target"
        target.write_bytes(b"original")
        self.output.symlink_to(target)
        with self.assertRaises(env.EnvelopeError):
            self.decrypt(raw)
        self.assertTrue(self.output.is_symlink())
        self.assertEqual(target.read_bytes(), b"original")

    def test_racing_destination_creation_never_overwrites(self):
        raw, _ = self.encrypt()
        original_link = os.link
        def racing_link(*args, **kwargs):
            self.output.write_bytes(b"racer")
            return original_link(*args, **kwargs)
        with mock.patch.object(env.os, "link", side_effect=racing_link):
            with self.assertRaises(env.EnvelopeError):
                self.decrypt(raw)
        self.assertEqual(self.output.read_bytes(), b"racer")
        self.assertEqual(list(self.work.glob(".od-envelope-*")), [])

    def test_temp_collision_does_not_remove_unowned_file(self):
        raw, _ = self.encrypt()
        existing = self.work / (".od-envelope-" + "a" * 32 + ".partial")
        existing.write_bytes(b"other-operation")
        with mock.patch.object(env.secrets, "token_hex", return_value="a" * 32):
            with self.assertRaises(env.EnvelopeError):
                self.decrypt(raw)
        self.assertEqual(existing.read_bytes(), b"other-operation")
        self.assertFalse(self.output.exists())

    def test_output_parent_must_be_private(self):
        raw, _ = self.encrypt()
        os.chmod(self.work, 0o755)
        try:
            self.reject(raw)
        finally:
            os.chmod(self.work, 0o700)

    def test_output_parent_symlink_refused(self):
        raw, _ = self.encrypt()
        alias = self.root / (self.work.name + "-alias")
        alias.symlink_to(self.work, target_is_directory=True)
        try:
            self.reject(raw, destination=alias / "restored.bin")
        finally:
            alias.unlink()

    def test_fsync_failure_before_publish_cleans_temp(self):
        raw, _ = self.encrypt()
        with mock.patch.object(env.os, "fsync", side_effect=OSError("SENSITIVE")):
            self.reject(raw)

    def test_keygen_permissions_fingerprint_and_location(self):
        self.assertEqual(stat.S_IMODE(self.keys.stat().st_mode), 0o700)
        self.assertEqual(stat.S_IMODE(self.private.stat().st_mode), 0o600)
        self.assertEqual(self.private.stat().st_nlink, 1)
        key = serialization.load_pem_public_key(self.public)
        self.assertEqual(self.key_receipt["keyFingerprint"], env._fingerprint(key))
        self.assertNotIn("PRIVATE KEY", json.dumps(self.key_receipt))
        self.assertEqual(list(self.backup.iterdir()), [])

    def test_keygen_rejects_backup_key_nesting_and_reuse(self):
        for keydir, backup in ((self.backup / "nested", self.backup),
                               (self.keys, self.backup), (self.backup, self.backup),
                               (self.root, self.backup)):
            with self.subTest(keydir=keydir), self.assertRaises(env.EnvelopeError):
                env.generate_keypair(keydir, backup)

    def test_private_key_permissions_symlink_and_hardlink(self):
        raw, _ = self.encrypt()
        os.chmod(self.private, 0o644)
        try:
            self.reject(raw)
        finally:
            os.chmod(self.private, 0o600)
        alias = self.keys / "alias.pem"
        alias.symlink_to(self.private)
        try:
            self.reject(raw, private_key_path=alias)
        finally:
            alias.unlink()
        os.link(self.private, alias)
        try:
            self.reject(raw)
        finally:
            alias.unlink()

    def test_keyfile_size_and_weak_key_rejected(self):
        oversized = self.work / "oversized.pem"
        oversized.write_bytes(b"x" * (env.MAX_KEY_BYTES + 1))
        with self.assertRaises(env.EnvelopeError):
            env.read_public_key(oversized)
        weak = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        weak_pem = weak.public_key().public_bytes(serialization.Encoding.PEM,
            serialization.PublicFormat.SubjectPublicKeyInfo)
        with self.assertRaises(env.EnvelopeError):
            env.encrypt_stream(io.BytesIO(b"x"), io.BytesIO(), weak_pem, CONTEXT, LIMITS)

    def test_io_and_custom_exceptions_are_sanitized(self):
        for error in (OSError("SENSITIVE-INPUT"), env.EnvelopeError("SENSITIVE-INPUT")):
            source = mock.Mock()
            source.read.side_effect = error
            with self.subTest(error=type(error).__name__):
                with self.assertRaises(env.EnvelopeError) as caught:
                    env.encrypt_stream(source, io.BytesIO(), self.public, CONTEXT, LIMITS)
                self.assertEqual(str(caught.exception), "backup envelope operation failed")
                self.assertTrue(caught.exception.__suppress_context__)

    def test_cli_stdin_stdout_round_trip(self):
        common = ["--identity", CONTEXT.identity, "--purpose", CONTEXT.purpose,
                  "--run-id", CONTEXT.run_id, "--chunk-bytes", "4096", "--max-bytes", "1048576"]
        encryption = subprocess.run([sys.executable, "-B", str(ROOT / "backup_envelope.py"),
            "encrypt", "--public-key", str(self.keys / "public.pem"), *common],
            input=self.payload, capture_output=True, timeout=20)
        self.assertEqual(encryption.returncode, 0, encryption.stderr.decode())
        self.assertTrue(encryption.stdout.startswith(env.MAGIC))
        receipt = json.loads(encryption.stderr)
        decryption = subprocess.run([sys.executable, "-B", str(ROOT / "backup_envelope.py"),
            "decrypt", "--private-key", str(self.private), "--output", str(self.output), *common],
            input=encryption.stdout, capture_output=True, timeout=20)
        self.assertEqual(decryption.returncode, 0, decryption.stderr.decode())
        self.assertEqual(decryption.stdout, b"")
        self.assertEqual(json.loads(decryption.stderr), receipt)
        self.assertEqual(self.output.read_bytes(), self.payload)

    def test_cli_sanitizes_invalid_arguments_and_input(self):
        first = subprocess.run([sys.executable, "-B", str(ROOT / "backup_envelope.py"),
            "encrypt", "--SENSITIVE-ARGUMENT"], capture_output=True, timeout=20)
        self.assertEqual(first.returncode, 2)
        self.assertEqual(first.stderr, b"backup envelope: invalid arguments\n")
        self.assertEqual(first.stdout, b"")
        second = subprocess.run([sys.executable, "-B", str(ROOT / "backup_envelope.py"),
            "decrypt", "--private-key", str(self.private), "--output", str(self.output),
            "--identity", CONTEXT.identity, "--purpose", CONTEXT.purpose,
            "--run-id", CONTEXT.run_id, "--max-bytes", "1048576"],
            input=b"SENSITIVE-INPUT", capture_output=True, timeout=20)
        self.assertEqual(second.returncode, 1)
        self.assertEqual(second.stderr, b"backup envelope: operation failed\n")
        self.assertEqual(second.stdout, b"")
        self.assertFalse(self.output.exists())


if __name__ == "__main__":
    unittest.main(verbosity=2)

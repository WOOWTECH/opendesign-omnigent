# Authenticated recovery archives

These Linux operator utilities implement encrypted archives and OD volume
capture/restore. They are separate from the plugin ZIP. They do not discover
credentials, configure Kubernetes, capture PostgreSQL, deploy a service, or
establish a coordinated recovery point by themselves.

Run the commands below from `deploy/omnigent/recovery/sealed/` in the repository.

Use Python 3.11+ and a maintained `cryptography` installation. A restore helper
must use a SQLite version compatible with the source databases. The real drill
used the application's Python 3.12 / SQLite 3.46.1 image, including JSONB and FTS5.
Running an older SQLite library against a newer database can falsely report
corruption or fail to open it.

## Separate key custody

Create private parent directories first. Keep the key directory separate from
the archive directory, and preserve an independently protected copy of the key.
The local separation tested here does not establish off-site or KMS recovery.

```bash
python3 -B crypto/backup_envelope.py keygen \
  --private-key-dir /private/keys/run-001 --backup-dir /private/backups/run-001
```

Only the public key goes to a capture helper. The format uses RSA-OAEP-SHA256 to
wrap a fresh AES-256-GCM key, with authenticated context, ordered bounded chunks,
and a final authenticated record. Decryption verifies the complete archive
before publishing a new plaintext file. Wrong keys, context, ordering,
truncation, extra bytes and existing destinations are rejected. A private-key
path requires its own mode-0700 parent and a mode-0600 file owned by the caller.

## Capture OD data and home

Mount both source volumes **read-only** in a dedicated helper. Scratch and target
paths must be separate from both sources and from each other. The target must
not exist. Scratch needs room for the largest SQLite snapshot; the encrypted
archive is an uncompressed tar and needs space for the complete volume contents.

```bash
python3 -B od-volume/od_volume.py backup \
  --od-root /source/od --home-root /source/home --scratch /scratch/sqlite \
  --target /backups/od.enc --public-key /keys/public.pem \
  --identity example-od --run-id run-001 --sqlite-timeout 120 --paseo-runtime
```

Ordinary files must remain stable. SQLite files use the native backup API and
`quick_check`; WAL/SHM are not restored independently. On a read-only mount, an
idle WAL-format database without sidecars may require a guarded stable copy in
writable scratch before the same native backup API can open it. A new sidecar,
changed source, or failed integrity check aborts capture.

`--paseo-runtime` opts into the reviewed native Paseo `state.json` atomic writer
and exact lock/temp filenames in `od/paseo` only. The state inode is pinned and
hashed; bounded Linux inotify events account for locks which appear and vanish
between listings. Unknown events and overflow fail closed. This is not a generic
ignore pattern for JSON files, secrets, temporary files or other directories.
The authenticated manifest records observed lifecycle changes. All unrelated
persistent files remain subject to the normal checks.

This is **per-file and per-SQLite consistency, not a globally atomic OD snapshot**.
No generic append policy was used in the real acceptance. Changes to any other
live file may require a retry or a separately planned application quiesce.

## Restore to new volumes

Create empty destination roots and private scratch. A suitably scoped root
helper is needed to restore different owners, including the MCP service owner.
Do not point this command at a running installation or its original volumes.
The tested container drops all capabilities, then adds `CHOWN`, `FOWNER`,
`DAC_OVERRIDE` and `FSETID`; it has no host namespace access and does not run
privileged. `FSETID` is needed to preserve source setgid directories when their
group differs from the helper's group. A helper missing it was correctly rejected
by the final metadata checks during the real drill.

```bash
python3 -B od-volume/od_volume.py restore \
  --source /backups/od.enc --private-key /private/keys/run-001/private.pem \
  --od-root /restore/od --home-root /restore/home --scratch /scratch/restore \
  --identity example-od --run-id run-001
```

The helper authenticates the envelope, validates all tar paths, types and hashes,
then restores file data, ownership and modes and rechecks SQLite databases.
Retain the trusted ciphertext hash and receipt separately. A successful volume
restore does not establish authentication, provider access or GUI functionality;
those require native application acceptance after isolated controller startup.

## Tests

Run from this directory. Fixtures contain synthetic data and make no production
or network calls.

```bash
python3 -B -m unittest discover -s crypto -p 'test_*.py'
python3 -B -m unittest discover -s od-volume -p 'test_*.py'
```

Do not distribute real archives, private keys, credential inventories, raw
runtime configuration, browser storage state or private session logs with this
source package.

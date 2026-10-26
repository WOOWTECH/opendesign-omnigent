# Omnigent recovery and version lifecycle

These operator tools restore a backed-up **server UI tree**. They are separate
from the downloadable OD plugin ZIP and do not restore a database, credentials,
host configuration, attachments, Helm release, or complete service.

## Restore a UI tree into a new directory

Use Python 3.11 or newer on Linux. The utility uses Python's standard library and
Linux `renameat2(RENAME_NOREPLACE)` so it can run beside the Python server without
installing Node or any agent engine. Obtain the archive's SHA-256 from your trusted
backup receipt through a separate trusted channel.

```bash
python3 -B deploy/omnigent/recovery/safe_restore.py \
  /private/backups/ui-tree.tar.gz \
  "$trusted_archive_sha256" \
  /private/ui-releases/restored-tree
```

The destination's parent must already exist and be trusted. The destination must
not exist; existing files, directories, and symlinks are refused. The command
verifies the archive before extraction, stages files privately, rereads the full
inventory, and publishes the new directory atomically. It does not change the
active server UI setting. Keep the parent stable while the command runs.

The archive must contain a regular root `index.html` and canonical relative paths.
Links, special entries, path collisions, traversal, malformed/truncated archives,
and file/directory conflicts are refused. Archive ownership, modes, and timestamps
are not restored. Limits are 32 MiB compressed, 96 MiB of file contents, 16 MiB per
file, and 2,000 members, with a separate cap on tar metadata/decompression. These
limits accommodate the verified 1,559-file serving tree, including older hashed
assets needed by clients that have already loaded an earlier UI.

The JSON receipt contains archive and full inventory hashes. `--inventory` adds
the complete file map. Exit 0 means a new UI tree was restored; it does not mean a
service was switched or is healthy. Atomic visibility is covered; power-loss
durability of publication has not been tested.

```bash
python3 -B -m unittest discover -s deploy/omnigent/recovery -p 'test_*.py'
python3 -O -B -m unittest discover -s deploy/omnigent/recovery -p 'test_*.py'
```

The unit suite is self-contained. Operators holding the two receipt-pinned
serving archives can separately run the full archive acceptance:

```bash
python3 -B deploy/omnigent/recovery/verify_real_archives.py /private/backups
```

This explicit check requires `served-reconstruction.tar.gz` and
`rollback-serving-tree.tar.gz`. Missing archives fail; they are never silently
counted as tested or substituted with small fixtures.

## Activate or roll back through the native deployment configuration

Before activation, capture the actual PaaS desired values, current Helm revision,
immutable image digest, resources, backend overlays, Secret references and PVC
identities. Update the server's `OMNIGENT_WEB_UI_DIST` through the native PaaS
configuration/operator path to select the verified tree. Check the new process's
environment, native health endpoint, login page, entry assets and authenticated
reads before declaring activation complete.

Choose the serving root explicitly. Older scripts which replace an init-container
command do not switch a UI root already configured on a persistent volume. Do not
assume an earlier Helm revision preserves current resources or security settings.

The native server rewrites relative URLs in `index.html` and removes the fallback
`base` tag. Record both the source-index and served-index identities; comparing the
unmodified source-index hash directly with an HTTP response gives a false failure.
Validate the expected asset URLs and bytes, retained historical assets, and a 404
for a missing asset. A 200 health response alone cannot establish UI rollback.

## Plugin version, SDK version and image version are separate

The candidate OD build bundles the Omnigent launcher. Its native plugin upgrade
endpoint returns HTTP 409 `bundled-plugin`: durable upgrades follow the daemon
image lifecycle. Actual ZIP reimports of 0.1.5 → 0.1.6 → 0.1.5 → 0.1.6 work in a
running daemon, but a restart re-registers the image's bundled version. Uploading
an older ZIP therefore does **not** provide a persistent rollback of that build.

The SDK remains 0.16.0, with the operator patch bundle identified separately as
0.16.0-od.2. Replacing the launcher ZIP does not downgrade the SDK or remove the
native interrupt patch. Keep the tested image, daemon, SDK patch manifest and
plugin version together when performing an image rollback. Use an isolated copy
of application data to test compatibility first.

The 2026-10-26 isolated image test completed 0.1.5 → 0.1.6 → 0.1.5 → 0.1.6
using fresh containers and the same synthetic OD data volume. The project,
settings, disabled Omnigent preference and SQLite integrity survived every step.
Both tested images use OD 0.24.1 and SDK 0.16.0 with different reviewed patch sets;
this establishes compatibility for these two integration images, not arbitrary
OD or database-schema versions. The exact image digests are in the receipt.

SDK patch validation rejects unknown target hashes before writes. A synchronous
failure during replacement can leave earlier targets patched; the set is not a
filesystem transaction. Verification refuses an incomplete set and retry can
finish it. Do not claim power-loss or concurrent-process safety from that test.

## Acceptance scope

The dated validation receipt beside this document records independently checked
results. The service drill uses the actual pinned server image, the two verified
backend overlays, PostgreSQL 16.15, native accounts login and project APIs, and
the full serving UI tree. It uses synthetic data and an isolated shared loopback
network namespace with no published ports or external network access.

Production disaster recovery additionally requires a consistent database/PVC
backup set, protected credential recovery, database roles/extensions/ACLs,
attachment-reference integrity, controller reconciliation and a timed restore of
the actual data volume. A synthetic PostgreSQL restore does not establish an RPO
or RTO for the production database, nor does a UI rollback establish support for
downgrading database schemas. Full commercial acceptance remains gated on those
operational checks and supported version combinations.

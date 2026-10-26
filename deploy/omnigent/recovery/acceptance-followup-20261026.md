# 2026-10-26 GUI and production-backup acceptance follow-up

The four installed engines now pass the native GUI path: OD Plugins → Omnigent
settings → Open Omnigent → fresh web login → host/workspace/engine/model picker →
chat. Each returned a unique requested reply, retained it after reload, and had
its test runner stopped. Pi and OpenCode used the dedicated LiteLLM configuration.

| Engine | Selected model | Reply and reload | Interrupt and reload |
| --- | --- | --- | --- |
| Claude Code | haiku | Passed | Not tested this round |
| Codex | gpt-5.6-sol | Passed | Not tested this round |
| Pi | omnigent/toypark1234/gpt-5.6-sol | Passed | Passed, 4.249 s to idle |
| OpenCode | litellm/toypark1234/gpt-5.6-sol | Passed | Passed, 3.347 s to idle |

OpenCode's backend `reportedModel` remained null. The selected model is verified
through the picker/request override and provider configuration, not invented from
that absent metadata. This was an existing installation test; it does not repeat
clean install, credential replacement or arbitrary provider combinations.

## Source validation

The full root typecheck passed all 27 workspaces plus root scripts with Node
24.21.0 and pnpm 10.33.2. The earlier missing `zod` dependency was resolved by
materializing the existing frozen lockfile and running native postinstall.
`pnpm guard` passed. Tracked source, package manifests and lockfile were unchanged.
This supersedes the earlier local dependency blocker, not the historical receipt.

## Actual native backup restored in isolation

The existing daily PostgreSQL custom-format backup from 2026-10-25 19:30 UTC
(2,585,934,201 bytes) was read through a temporary read-only PVC
mount. Its source, protected local copy and isolated database input had identical
SHA-256. No new production database dump or production shutdown was performed.

The backup was restored to a fresh PostgreSQL 16.15 volume with `pg_restore
--exit-on-error --jobs=2`, retaining the dump's owners and ACL instructions.
The `omnigent` database role was recreated for the clone; this is not a test of
cluster-wide role/password restoration. The isolated database restore took
305.65 seconds, excluding download, upload and
service checks. This is not a production RTO commitment.

All 2,514 captured attachment/module/UI files were verified after extraction.
The restored database contains 2,364,624
conversation items and 2,673 conversations.
Reference checks before native server startup found:

- File rows matched: 724 / 724.
- Missing file references: 0; size mismatches: 0.
- Module bundles matched: 228 / 228.
- Missing module bundles: 0.
- Relationship counts: `{"fileSessionOrphans": 0, "metadataConversationOrphans": 0, "metadataProjectOrphans": 0}`.

The reference-completeness gate passed for this restored set.

The receipt explicitly gates reference completeness; successful sample downloads
cannot hide missing references. Generic conversations without Omnigent session
metadata are counted separately and are not automatically treated as corruption.

The exact pinned server and its two live backend overlays passed native health,
new clone-only login, cross-owner project denial, original-owner project reads,
restored conversation reads, hash-matched attachment downloads with safe response
headers and conditional HTTP 304, and new project write/read. These are native API
and asset checks, not a browser GUI test against the restored clone.

The database had no network; the server shared only its loopback namespace with
no published ports. Active schedules were paused only in the clone. A fresh cookie
secret and test passwords were created there; no production runtime authentication
environment was copied. The protected DB backup itself still contains sensitive
application data and must never be published. Owned containers, volumes and the
derived test image were removed after the run.

## Findings addressed during verification

- A truncated large backup transfer failed hash verification. The reader now
  transfers bounded chunks and verifies the entire persisted result.
- Docker initially populated a new data volume from the server image. The restore
  rejected the nonempty destination; `volume-nocopy` now makes the volume fresh.
- The first project probe incorrectly expected a new admin to read another
  owner's private project. The final probe verifies denial and a separate owner
  read, without changing restored ownership.
- Attachment and module checks now gate all references before boot, while native
  endpoint tests retain separate results.

## Production state and remaining acceptance gates

OD currently reports Helm revision 40. Historical revision 38 is superseded, but
their chart content, values and rendered workload are equal; the same Pod and
SDK patch manifests remain healthy. Omnigent remains at Helm revision 6 with
matching PaaS/Helm/runtime UI and backend overlays. Both services retained zero
container restarts during this verification. No production deployment was made.

The database backup and artifact capture are from different times. This does not
establish a coordinated recovery point, lossless current-state recovery, protected
secret restoration, existing-cookie continuity or a controller/cluster rebuild.
The restored clone was not reconnected to live runners or providers. Arbitrary
core/schema upgrades, multiple workspaces and sustained load/fault behavior remain
outside this acceptance. The downloadable v0.1.6 remains a preview release.

The [sanitized receipt](acceptance-followup-20261026.json) records exact versions,
counts, scoped results and evidence hashes. Raw backups, credentials, session IDs,
private logs and infrastructure-specific drill scripts are not distributed.

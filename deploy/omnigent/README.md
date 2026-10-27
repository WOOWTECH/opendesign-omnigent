# Omnigent derivative packaging

For guarded UI-tree restoration, actual lifecycle results, and the distinction
between launcher ZIP replacement and durable image rollback, see
[Recovery and version lifecycle](recovery/README.md).

## v0.1.6 host SDK overlay

This release fixes an OpenCode retry that otherwise leaves an exhausted provider
quota invisible in the conversation. Explicit `usage_limit_reached` and
`insufficient_quota` retry errors produce one durable notice per turn. A separate
native interrupt patch wires Interrupt / Stop to OpenCode's session abort.
Confirm backend retries have stopped before choosing another model; clearing
the UI busy indicator is not evidence of cancellation. Temporary rate limits
retain native retries until the user interrupts.

The v0.1.6 candidate is not accepted solely on quota visibility or a successful
reply. Final acceptance must include the native interrupt regression suite and
a real GUI stop followed by a successful conversation with an available model.

The complete current Paseo/MCP image is pinned to digest `daf04fb889bd0545b3af14b2422305f86d2e5b77346498f6e0ca3a6c05e82a0b`
in Dockerfile and `base-overlay.json`. The overlay updates only the dependency
installer, reviewed SDK patch assets and the Omnigent launcher version. The
existing web output, branding, Paseo runtime, MCP additions, native CLI versions,
environment and entrypoint are inherited. Chart and service configuration are
handled separately during the guarded single-service deployment.

`verify-overlay.py` checks every unchanged compiled daemon file and the exact
base MCP hashes. The packager rejects web/contracts source changes relative to
`b3270069b93548bbfced8cb17fa90039fd88422a` and rejects unreviewed daemon changes.
This host-only contract must be deliberately updated before a frontend release.

The SDK remains version 0.16.0. A shared manifest pins the original and result
hashes of `host/connect.py`, `harnesses/opencode_native/forwarder.py`,
`runner/native/interrupt.py`, `harnesses/pi_native/credentials.py`, and
`harnesses/opencode_native/provider.py`.
The Pi patch includes configured `models.json` providers in Omnigent's model
picker without exposing their API keys. The OpenCode patch combines provider
definitions from both `opencode.jsonc` and `opencode.json` when both exist.
Each target occurs in exactly one patch.
Image builds apply all five patches before switching back to the application user.
Private installs verify or apply the same patches on installation, update and
reuse. External SDK installations are inspected without modification; an
incompatible external SDK falls back to the integration's private installation.

## Prepare the context

Finish the daemon host integration, commit all source/deploy changes, and build
the daemon from that final revision using its package-scoped command.
The packager consumes existing outputs and does not prove their build provenance;
retain the build logs with the packaging receipt. It refuses dirty source,
missing host assets, stale SDK patch copies, symlinks and special files.

Run from the repository root, with Python 3.11 or newer:

```bash
od_revision=$(git rev-parse HEAD)
od_context=/tmp/od-omnigent-context-${od_revision}.tar.gz
python3 -B deploy/omnigent/build-context.py \
  --expected-revision "$od_revision" "$od_context" > "${od_context}.json"
```

The JSON receipt includes source revision, base digest, manifest checksum,
archive checksum, size and file count. The archive contains a complete overlay
inventory with SHA-256 and modes, source lockfile checksum, plugin version, SDK
patch contract and host-kit asset names. `ARTIFACT_SHA256` means the checksum of
`manifest.json`; `archiveSha256` is the separate tar.gz checksum.

## Candidate build and publish, without the global pipeline

Build and deployment work on 2026-10-25 confirmed:

- `gitea/act-runner-runner-0`, container `dind`: Docker 28.5.2, linux/amd64,
  buildx 0.29.1. The sibling runner also has Docker 28.5.2.
- `/tmp/od-omnigent-build-auth/config.json` exists in the first runner's dind
  container. The authorized candidate build and push succeeded using that
  existing credential. Its contents are not copied into the build context.
- The historical local reference
  `/data/pi-agent/home/work/od-omnigent-team/private/build-candidate.py` uses this
  Docker socket via `kubectl exec`; it does not use Kaniko. Do not run that script
  unchanged: it packages another checkout, assumes the old SDK image path and
  immediately builds/pushes.

The commands below reproduce the release build. They build and push only
a unique candidate tag. They do not dispatch the paired core/PaaS workflow,
promote `latest`, change platform templates or update a workload. Do not print or
copy the runner's auth file. If its existing auth has expired, stop and use the
authorized registry credential provisioning flow.

```bash
set -euo pipefail
od_revision=$(git rev-parse HEAD)
od_context=/tmp/od-omnigent-context-${od_revision}.tar.gz
od_manifest_sha=$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["manifestSha256"])' "${od_context}.json")
od_archive_sha=$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["archiveSha256"])' "${od_context}.json")
printf '%s  %s\n' "$od_archive_sha" "$od_context" | sha256sum -c -
od_tag="$(date -u +%Y%m%d.%H%M%S)-omnigent-${od_revision:0:12}-candidate"
od_image="jcr-prod.woowtech.io/woow-paas-docker-local/open-design:${od_tag}"
od_kube=(/data/pi-agent/home/.local/bin/kubectl
  --kubeconfig=/data/pi-agent/home/.kube/config --context=woow-k3s)
od_docker=("${od_kube[@]}" exec -i -n gitea act-runner-runner-0 -c dind --
  docker --config /tmp/od-omnigent-build-auth)

"${od_docker[@]}" build --platform linux/amd64 --network=none \
  --build-arg "SOURCE_REVISION=$od_revision" \
  --build-arg "ARTIFACT_SHA256=$od_manifest_sha" \
  -t "$od_image" - < "$od_context"
"${od_docker[@]}" push "$od_image"
"${od_docker[@]}" image inspect --format '{{json .RepoDigests}}' "$od_image"
```

The base pull can use registry access; `--network=none` applies to Dockerfile RUN
steps, which require no package downloads. Preserve the push digest and use a
tag plus digest for the later service244-only update. `crane` is available locally
for registry inspection with an already authorized login, but is not an image
builder. No standalone Kaniko/BuildKit service was found; the existing dind
builder is the confirmed path.

The service244 image-only desired-state update uses the existing Odoo
controller/operator path. The immediately preceding service snapshot is the rollback reference.
Use the current baseline revision and chart version; reject concurrent changes. The packaging commands themselves do not update a workload or a
platform template. A normal restart preserves the configured image; the PaaS
Redeploy action may reapply a template image, so check the desired image before
using that separate action.

## Verification steps

- Build final daemon assets after the dependency installer and SDK changes land.
- Run the [installed SDK regression suite](sdk-patches/tests/native/README.md)
  against the entire SDK in a disposable candidate container. The unpatched
  base is the red control. Run the complete forwarder/reconnect suites plus the
  native interrupt suite; record the collected counts and failures for this
  exact candidate. The earlier 63-test quota suite alone is insufficient.
- Verify SDK reuse in a fresh container, then host lifecycle,
  GUI connection and model conversations. A successful image build verifies
  imports and native CLI resolution; it does not prove server login or replies.
- Build/push the candidate and retain its immutable digest.
- Trigger an explicitly exhausted model through the real GUI. Verify the quota
  notice appears and survives reload. Interrupt / Stop must reach native abort
  and end backend retries, with no continuing provider requests from that turn;
  a stopped-looking GUI alone does not pass. After confirming cancellation,
  obtain a reply from an available model. A visible catalog does not prove quota.
- In the actual tenant, test the native launcher, fresh connection form,
  disconnect, disable, re-enable and restart persistence. Check the original
  MCP Server and CLI Terminal launchers as well.
- Missing provider credentials are an incomplete conversation acceptance,
  even if the host is online. Record each engine separately and only report
  Ready after its real reply and test-session cleanup pass.

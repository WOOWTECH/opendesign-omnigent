# Omnigent derivative packaging

## v0.1.5 current-base overlay

During acceptance the target advanced from chart 0.5.7/revision29 to chart
0.5.8/revision30 with a newer Paseo/Omnigent image. The deployment preflight
rejected the stale baseline. The current base is now pinned in Dockerfile and
`base-overlay.json`; no chart or service values are reverted.

Image file hashes showed the same web output and three native MCP daemon
additions/updates, plus the Paseo launcher. Preserve that complete image and
replace only the reviewed Omnigent adapter, rebuilt web output and Omnigent
manifest. `verify-overlay.py` checks every unchanged compiled daemon file and
the exact base MCP hashes. A future additional backend change fails the build
until the overlay contract is deliberately extended. The packager also refuses
source edits to the base-owned MCP units that this release preserves.

The SDK, native CLI versions, Paseo files, launcher, environment and entrypoint
are inherited. The build verifies the existing SDK and applies the normal
branding scripts to the new web output. The former SDK donor/split-layer files
remain as historical packaging references; this release does not recopy them.

## Prepare the context

Finish the daemon host integration, commit all source/deploy changes, and build
the daemon and web from that final revision using their package-scoped commands.
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
archive checksum, size and file count. The archive contains a complete file
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

- Build final daemon assets after the private supervisor and SDK changes land.
- Verify SDK reuse in a fresh Alpine container, then host lifecycle,
  GUI connection and model conversations. A successful image build verifies
  imports and native CLI resolution; it does not prove server login or replies.
- Build/push the candidate and retain its immutable digest.
- In the actual tenant, test the native launcher, fresh connection form,
  disconnect, disable, re-enable and restart persistence. Check the original
  MCP Server and CLI Terminal launchers as well.
- Missing provider credentials are an incomplete conversation acceptance,
  even if the host is online. Record each engine separately and only report
  Ready after its real reply and test-session cleanup pass.

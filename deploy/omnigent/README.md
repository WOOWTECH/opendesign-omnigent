# Omnigent derivative packaging

## Scope

The Dockerfile inherits the complete PaaS `20261024.0943` image by digest. It
replaces daemon dist and web out, and adds the bundled Omnigent plugin. Existing
MCP, native CLI versions, contracts runtime, entrypoint and command come from
that base. It installs neither engines nor an image-wide Omnigent SDK.

The daemon owns private SDK installation, hash-verified SDK patching and the
private host supervisor. Its build must include SDK patch assets, `supervisor.sh`
and `og_helper.py` below `dist/integrations/omnigent/`. The packager copies the
entire dist tree, including other kit assets and executable modes. The reviewed
SDK patch stays in `deploy/omnigent/sdk-patches`; `build-assets.ts` copies it to
dist. There is no second Dockerfile patch operation.

The inherited branding scripts run again on the replacement web/daemon output.
Manifest file hashes describe the input bytes before those scripts run, not the
final transformed image filesystem.

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

Read-only inspection on 2026-10-25 confirmed:

- `gitea/act-runner-runner-0`, container `dind`: Docker 28.5.2, linux/amd64,
  buildx 0.29.1. The sibling runner also has Docker 28.5.2.
- `/tmp/od-omnigent-build-auth/config.json` exists in the first runner's dind
  container. Only existence was inspected; authentication and registry writes
  have not been attempted in this change.
- The historical local reference
  `/data/pi-agent/home/work/od-omnigent-team/private/build-candidate.py` uses this
  Docker socket via `kubectl exec`; it does not use Kaniko. Do not run that script
  unchanged: it packages another checkout, assumes the old SDK image path and
  immediately builds/pushes.

The commands below are prepared for the release owner. They build and push only
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

The main release owner handles the service244 image-only desired-state update
and verification through the existing Odoo controller/operator path. The
service244 snapshot at revision 27 is the rollback reference. This packaging
change does not edit Helm values, charts, templates or running services.

## Remaining release gates

- Build final daemon assets after the private supervisor and SDK changes land.
- Verify fresh dependency installation on the Alpine base, then host lifecycle,
  GUI connection and model conversations. The historical SDK layer needed musl
  build support for `google-re2` and `claude-agent-sdk`; packaging is not evidence
  that the new private installation succeeds.
- Build/push the candidate and retain its immutable digest. No image build,
  registry push or deployment was performed while preparing these files.

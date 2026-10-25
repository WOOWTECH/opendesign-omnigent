FROM jcr-prod.woowtech.io/woow-paas-docker-local/open-design:20261024.0943@sha256:5252ac5b3d97b094f3093619ac7eab8003455c817687f9d474f3bf9e26f9e5ce
ARG SOURCE_REVISION
# SHA-256 of manifest.json (the archive checksum is recorded separately).
ARG ARTIFACT_SHA256
USER root
RUN rm -rf /app/apps/daemon/dist /app/apps/web/out
# Private SDK installation and hash-verified patching belong to dependencies.js.
# build-assets.js carries the reviewed patch assets inside daemon-dist.
COPY --chown=1001:1001 daemon-dist/ /app/apps/daemon/dist/
COPY --chown=1001:1001 web-out/ /app/apps/web/out/
COPY --chown=1001:1001 bundled-omnigent/ /app/plugins/_official/woow/woow-omnigent/
COPY --chown=1001:1001 manifest.json /usr/local/share/woow/omnigent-native-build.json
USER open-design
RUN python3 -B -c 'import hashlib,json,pathlib,sys; raw=pathlib.Path("/usr/local/share/woow/omnigent-native-build.json").read_bytes(); m=json.loads(raw); assert hashlib.sha256(raw).hexdigest()==sys.argv[2], "manifest checksum mismatch"; assert m["sourceRevision"]==sys.argv[1] and len(sys.argv[1])==40, "source revision mismatch"' "$SOURCE_REVISION" "$ARTIFACT_SHA256" \
 && python3 /usr/local/share/woow/patches/export-basehref.py /app/apps/web/out \
 && python3 /usr/local/share/woow/patches/zh-tw-locale.py apply /app/apps/web/out /usr/local/share/woow/patches/zh-TW-s2twp.json \
 && python3 /usr/local/share/woow/patches/white-label.py /app/apps/web/out \
 && python3 /usr/local/share/woow/patches/white-label-links.py /app/apps/web/out \
 && python3 /usr/local/share/woow/patches/snapshot-inline-img.py /app \
 && test "$(id -u)" = 1001 \
 && node --check /app/apps/daemon/dist/cli.js \
 && python3 -B -c 'import pathlib; p=pathlib.Path("/app/apps/daemon/dist/integrations/omnigent/adapter.py"); compile(p.read_text(), str(p), "exec")' \
 && test -s /app/apps/daemon/dist/integrations/omnigent/sdk-patches/manifest.json \
 && test -s /app/plugins/_official/woow/woow-omnigent/open-design.json
LABEL org.opencontainers.image.revision="${SOURCE_REVISION}" \
      io.woow.opendesign.upstream-revision="89e64d813bb1c7a11519b3f668f011f7017637d7" \
      io.woow.opendesign.artifact-sha256="${ARTIFACT_SHA256}" \
      io.woow.opendesign.manifest-sha256="${ARTIFACT_SHA256}" \
      io.woow.opendesign.omnigent-integration="0.1.4"

FROM jcr-prod.woowtech.io/woow-paas-docker-local/open-design@sha256:daf04fb889bd0545b3af14b2422305f86d2e5b77346498f6e0ca3a6c05e82a0b
ARG SOURCE_REVISION
ARG ARTIFACT_SHA256
USER root
COPY manifest.json base-overlay.json verify-overlay.py /tmp/omnigent-overlay/
RUN python3 -B /tmp/omnigent-overlay/verify-overlay.py \
      /tmp/omnigent-overlay/manifest.json /tmp/omnigent-overlay/base-overlay.json /app/apps/daemon/dist \
 && rm -rf /tmp/omnigent-overlay
# Inherit the complete live web, Paseo and MCP runtime; replace only reviewed host assets.
COPY --chown=1001:1001 daemon-dist/integrations/omnigent/dependencies.js daemon-dist/integrations/omnigent/dependencies.js.map daemon-dist/integrations/omnigent/build-assets.js daemon-dist/integrations/omnigent/build-assets.js.map /app/apps/daemon/dist/integrations/omnigent/
COPY --chown=1001:1001 daemon-dist/integrations/omnigent/sdk-patches/ /app/apps/daemon/dist/integrations/omnigent/sdk-patches/
COPY --chown=1001:1001 bundled-omnigent/open-design.json /app/plugins/_official/woow/woow-omnigent/open-design.json
COPY --chown=1001:1001 manifest.json /usr/local/share/woow/omnigent-native-build.json
RUN /opt/omnigent/venv/bin/python -I -B /app/apps/daemon/dist/integrations/omnigent/sdk-patches/apply.py \
      /opt/omnigent/venv/lib/python3.12/site-packages/omnigent \
      /app/apps/daemon/dist/integrations/omnigent/sdk-patches/manifest.json
USER open-design
RUN python3 -B -c 'import hashlib,json,pathlib,sys; raw=pathlib.Path("/usr/local/share/woow/omnigent-native-build.json").read_bytes(); m=json.loads(raw); assert hashlib.sha256(raw).hexdigest()==sys.argv[2], "manifest checksum mismatch"; assert m["sourceRevision"]==sys.argv[1] and len(sys.argv[1])==40, "source revision mismatch"' "$SOURCE_REVISION" "$ARTIFACT_SHA256" \
 && test "$(id -u)" = 1001 \
 && node --check /app/apps/daemon/dist/integrations/omnigent/dependencies.js \
 && /opt/omnigent/venv/bin/python -I -B /app/apps/daemon/dist/integrations/omnigent/sdk-patches/apply.py \
      /opt/omnigent/venv/lib/python3.12/site-packages/omnigent \
      /app/apps/daemon/dist/integrations/omnigent/sdk-patches/manifest.json --check \
 && /opt/omnigent/venv/bin/python -I -B -c 'import omnigent,re2,celpy,yaml,httpx; from importlib.metadata import version; assert version("omnigent")=="0.16.0"; assert re2.match("sdk", "sdk")'
LABEL io.woow.opendesign.base-image="jcr-prod.woowtech.io/woow-paas-docker-local/open-design@sha256:daf04fb889bd0545b3af14b2422305f86d2e5b77346498f6e0ca3a6c05e82a0b" \
      org.opencontainers.image.revision="${SOURCE_REVISION}" \
      io.woow.opendesign.upstream-revision="89e64d813bb1c7a11519b3f668f011f7017637d7" \
      io.woow.opendesign.artifact-sha256="${ARTIFACT_SHA256}" \
      io.woow.opendesign.manifest-sha256="${ARTIFACT_SHA256}" \
      io.woow.opendesign.omnigent-integration="0.1.6"

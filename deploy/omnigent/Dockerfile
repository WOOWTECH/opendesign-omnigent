FROM jcr-prod.woowtech.io/woow-paas-docker-local/open-design:20261005.1409@sha256:0c257c7715f87c293744252aeb905d66dca87355b93c3293fde52bc2db586c7c
ARG SOURCE_REVISION
ARG ARTIFACT_SHA256
USER root
RUN rm -rf /app/apps/daemon/dist /app/apps/web/out
RUN python3 -c 'import hashlib,pathlib; p=pathlib.Path("/opt/omnigent/venv/lib/python3.12/site-packages/omnigent/host/connect.py"); assert hashlib.sha256(p.read_bytes()).hexdigest()=="1093a7c89de966c39e598f683b2651073abf613152e4e875676135bd28138ad1"'
COPY --chown=1001:1001 opencode-host-connect.py /opt/omnigent/venv/lib/python3.12/site-packages/omnigent/host/connect.py
RUN python3 -c 'import hashlib,pathlib; p=pathlib.Path("/opt/omnigent/venv/lib/python3.12/site-packages/omnigent/host/connect.py"); assert hashlib.sha256(p.read_bytes()).hexdigest()=="2de8af76672e8008731c7fe4500eec059cd0d54b68091a8b364f631af911d75e"'
COPY --chown=1001:1001 daemon-dist/ /app/apps/daemon/dist/
COPY --chown=1001:1001 web-out/ /app/apps/web/out/
COPY --chown=1001:1001 manifest.json /usr/local/share/woow/omnigent-native-build.json
USER open-design
RUN python3 /usr/local/share/woow/patches/export-basehref.py /app/apps/web/out \
 && python3 /usr/local/share/woow/patches/zh-tw-locale.py apply /app/apps/web/out /usr/local/share/woow/patches/zh-TW-s2twp.json \
 && python3 /usr/local/share/woow/patches/white-label.py /app/apps/web/out \
 && python3 /usr/local/share/woow/patches/white-label-links.py /app/apps/web/out \
 && python3 /usr/local/share/woow/patches/snapshot-inline-img.py /app \
 && test "$(id -u)" = 1001 \
 && node --check /app/apps/daemon/dist/cli.js \
 && /opt/omnigent/venv/bin/python -m py_compile /app/apps/daemon/dist/integrations/omnigent/adapter.py
LABEL org.opencontainers.image.revision="${SOURCE_REVISION}" \
      io.woow.opendesign.upstream-revision="89e64d813bb1c7a11519b3f668f011f7017637d7" \
      io.woow.opendesign.artifact-sha256="${ARTIFACT_SHA256}" \
      io.woow.opendesign.omnigent-integration="0.1.1"

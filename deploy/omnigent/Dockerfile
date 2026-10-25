FROM jcr-prod.woowtech.io/woow-paas-docker-local/open-design:20261005.1409@sha256:0c257c7715f87c293744252aeb905d66dca87355b93c3293fde52bc2db586c7c AS sdk
USER root
COPY sdk-runtime/ /tmp/sdk-runtime/
RUN python3 -B /tmp/sdk-runtime/materialize.py collect /tmp/sdk-runtime/manifest.json /opt/omnigent-runtime \
 && python3 -B /tmp/sdk-runtime/split-sdk.py

FROM jcr-prod.woowtech.io/woow-paas-docker-local/open-design:20261024.0943@sha256:5252ac5b3d97b094f3093619ac7eab8003455c817687f9d474f3bf9e26f9e5ce
ARG SOURCE_REVISION
# SHA-256 of manifest.json (the archive checksum is recorded separately).
ARG ARTIFACT_SHA256
USER root
COPY --from=sdk /opt/omnigent-layers/0/ /
COPY --from=sdk /opt/omnigent-layers/1/ /
COPY --from=sdk /opt/omnigent-layers/2/ /
COPY --from=sdk /opt/omnigent-layers/3/ /
COPY --from=sdk /opt/omnigent-layers/4/ /
COPY --from=sdk /opt/omnigent-layers/5/ /
COPY --from=sdk /opt/omnigent-layers/6/ /
COPY --from=sdk /opt/omnigent-layers/7/ /
COPY --from=sdk /opt/omnigent-runtime/ /tmp/omnigent-runtime/
COPY sdk-runtime/ /tmp/sdk-runtime/
RUN python3 -B /tmp/sdk-runtime/materialize.py install /tmp/sdk-runtime/manifest.json /tmp/omnigent-runtime \
 && rm -rf /tmp/omnigent-runtime /tmp/sdk-runtime
RUN rm -rf /app/apps/daemon/dist /app/apps/web/out
# The precompiled musl SDK avoids installing a compiler in tenant containers.
# Other compatible hosts retain dependencies.js's private-install fallback.
COPY --chown=1001:1001 daemon-dist/ /app/apps/daemon/dist/
COPY --chown=1001:1001 web-out/ /app/apps/web/out/
COPY --chown=1001:1001 bundled-omnigent/ /app/plugins/_official/woow/woow-omnigent/
COPY --chown=1001:1001 manifest.json /usr/local/share/woow/omnigent-native-build.json
RUN python3 -B /app/apps/daemon/dist/integrations/omnigent/sdk-patches/apply.py \
      /opt/omnigent/venv/lib/python3.12/site-packages/omnigent/host/connect.py \
      /app/apps/daemon/dist/integrations/omnigent/sdk-patches/opencode-host-model-options.patch \
 && /opt/omnigent/venv/bin/python -m compileall -q /opt/omnigent/venv/lib/python3.12/site-packages/omnigent/host/connect.py
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
 && test -s /app/plugins/_official/woow/woow-omnigent/open-design.json \
 && /opt/omnigent/venv/bin/python -B -c 'import omnigent,re2,celpy,yaml,httpx; from importlib.metadata import version; assert version("omnigent")=="0.16.0"; assert re2.match("sdk", "sdk"); from claude_agent_sdk import ClaudeAgentOptions; from claude_agent_sdk._internal.transport.subprocess_cli import SubprocessCLITransport; t=SubprocessCLITransport(prompt="contract",options=ClaudeAgentOptions()); assert t._find_bundled_cli() is None; assert t._find_cli()=="/usr/local/bin/claude"; print("Omnigent SDK imports and existing Claude CLI verified")'
LABEL org.opencontainers.image.revision="${SOURCE_REVISION}" \
      io.woow.opendesign.upstream-revision="89e64d813bb1c7a11519b3f668f011f7017637d7" \
      io.woow.opendesign.artifact-sha256="${ARTIFACT_SHA256}" \
      io.woow.opendesign.manifest-sha256="${ARTIFACT_SHA256}" \
      io.woow.opendesign.omnigent-integration="0.1.5"

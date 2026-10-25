FROM jcr-prod.woowtech.io/woow-paas-docker-local/open-design@sha256:5cf39559f13fc5e49c3564ad4668c8ce64a49b1a27ea2b445b2a701041d60595
ARG SOURCE_REVISION
ARG ARTIFACT_SHA256
USER root
COPY manifest.json base-overlay.json verify-overlay.py /tmp/omnigent-overlay/
RUN python3 -B /tmp/omnigent-overlay/verify-overlay.py \
      /tmp/omnigent-overlay/manifest.json /tmp/omnigent-overlay/base-overlay.json /app/apps/daemon/dist \
 && rm -rf /tmp/omnigent-overlay /app/apps/web/out
# The complete current base owns Paseo, MCP and the private Omnigent host.
# Only this release's reviewed daemon file is replaced; the guard above rejects
# any additional compiled daemon change that would otherwise be omitted.
COPY --chown=1001:1001 daemon-dist/integrations/omnigent/adapter.py /app/apps/daemon/dist/integrations/omnigent/adapter.py
COPY --chown=1001:1001 web-out/ /app/apps/web/out/
COPY --chown=1001:1001 bundled-omnigent/ /app/plugins/_official/woow/woow-omnigent/
COPY --chown=1001:1001 manifest.json /usr/local/share/woow/omnigent-native-build.json
USER open-design
RUN python3 -B -c 'import hashlib,json,pathlib,sys; raw=pathlib.Path("/usr/local/share/woow/omnigent-native-build.json").read_bytes(); m=json.loads(raw); assert hashlib.sha256(raw).hexdigest()==sys.argv[2], "manifest checksum mismatch"; assert m["sourceRevision"]==sys.argv[1] and len(sys.argv[1])==40, "source revision mismatch"' "$SOURCE_REVISION" "$ARTIFACT_SHA256" \
 && python3 /usr/local/share/woow/patches/export-basehref.py /app/apps/web/out \
 && python3 /usr/local/share/woow/patches/zh-tw-locale.py apply /app/apps/web/out /usr/local/share/woow/patches/zh-TW-s2twp.json \
 && python3 /usr/local/share/woow/patches/white-label.py /app/apps/web/out \
 && python3 /usr/local/share/woow/patches/white-label-links.py /app/apps/web/out \
 && python3 -B -c 'import runpy; p=runpy.run_path("/usr/local/share/woow/patches/snapshot-inline-img.py"); p["patch"]("/app/apps/web/out", (".js",), "web")' \
 && test "$(id -u)" = 1001 \
 && node --check /app/apps/daemon/dist/cli.js \
 && python3 -B -c 'import pathlib; p=pathlib.Path("/app/apps/daemon/dist/integrations/omnigent/adapter.py"); compile(p.read_text(), str(p), "exec")' \
 && test -s /app/apps/daemon/dist/integrations/omnigent/sdk-patches/manifest.json \
 && test -s /app/plugins/_official/woow/woow-omnigent/open-design.json \
 && /opt/omnigent/venv/bin/python -B -c 'import omnigent,re2,celpy,yaml,httpx; from importlib.metadata import version; assert version("omnigent")=="0.16.0"; assert re2.match("sdk", "sdk"); from claude_agent_sdk import ClaudeAgentOptions; from claude_agent_sdk._internal.transport.subprocess_cli import SubprocessCLITransport; t=SubprocessCLITransport(prompt="contract",options=ClaudeAgentOptions()); assert t._find_bundled_cli() is None; assert t._find_cli()=="/usr/local/bin/claude"; print("Omnigent SDK imports and existing Claude CLI verified")'
LABEL io.woow.opendesign.base-image="jcr-prod.woowtech.io/woow-paas-docker-local/open-design@sha256:5cf39559f13fc5e49c3564ad4668c8ce64a49b1a27ea2b445b2a701041d60595" \
      org.opencontainers.image.revision="${SOURCE_REVISION}" \
      io.woow.opendesign.upstream-revision="89e64d813bb1c7a11519b3f668f011f7017637d7" \
      io.woow.opendesign.artifact-sha256="${ARTIFACT_SHA256}" \
      io.woow.opendesign.manifest-sha256="${ARTIFACT_SHA256}" \
      io.woow.opendesign.omnigent-integration="0.1.5"

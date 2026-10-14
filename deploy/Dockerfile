ARG NODE_IMAGE=docker.io/library/node:24-alpine
ARG RUNTIME_IMAGE=docker.io/library/node:24-alpine
# WoowTech: upstream's released image of the same OpenDesign version, used only as the
# source of better-sqlite3's prebuilt binary (see the runtime stage). Pinned by digest.
ARG UPSTREAM_PREBUILT_IMAGE=ghcr.io/nexu-io/od:0.24.1@sha256:587a992857d0f8b71011e4bc55c5851e33ef9fc4c169fc17e6447700ac428f22

FROM ${UPSTREAM_PREBUILT_IMAGE} AS upstream-prebuilt

FROM ${NODE_IMAGE} AS build

ARG HTTP_PROXY
ARG HTTPS_PROXY
ARG http_proxy
ARG https_proxy
ARG no_proxy
ARG NO_PROXY

ENV HTTP_PROXY=${HTTP_PROXY}
ENV HTTPS_PROXY=${HTTPS_PROXY}
ENV http_proxy=${http_proxy}
ENV https_proxy=${https_proxy}
ENV no_proxy=${no_proxy}
ENV NO_PROXY=${NO_PROXY}
ENV CI=true
# WoowTech: onnxruntime-node's postinstall downloads the CUDA execution-provider binaries
# from github.com on linux/x64. These images never have a GPU, and that download is what
# fails in the woow k3s build network (ECONNRESET mid-stream). The CPU binaries ship in the
# npm package itself, so skipping only drops the unused CUDA EP.
ENV ONNXRUNTIME_NODE_INSTALL_CUDA=skip
# WoowTech: better-sqlite3 has no musl prebuild for this Node, so node-gyp compiles it
# and first downloads the Node headers from unofficial-builds.nodejs.org — the same
# mid-stream ECONNRESET. The node image ships those exact headers in /usr/local/include/node.
ENV npm_config_nodedir=/usr/local

RUN apk add --no-cache python3 make g++

WORKDIR /app
COPY package.json pnpm-lock.yaml pnpm-workspace.yaml ./
COPY scripts/postinstall.mjs ./scripts/
COPY packages ./packages
COPY tools ./tools
COPY apps/daemon/package.json ./apps/daemon/package.json
COPY apps/web/package.json ./apps/web/package.json
COPY e2e/package.json ./e2e/package.json
RUN corepack enable && \
    corepack prepare pnpm@10.33.2 --activate && \
    pnpm install --frozen-lockfile

COPY apps ./apps
RUN pnpm --filter @open-design/daemon build && \
    pnpm --filter @open-design/web build && \
    pnpm --filter @open-design/daemon deploy --legacy --prod /app/deploy/daemon && \
    pnpm store prune && \
    rm -rf \
      /root/.cache \
      /root/.local/share/pnpm/store \
      /app/deploy/daemon/node_modules/.cache \
      /app/deploy/daemon/node_modules/@types \
      /app/deploy/daemon/node_modules/.pnpm/@types+* \
      /app/deploy/daemon/node_modules/.pnpm/better-sqlite3@*/node_modules/better-sqlite3/deps \
      /app/deploy/daemon/node_modules/.pnpm/better-sqlite3@*/node_modules/better-sqlite3/src && \
    find /app/deploy/daemon/node_modules -type d \( \
      -name test -o \
      -name tests -o \
      -name "__tests__" -o \
      -name docs -o \
      -name doc -o \
      -name example -o \
      -name examples -o \
      -name ".github" \
    \) -prune -exec rm -rf '{}' + && \
    find /app/deploy/daemon/node_modules -type f \( \
      -name "*.md" -o \
      -name "*.markdown" -o \
      -name "*.d.ts" -o \
      -name "*.d.cts" -o \
      -name "*.d.mts" -o \
      -name "*.map" -o \
      -name "*.tsbuildinfo" -o \
      -name "binding.gyp" \
    \) -delete

# Stage-2 assets — copied into the build stage so the runtime stage
# can pull them via --from=build instead of reaching back into the
# build context.  This lets the Dockerfile work when the build context
# differs from the repo root (Railway, CI runners with a subdirectory
# context, etc.).
COPY skills ./skills
COPY design-systems ./design-systems
COPY craft ./craft
COPY prompt-templates ./prompt-templates
COPY assets ./assets
# `data/plugin-previews/manifest.json` is committed by the bake pipeline and is
# what `resolvePluginPreviewsDir()` looks for at runtime. Without it in the
# image, `loadManifest()` returns `{}` and every plugin silently falls back to
# the live-iframe preview path — the exact cost the bakes exist to avoid.
COPY data ./data
COPY plugins/_official ./plugins/_official

FROM ${RUNTIME_IMAGE}

RUN apk add --no-cache tini poppler-utils bash git && \
    addgroup -S -g 1001 open-design && \
    adduser -S -D -H -u 1001 -G open-design open-design

WORKDIR /app
COPY --from=build --chown=open-design:open-design /app/deploy/daemon ./apps/daemon
COPY --from=build --chown=open-design:open-design /app/apps/web/out ./apps/web/out
COPY --from=build --chown=open-design:open-design /app/skills ./skills
COPY --from=build --chown=open-design:open-design /app/design-systems ./design-systems
COPY --from=build --chown=open-design:open-design /app/craft ./craft
COPY --from=build --chown=open-design:open-design /app/prompt-templates ./prompt-templates
COPY --from=build --chown=open-design:open-design /app/data ./data
COPY --from=build --chown=open-design:open-design /app/assets/frames ./assets/frames
COPY --from=build --chown=open-design:open-design /app/assets/community-pets ./assets/community-pets
# Plan §3.J4 / spec §23.3.5 — bundled atom plugins registered on
# daemon boot. The directory contains `plugins/_official/atoms/<atom>/`
# pairs (SKILL.md + open-design.json); registerBundledPlugins() walks
# it on startup so the container ships with first-party atoms reachable
# via the same registry path third-party plugins use.
COPY --from=build --chown=open-design:open-design /app/plugins/_official ./plugins/_official
# WoowTech: use better-sqlite3's official prebuilt binary (as shipped in upstream's image)
# instead of the copy node-gyp compiled here. Compiled against Node >= 24.19 headers,
# node::ObjectWrap's destructor calls node::RemoveEnvironmentCleanupHook, and a Statement
# collected by GC without a current Environment aborts the daemon with
# "Assertion failed" / exit 134 (nodejs/node#65446). The prebuilt binary does not call it.
# Same package version as upstream's image; fails the build if either side drifts.
ARG BSQ=/app/apps/daemon/node_modules/.pnpm/better-sqlite3@12.10.0/node_modules/better-sqlite3
COPY --from=upstream-prebuilt --chown=open-design:open-design ${BSQ}/build/Release/better_sqlite3.node /tmp/better_sqlite3.node
RUN test -d "${BSQ}/build/Release" \
    && rm -rf "${BSQ}/build" \
    && mkdir -p "${BSQ}/build/Release" \
    && mv /tmp/better_sqlite3.node "${BSQ}/build/Release/better_sqlite3.node" \
    && chown -R open-design:open-design "${BSQ}/build" \
    && ! grep -q RemoveEnvironmentCleanupHook "${BSQ}/build/Release/better_sqlite3.node" \
    && cd /app/apps/daemon \
    && node -e 'const D=require("better-sqlite3"); const db=new D(":memory:"); if (db.prepare("select 1 as x").get().x !== 1) process.exit(1); db.close(); console.log("better-sqlite3 prebuilt OK")'

RUN mkdir -p /app/.od && \
    chown -R open-design:open-design /app

ENV NODE_ENV=production
ENV NODE_OPTIONS=--max-old-space-size=192
ENV OD_BIND_HOST=0.0.0.0
ENV OD_PORT=7456

EXPOSE 7456

USER open-design
ENTRYPOINT ["/sbin/tini", "--"]
CMD ["node", "apps/daemon/dist/cli.js", "--no-open"]

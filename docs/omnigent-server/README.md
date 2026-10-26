# Omnigent server: native OpenCode model picker

This companion server patch adds OpenCode's configured provider models to the native new-chat model menu. It is separate from the OpenDesign plugin ZIP: installing a plugin cannot replace the UI served by an independently hosted Omnigent server.

The patch applies to the public [Omnigent v0.17.0 release](https://github.com/omnigent-ai/omnigent/commit/12b5f67d2529b43eab7366a7288fb89f7587e48b), commit `12b5f67d2529b43eab7366a7288fb89f7587e48b`. It includes host-specific model validation for new chats, forks and project defaults, and preserves the deployed terminal-recovery behavior. OpenCode exposes model selection without adding unsupported launch-time effort or permission controls.

## Download the tested frontend

The [server UI release](https://github.com/WOOWTECH/opendesign-omnigent/releases/tag/omnigent-ui-v0.17.0-od.1) contains a ZIP with the tested `web-ui.tar.gz`, a source patch, release metadata, license notices, and SHA-256 checksums. Its frontend archive is byte-for-byte the artifact used for the recorded deployment and browser acceptance.

This is an **Omnigent server administrator package**. Users connecting to an already updated server can continue through the OpenDesign plugin GUI. Install the [OpenDesign plugin](../../plugin/README.md) through its own installation flow; the server UI ZIP is not a plugin import.

After downloading and unpacking the server UI ZIP:

```sh
sha256sum -c SHA256SUMS
mkdir ui-release
tar -xzf web-ui.tar.gz -C ui-release
```

Configure the Omnigent server's `OMNIGENT_WEB_UI_DIST` to the absolute path of `ui-release`, then restart through your deployment manager. Retain the previous directory and its configuration for rollback. For Kubernetes/PaaS, store the directory and environment/mount configuration in the owning chart or platform source; an ad-hoc Deployment patch alone does not establish upgrade persistence. The archive is static frontend content, not a complete server image, engine installer, or credential bundle. Existing custom frontend overlays require compatibility review before replacement.

## Build from public source

Use Node 24 and the package manager version specified by the upstream checkout. Apply to a clean checkout of the public release:

```sh
git clone https://github.com/omnigent-ai/omnigent.git
cd omnigent
git checkout --detach 12b5f67d2529b43eab7366a7288fb89f7587e48b
git apply --check /path/to/opencode-model-picker.patch
git apply /path/to/opencode-model-picker.patch
corepack pnpm install --frozen-lockfile --filter web
corepack pnpm --dir web test src/lib/nativeCodingAgents.test.ts src/lib/newChatPickerCache.test.ts src/shell/NewChatDialog.test.tsx src/shell/ForkSessionDialog.test.tsx src/shell/ProjectSettingsDialog.test.tsx src/store/chatStore.test.ts
corepack pnpm --dir web run type-check
corepack pnpm --dir web run build
```

Serve the resulting `omnigent/server/static/web-ui` through the server's supported static UI configuration. The patch changes no server API or database schema. It was built originally on a local descendant of v0.17.0 whose frontend is identical to the public release. Both patch application and the resulting frontend source tree are verified against the public revision; that local descendant is not required to build this package. See [release metadata](release.json) for the archive checksum and source provenance.

## Verify the installed version

To verify in the browser:

1. Open OpenDesign → Plugins → Omnigent → Open, then open Omnigent from its settings.
2. Select the connected host and working directory.
3. Open the harness menu → Other → OpenCode → Edit.
4. Choose a model from the host's configured provider, then send a short message.
5. Repeat with another provider/model and on a narrow screen. Confirm the reply and provider usage record match the chosen route.
6. Change the execution host in a fork or project settings. A model unavailable on that host must not be sent as a stale override.

Credentials are supplied privately at installation or through each engine's native configuration. No account, provider key or deployment credential is included in this patch.

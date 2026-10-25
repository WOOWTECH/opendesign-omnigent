# Omnigent server: native OpenCode model picker

This companion server patch adds OpenCode's configured provider models to the native new-chat model menu. It is separate from the OpenDesign plugin ZIP: installing a plugin cannot replace the UI served by an independently hosted Omnigent server.

The patch is based on Omnigent commit `0e846b01509a79a7feb8fc0b55261aff30df419e` (v0.17.0 frontend). It includes host-specific model validation for new chats, forks and project defaults, and preserves the deployed terminal-recovery behavior. OpenCode exposes model selection without adding unsupported launch-time effort or permission controls.

Apply to a clean checkout of that exact Omnigent revision:

```sh
git apply --check /path/to/opencode-model-picker.patch
git apply /path/to/opencode-model-picker.patch
pnpm --dir web test src/lib/nativeCodingAgents.test.ts src/lib/newChatPickerCache.test.ts src/shell/NewChatDialog.test.tsx src/shell/ForkSessionDialog.test.tsx src/shell/ProjectSettingsDialog.test.tsx src/store/chatStore.test.ts
pnpm --dir web run type-check
pnpm --dir web run build
```

Serve the resulting `omnigent/server/static/web-ui` through the server's supported static UI configuration. Check compatibility with your installed server and preserve any existing overlays. The patch changes no server API or database schema. Do not apply it blindly to a different Omnigent revision.

To verify in the browser:

1. Open OpenDesign → Plugins → Omnigent → Open, then open Omnigent from its settings.
2. Select the connected host and working directory.
3. Open the harness menu → Other → OpenCode → Edit.
4. Choose a model from the host's configured provider, then send a short message.
5. Repeat with another provider/model and on a narrow screen. Confirm the reply and provider usage record match the chosen route.
6. Change the execution host in a fork or project settings. A model unavailable on that host must not be sent as a stale override.

Credentials are supplied privately at installation or through each engine's native configuration. No account, provider key or deployment credential is included in this patch.

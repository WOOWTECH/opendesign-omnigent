# Omnigent native GUI Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Users install an OpenDesign plugin in the native GUI, receive one setup conversation, enter fresh credentials in a private native dialog, and connect/verify the CLIs that actually exist on the designated OpenDesign instance against the current Omnigent server.

**Architecture:** Retain native plugin import, registry, update and uninstall. A daemon-owned, allowlisted Omnigent service owns persistent enabled state, private jobs, official token storage, existing host-kit and bounded sentinel checks. The GUI uses existing Dialog/Button/Settings/Plugins navigation and never stores credentials in AppConfig, plugin inputs, GenUI, analytics, chat or job receipts. Existing external Omnigent HOME is an integration input; plugin state derives from resolved RUNTIME_DATA_DIR.

**Tech Stack:** OpenDesign upstream 89e64d813bb1c7a11519b3f668f011f7017637d7, Node24, pnpm10.33.2, TypeScript/React, Express/SQLite, installed Omnigent0.16 Python boundary, Vitest, Playwright.

---

## Authorized scope

Only service10 WOOWTECH OpenDesign / workspace2 / namespace paas-ws-7948b087 / release svc-61d5c868 is the live mutation target. Omnigent is https://omnigent.woowtech.io. Do not install missing Claude/Codex/Pi/OpenCode engines. Do not change or stop other hosts, conversations, tenants or deployments. Keep originals/private rollback metadata before a target rollout. User supplied Basic credentials are memory-only test inputs.

## Contract

Canonical plugin ID: woow-omnigent. Plugin source is an OpenDesign folder with open-design.json plus SKILL.md; native GitHub/archive/ZIP importer remains authoritative. A downloaded plugin may request only the existing prompt capability; its setup triggers the allowlisted daemon integration rather than arbitrary installer scripts.

GET /api/integrations/omnigent/status returns OmnigentIntegrationStatus: installed, enabled, phase, server, loginConfirmed, host, installedEngines, engines, verification, onboarding and optional operation/issue. No credentials are public.

POST /api/integrations/omnigent/actions accepts action enable|disable|setup|verify|disconnect|repair|update; setup alone accepts server, username, password. Verify may accept installed engine and listed model. Responses are safe public status; setup/verify/repair/disconnect/update run as one asynchronous operation. Conflicting mutations return409 OPERATION_BUSY. Unknown actions/bad URL/bad credential types return400 without echoing input. Same-origin UI requests and the existing authenticated daemon boundary are enforced; CLI uses local authenticated API.

On first install or enable, approval and receipts are invalidated and the dedicated native project/conversation is created/reused without an LLM turn. Any missing dependency is installed in daemon-private storage only after an explicit job; ready requires fresh successful login, exact host online, every installed supported engine's real assistant sentinel and stopped test sessions. Disable prevents plugin actions; disconnect clears only this target server's local token and observes this host offline. Upgrade retains a confirmed login unless server/engine identity changes. Startup reconciles registry+state instead of relying solely on volatile install events.

## Task1 — Shared DTO and native distribution (main)

Files: packages/contracts/src/api/omnigent.ts, packages/contracts/src/index.ts, plugins/woow-omnigent/open-design.json, plugins/woow-omnigent/SKILL.md. Validate manifest through existing plugin resolver; reject Pi-only distribution. Keep the canonical plugin downloadable independent of a daemon build.

## Task2 — Omnigent adapter (Newton)

Isolated work/od-omnigent-team/backend first; integrate to apps/daemon/src/integrations/omnigent/adapter.py as a documented external-SDK compatibility boundary. Reuse official token store, stdout sanitization, bounded native conversation checks and current resident supervisor. Add OpenCode native harness/agent discovery and model validation. Do not save passwords, rewrite provider credentials, or start a second host. Tests cover wrong login, no ambient approval, unavailable engine, exact host/model binding, sentinel extraction, cleanup and bounded failures. Return exact tests and changed files; main runs them independently.

## Task3 — Daemon service/routes/CLI (main)

Files: apps/daemon/src/integrations/omnigent/service.ts, apps/daemon/src/routes/omnigent.ts, apps/daemon/src/server.ts, apps/daemon/src/route-context-contract.ts, apps/daemon/src/cli.ts, apps/daemon/tests/integrations/omnigent.test.ts. Create one setup project/conversation using native transactional persistence and authority binding; append static nonsecret guidance only. Private atomic state, persistent installed/enabled generation, operation serialization, subprocess argv without shell, stdin credentials and sanitized errors. Route all plugin-owned paths through RUNTIME_DATA_DIR. Add CLI status/enable/disable/setup via stdin/verify/disconnect and preserve original command dispatch.

Verification: native Vitest red/green for lifecycle idempotency, registry removal, re-enable fresh approval, rejected login, busy jobs, timeout and isolation. Typecheck/build affected daemon/contracts packages; compare baseline failures before attributing them.

## Task4 — Native GUI (Sagan)

Files exclusive to GUI worker: apps/web/src/components/OmnigentSetupDialog.tsx, OmnigentIntegrationSection.tsx and colocated CSS Modules; apps/web/src/providers/omnigent.ts; native PluginsView/ExtensionsMarketplace import-success hooks; SettingsDialog integrations; shared owner/navigation as necessary; focused web tests and locale keys. Use shared Dialog/Button. Password is local dialog state only, cleared on all outcomes/identity changes. Native plugin install success opens/reuses setup conversation and fresh dialog; ordinary plugin behavior remains. Show status, installed engine inventory, bounded job progress and Open/Verify/Disconnect/Disable/Enable/Update actions. Native callbacks survive view navigation. No GenUI secret forms or standalone dashboard.

Verification: focused React tests for exact plugin activation, non-Omnigent invariance, credential-only request, empty fresh fields, wrong-login recovery, busy/disabled/reload and stale identity. Web typecheck/build; desktop320/360/1440 keyboard/mobile GUI live checks.

## Task5 — Build/deploy/acceptance (Singer advisory; main live writer)

Preserve current image runtime/dependencies and PVC/home/config/auth. Produce a reproducible source/build artifact using pinned upstream+patches and a narrowly scoped image/runtime delivery; do not install engines. Update only service10 via the existing PaaS/operator path where available, with stored image/config rollback. Native browser: import actual public package, auto setup, fresh URL/user/password, wrong login then correct login, actual Claude/OpenCode replies and stopped sessions, model/module listing, Open Omnigent, disable/re-enable fresh wizard, reconnect, native upgrade/uninstall, duplicate/reload/restart persistence, secret-scan evidence and no unintended projects/model turns.

Review spec before quality, fix all material issues, rerun relevant tests after changes. Record actual unsupported/provider restrictions; do not report HTTP200 as chat success. Publish code/plugin with exact version and evidence only after fresh verification, then final check target ready and unrelated workload unchanged.

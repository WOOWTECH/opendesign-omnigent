# Omnigent Native Plugin Launcher Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Open Omnigent connection settings from the same native plugin cards used by MCP Server and CLI Terminal, and verify the flow on the user-selected service 244.

**Architecture:** Bundle the canonical `woow-omnigent` integration as an `od.launch` plugin pointing to `/settings/omnigent`. Reuse the existing settings section, private connection dialog, and authenticated daemon service. A bundled installation waits for explicit configuration; a user import retains automatic onboarding. Keep approval and actual installed-engine reply verification unchanged.

**Tech Stack:** React, TypeScript, native OpenDesign plugin manifests, Vitest, Playwright, Node 24, pnpm 10.33.2, WoowTech PaaS container delivery.

---

### Task 1: Native launch and settings route

- Add `plugins/_official/woow/woow-omnigent/open-design.json` with localized copy and `od.launch.path=/settings/omnigent`.
- Extend `apps/web/src/router.ts` with a bounded settings-section deep link; wire `App.tsx` to the existing SettingsDialog.
- Restore the Omnigent section/nav and delayed form-focus fix from the preserved WIP without adding a second card implementation.
- Cover native launcher visibility/link semantics, route round trips, and settings rendering with focused tests.

### Task 2: Explicit activation of bundled integrations

- Add optional `installationSource` status metadata in `packages/contracts/src/api/omnigent.ts` and derive it from the current installation in the daemon service.
- Skip automatic background activation only for a bundled installation. Explicit Configure continues to create the scoped setup conversation and request fresh URL/account/password.
- Cover bundled-versus-imported policy and unchanged explicit activation with service/UI tests.

### Task 3: Build and package

- Use the pinned Node 24/pnpm toolchain in this isolated worktree.
- Run focused web/daemon tests, package typechecks, `pnpm guard`, and `pnpm typecheck`; distinguish baseline failures from changed behavior.
- Build the production static web and daemon outputs; use the current PaaS image/build pipeline including MCP/terminal support. Record source revision and resulting image digest.

### Task 4: Authorized service-244 acceptance

- Verify domain, namespace, release, active runs, and platform desired state; retain rollback metadata privately.
- Roll out only service 244 through the established platform deployment path, preserving current values and persistent storage.
- From Plugins open Omnigent in a new tab, confirm refresh/deep link, fresh private fields, connection state, actual installed engine/model catalogs, and real replies with scoped cleanup.
- Verify MCP Server and CLI Terminal entries remain operable, desktop/mobile layout, reconnect/disable behavior, and no background onboarding while browsing unrelated pages.
- Publish reviewed source/package metadata to the authorized repositories and report any engine authentication or upstream model limitations using actual receipts.

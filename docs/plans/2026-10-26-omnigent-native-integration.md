# Omnigent Native Integration Implementation Plan

> Execute in this isolated native worktree only; no remote writes or runtime operations.

**Goal:** Integrate verified Omnigent aa0f75f430b12fc1f82511d6af07e92418c7b6c6 into Gitea c8ae3900f9ff5d58f50d11f4055756f6e7f14a25 while preserving native Paseo, MCP CAS, and launchers.

**Architecture:** Three-way merge from common ancestor b8a1371924d3aedb74dff563a5ff35a783919dee. Resolve launcher catalogue expectations to include both Paseo and Omnigent. Preserve the 0.1.6 plugin contract, authenticated setup authority, installed-engine discovery, private host-kit, and verified SDK reuse.

**Tech Stack:** Node 24.21.0, pnpm 10.33.2, TypeScript/Vitest/Next.js, Python.

## 1. Merge the reviewed implementation

Merge aa0f75f43 with --no-commit; resolve catalogue tests and any overlapping UI changes without replacing the c8 tree. Preserve recovery tools/reports and SDK model/quota/native interrupt patches. Check c8 MCP CAS and Paseo paths against the base.

## 2. Verify fresh setup and engine repair

Inspect apps/daemon/src/integrations/omnigent/{service.ts,adapter.py,dependencies.ts,host-kit/} and apps/web/src/components/Omnigent*.tsx. Add focused regression coverage under the existing daemon/web tests for fresh setup without inherited host authority, mandatory server/username/password, plugin launch navigation, and existing-engine-only repair guidance. Repair only proven gaps; no engine installation or production SDK modifications.

## 3. Local verification

Install independent worktree dependencies using the pinned pnpm and reusable cache (no shared node_modules). Run focused daemon integration/launcher/MCP CAS and web settings/provider/router tests; Python adapter, SDK patch and recovery tests with synthetic fixtures only. Run pnpm guard, pnpm typecheck, and daemon/web builds, recording any limitations. Do not run container or GUI acceptance or access real backup archives.

## 4. Handoff

Record changed files, preserved contracts, exact commands/results, and unperformed checks in docs/omnigent-native-integration-20261026.md. Commit locally after verification. No push, remote merge, deployment, or edits outside this worktree.

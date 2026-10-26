# Omnigent 原生整合與本機驗證（2026-10-26）

本次僅完成 native 原始碼整合與本機驗證。沒有 push、遠端 merge、容器部署、GUI 驗收或正式 runtime 操作。

## 基底與版本

- Gitea 基底：`c8ae3900f9ff5d58f50d11f4055756f6e7f14a25`。
- 已驗收來源：`aa0f75f430b12fc1f82511d6af07e92418c7b6c6`。
- 共同祖先：`b8a1371924d3aedb74dff563a5ff35a783919dee`。
- 採用三方 merge，保留雙親歷史；未以 aa 整樹覆蓋 c8。
- 可攜插件與 bundled launcher 均維持 **0.1.6**。沒有在 aa 之上新增或修改 HTTP/action DTO、插件 manifest schema 或 SDK patch 契約；HA 最終版號由協調端另定。

## 整合內容與新增修正

恢復 Omnigent bundled launcher、`/settings/omnigent` 路由、設定 GUI、供應商登入引導、daemon 啟動時恢復已核准的本機 host、private host-kit、SDK 0.16.0 三項 hash 驗證補丁（OpenCode 模型、quota、native interrupt）、patched SDK reuse，以及公開 recovery 工具與既有報告。

c8 的以下內容經 `git diff --quiet c8ae3900f -- <paths>` 驗證完全一致：MCP config/routes/CAS mutation 與測試、MCP contract、`woow-paseo` 插件、PluginsView、PluginDetailView、手機 launcher CSS。

合併衝突只有 web launcher component 測試及 Playwright launcher 測試。合併後同時涵蓋 CLI Terminal、MCP Server、Omnigent、Paseo，保留 c8 的手機動作 class 與繁體中文斷言。Playwright fixture 改為讀取本次真正 bundled 的四個 manifest；本次只做型別檢查，沒有執行 GUI 測試。

在 aa 之上修正一個首次安裝邊界：service 以前接受繼承的 `OMNIGENT_CONFIG_HOME` / `OMNIGENT_DATA_DIR`。新增回歸測試先確認它錯用 legacy 目錄，然後修正成只使用 service 自身的 `stateDir/host`。資料根的權威規則見 [AGENTS.md](../AGENTS.md#daemon-data-directory-contract)。

service 也移除傳給 adapter 的 legacy supervisor home、config/data/status、server URL/file、provider seed、helper override，避免舊狀態檔誤認 host、舊 server 值影響 GUI 設定。此處操作的是複製的子程序環境，不修改呼叫者環境，也不讀取、搬移或刪除 legacy 設定。新增一項真實子程序測試驗證此環境邊界。

本 native 樹的 Dockerfile 沒有設定該全域 home；上游 core/HA 映像仍應取消指向 legacy host 的全域設定。chart 保留 `omnigentHost.enabled=false`：那是停用舊 supervisor，原生 service 自行管理 host。

## 首次設定與 engine 介面

- 新安裝不繼承 server/login approval，也不在啟動時恢復未核准 host。GUI 的網址、帳號、密碼欄位皆從空白開始並必填；後端也拒絕不完整輸入。
- 只有明確 setup 且登入成功後才允許 native host 啟動。既有 daemon-owned host 的有效核准狀態可在重啟後恢復；重新安裝、停用或其他 workspace 不借用此權限。
- 只盤點並連結既有 Claude、Codex、Pi、OpenCode。dependency installer 只管理 Omnigent/uv；外部 SDK 只做唯讀 hash 驗證，只有私有 SDK 可以套用補丁。
- plugins 清單及詳細頁的「開啟」連到 `/settings/omnigent`。
- **已有 engine 修復互動介面**：設定頁顯示認證、版本及 quota 問題；可進入本機 CLI 設定、開啟失敗驗證對話、展開供應商設定指南、複製各 CLI 登入指令、開啟同 workspace 的原生終端機，最後重新驗證或「修復連線」。這是登入／設定引導與連線修復，沒有自動安裝缺少的引擎。
- 保留 aa 語意：「停用」停止 OD 整合檢查並撤銷 OD 端登入核准；「取消連結」清除官方連線 token 並等待 host 離線，保留註冊和 supervisor。不能把「停用」測試當成 host 已關閉證據。容器停止及 supervisor lifecycle 由發行端候選環境另驗。

## 本機驗證

使用 Node **24.21.0**、pnpm **10.33.2**、`/data/pi-agent/venv/bin/python3`。本工作樹獨立 `node_modules` 由固定 lockfile、離線 cache、`--package-import-method=copy` 安裝。沒有共享可變依賴目錄。

首次 daemon 測試因忽略 install scripts 而缺少 SQLite binding；環境沒有可用 C++ 工具鏈，後來唯讀複製來源工作樹同版本 `better-sqlite3@12.10.0` 的 binary 到本工作樹，再驗證 Node ABI **137** 與 SQLite **3.53.1** 可實際建立 in-memory DB。來源沒有修改，也沒有 hardlink/symlink 共用此 binary。

| 檢查 | 結果 |
| --- | --- |
| 新增 legacy home 回歸，修正前 | 預期失敗：adapter 收到 legacy config/data 路徑 |
| Daemon：Omnigent service/routes/integrations、四個 launcher、MCP config/CAS | **13 檔、286 tests passed** |
| 最後一次 service 單獨重跑 | **54 tests passed** |
| Web：Omnigent integration/provider guide、launcher、settings、focus、provider、router | **7 檔、238 tests passed** |
| Python adapter 離線邊界 | **54 tests passed** |
| SDK patch installer | **15 passed，1 skipped**（缺原始 SDK fixture） |
| Overlay build context | **7 tests passed** |
| Recovery safe restore | **31 tests passed** |
| Sealed backup crypto | **58 tests passed** |
| Sealed OD volume/directory events | **73 tests passed** |
| `pnpm guard` | **通過**；design system 報告屬既有 report-only 資訊 |
| `pnpm typecheck` | **通過整個 workspace 與 scripts** |
| `pnpm --filter @open-design/daemon build` | **通過** |
| `pnpm --filter @open-design/web build` | **通過 Next.js production build** |
| `git diff --check` / cached check | **通過** |

新增子程序測試第一次 typecheck 發現 `exactOptionalPropertyTypes` 不接受 `adapter: undefined`，已改成省略此選項，重跑根級 typecheck 通過。沒有遺留失敗檢查。

主要可重現指令（先把指定 Node24 與 Python 的 bin 放入 PATH）：

```sh
pnpm install --offline --frozen-lockfile --ignore-scripts --package-import-method=copy
node scripts/postinstall.mjs build
pnpm --filter @open-design/daemon exec vitest run -c vitest.config.ts tests/omnigent-service.test.ts tests/integrations/omnigent tests/omnigent-routes.test.ts tests/plugins-builtin-launchers.test.ts tests/mcp-config.test.ts tests/mcp-servers-upsert.test.ts --maxWorkers=2
pnpm --filter @open-design/web test tests/components/OmnigentIntegration.test.tsx tests/components/OmnigentProviderGuide.test.tsx tests/components/ExtensionsMarketplace.builtin-launchers.test.tsx tests/components/SettingsDialog.execution.test.tsx tests/omnigent-setup-dialog.focus.test.tsx tests/providers/omnigent.test.ts tests/router.test.ts
python3 -B -m unittest discover -s apps/daemon/src/integrations/omnigent -p test_adapter.py
python3 -B -m unittest discover -s deploy/omnigent/sdk-patches/tests -p test_apply.py
python3 -B -m unittest discover -s deploy/omnigent/tests -p 'test_*.py'
python3 -B -m unittest discover -s deploy/omnigent/recovery -p test_safe_restore.py
python3 -B -m unittest discover -s deploy/omnigent/recovery/sealed/crypto -p 'test_*.py'
python3 -B -m unittest discover -s deploy/omnigent/recovery/sealed/od-volume -p 'test_*.py'
pnpm guard
pnpm typecheck
pnpm --filter @open-design/daemon build
pnpm --filter @open-design/web build
```

本機 command logs 保留在忽略版控的 `.tmp/native-validation/`。Python 測試皆使用合成資料；未執行 `verify_real_archives.py`、讀取真實備份或測試帳號。

## 尚未驗收的範圍

本機沒有安裝完整 Omnigent SDK 0.16.0，因此沒有重跑針對 installed SDK 的 90 個 quota/native interrupt tests；patch installer 的真實 SDK fixture 測試亦明確 skipped。SDK/host-kit/recovery 原始碼與 aa 完全一致，並保留其歷史驗收材料，但這不能代替本次新映像驗收。

沒有執行容器 build/smoke、真實 engine/provider 對話、Playwright/桌面 GUI、HA Ingress/Supervisor、Store同步、候選環境重啟與停止驗收。沒有選定 HA 最終版號或發布 image digest。`deploy/omnigent/Dockerfile` 的既有 overlay pin 屬 aa 歷史交付工具；新的正式 core/HA 必須由協調端以本次整合提交建置。

## 變更檔案（相對 c8）

共 108 個檔案，包含本報告與實作計畫。

```text
.gitattributes
.github/workflows/docker-image.yml
apps/daemon/src/integrations/omnigent/adapter.py
apps/daemon/src/integrations/omnigent/build-assets.ts
apps/daemon/src/integrations/omnigent/dependencies.ts
apps/daemon/src/integrations/omnigent/host-kit/manifest.json
apps/daemon/src/integrations/omnigent/host-kit/og_helper.py
apps/daemon/src/integrations/omnigent/host-kit/supervisor.sh
apps/daemon/src/integrations/omnigent/service.ts
apps/daemon/src/integrations/omnigent/test_adapter.py
apps/daemon/src/server.ts
apps/daemon/tests/integrations/omnigent/build-assets.test.ts
apps/daemon/tests/integrations/omnigent/dependencies.test.ts
apps/daemon/tests/integrations/omnigent/deploy-overlay.test.ts
apps/daemon/tests/integrations/omnigent/host.test.ts
apps/daemon/tests/omnigent-service.test.ts
apps/daemon/tests/plugins-builtin-launchers.test.ts
apps/web/src/App.tsx
apps/web/src/components/EntrySettingsMenu.tsx
apps/web/src/components/EntryView.tsx
apps/web/src/components/OmnigentIntegrationProvider.tsx
apps/web/src/components/OmnigentIntegrationSection.module.css
apps/web/src/components/OmnigentIntegrationSection.tsx
apps/web/src/components/OmnigentIssue.tsx
apps/web/src/components/OmnigentProviderGuide.tsx
apps/web/src/components/OmnigentSetupDialog.tsx
apps/web/src/components/SettingsDialog.tsx
apps/web/src/i18n/locales/ar.ts
apps/web/src/i18n/locales/de.ts
apps/web/src/i18n/locales/en.ts
apps/web/src/i18n/locales/es-ES.ts
apps/web/src/i18n/locales/fa.ts
apps/web/src/i18n/locales/fr.ts
apps/web/src/i18n/locales/hu.ts
apps/web/src/i18n/locales/id.ts
apps/web/src/i18n/locales/it.ts
apps/web/src/i18n/locales/ja.ts
apps/web/src/i18n/locales/ko.ts
apps/web/src/i18n/locales/pl.ts
apps/web/src/i18n/locales/pt-BR.ts
apps/web/src/i18n/locales/ru.ts
apps/web/src/i18n/locales/th.ts
apps/web/src/i18n/locales/tr.ts
apps/web/src/i18n/locales/uk.ts
apps/web/src/i18n/locales/zh-CN.ts
apps/web/src/i18n/locales/zh-TW.ts
apps/web/src/i18n/types.ts
apps/web/src/providers/omnigent.ts
apps/web/src/router.ts
apps/web/tests/components/ExtensionsMarketplace.builtin-launchers.test.tsx
apps/web/tests/components/OmnigentIntegration.test.tsx
apps/web/tests/components/OmnigentProviderGuide.test.tsx
apps/web/tests/components/SettingsDialog.execution.test.tsx
apps/web/tests/omnigent-setup-dialog.focus.test.tsx
apps/web/tests/providers/omnigent.test.ts
apps/web/tests/router.test.ts
deploy/omnigent/Dockerfile
deploy/omnigent/README.md
deploy/omnigent/base-overlay.json
deploy/omnigent/build-context.py
deploy/omnigent/recovery/README.md
deploy/omnigent/recovery/acceptance-followup-20261026.json
deploy/omnigent/recovery/acceptance-followup-20261026.md
deploy/omnigent/recovery/application-recovery-20261026.json
deploy/omnigent/recovery/application-recovery-20261026.md
deploy/omnigent/recovery/console-terminal-base-path.patch
deploy/omnigent/recovery/safe_restore.py
deploy/omnigent/recovery/sealed/README.md
deploy/omnigent/recovery/sealed/crypto/backup_envelope.py
deploy/omnigent/recovery/sealed/crypto/test_backup_envelope.py
deploy/omnigent/recovery/sealed/od-volume/od_volume.py
deploy/omnigent/recovery/sealed/od-volume/test_directory_events.py
deploy/omnigent/recovery/sealed/od-volume/test_od_volume.py
deploy/omnigent/recovery/test_safe_restore.py
deploy/omnigent/recovery/validation-20261026.json
deploy/omnigent/recovery/verify_real_archives.py
deploy/omnigent/sdk-patches/apply.py
deploy/omnigent/sdk-patches/manifest.json
deploy/omnigent/sdk-patches/opencode-native-interrupt.patch
deploy/omnigent/sdk-patches/opencode-quota-error.patch
deploy/omnigent/sdk-patches/tests/native/LICENSE
deploy/omnigent/sdk-patches/tests/native/NOTICE
deploy/omnigent/sdk-patches/tests/native/PROVENANCE.json
deploy/omnigent/sdk-patches/tests/native/README.md
deploy/omnigent/sdk-patches/tests/native/pytest.ini
deploy/omnigent/sdk-patches/tests/native/requirements-offline-linux-py312.txt
deploy/omnigent/sdk-patches/tests/native/requirements-test.txt
deploy/omnigent/sdk-patches/tests/native/test_opencode_forwarder_reconnect.py
deploy/omnigent/sdk-patches/tests/native/test_opencode_native_forwarder.py
deploy/omnigent/sdk-patches/tests/native/test_opencode_native_interrupt.py
deploy/omnigent/sdk-patches/tests/test_apply.py
deploy/omnigent/sdk-runtime/manifest.json
deploy/omnigent/sdk-runtime/materialize.py
deploy/omnigent/sdk-runtime/split-sdk.py
deploy/omnigent/tests/test_build_context.py
deploy/omnigent/verify-overlay.py
docs/omnigent-native-integration-20261026.md
docs/omnigent-server/README.md
docs/omnigent-server/opencode-model-picker.patch
docs/omnigent-server/release.json
docs/plans/2026-10-25-omnigent-builtin-launcher.md
docs/plans/2026-10-25-omnigent-provider-guide.md
docs/plans/2026-10-26-omnigent-native-integration.md
e2e/ui/native-launchers.test.ts
packages/contracts/src/api/omnigent.ts
plugin/README.md
plugin/open-design.json
plugins/_official/woow/woow-omnigent/open-design.json
```

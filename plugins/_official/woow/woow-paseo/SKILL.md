---
name: woow-paseo
description: Open Paseo setup in OpenDesign when the user wants to manage the Paseo service, connect the Paseo app, or check provider readiness.
---

# Paseo 設定

在 OpenDesign「插件」頁找到「Paseo 設定」，按「開啟」前往同源的 [/paseo/setup/](/paseo/setup/)。使用設定頁提供的服務管理、帳號授權與 App 配對流程。

依設定頁的實際結果區分服務就緒、供應商授權與模型對話成功。CLI 或帳號檔存在、模型清單可讀、配對連結已產生，都不能單獨證明新對話或 App 配對成功。若頁面沒有 Paseo 設定內容，回報主機整合尚未就緒。

帳號授權與配對資訊只在受保護的設定頁及官方授權流程中處理；不要將密碼、API key、OAuth 資料或配對連結放入聊天。

此入口的原生插件 ID 為 `woow-paseo`，由支援 `od.launch` 的主機映像註冊為 bundled。匯入此指引不會安裝或啟動 Paseo；主機提供設定頁與後端服務。

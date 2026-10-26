**OD / Omnigent 應用層復原驗收｜2026-10-26**

本輪三項缺口已完成實測：協調 PostgreSQL／附件備份、原始登入秘密復原，以及隔離環境的原生應用重建。四引擎 GUI、服務重啟、Console 與演練清理均有成功證據。完整彙總見 [JSON 收據](application-recovery-20261026.json)。

| 驗收項目 | 實測結果 |
| --- | --- |
| PostgreSQL 與附件協調備份 | 同一 exported snapshot 還原 2,385,522 筆 conversation items、2,757 個 conversations；727 筆檔案與 229 個模組 bundle 引用驗證通過。2,522 個實體檔案的內容、權限一致；invalid indexes、unvalidated constraints 與三類 orphan 均為 0。 |
| OD 資料卷 | 還原 31,278 個 entries，含 39 個 SQLite；保留檔案內容、擁有者與模式。來源以唯讀方式擷取，未重啟正式 OD。 |
| 原始登入秘密 | 六個認證檔先以原內容復原並通過初次原生啟動；原 DB role／密碼、應用帳密及備份前的原 cookie 均有效。錯誤密碼、錯簽章及匿名請求均回覆 401。 |
| 原生環境重建 | 以兩個原生 Helm releases、四個全新 PVC 重建 PostgreSQL、Omnigent、OD 與 Console。重新套用相同部署時，Pod／PVC 身分及容器重啟次數不變。 |
| 四引擎 GUI | Claude Code、Codex、Pi、OpenCode 均從外掛卡片進入設定、登入 Omnigent、選擇原生模型、取得指定回覆，重載後保留對話，再從 GUI 正常停止 runner。Pi／OpenCode 的中斷與重載後閒置狀態亦通過。 |
| 服務重啟 | 四個工作負載的 Pod 均由原生 controller 重建，沿用同四個新 PVC。五張資料表筆數、插件連線、四引擎驗證紀錄與四個靜態供應商認證檔一致。 |
| 重啟後登入與 Console | 再次通過原帳密、原 cookie、三個附件樣本 hash／304、72 個 UI assets 與新寫入後讀取。全新瀏覽器看到插件 ready、host connected 並登入成功；Console 再跑 25/25 通過，含真實終端 WebSocket 與無害命令回應。 |
| 網路與清理 | 11 項連線檢查符合預期：9 項正式環境連線拒絕，隔離 Omnigent 與授權 LiteLLM 可用。演練 namespace、四個 PV、Longhorn volume／engine／replica 與 attachment 均移除；臨時 LiteLLM 規則移除，原規則與正式 Pod 身分、映像、ready、restart counts 不變。 |

**Codex 最終對話使用使用者授權更新的帳號，僅修改隔離副本。** 原還原帳號可啟動，但遭上游額度限制；原六檔復原成功與換帳號後對話成功是兩項獨立證據。Pi／OpenCode 使用既有 OD 專用 LiteLLM 金鑰與另一個已授權的 ChatGPT 路由，沒有建立新供應商金鑰。

四引擎 GUI 結果由三次執行中各自完整成功的案例彙整；早先整輪失敗紀錄保留，沒有改寫成通過。每個成功案例都建立自己的新對話並從 GUI 停止。測試只允許兩個隔離網址與自身對話的寫入，合計攔截 33 次 HTTP、10 次 WebSocket 嘗試；這些被攔截請求涉及的其他功能不列入驗收。

本輪修正了備份與復原過程的實際問題：將大表計數移至同一 snapshot 的鎖外唯讀交易、pin 附件檔案描述符、處理唯讀 SQLite 的 idle-WAL 相容性、嚴格辨識 Paseo 原子寫入事件，以及用最小 helper capabilities 保留 setgid。原生 HTTPX 忽略環境 CA 設定的情況，以實際信任 bundle 處理，沒有停用 TLS 驗證。

Console 繼承的 chart 0.5.8 漏接 terminal base path，已在隔離部署修正並完成 25 項驗收。另提供可套用於該原生 chart 的 [修正 patch](console-terminal-base-path.patch)，已通過 `git apply --check`。**這個 chart 修正尚未套用到正式環境或發布成上游 chart 版本。**

測試工具另遇到 64 MiB shared-memory 導致的瀏覽器崩潰、同步 Python driver 卡住、本機轉送中斷，以及工作區選單的滑鼠操作逾時。已改用私有 0700 暫存與非同步 Node driver、補上轉送預檢，並以原生鍵盤選擇工作區。失敗測試的兩個自有 runner 已經正常停止，暫存 profiles 已清理；沒有保留 HAR 或 storage_state。

四份加密備份的大小、SHA-256 及 0600 模式已核對，復原金鑰保存在分開的 0700 目錄／0600 檔案。演練清理後仍保留加密備份與金鑰。公開倉庫只提供 [復原工具與使用說明](sealed/README.md)、彙總收據及 ciphertext hashes，不含備份內容、帳密、cookie、私鑰或私有畫面。公開工具副本通過 **58 項加密封裝＋73 項資料卷測試，共 131 項**。

**範圍是既有 k3s 基礎設施上的應用層復原。** PostgreSQL／附件使用同一協調快照；OD 採 per-file／per-SQLite consistency，沒有建立 OD 全域或跨應用共同原子復原點。本次不涵蓋整個叢集、節點、儲存、Cloudflare、DNS、registry 從零重建／切換、異地／KMS 復原、任意版本與 schema 升降級、多工作區或長期負載承諾。

來源 files／agents 鎖定觀測為 1.071 秒；DB 原生還原為 382.72 秒，含解密與驗證為 462.119 秒。這些是個別步驟的觀測值，不是整套服務 RPO／RTO 保證。資源刪除檢查也不代表實體媒體安全抹除。

本次更新提供驗收文件與 operator 工具，**不變更 v0.1.6 的七個二進位附件；版本仍為 preview**。先前 [follow-up](acceptance-followup-20261026.md) 保留當時的待驗收狀態，本報告記錄後續完成結果。

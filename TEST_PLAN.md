# JT Windows SNMP Agent，測試計劃

> 狀態標記：`[已實作]` 測試已存在且會跑 / `[待實作]` 已定義但尚未寫 / `[受阻]` 缺環境

---

## 0. 原則

**0.1 這個 agent 的失效是無聲的。** 服務顯示 Running、LibreNMS 圖表卻是斷的，
是本專案最常見的故障型態。因此測試的重點不是「功能會不會動」，而是
**「壞掉的時候看不看得出來」**。每一層都必須有一個能在無人看管下判定失敗的斷言。

**0.2 不得發出有已知 bug 的版本。** §10 的 Release Gate 是硬性閘門，
任一項未通過即不得打 tag、不得產出 MSI。「先發再修」在客戶是政府與醫院的
情況下不成立，他們的變更管控會讓修補版排到數週之後。

**0.3 測試必須能判定「變差」，不只判定「有沒有壞」。**
效能與 host impact 一律以基準線比較，回歸超過門檻即 fail build，
不接受「還在可接受範圍」這種人為判斷。

**0.4 不寫死期望值於兩處。** 期望值只留一份來源，其餘由程式實算比對。
同一份清單寫在兩個地方一定會漂，這是 jt-doc-tools 踩過五次的坑。

**0.5 乾淨的機器會遮蔽一整類問題。**（2026-08-25 新增，來自一次差點出貨的修法）
安裝程式的行為只在一台沒有其他軟體的機器上驗證，等於沒有驗證它與別人共存的行為。
客戶的機器上有防毒、記錄轉送、備份代理。任何動到安裝程式行為的修改，
都必須同時在**一台乾淨機器**與**一台裝有其他常駐服務的機器**上驗證，
只有其中一台綠不算通過。見 6.1d。

**0.6 有些主張只有從畫面上看得出來。** 讀產物的表格可以證明「宣告存在」，
不能證明「行為正確」。連續三版壞掉的 GUI 安裝、以及 6.1c.12 那次失敗的修法，
在原始碼與 MSI 表格上都是對的。凡是關於精靈行為的項目，
**必須以真人操作或自動化驅動精靈完成一次**才可標為已驗。

---

## 1. L0，單元測試

| # | 項目 | 狀態 |
|---|---|---|
| 1.1 | BER 大小解析式計算 vs 真實編碼器（含負數邊界、base-128 sub-id、長字串） | **[已實作]** `tests/test_ber_size.py`（540 例，含 4,000 組隨機） |
| 1.2 | hrStorage allocation unit 動態放大：20 TB / 100 TB / 8 TB Storage Spaces 不得溢位 Integer32 | [待實作] |
| 1.3 | 虛擬記憶體定義：commit limit / commit total，不得與 pagefile 大小混用 | [待實作] |
| 1.4 | ifIndex 配發：同一 LUID 永遠取得同一 index；介面消失後 index 不得重用 | [待實作] |
| 1.5 | hrDeviceIndex 共用：hrProcessor / hrDiskStorage / hrNetwork 的 index 必須來自同一組 | [待實作] |
| 1.6 | TimeTicks 回捲：497 天邊界自然回捲，wrap count 正確遞增 | [待實作] |
| 1.7 | 介面篩選：Hyper-V host 的 40～80 個介面，僅硬體介面通過 | [待實作] |
| 1.8 | 快取失效語意：新鮮 / stale 內 / 超過 stale threshold（整列移除，不得捏造值） | [待實作] |
| 1.9 | 狀態檔原子寫入：temp → fsync → ReplaceFileW；主檔損毀時讀 .bak | [待實作] |
| 1.10 | schema_version 遷移：舊版 index-map / engine-state 能正確升級 | [待實作] |
| 1.11 | 設定合併：ADMX 原則覆寫 config.yaml，`--effective` 正確標示各值來源 | [待實作] |
| 1.12 | engineBoots：一次寫 +10、記憶體用實際值、超過已存值才再寫盤、單調遞增 | [待實作] |
| 1.13 | MachineGuid 不符時重新產生 engineID 並將 boots 重置為 1 | [待實作] |
| 1.14 | MS SNMP 移轉對照：權限 8/16 降級為唯讀、1/2 不匯入、trap/ExtensionAgents 列出但不匯入 | [待實作] |
| 1.15 | 移轉冪等性：`import-ms-snmp` 重複執行不產生重複項目 | [待實作] |
| 1.16 | base OID 常數對照 RFC 標準值 | **[已實作]** `tests/test_oid_constants.py`（10 例）|
| 1.17 | collector 失敗語意：回 default 不拋出、錯誤計數累計、恢復不歸零 | **[已實作]** `tests/test_collector_health.py`（8 例）|
| 1.18 | sysObjectID 三分支與 Server Core / DC 判定 | **[已實作]** `tests/test_product_type.py`（9 例）|
| 1.19 | SMBIOS 佔位字串篩選（`To Be Filled By O.E.M.` 等） | [待實作] |

---

## 2. L1 — SNMP 協定正確性

下列每一種都列為「不可接受」。snapshot + bisect 架構讓這些成為
「結構保證」，**本層存在的目的就是驗證那個聲稱，而不是相信它**。

| # | 項目 | 狀態 |
|---|---|---|
| 2.1 | 全樹 walk 回傳每個 OID 恰好一次，內容與 snapshot 完全一致 | **[已實作]** `tests/test_walk_correctness.py` |
| 2.2 | 嚴格字典序，無 duplicate OID | **[已實作]** |
| 2.3 | walk 必定終止（GETNEXT loop 偵測） | **[已實作]** |
| 2.4 | max-repetitions = 1 / 2 / 10 / 25 / 100 / 1000 結果集完全相同 | **[已實作]** |
| 2.5 | 回應不超過 1400 bytes（即使客戶端要求 10000 筆） | **[已實作]** |
| 2.6 | 過大的 max-repetitions 被截斷為有效回應，而非錯誤或空回應 | **[已實作]** |
| 2.7 | 走到 MIB 結尾時不得用 endOfMibView 塞滿回應 | **[已實作]** |
| 2.8 | GET 命中 / noSuchInstance / noSuchObject 三種語意正確 | **[已實作]** |
| 2.9 | GETNEXT 在首筆之前、末筆之後的邊界行為 | **[已實作]** |
| 2.10 | 唯讀：SET 一律回 noSuchObject / notWritable，任何 OID 皆不可寫 | [待實作] |
| 2.11 | 走訪中途快照換手：整趟 walk 必須讀同一份 snapshot，不得出現列數變動 | [待實作] |
| 2.12 | 型別正確性：Counter64 用於 ifXTable、Gauge32 不得回負值、TimeTicks 單位為 1/100 秒 | [待實作] |
| 2.13 | Golden `.snmprec` fixture 比對：各 Windows 版本一份，回歸時逐筆對照 | [待實作] |

---

## 3. L2，資料正確性（collector → MIB 值）

協定對了不代表數字對。本層驗證「Windows 真實狀態」與「SNMP 吐出的值」一致。

| # | 項目 | 狀態 |
|---|---|---|
| 3.1 | hrStorage 容量與 `Get-Volume` 一致（誤差在一個 allocation unit 內） | [待實作] |
| 3.2 | ifTable/ifXTable 計數器與 `Get-NetAdapterStatistics` 一致 | [待實作] |
| 3.3 | hrProcessorLoad 為過去一分鐘平均，與 PDH 對照誤差 < 5% | [待實作] |
| 3.4 | 記憶體：Physical / Virtual 與 `GlobalMemoryStatusEx` 一致 | [待實作] |
| 3.5 | UCD-DISKIO 與 `IOCTL_DISK_PERFORMANCE` 原始值一致 | [待實作] |
| 3.6 | sysDescr 格式能被 LibreNMS `Windows.php` 的 regex 正確 match | [受阻] 需 Windows |
| 3.7 | sysObjectID 依 GetProductInfo / DsRole 正確分支（工作站 / 伺服器 / DC） | [受阻] 需三種機器 |
| 3.8 | **量不到就不回報**：collector 失敗時該列必須消失，不得出現 0 或前值 | [待實作] |

---

## 4. L3，效能與 host impact

### 4.1 Agent 自身效能（、）

| # | 項目 | 門檻 | 狀態 |
|---|---|---|---|
| 4.1.1 | 每 varbind 處理成本 | < 80 µs | **[已實作]** `bench/gate_c/run_bench.py` |
| 4.1.2 | 完整 `snmpbulkwalk .1.3.6` | < 10 秒 | **[已實作]** |
| 4.1.3 | GETBULK（max-repetitions = 25）回應 | < 30 ms | **[已實作]** |
| 4.1.4 | 完整裝置 poll wall clock | < 20 秒 | [待實作] 需真實 LibreNMS poll |
| 4.1.5 | 快照重建 | < 500 ms，且不在請求路徑上 | **[已實作]** 建立時間已量測 |
| 4.1.6 | 服務啟動到可回應 | < 10 秒 | [受阻] 需 Windows 服務 |
| 4.1.7 | Idle CPU | < 0.5% | [待實作] |
| 4.1.8 | RSS | < 80 MB（250 MB 觸發自我重新啟動） | [待實作] |
| 4.1.9 | 合成規模回歸 1k / 10k / 50k varbind，退步 > 20% 即 fail build | 基準線比較 | **[已實作]** |
| 4.1.10 | 併發：3 台 manager 同時 poll | 全部在 SLA 內 | [待實作] |

### 4.2 Host impact（**spec 完全缺漏，本計劃新增**）

「不能讓 Windows 在被 poll 時變慢或卡住」是硬性要求。 只量 agent 自己快不快，
沒有任何一項在量 host 被拖慢多少。以下為新增：

| # | 項目 | 門檻 | 狀態 |
|---|---|---|---|
| 4.2.1 | poll 期間 agent 程序 CPU | < 單一邏輯核心的 25% | [待實作] |
| 4.2.2 | poll 期間整機 CPU 增量 | < 5% | [待實作] |
| 4.2.3 | **固定基準工作負載在 poll 期間的效能退化** | **< 3%** | [待實作] |
| 4.2.4 | poll 期間磁碟 I/O 佇列深度增量 | 可忽略（不得產生持續性 I/O） | [待實作] |
| 4.2.5 | 程序優先權為 `BELOW_NORMAL_PRIORITY_CLASS` | 斷言 | [待實作] |
| 4.2.6 | collector 執行緒處於 `THREAD_MODE_BACKGROUND_BEGIN`（同時降 CPU 與 I/O 優先權） | 斷言 | [待實作] |
| 4.2.7 | 快照重建 single-flight：3 台 manager 同時 poll 只觸發 1 次採集 | 斷言採集次數 | [待實作] |
| 4.2.8 | 最壞情境：Hyper-V host、64 vCPU、40 介面、500 ARP、20 volume、8 實體碟、hrSWInstalled 啟用 | 上列全數達標 | [受阻] 需 Server |
| 4.2.9 | 斷線網路磁碟存在時，poll 不得卡住（ 的 30 秒卡死） | walk 時間不變 | [待實作] |

---

## 5. L4，資安（）

| # | 項目 | 通過條件 | 狀態 |
|---|---|---|---|
| 5.1 | 24 小時 boofuzz fuzzing（UDP/161） | 零 crash、零 hang、RSS 不成長 | [待實作] |
| 5.2 | PROTOS c06-snmpv1 測試集 | 同上 | [待實作] |
| 5.3 | VACM 逃逸：`librenms-minimal` 下 walk `.1.3.6`，被排除的 subtree 完全取不到 | GET 與 GETNEXT 皆測 | [待實作] |
| 5.4 | **VACM 必須在走訪路徑上生效**（典型漏洞是 GET 有篩選、walk 直接跨過去） | 專項測試 | [待實作] |
| 5.5 | 未認證封包風暴 | CPU 不超標、RSS 不成長、正常 manager 仍在 SLA 內 | [待實作] |
| 5.6 | Pre-auth gate：來源 IP 不在白名單者零解析即丟棄 | 斷言未進入 BER decoder | **[已實作]** `tests/test_preauth_gate.py`（32 例）+ 實機驗證 |
| 5.6.1 | 閘門掛點正確性（覆寫不存在的方法會讓整個閘門無聲失效） | 突變測試證實可攔截 | **[已實作]** `tests/test_gate_hookpoint.py`（6 例）|
| 5.6.2 | loopback 永遠放行（否則安裝程式健康檢查必定失敗） | 斷言 | **[已實作]** |
| 5.6.3 | SAST / SCA / SBOM 基線 | Bandit HIGH=0、相依 CVE=0 | **[已實作]** 見 `docs/security-scanning_zh-TW.md` |
| 5.7 | 深度巢狀 SEQUENCE | 事件迴圈上無未攔截的 RecursionError | [待實作] |
| 5.8 | 速率限制在 USM 密碼學處理**之前**生效 | 斷言呼叫順序 | [待實作] |
| 5.9 | 所有 HMAC 比對使用 `compare_digest` | 靜態檢查 + code review checklist | [待實作] |
| 5.10 | 拒絕 MD5 / DES / 3DES，即使 library 提供 | 載入即拒 | [待實作] |
| 5.11 | 金鑰明文不得出現在 config / log / Event Log / MSI 屬性 | 全文掃描 | [待實作] |
| 5.12 | localized key 以 DPAPI machine scope 儲存 | 磁碟上找不到明文 passphrase | [待實作] |
| 5.13 | 回應大小驗證 | 所有回應 < 1400 bytes，無 IP 分片 | **[已實作]** L1-2.5 |
| 5.14 | 依賴弱點掃描 + SBOM 產出 | 零 High | [待實作] |
| 5.15 | 未簽章檔案在 WDAC 強制模式下可用雜湊規則放行 | 服務可啟動 | [受阻] 缺 WDAC 端點 |
| 5.16 | ProgramData ACL：攻擊者搶先建目錄 | 偵測到即重設 ACL 並記錄 | [待實作] |
| 5.17 | 服務 ImagePath 加引號（unquoted service path） | 斷言 | [待實作] |
| 5.18 | `SO_EXCLUSIVEADDRUSE` 已設定 | 第二個程序綁定同一 port 失敗 | [待實作] |
| 5.19 | 特權縮減生效：SeDebug / SeLoadDriver / SeTcb 等已放棄 | `sc qprivs` 比對 | [待實作] |

---

## 5.5 Windows Server 情境（本計劃新增）

使用者要求「考慮本程式安裝在 Windows Server 的各種狀況」。
目前所有驗證都在 Windows 10 / 11 工作站上完成，**Server 端一項未驗**。
以下逐項列出，未涵蓋即為未通過。

### 5.5.1 版本與安裝型態

| # | 情境 | 風險 | 狀態 |
|---|---|---|---|
| 5.5.1 | Server 2016 / 2019 / 2022 / 2025 各自的 build number 對應 | LibreNMS 以 build 查表，錯了顯示錯版本 | **[部分已驗]** 2016 = build 14393、2022 = build 20348，兩台都由正式 LibreNMS 判讀正確（2022 顯示 `Server 2022 (21H2)`）。**2019 與 2025 無環境** |
| 5.5.2 | **`InstallationType` 為 `Server Core`** | 等值比較會把它誤判為工作站，sysObjectID 就走錯分支 | **[已實作]** `tests/test_product_type.py`。Server Core 不是目標平台，但**程式仍必須認得這個字串** —— 誤判的代價是 LibreNMS 顯示錯的版本，而不是安裝失敗，沒有人會發現 |
| 5.5.3 | **網域控制站** | sysObjectID 需走第三分支，LibreNMS 才會呼叫 `getDatacenterVersion()` | **[已驗]** 2026-08-25 在一台運作中的 PDC 上實測，回報 `1.3.6.1.4.1.311.1.1.3.1.3`。三支分支現在都有實測：用戶端 `.1.1`（`.163`）、伺服器 `.1.2`（`.187`）、DC `.1.3`（`.49`）|
| 5.5.4 | Nano Server / 容器映像 | 多數 Win32 API 不存在 | [待實作] 明確列為不支援 |
| 5.5.5 | 舊版無 `InstallationType` | 需退回 `ProductOptions\ProductType`（WinNT / LanmanNT / ServerNT） | **[已實作]** |

### 5.5.2 Server 特有的資料來源差異

| # | 情境 | 風險 | 狀態 |
|---|---|---|---|
| 5.5.6 | **Hyper-V host**：`GetIfTable2()` 回 40～80 個介面 | 全部輸出會產生大量無用 port 與無主的 RRD | [待驗] 篩選邏輯已實作，未在真 Hyper-V 上驗 |
| 5.5.7 | **NIC teaming / SET** | team 成員與 team 介面都會出現，需決定輸出哪個 | [待實作] |
| 5.5.8 | **多網路卡跨網段**（管理網與業務網分離） | 回應來源 IP 可能錯誤 → 間歇性 timeout（閘門 A） | [受阻] 無環境 |
| 5.5.9 | **iSCSI / FC / 多路徑磁碟** | `PhysicalDriveN` 可能重複出現同一顆 LUN | [待實作] |
| 5.5.10 | **Storage Spaces 虛擬磁碟**（> 8 TB） | `hrStorageSize` 為 Integer32，需動態放大 allocation unit | [待實作] 邏輯已寫，未以大容量驗證 |
| 5.5.11 | **BitLocker 鎖定的磁碟區** | `GetDiskFreeSpaceEx` 可能卡住或失敗 | [待實作] |
| 5.5.12 | **叢集共用磁碟區（CSV）** | 多節點看到同一磁碟區，容量重複計算 | [待實作] 列為非目標 |
| 5.5.13 | **64 / 128 核心** | `hrProcessorTable` 列數大增；PDH wildcard 展開昂貴 | [待驗] 已改用 `NtQuerySystemInformation` |
| 5.5.14 | **RAID 控制器後的實體磁碟** | SMART / 溫度路徑不同 | **[已驗]** Intel RST 需走 ATA SMART（實體機驗證） |
| 5.5.15 | **BMC 存在的伺服器** | 感測器建議改由 BMC 帶外取得，不從 OS 內取 | [待決策] |

### 5.5.3 Server 環境的部署差異

**2026-08-25 首次取得 Server 環境**：Windows Server 2016 Standard（build 14393）、
**主網域控制站**（`jasontools.com.tw`，NTDS / DNS / Netlogon 皆執行中，持有 PDC 模擬器角色）。
存取方式為自行安裝的 OpenSSH（Server 2016 沒有內建，安裝檔以 SMB 送入並在機器上核對雜湊）。

在這台上跑完的結果：

| 項目 | 結果 |
|---|---|
| **40 項生命週期** | **PASS=40 FAIL=0**，測的是發布的 0.9.8 MSI，雜湊在機器上核對過 |
| **網域控制站的 `sysObjectID`** | 實測回報 `1.3.6.1.4.1.311.1.1.3.1.3`，**DC 分支第一次被真的走到**；先前只驗過 client 分支 |
| **無 community 可繼承時的行為** | 見 6.1c.10，1603 回滾、無殘留、內建服務未被動過 |
| **MS SNMP 移轉** | 唯讀 community 帶過來、**可寫的沒有**（記錄檔明載降級理由）；sysContact / sysLocation 由 agent 在執行時直接讀內建服務的登錄機碼，實測回報正確 |

**2026-08-25 第二台**：Windows Server 2022 Standard（build 20348），
**獨立伺服器、完全沒有安裝內建 SNMP**（服務不存在、161 無人佔用）。
存取走 WinRM 與 HTTP，機器不需要對外網路，也不必在上面安裝任何額外軟體。

| 項目 | 結果 |
|---|---|
| **生命週期** | **PASS=33 FAIL=0**。少的 7 項全部是內建 SNMP 相關（接管、還原記錄、歸還），這台沒有那個服務，屬**合理跳過**而非未執行 |
| **完全沒有內建 SNMP 且未給 community** | msiexec 1603 回滾、服務不存在、程式目錄與防火牆規則皆無、Apps 清單沒有；記錄檔給出兩種補救方式 |
| **`sysObjectID`** | `1.3.6.1.4.1.311.1.1.3.1.2`，**伺服器分支**。至此用戶端（`.1.1`）、伺服器（`.1.2`）、網域控制站（`.1.3`）三支都有實測 |
| **正式 LibreNMS 端對端** | 已加入為 device 119，探索並輪詢成功。LibreNMS 判定 **`os=windows`、`version=Server 2022 (21H2)`、`hardware=Intel x64`**；收到 1 個連接埠、1 個儲存、4 個記憶體池、4 個處理器、1 個磁碟 I/O、5 筆 entPhysical |
| 感測器 / 應用程式 | 0 / 0。虛擬機沒有 ACPI 溫度區、沒有電池、虛擬磁碟沒有 SMART，屬預期 |
| VBS / Credential Guard / HVCI | 這台皆為 0（未啟用），所以 **2025 的預設值那條仍未實測** |


| # | 情境 | 狀態 |
|---|---|---|
| 5.5.16 | GPO 軟體安裝派送 MSI 到網域內多台 Server | [受阻] 需 MSI + 網域 |
| 5.5.17 | 安裝程式必須完全非互動（無 GUI 環境） | **[已驗]** 每個值都以 MSI 屬性傳入，缺了就失敗，不會停在那裡等人輸入；40 項生命週期全程走 `/qn`。**Server Core 不列為目標平台**，因此不再追蹤「在 Server Core 上實測」這一項 |
| 5.5.18 | 已安裝 SNMP 功能的 Server，設定仍應移轉 | **[已驗]** Server 2016 DC：內建服務執行中且設有一個唯讀、一個可寫 community，安裝後唯讀那個進了 `config.json`，可寫的被擋下並記錄；sysContact / sysLocation 實測回報正確。**服務「停用」的變體仍待驗** |
| 5.5.19 | 已有第三方監控 agent 佔用 161（Zabbix / Net-SNMP） | **[已實作]** 中止且不動它 |
| 5.5.20 | 遠端桌面工作階段主機（大量使用者、`hrSystemNumUsers` 應正確） | [待實作] 目前固定回 1 |
| 5.5.21 | 網域控制站上的登錄檔存取 | **[部分已驗]** 可寫 DC（PDC）上安裝、移轉、40 項全過，`sysObjectID` 走 DC 分支。**RODC 仍待驗**，需另一台 |
| 5.5.22 | 啟用 HVCI / WDAC 的 Server | [受阻] 無環境 |

### 5.5.4 已知會出錯、需明確處理的項目

- **`hrSystemNumUsers` 目前固定回 1**（5.5.20）。在 RDS 主機上這是錯的，
  應以 `WTSEnumerateSessions` 計算實際工作階段數。**這是已知缺陷，不是待驗項目。**
- **NIC teaming**（5.5.7）目前沒有任何處理，team 與成員介面都會被當成硬體介面輸出，
  造成流量重複計算。**這是已知缺陷。**

---

## 6. L5，安裝與部署（、）

### 6.1 安裝矩陣

| # | 項目 | 狀態 |
|---|---|---|
| 6.1.1 | 乾淨安裝 → 服務啟動 + 可回應 + 防火牆規則正確 | **[已驗]** `msiexec /qn` EXIT=0，服務 Running、161 服務中、規則正確 |
| 6.1.2 | 升級 v(n-1) → v(n)：index-map 保留、ifIndex 不變 | **[已驗]** 0.1.0→0.1.1 直接安裝新版，EXIT=0，安裝項目數維持 1（UpgradeCode 正確），index-map hash 完全相同 |
| 6.1.3 | 升級失敗倒回 → 舊版本恢復且服務可用 | **[部分已驗，且發現缺口]** 2026-08-25 在 `.154` 上真的發生過：健康檢查失敗、msiexec 回 1603、**舊版本正確恢復**（版本仍是 0.9.8、程式檔完整）。但**服務被留在停止狀態** —— 「舊版本恢復」成立，「服務可用」不成立。在無訊息安裝下沒有人會看見，監控就這樣靜靜停掉。追蹤於 6.1d.4 |
| 6.1.4 | 解除安裝 → 服務刪除、規則刪除、ProgramData 保留、不需重開機 | **[已驗]** `msiexec /x` EXIT=0，服務/埠/規則/程式目錄皆清除，資料目錄與 index-map 保留 |
| 6.1.5 | PURGE 解除安裝 → 完全清除 | **[已驗]** `PURGE=1` EXIT=0，資料目錄完整消失。首次實測**失敗**，自訂動作的記錄檔就在被清除的目錄裡，刪完後收尾的 `Log` 又把 `logs\` 重建回來。已修（清除前關閉檔案記錄 + 重試 + 驗證），迴歸測試見 `tests/test_uninstall_purge.py` |
| 6.1.6 | 重複安裝冪等 | **[已驗]** 解除安裝後重裝 EXIT=0 |
| 6.1.6a | 出現在「加入或移除程式」 | **[已驗]** `jt-snmpd v0.1.0 / Jason Tools` |
| 6.1.6b | 健康檢查失敗時 MSI 倒回 | **[已驗]** loopback 失敗時 MSIEXEC_EXIT=1603 並完整倒回（實測發生過）|
| 6.1.7 | 161 被 MS SNMP Service 佔用 → 自動停用 + 設定移轉 | **[已驗]** Win11 內建 SNMP 設為 Automatic/Running 後安裝，安裝後內建為 Stopped/Disabled、161 由 jt-snmpd 持有 |
| 6.1.8 | 161 被非 Microsoft 程式佔用 → 中止且不動它，訊息正確 | [受阻] |
| 6.1.9 | 安裝過程強制斷電 → 重開機後可修復或可重裝 | [受阻] |
| 6.1.10 | GPO 無訊息部署到 5 台 VM → 全部就緒 | [受阻] 需 Proxmox |
| 6.1.11 | ADMX 原則變更 → 5 分鐘內生效 | [受阻] |
| 6.1.12 | 解除安裝後 → Windows SNMP Service 正確還原 | **[已驗]** 移除後內建自動還原為 Automatic 並重新啟動，161 交還給 `snmp.exe` |
| 6.1.13 | **安裝 → 升級 → 移除後仍正確還原內建 SNMP** | **[已驗]** 首次實測**失敗**，升級時重讀當下狀態（已被停用）覆寫還原記錄，導致解除安裝端 `$orig -ne 'Disabled'` 不會成立，內建 SNMP 再也回不來。已修（既有還原記錄優先），迴歸測試見 `tests/test_ms_snmp_takeover.py` |
| 6.1.15 | **跨改名邊界升級**（0.9.5 以前 → 現行版）：資料目錄從 `JT-SNMP` 移到 `jt-snmpd`，index-map 必須逐位元組保留 | **[已驗]** 2026-08-25 在 `.163` 上 0.9.1 → 1.0.0：舊目錄已移除、新目錄成立、`index-map.json` 的 SHA-256 與升級前完全相同、`first_seen` 未變，LibreNMS 上該裝置的連接埠沒有被刪除重建。升級前另有整份備份 |
| 6.1.16 | **完全沒有內建 SNMP 服務、又未給 `COMMUNITY`** → 中止並說明 | **[已驗]** `.187`（Server 2022，服務不存在）：1603 回滾、服務不存在、程式目錄與防火牆規則皆無、Apps 清單沒有。與 6.1c.10（服務存在但沒有 community）是兩條不同的路徑，兩條都驗過 |
| 6.1.14 | 內建 SNMP 停用失敗（群組原則阻擋）→ 安裝中止並說明原因 | **[已實作]** 停用後驗證實際狀態，不符即 `exit 1`；[待驗] 需受管控環境 |

### 6.1a 完整生命週期自動化測試

`tests/lifecycle.ps1`，一次跑完五個階段，**40 項檢查全綠才算通過**。
每次改版與每次發版前都要在目標機重跑。

```powershell
powershell -ExecutionPolicy Bypass -File tests\lifecycle.ps1
# 結尾輸出 LIFECYCLE_RESULT=PASS / FAIL
```

| 階段 | 涵蓋 |
|---|---|
| 0. 清除既有安裝 | 建立乾淨起點；將內建 SNMP 設回 Automatic/Running，否則接管與歸還兩段都測不到東西 |
| 1. 乾淨安裝 | 結束碼、服務狀態、UDP/161、防火牆（UDP + ICMP）、程式與資料目錄、index-map、ARP 項目、版本、LocalSystem、啟動類型、**ImagePath 引號**、資料目錄 ACL、**內建 SNMP 接管**、**還原記錄內容** |
| 2. 升級（安裝同一版本） | 冪等、服務續跑、ARP 僅一項（UpgradeCode 正確）、index-map 未被覆寫、**還原記錄未被污染** |
| 3. 移除（預設保留資料） | 服務刪除、**161 持有者不再是 jt-snmpd**、防火牆與程式目錄清除、**資料目錄與 index-map 保留**、不需重開機、**內建 SNMP 歸還為 Automatic 並重新啟動** |
| 4. 重裝 | 沿用保留的狀態，**index-map hash 不變（ifIndex 穩定）** |
| 5. PURGE 移除 | 資料目錄完整清除、內建 SNMP 狀態不受影響 |

**最近一次結果**：2026-08-25，**jt-snmpd 1.0.0（GitHub Release 上下載、機器上核對過 SHA-256）**：

| 機器 | 結果 |
|---|---|
| Windows Server 2016 網域控制站 | 40/40（測的是 0.9.8；1.0.0 的封裝變更不影響這台已驗的路徑，下次改版時重跑） |
| Windows Server 2022 獨立伺服器 | **PASS=33 FAIL=0**，第 1 階段版本檢查回報 `1.0.0`。沒跑的 7 項全是內建 SNMP 相關，那台沒有那個服務 |
| Windows 11 26200 | 40 項，見下 |

先前紀錄：2026-08-25，Win11 26200，jt-snmpd 0.9.8，`PASS=40 FAIL=0`。
測的是 GitHub Release 上下載的那一顆 MSI，在機器上核對過 SHA-256，
與發布的 `.sha256` 相符，不是本機建置的產物。第 1 階段的版本檢查回報 `0.9.8`，
所以測到的確實是這一版。

這一次是**開跑前的準備**抓到問題，測試本身全綠：

- **測試機上最新的 MSI 是 0.9.2。** 腳本挑 `dist\` 裡最新的那一顆，直接跑下去會
  測到 0.9.2 然後回報全綠。這正是本專案一路在防的那類失敗：綠燈，但測的是舊東西。
- **測試機上那份 `lifecycle.ps1` 是改名時壞掉的舊版**（還在比對 `JT SNMP`、
  還釘死 `0.2.0`）。修好的版本在 repo 裡放了一段時間，**從來沒有真的在機器上執行過**。
  一支沒跑過的測試腳本不是證據。**每次都要從 repo 複製過去再跑。**
- 用 `powershell -EncodedCommand` 送進去執行時要記得帶執行原則，
  否則載入腳本檔會以 `PSSecurityException` 失敗（`TEST_PLAN` 上面寫的執行方式本來就有帶）。

順帶驗到一件設計上的事：第 5 階段以 `PURGE=1` 把資料目錄整個刪掉之後重裝，
agent 回報的 `ifIndex` 仍然是 1、`ifName` 仍然是 `乙太網路`，與 LibreNMS
既有紀錄相同。**ifIndex 是從持久的 `NET_LUID` 推出來的，不是從 index-map 檔案讀的**，
所以即使狀態目錄被毀掉，連接埠歷史仍然接得回去。

歷次以此測試抓到並修正的缺陷：

| 版本 | 缺陷 | 為什麼其他測試抓不到 |
|---|---|---|
| 0.2.0 | `PURGE=1` 未清除資料目錄 | 自訂動作的記錄檔就在被刪的目錄裡，收尾訊息把 `logs\` 重建回來。結束碼 0、記錄檔也寫著「已完整清除」 |
| 0.2.0 | 升級後再移除，內建 SNMP 永遠停在停用 | 安裝→移除正確，安裝→**升級**→移除才失敗。升級時重讀當下狀態覆寫了還原記錄 |
| 0.9.1 | agent 從未讀取設定檔 | 寫死的預設值正好是實驗室在用的值。換成別的值即 1603 回滾，但那組值從沒被試過 |
| 0.9.2 | MSI 打包到比原始碼舊的執行檔 | 建置回報成功、版本號與 SHA-256 都是新的，只有裡面的程式碼是舊的 |

---

### 6.1b 建置閘門（本計劃新增）

同一類失敗發生過三次：**綠燈的建置，出貨的是舊東西**。三道閘門由
`tests/test_build_gates.py` 靜態守著。

| # | 閘門 | 擋掉的情況 | 狀態 |
|---|---|---|---|
| 6.1b.1 | `build-exe.ps1`：產物必須比原始碼新 | PyInstaller 失敗，舊 exe 還在，只檢查存在會誤判成功 | **[已驗]** |
| 6.1b.2 | `build-exe.ps1`：`--selftest` | exe 產出但缺 pysnmp 的 MIB 資料檔，服務顯示 Running 卻每次請求都拋 `MibNotFoundError` | **[已驗]** |
| 6.1b.3 | `build-msi.ps1`：`wix build` 的結束碼 | 缺 WiX 擴充導致建置失敗，卻取到上一個 MSI 並以舊版本號回報成功 | **[已驗]** 實測攔下 |
| 6.1b.4 | `build-msi.ps1`：MSI 必須是本次產出 | 殘留檔案冒充本次建置的成果 | **[已驗]** |
| 6.1b.5 | `build-msi.ps1`：執行檔必須比原始碼新 | 忘了先跑 build-exe，修正沒進 MSI，但版本號、SHA-256、歸檔目錄全是新的 | **[已驗]** 實測攔下 |
| 6.1b.6 | BUILDINFO 記錄 configure / wxs / agent 三份來源指紋 | 同一台機器上有兩份 `msi-configure.ps1`，改到不被用的那份 | **[已驗]** |

### 6.1c 圖形安裝介面（本計劃新增）

> **這一節曾經整段是假的綠燈。** 6.1c.1 原本寫著「已驗：MSI 內含 `JtSettingsDlg`、
> 15 個控制項、18 個 UI 序列動作」。那句話每個字都對，而且完全沒有驗到重點：
> 它證明的是**對話框存在於 MSI 裡**，不是**精靈走得到它**。
> 0.9.2 到 0.9.4 連續三版的 GUI 安裝都是壞的，而這張表一直是綠的。
>
> 三個缺陷的共同點是：WiX 原始碼看起來完全正常，而建出來的 MSI 表格一目了然。
> 教訓是**驗產物，不要驗來源**。現在的做法是直接讀 MSI 的
> `LaunchCondition`、`ControlEvent`、`Property` 三張表。

| # | 項目 | 狀態 |
|---|---|---|
| 6.1c.1 | 雙擊 MSI 後精靈**走得到**設定畫面 | **[已驗]** 讀建出的 MSI `ControlEvent` 表：`JtSettingsDlg` 在 Order 5，壓過 WixUI 的 `VerifyReadyDlg`（Order 4）；0.9.5 實機五頁逐頁確認 |
| 6.1c.2 | 啟動條件不得在 GUI 收集到值之前就擋下安裝 | **[已驗]** `MANAGEMENTNETWORKS OR REMOVE OR UILevel > 4`；CI 讀 `LaunchCondition` 表 |
| 6.1c.3 | 管理網段未填時不允許繼續 | **[已驗]** 2026-08-25 在 `.154` 對**發布的 0.9.8** MSI 實際操作：兩欄留空按「下一步」，跳出「Please enter the management networks. Without them the agent answers local queries only, which means it is installed but not monitoring.」，停在原頁不放行 |
| 6.1c.4 | 無訊息安裝不受 UI 影響 | **[已驗]** `/qn` 下 UI 序列不執行，屬性照舊生效 |
| 6.1c.5 | 選用核取方塊預設未勾選，且標示與行為一致 | **[已驗]** `KEEPMSSNMP` 不在 `Property` 表中（非空值會讓方塊顯示為已勾選）；CI 讀 `Property` 表 |
| 6.1c.6 | 授權頁顯示本專案的 LICENSE，不是佔位文字 | **[已驗]** GPL-3.0 全文；CI 檢查沒有 `Lorem ipsum` |
| 6.1c.7 | 精靈美術為本專案的，且與產生器輸出一致 | **[已驗]** CI 重新產生後比對雜湊 |
| 6.1c.8 | 標題列在整個精靈中一致 | **[已驗]** 所有對話框皆為 `jt-snmpd Setup` |
| 6.1c.9 | 「加入或移除程式」顯示圖示 | **[已驗]** `DisplayIcon` 指向已安裝的執行檔 |
| 6.1c.10 | 無法決定 community 時給出可行動的錯誤 | **[已驗]** 2026-08-25 Server 2016 DC：內建 SNMP 裝著、跑著、但未設定任何 community。不給 `COMMUNITY` 安裝 → msiexec 1603 回滾、服務不存在、程式目錄與防火牆規則皆無、內建服務仍 Running/Automatic 未被動過；記錄檔給出兩種補救方式 |
| 6.1c.11 | **端對端 GUI 安裝**（真的按下 Install 並完成） | **[已驗]** 2026-08-25 對**發布的 0.9.8** MSI 在 `.154` 以 RDP 走完全程：歡迎頁 → 授權（GPL-3.0 全文）→ 目的地資料夾（`C:\Program Files\jt-snmpd\`）→ 監控設定 → 準備安裝 → UAC → 安裝 → 完成。裝完實測：服務 Running/Automatic、版本 0.9.8、161 由 jt-snmpd 持有、內建 SNMP 已停用、防火牆兩條、`config.json` 寫入了精靈裡填的網段與 community。`/qn` 路徑由 6.1a 的 40 項涵蓋 |
| 6.1c.12 | 圖形升級不應跳出「使用中的檔案」對話框 | **[已知缺陷，未修，兩種修法都實測失敗]** 服務在升級時還在跑，Restart Manager 偵測到 `jt-snmpd.exe` 被占用。`/qn` 與 GPO 派送不受影響，所以 6.1a 那 40 項（全部走 `/qn`）永遠抓不到它。<br>**修法一：`ServiceControl` 放在獨立元件。** 那一列確實進了建出來的 MSI，但對話框照樣列出 `jt-snmpd`。<br>**修法二：`ServiceControl` 放進擁有 `jt-snmpd.exe` 的元件。** `jt-snmpd` **確實從清單上消失了**，但同一台機器的清單變成 `nxlog` 與 `Windows Event Log` —— Windows Installer 開始去列舉並關閉**與我們無關的服務**。`/qn` 之下它會直接關掉再重啟它們，而關掉事件記錄服務會干擾我們的服務啟動：升級以 1603 回滾，且**服務被留在停止狀態**。在 `.154`（有 nxlog 等軟體）可重現，在乾淨的 Server 2022 上則全部成功 —— 也就是說這個修法只在乾淨機器上看起來是對的。<br>**判斷：讓安裝程式去關閉別人的服務，比多跳一個對話框糟糕得多**，因此撤回。`tests/test_msi_ui_gating.py` 現在守著「不得再加回 ServiceControl」。<br>**下一個人可以從這裡開始**：exe 本身沒問題（`--selftest` 在失敗的那台上照樣通過），乾淨安裝與乾淨機器的升級都成功，所以要找的是「如何只停自己的服務而不驚動 Restart Manager 去碰別人的」 |

**仍然沒有覆蓋的**：整套生命週期自動化（`tests/lifecycle.ps1`）從頭到尾都走
`msiexec /qn`，所以任何只在 GUI 路徑上出現的問題，自動化一律抓不到。
GUI 那條路目前靠人工操作與截圖補，還沒有自動化；要自動化需要一台不在正式監控中的
Windows 機器，能反覆重裝而不會影響到任何人。

---

### 6.1d 與第三方軟體共存（本計劃新增，2026-08-25）

**這一整類先前不存在，而它差一點讓一個壞掉的修法出貨。**

修「使用中的檔案」對話框時，做了一版把 `ServiceControl` 放進擁有 `jt-snmpd.exe`
的元件。它在三個地方看起來都是對的：原始碼、建出來的 MSI 表格、以及一台乾淨的
Server 2022 上的全新安裝與升級。**只有在一台裝了其他軟體的 Windows 11 上才顯出問題** ——
Windows Installer 轉而去列舉並關閉與我們無關的服務（`nxlog`、`Windows Event Log`），
升級以 1603 回滾，而且服務被留在停止狀態。

教訓不是「要驗產物」，那條早就有了。教訓是**乾淨的機器會遮蔽一整類問題**，
而客戶的機器不會是乾淨的。

| # | 情境 | 為什麼重要 | 狀態 |
|---|---|---|---|
| 6.1d.1 | 安裝／升級**不得停止或重新啟動任何不屬於本產品的服務** | 動到別人的服務比任何顯示問題都嚴重，而且在無訊息安裝下不會有人看見 | **[已驗]** 目前無 `ServiceControl`，Restart Manager 只會列出本來就持有檔案的處理程序 |
| 6.1d.2 | 至少一台驗證機器上要有**其他常駐服務**（記錄轉送、防毒、備份代理） | 只在乾淨機器上測，等於沒測這一類 | **[已驗]** `.154` 裝有 nxlog 等軟體，且**刻意保留** |
| 6.1d.3 | 圖形升級時「使用中的檔案」清單**不得列出 `jt-snmpd`** | 見 6.1c.12 | **[未達成]** 已知缺陷，兩種修法都撤回 |
| 6.1d.4 | 安裝失敗回滾後，**服務不得被留在停止狀態** | 失敗的升級讓監控靜靜停掉，比失敗本身更糟 | **[待實作]** 實測過一次會發生（見 6.1c.12），目前沒有自動化檢查 |
| 6.1d.5 | 目標機上已有第三方 SNMP agent 佔用 161 | 見 6.1.8 | [受阻] |

**新增的規則：任何「安裝程式行為」的修改，都必須同時在一台乾淨機器與一台
裝有其他軟體的機器上驗證。** 只有其中一台是綠的，不算通過。

### 6.2 MS SNMP 移轉（）

| # | 情境 | 狀態 |
|---|---|---|
| 6.2.1 | SNMP Service Running、2 組 community、有 PermittedManagers | **[部分已驗]** Running 狀態的偵測、停用與還原記錄已實測；多組 community 情境待驗 |
| 6.2.2 | SNMP Service Stopped/Disabled（設定仍應移轉） | [受阻] |
| 6.2.3 | PermittedManagers 為空 → 必須要求輸入管理網段，不得 Any/Any | [受阻] |
| 6.2.4 | community 權限為 8（READ WRITE）→ 降級 + 警告 | [受阻] |
| 6.2.5 | PermittedManagers 含無法解析的主機名稱 → 列出並警告 | [受阻] |
| 6.2.6 | 有 TrapConfiguration → 完整列出、明確警告 | [受阻] |
| 6.2.7 | 有 ExtensionAgents → 完整列出、明確警告 | [受阻] |
| 6.2.8 | 乾淨機器無 SNMP Service → 流程跳過，不產生錯誤 | **[已驗]** 生命週期測試以 `$msExists` 分支涵蓋；無內建 SNMP 時全部檢查跳過且安裝成功 |
| 6.2.9 | 功能已移除但登錄檔殘留 → 設定仍成功移轉 | [受阻] |
| 6.2.10 | GPO 已定義 community → 原則值優先，移轉結果不覆蓋 | [受阻] |

---

## 7. L6，服務生命週期與穩定性（）

| # | 項目 | 狀態 |
|---|---|---|
| 7.1 | preshutdown 收到後正確 flush engineBoots 與 index-map | [受阻] |
| 7.2 | 電源事件（睡眠恢復）後重新初始化 PDH handle 與所有 collector | [受阻] |
| 7.3 | Loopback 自我測試：事件迴圈卡死時連續 3 次失敗 → 非零碼結束 | [待實作] |
| 7.4 | SCM failure actions 生效（`failureflag 1`，非零結束碼也觸發重新啟動） | [受阻] |
| 7.5 | 5 分鐘內自我重新啟動 > 3 次 → 進入降級模式，不得無限重新啟動 | [待實作] |
| 7.6 | 啟動不硬失敗：config 語法錯誤 / 161 被佔用 / collector 初始化失敗 / 狀態檔損毀 | [待實作] |
| 7.7 | 降級模式下自我健康 OID 與 system group 仍可回應 | [待實作] |
| 7.8 | 設定重新載入為原子操作，無效設定保留舊值並記 Event 3001 | [待實作] |
| 7.9 | 所有內部計時使用單調時鐘：NTP 往回校時後快取邏輯不得卡死 | [待實作] |
| 7.10 | 具名管線控制通道：ACL 僅 SYSTEM/Administrators，一般使用者不可連 | [待實作] |
| 7.11 | **30 天長時間穩定性**：RSS / handle / thread 曲線平坦 | [受阻] 需 Server |
| 7.12 | 期間內完成：NTP 校時、NIC 熱插拔、磁碟熱插拔、config reload、服務重新啟動、主機重開機、快照還原 | [受阻] |
| 7.13 | LibreNMS 端無 counter reset 誤判、無 port 重複、無 storage 重複 | [受阻] |
| 7.14 | 動態 IP 增減時 socket 正確增減（ P1 路徑） | [受阻] 需多網路卡 |

---

## 8. L7 — LibreNMS 端對端驗收（）

**必須在真實 LibreNMS 上進行，且不得對 LibreNMS 打任何 patch。**

| # | 項目 | 狀態 |
|---|---|---|
| 8.1 | OS Detection：Hardware / Version / Features 三欄位皆有正確值，不得空白 | [受阻] |
| 8.2 | 加入裝置後執行 discovery，確認完整 OID 被抓到 | [受阻] |
| 8.3 | Overview / Processor / Memory / Storage / DiskIO / Ports 六個頁面全滿 | [受阻] |
| 8.4 | Ports：speed / state / traffic / packets / errors / discards 皆有值 | [受阻] |
| 8.5 | 連續 poll 24 小時後 RRD 無斷點、沒有失去對應的項目 | [受阻] |
| 8.6 | 重新啟動 agent 後 LibreNMS 不得誤判 counter reset 或 device reboot | [受阻] |
| 8.7 | 升級 agent 後 port / storage / processor 不得重新 discovery | [受阻] |
| 8.8 | 自我健康 OID 的 alert rule 範本能正確觸發 | [受阻] |

> **注意**：deploy 到目標機器後必須主動觸發 LibreNMS discovery，
> 否則只會跑 poll 而抓不到新增的完整 OID 集合。此步驟要寫進部署 SOP 與 8.2。

---

## 9. 測試環境矩陣

**這張表過期過一次，2026-08-25 全面重寫。** 上一版還寫著 Windows 11「SSH 待授權」、
LibreNMS「存取待提供」、Windows Server「無」，而那三項當時都已經在用了。
狀態表會過期，**每次改版都要回頭核對這一張**。

### 可用

| 環境 | 位址 | 身分 | 存取方式 | 涵蓋 |
|---|---|---|---|---|
| Ubuntu 24.04 開發機 | — | — | 本機 | L0、L1、L3 agent 自身效能、所有靜態檢查 |
| **Windows 11 26200** | `.154` | QEMU 虛擬機，已納管 | OpenSSH（`scratchpad/win.sh`）、RDP 可用於 GUI | L2、L4、L5 全套、host impact、**圖形安裝與升級** |
| **Windows 10 22H2** | `.163` | Dell Latitude E5270 **實體機**，已納管 | OpenSSH | 實體硬體才有的路徑：SMART、ACPI 溫度區、電池、entPhysical |
| **Windows Server 2016** | `.49` | Standard，**網域控制站** | 自行安裝的 OpenSSH（2016 無內建） | Server 安裝矩陣、DC 分支 `sysObjectID`、內建 SNMP 移轉 |
| **Windows Server 2022** | `.187` | Standard，獨立伺服器，**無內建 SNMP** | **WinRM 5985**，機器上不必裝任何東西 | Server 安裝矩陣、「完全沒有可移轉來源」那條路徑 |
| LibreNMS 26.8.1 | `.68` | **正式機** | SSH 金鑰 | L7 端對端。**任何會改動的操作都要先問使用者** |

兩台 Server 都**不提供對外網路**。送檔案的方法是在開發機起
`python3 -m http.server`，由目標機從內網拉；`.187` 的 SMB 445 是關的。
兩台一開始都完全連不進去，**兩次都是 Proxmox 的防火牆規則沒加**，
不是 Windows 防火牆 —— 判斷方式是看 ARP：有 MAC 且 REACHABLE 就表示第二層通，
被擋的是第三層。

### 仍然沒有

| 環境 | 擋住什麼 | 有無替代 |
|---|---|---|
| Windows Server 2019 / 2025 | 5.5.1 的 build number 對應；2025 的 Credential Guard / VBS 預設啟用 | 無。2025 那條目前只有 Microsoft 文件依據 |
| 唯讀網域控制站（RODC） | 5.5.21 的一半 | 無 |
| HVCI / WDAC 強制的端點 | 閘門 D 剩餘、5.5.22 | 無。`.187` 的 VBS 實測為未啟用 |
| 多網路卡跨網段主機 | 閘門 A、5.5.8 | 無 |
| **不在正式監控中、可反覆重裝的機器** | 端對端 GUI 自動化 | 目前所有實機驗證都是在正式監控底下小心繞著做的 |

**未涵蓋即為未通過。** 任何因缺環境而未驗證的項目，一律在
release notes 中列為「未驗證項目與風險」，
不得以「應該沒問題」帶過。

---

## 10. Release Gate，出貨前必須全綠

打 tag 與產出 MSI 之前，下列每一項都必須通過。**任一項紅燈即不得出貨。**

```
□ L0 單元測試全數通過
□ L1 協定正確性全數通過（含 golden snmprec 比對）
□ L2 資料正確性全數通過
□ L3 效能達標，且與前一版基準線比較無 > 20% 退步
□ L3 host impact 全數達標（基準工作負載退化 < 3%）
□ L4 資安 harness 全數通過（含 24 小時 fuzzing、VACM 逃逸、SBOM 零 High）
□ L5 安裝矩陣全數通過（含升級、倒回、PURGE、GPO 無訊息部署）
□ L5 MS SNMP 移轉 10 個情境全數通過
□ L6 生命週期全數通過；major 版本另需 30 天穩定性驗證
□ L7 LibreNMS 端對端六個頁面全滿，且不需任何 LibreNMS patch
□ 已公布 SHA-256，且下載後核對相符
□ 「未驗證項目與風險」清單已更新並隨版本發布
□ CHANGELOG 已列出所有行為變更與需要人工介入的升級步驟
```

---

## 11. 目前實作進度

這一段曾經停在「閘門 C 進行中、其餘全部未開始」很久，早就與事實不符。
**改版時要一起改。**

| 層 | 現況 |
|---|---|
| L0 單元測試 | **830 例**，每次推送在 CI 跑 |
| L1 SNMP 協定正確性 | 已實作，CI 另有一步確認那些測試**真的有跑到**，不是被略過 |
| L2 資料正確性 | 已在 Win11、Win10 實體機、Server 2016 DC、Server 2022 上實測 |
| L3 效能與 host impact | 已量測（固定工作負載退化 0.41%，見 §4.2） |
| L4 資安 | 前置閘門已實作並實測；Bandit / pip-audit / 隱私掃描 / 用語檢查在 CI |
| L5 安裝與部署 | 40 項生命週期自動化 + 5 項 MSI 產物檢查 + 圖形安裝實測 |
| L6 服務生命週期 | 部分。長期穩定性（30 天）未做 |
| L7 LibreNMS 端對端 | 三台裝置在正式 LibreNMS 上驗過 |

執行方式：

```bash
.venv/bin/python -m pytest tests/ -q          # L0 + L1，830 例
python3 tools/check-privacy.py                # 個資與已知憑證
python3 tools/check-terminology.py            # 台灣用語
.venv/bin/python -m bench.gate_c.run_bench    # L3

# 目標機（從 repo 複製腳本過去，不要用機器上那一份，它會過期）
powershell -ExecutionPolicy Bypass -File tests\lifecycle.ps1 -Community <community> -ManagementNetworks <cidr>
```

# jt-snmpd v1.0.0

[![License](https://img.shields.io/badge/License-GPL--3.0--or--later-blue)](LICENSE)
![Python](https://img.shields.io/badge/Python-3.12-3776AB?logo=python&logoColor=white)
![Windows](https://img.shields.io/badge/Windows-10%20%2F%2011%20%2F%20Server%202016%2B-0078D6?logo=windows&logoColor=white)
![SNMP](https://img.shields.io/badge/SNMP-v2c-orange)
![LibreNMS](https://img.shields.io/badge/LibreNMS-ready-1e88e5)
![Read only](https://img.shields.io/badge/agent-%E5%94%AF%E8%AE%80-success)
![No outbound](https://img.shields.io/badge/%E5%B0%8D%E5%A4%96%E9%80%A3%E7%B7%9A-%E7%84%A1-success)

> 為 Windows 撰寫的**唯讀** SNMP Agent，以標準 MIB 提供主機監控資料。
> 用來取代已被 Microsoft 標記為**棄用**的內建 SNMP Service，
> 並在不修改 LibreNMS 的前提下餵給 LibreNMS。
>
> 作者 Jason Cheng (Jason Tools) · 授權 GPL-3.0-or-later · English: [README.md](README.md)

> **專案頁面：[https://jasoncheng7115.github.io/jt-snmpd/](https://jasoncheng7115.github.io/jt-snmpd/)**，對照截圖、實測數字與設計取捨，英文與繁體中文可切換。


**資安通報**：要私下回報弱點，請看 [SECURITY.md](SECURITY.md)。

---

## 為什麼要有 jt-snmpd？

Microsoft 已將內建的 SNMP Service 標記為棄用，不再積極開發，未來版本可能移除；
而 Net-SNMP 也沒有現行的官方 Windows
建置。結果是 Windows 主機要嘛沒被監控，要嘛只能改用「主動推送到時序資料庫」的
agent，但當 NMS 講的是 SNMP 時，那並不能解決問題。

jt-snmpd 填補這個缺口，並帶著幾條刻意的限制，全部來自目標環境
（政府機關與醫院：無對外網路、Defender / HVCI / WDAC 普遍啟用、
數十到數百台以 GPO 部署）：

- **唯讀。** 不支援 SNMP SET、不發 trap、不做任何寫入。
- **不主動對外連線。** 不檢查更新、不回報遙測、不在執行時下載任何程式碼。
  安裝檔把需要的東西全部包在裡面。
- **不依賴核心驅動程式。** 磁碟溫度走 Windows 原生 IOCTL，**不用**
  LibreHardwareMonitor，它依賴的 WinRing0 驅動已列入 Microsoft
  vulnerable driver blocklist，在啟用 HVCI 的端點上會觸發 Defender 告警。
- **資料路徑不用 WMI、不用 PowerShell 子處理程序。** collector 以 ctypes 直接呼叫
  Win32 API。
- **從內建服務移轉設定。** community、允許的管理主機、sysContact、sysLocation
  都會從既有的 SNMP Service 登錄檔沿用，換過來不必在每台機器重新填一次。
- **設計上就不該拖慢主機。** 這是量測出來的，不是講出來的，見
  [對主機的影響](#對主機的影響)。

## LibreNMS 上會看到什麼

以下全部在正式的 LibreNMS 26.8.1 上驗證過，且**不需要對 LibreNMS 打任何 patch**。

| LibreNMS 頁面 | 資料來源 | 狀態 |
|---|---|---|
| OS 偵測（Hardware / Version / Features）| 模仿 Microsoft 格式的 `sysDescr` / `sysObjectID` | ✅ |
| 連接埠 | IF-MIB `ifTable` + `ifXTable`（64-bit 計數器），保存 `ifIndex` | ✅ |
| Processor | `hrProcessorTable`，來源 `NtQuerySystemInformation` | ✅ |
| Memory | `hrStorage`，實體、虛擬、快取、分頁檔 | ✅ |
| Disk Usage | `hrStorage` 固定磁碟，含真實磁碟區標籤 | ✅ |
| Disk I/O | UCD-DISKIO，來源 `IOCTL_DISK_PERFORMANCE` | ✅ |
| 溫度 | ENTITY-SENSOR-MIB，磁碟溫度，走 SMART / NVMe | ✅ |
| Inventory | ENTITY-MIB `entPhysicalTable`，解析 SMBIOS 而得 | ✅ |
| 設備 | `hrDeviceTable`，處理器、網路卡、實體磁碟 | ✅ |
| IP 位址 | `ipAddrTable` + `ipAddressTable`（IPv4 + IPv6）| ✅ |
| Netstats | `ip` / `icmp` / `tcp` / `udp` / `snmp` 群組 | ✅ |
| Agent 自我健康 | JT 私有 OID（見下）| ✅ |
| ARP / 鄰居 | `ipNetToPhysicalTable` | ⚙️ 預設停用 |

### Agent 自我健康 OID

這個 agent 的失效是**無聲的**：服務顯示「執行中」，而圖表卻是平的。
因此我們用一組私有 OID 把 agent 自己的狀態暴露出來，讓 LibreNMS
可以監控 agent 本身，版本、服務執行時間、RSS、執行緒與 handle 數、
快照年齡與建立耗時、設定來源與各路徑，加上一張逐 collector 的健康表
（狀態、上次成功時間、耗時、累計錯誤數）。

實際價值：升級數百台之後，**一次 SNMP walk 就知道哪幾台沒升級成功**。

### 與內建 SNMP Service 的對照

我們在一台仍使用 Windows 內建 SNMP Service 的 Windows 10 主機上做了逐項量測對照，
**包含 jt-snmpd 刻意回報「更少」的地方與原因**，見
[`docs/comparison-vs-builtin-snmp_zh-TW.md`](docs/comparison-vs-builtin-snmp_zh-TW.md)。

摘要（在受測機器上實際 walk）：內建服務暴露 7,582 個 OID、jt-snmpd 為 767，
但這個差距中有 3,999 個屬於預設關閉的資訊揭露（已安裝軟體、執行中程序、連線表、ARP），
而 jt-snmpd 另外提供了內建服務完全沒有的 inventory、Disk I/O、感測器、
磁碟 SMART 與自我健康。這些數字是在受測機器上量的，會隨機器變動：裝了多少軟體、跑著多少處理程序、開著多少連線，都直接反映在內建服務的總數上。

以下每張圖的上下兩半都來自**同一台機器**：Dell Latitude E5270
（Core i5-6300U、16 GB、Samsung PM871b），Windows 10 22H2，
由同一套 LibreNMS 監控，LibreNMS 端未做任何客製。
做法是先裝上 Windows 內建 SNMP 功能、由它服務 UDP 161，
讓 LibreNMS 重新探索後截圖，再把 161 交還給 jt-snmpd，重新探索後截同樣的頁面。
硬體、作業系統、監控端完全相同，唯一的變數是誰在回答。

#### 感測器

內建服務完全不回報感測器，所以 LibreNMS 根本不會建立「溫度」頁籤。
jt-snmpd 提供磁碟溫度與 ACPI 溫度區，兩者都帶著韌體自己宣告的門檻值。
ACPI 溫度區是主機板韌體自己定義的溫度量測點，通常對應 CPU 周邊或機殼區域；
讀它不需要核心驅動，這也是本專案能提供系統溫度而不必裝驅動的原因。

![感測器對照](docs/images/temperature-zh-TW.png)

#### 磁碟 SMART

SMART **完全透過 SNMP** 送達 LibreNMS，走 `NET-SNMP-EXTEND-MIB`。
在其他平台上，這個應用程式是靠主機上一支輔助腳本呼叫 smartmontools 餵資料的；
jt-snmpd 是自己以 `IOCTL_STORAGE_QUERY_PROPERTY` 讀 SMART 屬性，
所以被監控端裝的就只有 jt-snmpd 一個。
沒量到的屬性保持 `null`，不會以 0 回報。

> **SMART 需要在 LibreNMS 開一個設定。** 找到它的探索模組預設是關的，
> 沒開啟時 jt-snmpd 照樣供應資料，但不會有人來取。網頁介面的路徑（用語與
> LibreNMS 繁體中文介面一致）：**齒輪圖示 → 全域設定 → 分頁「探索」→
> 展開「探索模組」→ 打開「應用程式」**，然後回到該裝置按「重新探索裝置」。
> 完成後裝置的「應用程式」分頁就會出現 SMART。命令列的等效指令是
> `lnms config:set discovery_modules.applications true`。
>
> **全域打開之後，有自己設定的裝置不會跟著生效。** LibreNMS 的判定順序是
> 命令列、裝置層、OS 層、全域，先設到的先贏。裝置頁齒輪選單裡的 Modules
> 開關只要動過就會留下一筆裝置層設定，關掉留下的那個「否」會一直壓過全域的
> 「是」。整批 Windows 主機要一次打開，
> `lnms config:set os.windows.discovery_modules.applications true` 比全域更準。

![SMART 對照](docs/images/smart-zh-TW.png)

#### 連接埠

內建服務把每一個 NDIS 篩選器驅動程式都當成獨立介面輸出，這台就有九個，
全是自動命名、看不出用途的項目。jt-snmpd 只輸出實體網路介面，
並以持久的 `NET_LUID` 配發 `ifIndex`，更新驅動不會讓歷史資料失去對應。

![連接埠對照](docs/images/ports-zh-TW.png)

#### 記憶體

內建服務提供實體與虛擬記憶體。jt-snmpd 另外提供快取記憶體與 Swap，
那是 LibreNMS 記憶體頁其餘欄位的來源。

![記憶體對照](docs/images/memory-zh-TW.png)

## 架構

整份 MIB 是一個依 OID 字典序排好的 `(OID, 值)` 陣列。
`GET` 是一次 `bisect_left`、`GETNEXT` 是一次 `bisect_right`、
`GETBULK` 是一段連續切片。

這不是微幅調校，而是**讓協定正確性變成結構性質**。字典序、無重複 OID、
無 GETNEXT 迴圈、正確的 `endOfMibView`，全都直接來自「陣列是排序的」這件事，
所以不會因為某個 collector 改動而悄悄退步。測試套件負責驗證這個聲稱，
而不是相信它。

```
Collectors（以 ctypes 呼叫 Win32 API）
        │
        ▼
Snapshot builder ──► 排序陣列 + 預先編碼好的 BER 位元組
        │
        ▼ 一次換上（只改參考，走訪中的 walk 仍讀完整的舊那份）
自訂 MibInstrumController        bisect
        │
        ▼
pysnmp（只負責 message / USM / VACM / transport）
        ▲
        │
前置解析閘門 ── 來源 ACL → 大小上限 → 速率限制 → TLV 合法性
        ▲
        │
   UDP/161
```

其中兩點值得特別說明：

- **回應的位元組在建立快照時就編碼好了。** 組一個回應只剩切片與串接，
  這讓回應編碼從 164 µs 降到每個 varbind 0.35 µs。
- **在閘門之前，沒有任何位元組會到達 BER decoder。** agent 以 LocalSystem
  執行，任何解析器漏洞都等同 SYSTEM 層級的漏洞。來源 ACL、封包大小上限、
  每來源 token bucket、外層 TLV 檢查，全部在 pysnmp 看到第一個位元組之前執行。

## 對主機的影響

要求是「poll 的時候不能讓機器變慢」。這是量測出來的，不是假設的。
在 Windows 11 主機上以**約 7,000 倍的真實輪詢速率**施壓
（60 秒內 1,406 次完整 walk）：

| 指標 | Normal 優先權 | **BelowNormal（實際採用）** |
|---|---|---|
| 固定工作負載退化 | 4.19% ❌ | **0.41% ✅** |
| agent CPU | 單核 23.4% / 整機 3.9% | — |
| 1,406 次 walk 後 RSS 成長 | +0.12 MB | 無洩漏 |
| 執行緒 / handle | 平坦 | 平坦 |

單次完整 walk 的成本是 12.5 ms CPU。LibreNMS 每五分鐘 poll 一次，
換算到實際使用約為 **0.004% CPU**。

## 資訊安全

| 面向 | 做法 |
|---|---|
| 威脅模型 | 主要對手是**已經在內網的攻擊者**，不是外部掃描。agent 以 LocalSystem 執行，任何 RCE 直接等同 SYSTEM 被攻陷 |
| 前置認證 | 來源 IP 白名單、4096 位元組封包上限、每來源 token bucket、外層 TLV 合法性，全部在 BER decoder 之前 |
| 存取控制 | 預設 deny。安裝時必須提供管理網段，`Any/Any` 會被拒絕 |
| 防火牆 | 入向 UDP/161 限制在管理網段，由安裝程式建立，解除安裝時移除 |
| 特權 | 以 `sc privs` 只保留 `SeChangeNotify` / `SeSystemProfile` / `SeIncreaseQuota` |
| 檔案系統 | 程式在 `%ProgramFiles%`，狀態在 `%ProgramData%` 且 ACL 重設為 SYSTEM + Administrators（`ProgramData` 的預設 ACL 允許任何使用者建立子目錄）|
| 打包 | 只用 PyInstaller **one-folder**。one-file 會先解壓到 `%TEMP%` 再執行，那是已知的 DLL 劫持路徑 |
| 回應大小 | 上限 1400 位元組，回應不分片 |
| 資訊揭露 | 已安裝軟體、執行中程序、ARP 表、監聽埠一律預設停用 |
| 掃描 | Bandit / Semgrep / Ruff-S / pip-audit / CycloneDX SBOM，加上協定層 fuzzing 與 Windows 專屬檢查，見 [`docs/security-scanning_zh-TW.md`](docs/security-scanning_zh-TW.md) |

## 技術組成

| 層 | 選擇 |
|---|---|
| 語言 | Python 3.12 |
| SNMP | pysnmp 7.1，只用它的 message / USM / VACM / transport 層，MIB 層由我們取代 |
| 資料來源 | 以 ctypes 呼叫 Win32 API，iphlpapi、psapi、ntdll、kernel32 IOCTL、SMBIOS、登錄檔 |
| 執行時相依 | `pysnmp` → `pyasn1`。就這樣 |
| 打包 | PyInstaller one-folder → 完整一包的 `jt-snmpd.exe`，目標機不需安裝 Python |
| 服務 | pywin32 服務框架，LocalSystem，自動啟動 |
| 部署 | MSI（WiX v5）：點兩下有設定對話框、`/qn` 無訊息安裝、GPO / Intune / SCCM 派送 |

## 安裝

從 [Releases](https://github.com/jasoncheng7115/jt-snmpd/releases/latest) 下載
`jt-snmpd-<版本>-x64.msi`，並以同一個版本附的 `.sha256` 核對雜湊。

> 本安裝檔目前未經 Authenticode 簽章，點兩下安裝時 SmartScreen 會出現警告，
> UAC 提示的發行者會顯示為「不明」。日後規劃透過開源專案的憑證方案申請簽章。
> 手動信任的做法與 WDAC / AppLocker 環境的處理，見
> [程式碼簽章](https://jasoncheng7115.github.io/jt-snmpd/code-signing_zh-TW.html)。

**管理網段是必填的**，agent 不接受監聽 `Any/Any`。

### 方式一：點兩下安裝

安裝程式會逐步詢問安裝路徑與監控設定。兩個必填的設定是**管理網段**與
**community**，它們決定誰查得到這台主機。沒有填管理網段就無法繼續，
因為空清單等於只回應 loopback：裝好了，但沒有在監控。

### 方式二：命令列與 GPO 派送

下面這道指令是**命令列 / 無人值守**安裝用的，`/qn` 表示不顯示任何介面。
同一個 MSI 與同一組屬性也直接適用於**群組原則（GPO）軟體派送**，
安裝過程以 SYSTEM 身分執行，不會有任何提示。

```powershell
msiexec /i jt-snmpd-1.0.0-x64.msi /qn MANAGEMENTNETWORKS=192.168.1.0/24 COMMUNITY=你的community
```

以 GPO 派送時，把 MSI 放在網域內的共用資料夾，並確保電腦帳戶對該資料夾有讀取權限。
從內部共用資料夾安裝也不會帶網頁標記，因此不會遇到 SmartScreen。

**不需要事先安裝 Windows 內建的 SNMP Service。** 把 community 填進安裝程式
（或在精靈的設定頁輸入）就能用，機器上沒有內建服務也一樣。
下面第 2 與第 5 步只有在**已經有**內建服務時才會發生。

安裝程式會：

1. 檢查 OS 版本、架構、磁碟空間，以及 UDP/161 目前由誰佔用
2. **如果**機器上有 Microsoft SNMP Service，讀取它的設定，沿用 community、
   允許的管理主機、sysContact 與 sysLocation。沒有的話就用你提供的 community；
   兩者皆無時安裝程式會中止並說明，不會自己編一個
3. 停止任何舊版本，並**等待其檔案控制代碼真的釋放**
4. 安裝到 `%ProgramFiles%\jt-snmpd\`，建立 `%ProgramData%\jt-snmpd\`
   並收緊 ACL
5. **如果**有內建 SNMP Service，把它停用，是**停用，不是移除**，並記錄足以還原的資訊
6. 註冊服務，設定失效自動復原與特權縮減
7. 建立僅限管理網段的防火牆規則
8. 啟動服務並**確認它真的回應 loopback SNMP 查詢**，
   服務處於「執行中」不等於服務會回應
9. 輸出移轉報告，以及管理員可能需要的每一個路徑

若 UDP/161 被「非 Microsoft SNMP Service」的程式佔用，安裝程式會**中止並且
不動它**，而不是去停用第三方 agent。

### 解除安裝

從「應用程式與功能」移除，或以命令列：

```powershell
# 解除安裝（還原內建 SNMP Service，保留設定與狀態）
msiexec /x jt-snmpd-1.0.0-x64.msi /qn

# 解除安裝並清除全部資料
msiexec /x jt-snmpd-1.0.0-x64.msi /qn PURGE=1
```

萬一安裝或解除安裝走不完，[手動移除](https://jasoncheng7115.github.io/jt-snmpd/manual-removal_zh-TW.html)
逐步列出安裝程式做過的每一件事，以及如何用手做完。

預設保留設定與狀態是刻意的：管理員常以「移除再重裝」來排除問題，
若把介面索引映射一起清掉，LibreNMS 會重新 discovery 每一個 port，
舊的歷史 RRD 全部失去對應。

## 路徑

| 用途 | 位置 |
|---|---|
| 程式本體 | `%ProgramFiles%\jt-snmpd\` |
| 設定檔 | `%ProgramData%\jt-snmpd\config.json` |
| 群組原則（優先於設定檔）| `HKLM\SOFTWARE\Policies\JasonTools\JTSNMPD` |
| 介面索引映射 | `%ProgramData%\jt-snmpd\state\index-map.json` |
| 還原資訊 | `%ProgramData%\jt-snmpd\state\ms-snmp-restore.json` |
| 記錄檔 | `%ProgramData%\jt-snmpd\logs\` |
| 服務名稱 | `jt-snmpd` |

同一組路徑也透過 SNMP 回報（`jtAgentConfigPath`、`jtAgentLogPath`、
`jtAgentInstallPath`），所以「設定檔在哪」可以直接從 LibreNMS 查到，
不必登入那台主機。

## 專案結構

```
jt-snmpd/
├── deploy/          # agent 原始碼：jt_agent.py、preauth.py、smbios.py、diskhealth.py
├── packaging/       # build-exe.ps1、install.ps1、make-release.ps1
├── build/           # PyInstaller one-folder 產物（不進 git）
├── dist/            # 發佈成品（不進 git）
├── tests/           # 跨平台測試，以靜態解析為基礎，可在 Linux CI 上跑
├── bench/gate_c/    # 架構原型與效能量測
└── docs/            # 查證結果、命名決策、資安掃描、fixtures
```

## 目前狀態

| 項目 | 狀態 |
|---|---|
| SNMPv2c、IF-MIB、HOST-RESOURCES、UCD-DISKIO、ENTITY-MIB、ENTITY-SENSOR、IP / TCP / UDP / ICMP、自我健康 OID | ✅ 已在正式 LibreNMS 驗證 |
| Windows 服務、開機自啟、從內建服務移轉 | ✅ 已在 Windows 10 與 11 驗證 |
| 磁碟溫度與 SMART 健康度 | ✅ 已在實體硬體驗證 |
| **MSI 安裝程式**（點兩下的圖形介面、`/qn` 無訊息安裝、GPO / Intune / SCCM 派送）| ✅ 已發版；安裝、升級、解除安裝、重裝、清除移除共 40 項生命週期檢查在實機全綠 |
| SNMPv3（SHA-256 + AES-128）| 🚧 開發中，目前只有 v2c |
| OID 檢視範圍預設集（VACM）| ⛔ 未實作 |
| Authenticode 簽章 | ⏳ 日後規劃申請開源專案憑證，見[程式碼簽章](https://jasoncheng7115.github.io/jt-snmpd/code-signing_zh-TW.html) |
| **Windows Server** | ✅ **2016（網域控制站）與 2022 已實機驗證**，含安裝生命週期、內建 SNMP 移轉、LibreNMS 端對端；見[部署到 Windows Server](https://jasoncheng7115.github.io/jt-snmpd/windows-server-notes_zh-TW.html) |
| Server 2019 / 2025、唯讀網域控制站 | ⛔ 尚未驗證，無環境 |
| 圖形升級的「使用中的檔案」對話框 | ⚠️ **已知缺陷**。無訊息安裝與 GPO 派送不受影響;兩種修法都實測後撤回，原因見 `TEST_PLAN.md` 6.1c.12 |
| 多網路卡來源位址選擇 | ⛔ 尚未驗證 |

v1.0 不列入計畫：SNMP trap 與 inform、SNMP SET、ARM64、純 IPv6 部署、
叢集感知。

**VACM** 是 SNMP 標準裡的檢視型存取控制（RFC 3415），用來限制某一組憑證
「看得到 OID 樹的哪幾段」。目前 jt-snmpd 的做法是整棵樹唯讀，
資訊揭露則以「哪些子樹預設不輸出」來控制。VACM 預設集要做的是更細的一層：
例如一組 `librenms-minimal` 檢視，只開放 LibreNMS 實際會取用的子樹，
其餘一律取不到。這在多個監控系統共用同一台主機時才有意義，因此排在 SNMPv3 之後。

## 授權

GPL-3.0-or-later。商業支援請洽 Jason Tools。

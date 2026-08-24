# 變更記錄

本專案的所有重大變更都記錄在此檔案。

格式依循 [Keep a Changelog](https://keepachangelog.com/zh-TW/1.1.0/)，
版本號依循 [語意化版本](https://semver.org/lang/zh-TW/)。

English version: [CHANGELOG.md](CHANGELOG.md)

---

## [0.9.1] - 2026-08-24

### 修正

- **agent 從來沒有讀取過自己的設定檔。** 安裝程式收集了 community 與管理網段、
  驗證過、寫進 `config.json`。而 agent 宣告的 `CFG_PATH` 指向 `config.yaml`
  ——不同的檔案——而且兩個它都沒有打開過。每一次安裝跑的都是原始碼裡的預設值。

  那組預設值是 `community="mon2"` 與 `allowed_networks=("192.168.1.0/24",)`，
  正好就是開發實驗室用的值，這也是為什麼數個月的測試都沒發現。換成別的值安裝，
  loopback 健康檢查會用操作者的 community 查詢、agent 卻在另一個上面回應，
  檢查逾時，MSI 以 1603 回滾整筆交易。失敗是徹底的，卻仍然看不見——
  因為唯一能成功的那組設定，正是唯一被試過的那組。

  agent 現在在啟動時載入 `config.json`，而且是在進入點讀取任何設定**之前**：
  晚一步就等於同一個 bug，因為那些值是以參數傳入的，早就綁定完成了。
  兩個預設值都改為空：沒有 community 就拒絕服務而不是自己編一個，
  讀檔用 `utf-8-sig`，因為 PowerShell 與記事本都會寫 BOM。

  設定現在可以照文件一直以來暗示的方式修改：編輯
  `C:\ProgramData\JT-SNMP\config.json`，重新啟動服務。

- **未設定來源 ACL 時等於放行所有來源。** 前置閘門把空的網段清單當成「不過濾」。
  在安裝程式是設定檔唯一作者的時候，那個狀態到不了；但手動編輯設定檔現在是
  受支援的流程，一個被清空的清單會無聲地把 agent 暴露給整個網路。
  現在改為除 loopback 外一律拒絕——監控會明顯停掉，而不是安靜地過度分享。
  要刻意服務所有來源，請明確寫出 `0.0.0.0/0`。

### 新增

- **專案圖示**——一棵 OID 樹，因為物件識別碼的階層正是 SNMP 的本質。
  以單一線寬繪製，讓它在瀏覽器分頁與 Windows 服務清單的 16 px 下仍然可辨。
  它取代了原本的空白佔位圖示——那讓「加入或移除程式」裡的項目看起來像
  安裝到一半的東西。

- **持續整合**——測試在 Linux 與 Windows 上執行；打上標籤即建置 MSI 並發佈。
  失敗會以工作流程註記呈現，因為 GitHub 的執行日誌需要認證才讀得到，
  而「exit code 1」不構成診斷。Linux 端會安裝 net-snmp，並在之後確認
  協定正確性測試真的跑過，不讓它們悄悄跳過。

## [0.9.0] - 2026-08-24

### 新增

- **磁碟健康狀態顯示於 LibreNMS**——SMART 應用程式現在會在每顆磁碟旁顯示
  `PhysicalDrive0 (OK)` / `(FAIL)` / `(Overheating)`。判定來自 ATA
  `SMART RETURN STATUS`（0xDA），也就是 `smartctl -H` 顯示的那一行，
  或 NVMe 的 critical warning 位元圖，**絕不從屬性推導**：重新配置磁區為 0
  不代表健康，韌體可能因為別的屬性跌破門檻而已在預測故障；反過來說少量
  重新配置磁區在某些型號上完全正常。磁碟完全不回答時（USB 橋接器通常不轉送
  SMART 命令）就不輸出該鍵，LibreNMS 顯示空白，而不是一個捏造的 `(OK)`。

- **`jtDiskHealthTable`** 置於私有 OID 子樹——每顆磁碟一個狀態值
  （ok／warning／critical／unknown），供需要直接告警的使用者取用。
  要注意的是，LibreNMS **裝置概觀頁**上的綠／紅燈必須在 LibreNMS 伺服器端
  新增探索定義才做得到，而本專案刻意不要求修改伺服器。

- **公開前的個資檢查工具**——`tools/check-privacy.py` 掃描的正是 git 會推上去
  的那些檔案，檢查金鑰、密碼、community 字串、MAC 位址、位址與序號；
  `docs/release-checklist.md` 記錄完整流程。圖片改用「人工審閱 + 雜湊」而非
  比對規則，因為正規表示式讀不到像素：第一批 README 截圖帶出了四組 MAC 位址
  與六個鄰居裝置名稱，那等於一張內網拓撲圖。

- **磁碟 SMART 透過 SNMP 提供（`NET-SNMP-EXTEND-MIB`）**——LibreNMS 讀 SMART 走它的
  `smart` 應用程式，而那個應用程式**完全透過 SNMP** 取得
  （`snmp_get nsExtendOutputFull."smart"`）。被監控端不需要安裝 LibreNMS agent、
  不需要 smartctl、不需要任何腳本。jt-snmpd 本來就以 IOCTL 直接讀到 SMART 屬性，
  現在把它序列化成該應用程式期望的 JSON。已在 Dell Latitude E5270 實機驗證：
  重新配置磁區 0、磨損平衡 4、UDMA CRC 錯誤 0、溫度 33°C、通電 491 小時，
  確實寫入 `app-smart-*.rrd`。

  內容是 `base64(gzip(json))`——這是 `json_app_get()` 明確支援的形式，而且是必要的：
  回應上限 1400 位元組且不分片，未壓縮的 JSON 在兩顆磁碟時就會超出。
  沒量到的屬性一律 `null`，絕不填 `0`；在「重新配置磁區」欄位填一個假的 0，
  讀起來的意思是「這顆磁碟很健康」。

  **這需要在 LibreNMS 啟用 `discovery_modules.applications`**——它預設是 `false`。
  沒啟用的話，extend 資料照樣供應，但不會有人來取。

- **磁碟最高溫度**（`max_temp`）——LibreNMS 的 SMART 應用程式無論有沒有資料都會
  渲染一張「Max Temp(C)」面板，因此少了這個鍵，每一套安裝都會看到一張破圖。
  Windows 的儲存 API 給的是門檻值（warning、critical），不是「這輩子最高溫」，
  拿門檻值去填那條線是標錯標籤；因此 jt-snmpd 改記錄**自己實際觀測到**的最高溫，
  跨重新啟動持久化，且只在最高溫真的上升時才寫檔——快照每五秒重建一次，
  每次都寫會是一天一萬七千次不必要的磁碟寫入。

- **對照截圖**置於 `docs/images/`，取自正式 LibreNMS，英文與台灣繁體中文各一套，
  皆為淺色主題：感測器、SMART、連接埠、記憶體，每組都以同一個頁面對照
  「使用內建 SNMP Service 的 Windows 10 主機」與「使用 jt-snmpd 的主機」。

- **ACPI 熱區溫度**——不需核心驅動的系統／主機板溫度，以
  `advapi32!WmiOpenBlock` + `WmiQueryAllDataW`（WMI 資料區塊 API，不是 WMI COM，
  也不開子行程）讀取。實體機實測 25°C，臨界跳脫點 107°C；虛擬機回
  `ERROR_WMI_GUID_NOT_FOUND`，該感測器直接不出現。

  CPU 封裝溫度仍然做不到，而且會一直做不到：它需要存取 MSR，而那需要核心驅動。
  業界慣用的那個驅動（WinRing0）已列入 Microsoft 的易受攻擊驅動封鎖清單，
  在 HVCI/WDAC 下載不進去——那正是我們客戶的環境設定。

- **CPU 頻率感測器**（`entPhySensorType = hertz`，`mega` 刻度）——只輸出一筆而非
  每個邏輯處理器一筆，因為 `CallNtPowerInformation` 回報的是封裝層級的 P-state，
  各核心數值相同。要注意 LibreNMS 目前會丟棄這類感測器：
  `entity-sensor.inc.php` 把 `hertz` 對應到類別 `freq`，但
  `LibreNMS/Enum/Sensor.php` 中合法的類別是 `frequency`。
  同一個缺陷也影響 `cisco-entity-sensor.inc.php` 與 `openbsd.inc.php`。
  我們的 OID 依 RFC 3433 是正確的，`snmpwalk` 查得到；等 LibreNMS 修正對照表後
  圖表就會出現。

- **電池狀態**放在私有 OID 子樹（電量百分比、市電狀態、預估可用時間），
  來源 `GetSystemPowerStatus`。刻意只放私有：LibreNMS 的 entity-sensor 對照表
  沒有 charge 或 percent，送成標準感測器不會有任何結果。

- **`SNMP-FRAMEWORK-MIB` engine 群組**（`snmpEngineID`、`snmpEngineBoots`、
  `snmpEngineTime`、`snmpEngineMaxMessageSize`）——這修掉了一個原本會在每台主機
  開機滿 497 天後發出的假「Device rebooted」告警。`sysUpTime` 的型別是
  `TimeTicks`，在 2^32 個百分之一秒 ≈ 497.1 天必然回捲；這是 RFC 3418 規定的，
  Windows 內建 SNMP Service 一樣會回捲。能修的是後果：LibreNMS 取
  `max(sysUpTime/100, snmpEngineTime, hrSystemUptime/100)`，而 `windows.yaml`
  只停用了 `hrSystemUptime`。`snmpEngineTime` 以秒計、上限 2147483647
  （約 68 年），回捲發生後最大值仍持續上升，重開機判斷因此不會成立。

- **記錄檔輪替與 Windows 事件檢視器整合。** agent 的記錄檔原本沒有大小上限；
  快照重建持續失敗時每五秒一行，一天一萬七千行。數百台跑上數年，
  監控代理程式把它所監控主機的系統碟寫滿，是最不能接受的失效方式。
  錯誤現在同時進事件檢視器——現場人員第一個看的是那裡，
  而遠端診斷數百台時 `Get-WinEvent` 可以集中撈。

- **完整生命週期測試**（`tests/lifecycle.ps1`）——安裝、升級、移除、重裝、
  PURGE 移除，共 40 項斷言，在實機上以打包好的 MSI 執行。

### 修正

- **agent 執行緒死亡後，服務仍回報 `Running`。** `SvcDoRun` 只等停止事件，
  因此啟動階段的任何失敗——綁定失敗、MIB 載入失敗、快照建置失敗——都會讓
  服務控制管理員回報一個健康的服務，而實際上沒有任何監聽器。
  服務控制管理員說 `Running`、監控系統說逾時，是現場最難查的狀態；
  而且這也代表已設定的三段式自動復原永遠不會觸發，因為程序根本沒有結束。

- **升級之後再移除，內建 SNMP Service 會永遠停在停用狀態。** 設定腳本每次執行
  都重讀內建服務的當下狀態並覆寫還原記錄。第一次安裝時讀到的是真實原狀；
  升級時該服務早已被上一次安裝停用，於是 `Disabled` 被當成原始設定寫回，
  解除安裝端的 `$orig -ne 'Disabled'` 判斷從此不會成立。
  安裝 → 移除可以正確還原，安裝 → 升級 → 移除不行——而升級正是這個產品的常態操作。

- **`PURGE=1` 沒有清掉資料目錄。** 自訂動作的記錄檔就放在它要刪除的目錄裡，
  刪完之後的兩行收尾訊息又把 `logs\` 重建了回來。刪除失敗也被
  `-ErrorAction SilentlyContinue` 吞掉並回報成功。

- **內建 SNMP 的停用是「假設」而非「確認」。** 群組原則或第三方管控可能擋下它；
  現在安裝程序會確認該服務確實已停止且已停用，不符就帶著說明失敗，
  而不是繼續走到一個看不出原因的健康檢查逾時。

- **`CallNtPowerInformation` 的緩衝區大小。** 原型用 `os.cpu_count()`，
  而那只反映呼叫端所屬的處理器群組；在超過 64 個邏輯處理器的機器上，
  核心會寫超出我們配置的範圍。現在改以
  `GetActiveProcessorCount(ALL_PROCESSOR_GROUPS)` 決定大小。
  ctypes 正是 Python 記憶體安全性不再適用之處。

### 變更

- 磁碟感測器標籤保留 Windows 原生名稱（`PhysicalDrive0 Temp` 而非 `Drive0 Temp`）。
- SMART 屬性依讀到的 ID 完整保留，不再只留有名稱對照的那些。
  LibreNMS 需要 10、183、184、188、196、199，這幾個原本都沒有名稱。
- 所有來自韌體的緩衝區改以防禦性方式解析，並把解析與採集拆成純函式，
  以便在 Linux 上用敵意輸入測試。WMI 資料區塊裡的長度與偏移量都取自區塊自身；
  一個不合理的執行個體數，就是對一台我們承諾不會拖慢的主機發動自我阻斷服務。


### 新增

- **UCD-SNMP-MIB `systemStats`**——這是 LibreNMS 的 System 圖表群組真正的來源。
  Windows 主機先前只有三張圖（Processes、Users、Uptime），因為那些來自
  HOST-RESOURCES；Linux 裝置上其餘的 Detailed Processor Usage、Context Switches、
  Interrupts、I/O、Swap I/O 全部來自 UCD-SNMP-MIB。現以
  `NtQuerySystemInformation`（`SystemPerformanceInformation` 與逐 CPU 時間）供應，
  新增五張圖表

- **`hrFSTable`、`hrPartitionTable` 與 `ipRouteTable`**——在製作與內建 SNMP Service
  的對照表時發現這三張表我們真的沒有。檔案系統與分割來自 `GetVolumeInformationW`，
  路由來自 `GetIpForwardTable2`。它們都沒有「軟體清單／連線表」那類資訊揭露顧慮，
  故預設輸出
- **對照文件**（`docs/comparison-vs-builtin-snmp.md`）逐表量測 jt-snmpd 與
  仍使用內建 SNMP Service 的 Windows 10 主機，並為每一處「我們回報得更少」
  給出理由

- **MSI 安裝檔（WiX v5）**——這是群組原則派送的前提，GPO 軟體安裝只接受 MSI。
  已在 Windows 11 端對端驗證：無訊息安裝（`msiexec /qn`）、
  **直接安裝新版即完成升級**（0.1.0 → 0.1.1，「加入或移除程式」維持一筆，
  `index-map.json` 位元組完全相同，LibreNMS 不會重新 discovery）、
  解除安裝會還原內建 SNMP Service 並保留設定與狀態、以及重複安裝。
  loopback 健康檢查失敗時整個交易會倒回

- **README** 英文與台灣繁體中文雙檔，格式參照 jt-ipam
- **資安檢測工具鏈**寫入 `docs/security-scanning.md`，並產出第一份基線：
  Bandit HIGH=0、pip-audit 掃過 59 個相依無弱點、CycloneDX SBOM 已產出。
  ZAP 不適用——它是 web DAST，而本 agent 沒有 HTTP 介面；正確組合是
  SAST + SCA/SBOM + 協定層 fuzzing，加上 Windows 專屬檢查（Authenticode、
  unquoted service path、`sc qprivs`、`accesschk`、PrivescCheck）
- **三分支 `sysObjectID`**，以 `DsRoleGetPrimaryDomainInformation` 判定網域控制站。
  LibreNMS 靠第三分支呼叫 `getDatacenterVersion()`，先前 DC 會顯示錯誤的 Windows 版本
- **Windows Server 情境**整理進 `TEST_PLAN.md` §5.5——22 項，涵蓋版本與安裝型態、
  Server 特有資料來源、部署差異

- **IP 位址表**：`ipAddrTable`（RFC 1213）與 `ipAddressTable`（IP-MIB，IPv4 + IPv6），
  以 `GetUnicastIpAddressTable` 取得，供 LibreNMS 的 ipv4-addresses /
  ipv6-addresses 模組使用
- **鄰居快取**（`ipNetToPhysicalTable`，ARP 與 IPv6 ND），以 `GetIpNetTable2` 取得。
  **預設停用**——spec §3.5 指出內網 ARP 表等同現成的橫向移動目標清單
- **磁碟溫度與健康度**（ENTITY-SENSOR-MIB `entPhySensorTable`），以
  `IOCTL_STORAGE_QUERY_PROPERTY` 搭配 `StorageDeviceTemperatureProperty`
  與 NVMe SMART health log 取得。依 spec §2.9 刻意不使用 LibreHardwareMonitor——
  其 WinRing0 驅動已列入 Microsoft vulnerable driver blocklist，
  在 HVCI 端點會觸發 Defender

- **以 `GetPerformanceInfo` 補齊記憶體資訊**：除了 Physical 與 Virtual Memory，
  新增 **Cached Memory**、**Swap Space**（commit limit 中屬於分頁檔的部分，
  與 commit charge 是不同概念，見 spec §2.2）以及核心分頁／非分頁集區。
  LibreNMS 上的記憶體池由 2 個增為 4 個
- **`hrStorageDescr` 讀取真實磁碟區標籤與序號**（`GetVolumeInformationW`），
  取代原本硬編碼的預留字串。非 ASCII 標籤（例如中文磁碟區名稱）以 UTF-8 編碼，
  並已透過 LibreNMS 端對端驗證

- **`sysContact` / `sysLocation` 設定來源**：ADMX 原則優先，其次沿用
  Windows 內建 SNMP Service 的既有登錄檔設定（spec §5.5、§5.9.3）。
  客戶原本就在用內建 SNMP 時，換過來不必重新填寫——即使內建服務已停用，
  其登錄檔仍在，設定仍會自動沿用。`jtAgentConfigSource` 會回報實際生效的來源
- **`build/` 與 `dist/` 資料夾**：`build/` 放 PyInstaller one-folder 的執行檔產物，
  `dist/` 放對外交付的安裝檔（MSI 等）。兩者都只有 README 進版本控制

- **完整 `hrSystem`**：補上 `hrSystemProcesses`（LibreNMS System → Processes 圖的來源）、
  `hrSystemDate`（RFC 2579 DateAndTime 二進位格式，含時區）、
  `hrSystemInitialLoadDevice` / `hrSystemInitialLoadParameters`
- **網路協定統計**（LibreNMS Netstats 整組圖表）：`ip`、`icmp`、`tcp`、`udp` 四個群組，
  全部走 iphlpapi 的 `GetIpStatisticsEx` / `GetIcmpStatistics` /
  `GetTcpStatisticsEx` / `GetUdpStatisticsEx`，一次呼叫取得整組計數器
- **SNMPv2-MIB `snmp` 群組**：agent 自身的封包統計，同時作為前置解析閘門
  丟棄量的對外出口

- **完整 inventory**：
  - **ENTITY-MIB `entPhysicalTable`**（LibreNMS Inventory 頁）——資料來自
    `GetSystemFirmwareTable('RSMB')` 解析 SMBIOS，不需 WMI、不需特權（spec §2.10）。
    涵蓋 Type 0 BIOS、Type 1 System、Type 2 Baseboard、Type 4 Processor、
    Type 17 Memory Device，以 §34.5 的分段 index 配置
    （1000 system / 1100 mainboard / 2000+ CPU / 3000+ DIMM / 4000+ 磁碟）
  - **`hrDeviceTable` 全家族**（LibreNMS 設備頁）：處理器、網路介面、實體磁碟，
    搭配 `hrProcessorTable`、`hrNetworkTable`、`hrDiskStorageTable`。
    所有衍生表共用同一組 `hrDeviceIndex`（spec §2.3）
  - **實體磁碟 inventory**：以 `IOCTL_STORAGE_QUERY_PROPERTY` 取型號、序號、
    匯流排類型，`IOCTL_DISK_GET_DRIVE_GEOMETRY_EX` 取容量
  - 硬體 inventory 永久快取（spec §2.7）——SMBIOS 開機後不會變

- **前置解析閘門**（spec §3.2，標為最高優先的資安項目）：位於 pysnmp 之前的
  四道檢查——來源 IP 白名單、封包大小上限、每來源 token bucket 速率限制、
  外層 TLV 粗略合法性。被擋下的封包**完全不會進入 BER decoder**，
  因此深度巢狀、超長長度欄位、OID 放大等攻擊碰不到 pyasn1

- **自我健康 OID**（spec §7，從 Phase 7 提前）：agent 的失效是無聲的，
  這組 OID 讓 LibreNMS 能監控 agent 本身。含版本、服務執行時間、RSS、
  執行緒與 handle 數、快照年齡與建立耗時、設定路徑、安全性警告摘要
- **`jtAgentCollectorTable`**：每個 collector 的狀態、上次成功時間、耗時、
  累計錯誤數與最後錯誤訊息
- **collector 健康追蹤**：所有 collector 經 `_collector()` 包裝，
  失敗時回傳 default 而非拋出，agent 不會因單一 collector 故障而垮掉

- **專案定名 `jt-snmpd`**，服務名稱、執行檔名、安裝路徑一併定案（`docs/naming-and-paths.md`）
- **snapshot + bisect 架構**：整份 MIB 為依 OID 排序的陣列，GET 用 `bisect_left`、
  GETNEXT 用 `bisect_right`，SNMP 協定正確性成為結構保證而非人工維護
- **wire 預編碼**：快照建立時預先產生 BER 位元組，回應組裝退化為位元組串接
- **IF-MIB**（ifTable + ifXTable，含 64-bit counters）、**HOST-RESOURCES**
  （hrStorage / hrProcessor / hrDevice）、**UCD-DISKIO**
- **介面過濾**：只輸出實體網路卡，排除 WFP 過濾驅動、VPN 虛擬卡、隧道、loopback
- **ifIndex 持久化**：以 NET_LUID 為主鍵，避免重開機後 LibreNMS 重建 port 與孤兒 RRD
- **Windows 服務**：PyInstaller one-folder 打包成 `jt-snmpd.exe`，
  以自身為服務主程式，開機自啟、LocalSystem、目標機零 Python 依賴
- **`--selftest` 建置閘門**：建置後實際初始化 SNMP engine 並建立快照，
  可攔截「exe 產出但缺資料檔」的情況
- **程序優先權降級**：服務以 `BELOW_NORMAL_PRIORITY_CLASS` 執行
- **建置腳本** `packaging/build-exe.ps1`：參數單一來源，含控制代碼釋放驗證與產物新鮮度檢查
- **測試**：BER 大小對照（540 例）、walk 正確性（20 例）、base OID 對照 RFC（10 例）

### 修正

- **UCD `systemStats` 的欄位編號是憑記憶寫的，而且寫錯了。**
  正確順序是 IOSent(57) / IOReceived(58) / Interrupts(59) / Contexts(60) /
  SwapIn(62) / SwapOut(63)，我卻把 SwapIn/SwapOut 排在前面，
  結果 context switches 被畫在 I/O 圖上。從 agent 端完全看不出異常——
  walk 成功、圖表有線、數字在動。唯一能發現的方法是用 MIB 解析輸出
  （`snmpwalk -m UCD-SNMP-MIB -O QUs`）。
  `tests/test_ucd_field_numbers.py` 已把每個欄位釘死在 MIB 名稱上

- **`ipRouteTable` 在多網路卡主機上產生重複 OID。** RFC 1213 以目的位址單獨當索引，
  但每張網路卡都會有自己的 224.0.0.0 多播與 255.255.255.255 廣播路由。
  在一台有七個位址的筆電上，這觸發了重複 OID 護欄而讓 agent 拒絕啟動，
  連帶使 MSI 的健康檢查失敗並倒回安裝。現已依目的位址去重，
  保留 metric 最小者（即實際會被選用的路由）。此問題在單網路卡機器上永遠不會出現

- **`hrSystemNumUsers` 原本固定回 1。** 在遠端桌面工作階段主機上這直接就是錯的
  ——一台可能有數十個使用者。改以 `WTSEnumerateSessions` 列舉實際工作階段，
  計入 Active 與 Disconnected（斷線的使用者仍在登入狀態、仍佔用資源）
- **NIC team 成員與 team 介面同時被輸出**，導致 LibreNMS 對同一份流量計算兩次。
  team 成員的 `ConnectionType` 為 `Passive`，現已排除

以下皆為實機部署過程中發現並修正的缺陷：

- **服務顯示 Running 但未綁定 socket**：pysnmp 的 `open_server_mode()` 必須在
  running event loop 內呼叫，否則 socket 從未真正綁定
- **64 位回傳值被截斷**：所有 Win32 呼叫未宣告 `argtypes`/`restype` 時，
  ctypes 預設以 `c_int` 處理，導致 C: 磁碟顯示 0 GB、uptime 超過 24.8 天溢位
- **pywin32 服務類別必須在模組層級**：定義在函式內會得到
  `AttributeError: module has no attribute`，且服務啟動失敗時無任何記錄
- **ifXTable OID 錯誤**：`1.3.6.1.31.1.1.1` 少了 `2.1`，整張表掛在無效分支。
  LibreNMS 的 `ifname: true` 依賴此表，錯誤時 Ports 頁缺名稱與 64-bit counters
- **非 ASCII OCTET STRING 編碼失敗**：pyasn1 預設以 latin-1 編碼字串，
  正體中文網路卡名（「乙太網路」）會直接拋出 `PyAsn1UnicodeEncodeError`
- **含空白路徑未加引號會被截斷**：預設安裝路徑 `%ProgramFiles%\JT SNMP Agent\`
  本身即含空白，未加引號時行程啟動失敗且無記錄
- **PowerShell 指令碼需 UTF-8 BOM**：Windows PowerShell 5.1 在無 BOM 時
  以系統 ANSI 代碼頁讀取 `.ps1`，中文註解會打斷語法剖析
- **建置產物新鮮度誤判**：僅以「exe 是否存在」判定建置成功，
  在建置失敗時會取到殘留的舊版本
- **已載入映像無法刪除**：Windows 對已載入為映像的 `.pyd`/`.dll` 回傳
  存取被拒，即使服務已停止並解除註冊。改以重新命名處理

### 效能

- MIB 層查詢：**8 µs/varbind**
- 回應組裝：**164 → 0.35 µs/varbind**（改用 wire 預編碼）
- 完整請求路徑：**18.3 µs/varbind**
- **主機影響**（7,000 倍於實際輪詢負載的壓力測試下）：
  固定工作負載退化自 **4.19% 降至 0.41%**（程序優先權降級後）
- 記憶體：1,406 次完整 walk 後 RSS 增加 0.12 MB，執行緒與 handle 數持平

### 已知限制

- 尚未驗證：多網路卡來源位址（測試環境為單網路卡）、HVCI/WDAC 端點、Authenticode 簽章
- 尚未實作：SNMPv3、前置解析閘門、VACM 預設集、自我健康 OID、MSI 安裝程式

---
layout: default
title: 與內建 SNMP Service 對照（繁體中文）
description: Compared with the built-in SNMP Service, table by table
---

[← 回到說明文件首頁](https://jasoncheng7115.github.io/jt-snmpd/) ·
[English](https://jasoncheng7115.github.io/jt-snmpd/comparison-vs-builtin-snmp.html) | **繁體中文**

# jt-snmpd 與 Windows 內建 SNMP Service 對照

| 量測項目 | 內容 |
|---|---|
| 量測日期 | 2026-08-24 |
| 監控端 | LibreNMS 26.8.1，未做任何 LibreNMS 端修改 |
| 對照組 | Windows 10 22H2，**Windows 內建 SNMP Service** |
| 實驗組 | Windows 10 22H2，**jt-snmpd** |

---

## LibreNMS 頁面對照

這是使用者實際看得到的差異。

| LibreNMS 資料表 | 內建 SNMP | **jt-snmpd** | 說明 |
|---|---:|---:|---|
| **entPhysical**（Inventory 頁）| **0** | **5** | 內建完全沒有 Inventory。我們從 SMBIOS 解析出機箱、主機板、CPU、DIMM、磁碟 |
| **ucd_diskio**（Disk I/O 頁）| **0** | **2** | 內建沒有 Disk I/O。我們以 `IOCTL_DISK_PERFORMANCE` 提供 |
| **sensors**（溫度頁）| **0** | **2** | 內建沒有任何感測器。我們提供磁碟溫度與 ACPI 溫度區，也就是主機板韌體自己定義的溫度量測點（實體機實測 33°C / 25°C）|
| **applications**（SMART 頁）| **0** | **1** | 內建沒有。我們以 NET-SNMP-EXTEND-MIB 提供 LibreNMS 的 `smart` 應用程式（需啟用 `discovery_modules.applications`）|
| **mempools**（Memory 頁）| 2 | **4** | 內建只有 Physical / Virtual。我們另加 Cached / Swap |
| **System 圖表** | 3 | **8** | 內建只有 Processes / Users / Uptime，因為那三張來自 HOST-RESOURCES。其餘五張在 Linux 上來自 UCD-SNMP-MIB，而 Windows 內建服務沒有實作那個 MIB |
| storage（Disk Usage 頁）| 2 | 2 | 筆數相同，描述不同：我們讀出真實磁碟區標籤與序號，含中文標籤 |
| processors | 8 | 6 | 各自的實際核心數，無差異 |
| ipv4_addresses | 2 | 2 | 相同 |
| **ports**（連接埠頁）| 9 | **1** | **刻意不同**，見下 |
| **hrDevice**（設備頁）| 68 | 9 | **刻意不同**，見下 |

## System 圖表：Windows 上原本只有三張

LibreNMS 的 System 圖表群組在 Linux 裝置上有八張圖，在 Windows 上只有三張。
原因不是 LibreNMS 對 Windows 支援不好，而是那五張圖的資料來源是
**UCD-SNMP-MIB 的 `systemStats`**，那是 net-snmp 的企業 MIB，
Windows 內建 SNMP Service 沒有實作。

| 圖表 | 資料來源 | 內建 SNMP | jt-snmpd |
|---|---|---|---|
| Processes | HOST-RESOURCES `hrSystemProcesses` | ✅ | ✅ |
| Users | HOST-RESOURCES `hrSystemNumUsers` | ✅ | ✅ |
| Uptime | `sysUpTime` | ✅ | ✅ |
| Detailed Processor Usage | UCD `ssCpuRawUser/Nice/System/Idle` | ❌ | ✅ |
| Context Switches | UCD `ssRawContexts` | ❌ | ✅ |
| Interrupts | UCD `ssRawInterrupts` | ❌ | ✅ |
| I/O | UCD `ssIORawSent` / `ssIORawReceived` | ❌ | ✅ |
| Swap I/O | UCD `ssRawSwapIn` / `ssRawSwapOut` | ❌ | ✅ |

資料以 `NtQuerySystemInformation`（`SystemPerformanceInformation` 加逐 CPU 時間）
取得，不開 WMI、不開子處理程序。

無法在 Windows 上量測的欄位（`ssCpuRawWait`、`ssCpuRawSteal`、`ssCpuRawSoftIRQ`、
`ssCpuRawGuest`）**不輸出**，而不是填 0。填 0 會讓 LibreNMS 建立圖表並畫一條零線，
看起來像「量過而且是零」，實際上是「根本量不到」。

`ssCpuRawNice` 是例外：Windows 沒有 nice，但這裡輸出 0，因為
「Windows 上永遠沒有 nice 時間」是正確的陳述，而 LibreNMS 的 ucd-mib poller
要求 user / nice / system / idle **四個都存在**才會建立 Detailed Processor Usage 圖表，
少一個整張圖就不會出現。

## 磁碟區標籤的編碼問題

`hrStorageDescr` 帶的是磁碟區標籤。在台灣的現場，磁碟區標籤常常是中文，
而這是一個會實際炸掉的地方：pysnmp 的 `rfc1902.OctetString(str)` 遇到非 ASCII
會拋 `PyAsn1UnicodeEncodeError`，整個快照建不起來，agent 看起來是「啟動了但沒資料」。

處理方式是所有 OCTET STRING 一律先自行編成 UTF-8 位元組再交給 pysnmp，
避免讓它自己猜編碼。這條規則涵蓋磁碟區標籤、介面描述、
`sysContact` / `sysLocation`，以及 SMBIOS 解析出來的字串。

實測結果：一個標籤為「乙太網路」的磁碟區，在 LibreNMS 的 Disk Usage 頁面
顯示正確，沒有亂碼也沒有問號。這件事有端對端驗證，不只是編碼層面的單元測試。

## 為什麼 ports 與 hrDevice 少那麼多

內建 SNMP 把每一個 NDIS 篩選器驅動程式都當成獨立介面輸出。實測 `192.0.2.208`
的 ports 內容：

```
ethernet_32777    ethernet_32770    ppp_32768 (down)
ethernet_8   ethernet_9   ethernet_11   ethernet_12   ethernet_13   ethernet_15
```

全部是自動命名、看不出用途的項目，`ppp_32768` 還是一個 down 的 WAN Miniport。
`hrDevice` 的 68 筆中有 **51 筆是 `hrDeviceNetwork`**，同樣包含 WFP 篩選器驅動程式、
QoS 排程器與通道介面。

在一台 Hyper-V host 上這個數字會膨脹到 40～80 個介面。每一個都會在 LibreNMS
產生一個 port 與一組 RRD，而虛擬介面時有時無，離開時 RRD 就失去對應。

jt-snmpd 只輸出 `HardwareInterface = TRUE` 且非 `FilterInterface` 的介面，
並排除 loopback 與 NIC team 成員。同一台機器上 11 個介面裡準確挑出 1 張實體網路卡，
正確排除了 WFP 篩選器驅動程式 ×3、VPN 虛擬卡 ×2（PANGP / F5）、Kernel Debug、
Loopback、Teredo / IP-HTTPS / 6to4。

**這是設計決定，不是缺漏。** 需要完整清單時可將 `interface_filter.mode`
設為 `all`。

## OID 總數：7,582 vs 767

差距集中在幾張表，逐一說明。

### 刻意不提供（資訊揭露）

| Subtree | 內建 | jt-snmpd | 為什麼不提供 |
|---|---:|---:|---|
| `hrSWInstalled` | 660 | 0 | 每個軟體的精確版本 = 現成的 CVE 清單 |
| `hrSWRun` | 1,449 | 0 | 哪套 EDR 在跑、裝在哪 = 客製化規避 |
| `hrSWRunPerf` | 414 | 0 | 同上 |
| `tcpConnTable` | 1,230 | 0 | 完整連線清單 |
| `udpTable` | 50 | 0 | 服務清單 |
| `ipNetToMedia`（ARP）| 196 | 0 | 內網 ARP 表 = 橫向移動的目標清單 |

合計 **3,999 個 OID**，佔兩者差距一半以上。這些數字是在受測機器上量的，會隨機器變動：裝了多少軟體、跑著多少處理程序、開著多少連線，都直接反映在內建服務的總數上。

這些**都已實作或可實作**，但預設關閉。威脅模型認定主要對手是
已在內網的攻擊者：一次未認證的唯讀 walk 就能取得完整的弱點評估報告與內網拓撲，
而 agent 以 LocalSystem 執行。

### 預設關閉的三千個 OID，裡有用的只有一項

「刻意不提供」這個說法，隱含著「提供了會很有用」。實際去查 LibreNMS 26.8.1
的原始碼之後，四類裡有三類**在 LibreNMS 根本沒有取用端**：送出去也不會變成
任何一個頁面、任何一張圖、任何一筆資料表。

| Subtree | OID 數 | LibreNMS 取用端 | 送出去會得到什麼 |
|---|---:|---|---|
| `hrSWInstalled` | 407 | 只有 `LibreNMS/OS/Junos.php` 讀其中兩筆特定索引，用來解析 JUNOS 版本字串 | 沒有任何軟體清單頁面。在 Windows 上等於零 |
| `hrSWRun` / `hrSWRunPerf` | 1,792 | 只有 `LibreNMS/OS/Edgeos.php` 與 `Edgeosolt.php` | LibreNMS 沒有處理程序模組。等於零 |
| `tcpConnTable` / `udpTable` | 528 | **完全沒有**。整份原始碼零引用 | 等於零 |
| `ipNetToMedia` / `ipNetToPhysical` | 448 | **有**：`LibreNMS/Modules/ArpTable.php` 會 walk 這兩張表，寫進 `ipv4_mac` | ARP 搜尋、FDB 搜尋、連接埠的鄰居資料 |

換句話說，2,727 個 OID 揭露的是弱點清單與連線狀態，換來的 LibreNMS 功能是**零**。
這不再是「安全與功能的取捨」，而是單純沒有理由送出去。

只有 ARP 是真的有用的那一項，它也**已經實作**，預設關閉。要開啟的話，
編輯 `C:\ProgramData\jt-snmpd\config.json`：

```json
{
  "enable_arp_table": true
}
```

存檔後重新啟動服務（`Restart-Service jt-snmpd`），下一次探索就會出現。

開啟前請衡量：ARP 表是內網的鄰居清單，對已經進到內網的攻擊者而言是橫向移動的
目標清單。在一台 Windows 端點上，它通常只有同網段的少數幾筆，對 LibreNMS 的
價值主要是「用 MAC 反查 IP」，而那件事在路由器與交換器上做的效益高得多。

### 已補齊（原本真的缺）

以下三張表在第一次對照時發現我們真的沒有，已於 0.1.2 補上：

| Subtree | 內建 | jt-snmpd 0.1.2 | 資料來源 |
|---|---:|---:|---|
| `hrFSTable` | 27 | **18** | `GetVolumeInformationW`，掛載點 `C:` / `D:`，型別 NTFS |
| `hrPartitionTable` | 20 | **10** | 同上 |
| `ipRouteTable` | 130 | **42** | `GetIpForwardTable2`，含預設閘道與直連網段 |

筆數少於內建，因為內建把每個虛擬介面的路由都列出來，而我們只列對映到
實體介面的路由。

### jt-snmpd 獨有

| Subtree | 內建 | jt-snmpd | 內容 |
|---|---:|---:|---|
| `entPhysicalTable` | 0 | 80 | SMBIOS 解析：機箱、主機板、CPU、DIMM（含料號與速度）、磁碟 |
| `entPhySensorTable` | 0 | 24 | 磁碟溫度、ACPI 溫度區、CPU 頻率 |
| `diskIOEntry`（UCD）| 0 | 20 | 讀寫位元組與次數，含 64-bit 版本 |
| JT 自我健康 OID | 0 | 65 | 版本、RSS、快照年齡、collector 健康表 |
| `ipAddressTable`（IPv6）| 0 | 24 | 內建只有 IPv4 的 `ipAddrTable` |

### 為什麼 SMART 不放在 entPhySensorTable

第一版把 NVMe 耐用度與可用備援空間送成 `entPhySensorType = other(1)`，
在 LibreNMS 上完全看不到。`includes/discovery/sensors/entity-sensor.inc.php`
的對照表只認 9 種型別：

    voltsDC voltsAC amperes watts hertz percentRH rpm celsius dBm

`other` 不在裡面，整筆被無聲丟棄。agent 端一切正常、`snmpwalk` 也查得到值，
只是 LibreNMS 不收，這種「兩邊都沒錯但接不起來」的落差最難查。

計數型的 SMART 指標因此改走 **NET-SNMP-EXTEND-MIB**，那是 LibreNMS 讀 SMART 的
正規路徑，而且完全走 SNMP。在其他平台上這條路徑是靠主機上一支輔助腳本
呼叫 smartmontools 餵資料的，這裡是 agent 自己以 `IOCTL_STORAGE_QUERY_PROPERTY`
讀屬性，所以被監控端裝的就只有 jt-snmpd 一個。

順帶記錄一個 LibreNMS 的上游缺陷：`entity-sensor.inc.php:47` 把 `hertz` 對應到
類別 `freq`，但 `LibreNMS/Enum/Sensor.php:24` 定義的合法類別是 `frequency`，
因此所有以 `hertz` 回報的感測器都會被丟棄。同樣的寫法也出現在
`cisco-entity-sensor.inc.php:56` 與 `openbsd.inc.php:28`，影響範圍不只本專案。

## 實體機上的 Inventory 實測內容

`192.0.2.63`（Dell Latitude E5270，Windows 10 22H2）的 Inventory 頁，
全部來自 SMBIOS 解析，**內建 SNMP 一項都沒有**：

```
Latitude E5270 (DESKTOP-9PNNQ34)        Serial ****
└── 0DV5YH (Mainboard)                  Dell Inc.
    ├── Intel(R) Core(TM) i5-6300U @ 2.40GHz (U3E1)   2 cores, 2400 MHz
    └── HMA82GS6AFR8N-UH (DIMM A)        16384 MB 2133 MT/s   Serial ****
└── SAMSUNG SSD PM871b M.2 2280 256GB    238 GB (RAID)   Serial ****
    └── PhysicalDrive0 Temp              34 °C
```

> 序號在本文件中以 `****` 取代。agent 本身**會**回報真實序號，現場要換哪一顆
> 磁碟、哪一條記憶體時，序號才是找得到的依據，那些資料停留在客戶自己的
> 監控系統內。這裡遮蔽只是因為這份文件會公開。

## 行為差異摘要

| 面向 | 內建 SNMP | jt-snmpd |
|---|---|---|
| 開發狀態 | Microsoft 已標記為棄用，不再積極開發 | 持續維護 |

> **「棄用」不等於「停止支援」。** 依 Microsoft 自己的定義，棄用指的是不再積極開發、
> 未來版本可能移除；被棄用的元件仍隨產品出貨、**仍支援用於正式環境**，
> 並依產品生命週期繼續取得安全性與品質更新。所以換掉它是規劃問題，不是急件 ——
> 真正該換的理由是它提供的資料太少（見上表），而不是它明天就不能用。
| SNMP 版本 | v1 / v2c / v3 | v2c（v3 開發中）|
| 寫入 | 支援 SET | **唯讀** |
| Trap | 支援 | 不支援（v1.0 非目標）|
| 來源存取控制 | `PermittedManagers`，在解析之後生效 | 前置解析閘門，在 BER decoder **之前** |
| 速率限制 | 無 | 每來源 token bucket |
| 回應大小控制 | 無 | 上限 1400 bytes，不分片 |
| 介面篩選 | 無，全部輸出 | 只輸出實體網路卡 |
| ifIndex 穩定性 | Windows 原生索引，**不保證跨重開機穩定** | 以 NET_LUID 保存 |
| `sysUpTime` | **SNMP 服務**啟動以來的時間 | **機器**開機以來的時間 |
| 重新啟動 agent | 在 LibreNMS 看起來像重開機（見下） | 不影響回報的 uptime |
| 自我健康監控 | 無 | 私有 OID 子樹 |
| 部署 | Windows 功能（DISM / Add-WindowsCapability）| MSI（GPO / Intune / SCCM）|

`ifIndex` 那一列值得特別注意：更換驅動、拔插網路卡、重建 vSwitch 都可能讓
Windows 重新編號，而 LibreNMS 以 ifIndex 對應 port。編號一變，舊 port 被標記
刪除、新 port 重新建立，歷史 RRD 全部失去對應。jt-snmpd 以 NET_LUID 為主鍵
長期保存的配發，首次見到某介面時給一個 ifIndex，之後不再變更。

### 內建服務只要重新啟動，看起來就像重開機

同一台機器、前後幾分鐘，分別在兩個 agent 下實測：

| OID | Windows 內建 SNMP Service | jt-snmpd |
|---|---|---|
| `sysUpTime.0` | **19 秒** | 179 天 |
| `hrSystemUptime.0` | 179 天 | 179 天 |
| `snmpEngineTime.0` | 不提供 | 179 天 |

內建服務照 RFC 3418 的字面實作：`sysUpTime` 從網路管理部分上一次重新初始化算起，
而那個部分就是 SNMP 服務本身。jt-snmpd 回報的則是主機的開機時間，來源是 `GetTickCount64`。

LibreNMS 取 `sysUpTime`、`snmpEngineTime`、`hrSystemUptime` 的最大值，
但 `windows.yaml` 設了 `bad_hrSystemUptime: true`，而內建服務又不提供 `snmpEngineTime`。
於是對內建服務來說那個最大值就是 19 秒，比上一次輪詢記錄的 uptime 低，
LibreNMS 就對一台已經開機半年的機器發出 **Device rebooted**。
只要 SNMP 服務重新啟動就會這樣：Windows Update、服務自動復原、解除安裝，都算。

jt-snmpd 在兩個層面上避開了它：服務重啟不會讓它的 `sysUpTime` 歸零，
而 `snmpEngineTime` 又給了 LibreNMS 第二個穩定來源。
這件事的意義不只是假告警，因為 `sysUpTime` 的型別是 TimeTicks，
依型別定義在 497 天必然回捲；`snmpEngineTime` 的單位是秒，不會。


## 移轉行為

安裝時自動從內建 SNMP 的登錄檔沿用：

| 來源 | 目標 | 處理 |
|---|---|---|
| `ValidCommunities` 權限 4（唯讀）| community | 直接匯入 |
| `ValidCommunities` 權限 8 / 16（可寫）| community | **降級為唯讀**並警告 |
| `ValidCommunities` 權限 1 / 2 | — | 不匯入（對唯讀 agent 無意義）|
| `PermittedManagers` | 來源 ACL + 防火牆範圍 | 主機名稱解析為 IP，失敗則列出警告 |
| `PermittedManagers` 為空 | — | **安裝中止**，不會移轉為 Any/Any |
| `RFC1156Agent\sysContact` | `sysContact` | 直接沿用 |
| `RFC1156Agent\sysLocation` | `sysLocation` | 直接沿用 |
| `RFC1156Agent\sysServices` | — | 不匯入（固定 76），原值不同時列入報告 |
| `TrapConfiguration` | — | 不匯入，**完整列出並警告 trap 將停止發送** |
| `ExtensionAgents` | — | 不匯入，列出名稱並警告其 OID 將不再可用 |

停用內建服務時記錄原始啟動類型與狀態，解除安裝時自動還原。

## 如何自行複製這份對照

```bash
# 在 LibreNMS 主機上
for oid in 1.3.6.1.2.1.25.4 1.3.6.1.2.1.25.6 1.3.6.1.2.1.47.1.1.1.1 \
           1.3.6.1.2.1.4.21 1.3.6.1.2.1.25.3.8; do
  echo -n "$oid  "
  echo -n "內建=$(snmpbulkwalk -v2c -c COMMUNITY -On -Cr20 內建主機 $oid 2>/dev/null | wc -l)  "
  echo    "jt-snmpd=$(snmpbulkwalk -v2c -c COMMUNITY -On -Cr20 jt主機 $oid 2>/dev/null | wc -l)"
done
```

LibreNMS 端的表格筆數：

```sql
SELECT 'ports', COUNT(*) FROM ports WHERE device_id=? AND deleted=0
UNION ALL SELECT 'entPhysical', COUNT(*) FROM entPhysical WHERE device_id=?
UNION ALL SELECT 'sensors', COUNT(*) FROM sensors WHERE device_id=?
UNION ALL SELECT 'mempools', COUNT(*) FROM mempools WHERE device_id=?
UNION ALL SELECT 'ucd_diskio', COUNT(*) FROM ucd_diskio WHERE device_id=?;
```

---

## 相關文件

- [說明文件首頁](https://jasoncheng7115.github.io/jt-snmpd/)
- [安全性評估](https://jasoncheng7115.github.io/jt-snmpd/attack-surface_zh-TW.html)
- [程式碼簽章](https://jasoncheng7115.github.io/jt-snmpd/code-signing_zh-TW.html)
- [發版檢查清單](https://jasoncheng7115.github.io/jt-snmpd/release-checklist_zh-TW.html)

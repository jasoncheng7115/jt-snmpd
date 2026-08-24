---
layout: default
title: 與內建 SNMP Service 對照
description: Compared with the built-in SNMP Service
---

# jt-snmpd 與 Windows 內建 SNMP Service 對照

> 量測環境：正式 LibreNMS 26.8.1（192.0.2.10），無任何 LibreNMS 端修改
> 對照組：`192.0.2.208` Windows 10 22H2，**Windows 內建 SNMP Service**
> 實驗組：`192.0.2.54` Windows 11 26200，**jt-snmpd 0.1.2**
> 量測日期：2026-08-24

---

## LibreNMS 頁面對照

這是使用者實際看得到的差異。

| LibreNMS 資料表 | 內建 SNMP | **jt-snmpd** | 說明 |
|---|---:|---:|---|
| **entPhysical**（Inventory 頁）| **0** | **5** | 內建完全沒有 Inventory。我們從 SMBIOS 解析出機箱、主機板、CPU、DIMM、磁碟 |
| **ucd_diskio**（Disk I/O 頁）| **0** | **2** | 內建沒有 Disk I/O。我們以 `IOCTL_DISK_PERFORMANCE` 提供 |
| **sensors**（溫度頁）| **0** | **2** | 內建沒有任何感測器。我們提供磁碟溫度與 ACPI 熱區（實體機實測 33°C / 25°C）|
| **applications**（SMART 頁）| **0** | **1** | 內建沒有。我們以 NET-SNMP-EXTEND-MIB 提供 LibreNMS 的 `smart` 應用程式（需啟用 `discovery_modules.applications`）|
| **mempools**（Memory 頁）| 2 | **4** | 內建只有 Physical / Virtual。我們另加 Cached / Swap |
| storage（Disk Usage 頁）| 2 | 2 | 相同，但我們的描述含真實磁碟區標籤（含中文）|
| processors | 8 | 6 | 各自的實際核心數，無差異 |
| ipv4_addresses | 2 | 2 | 相同 |
| **ports**（連接埠頁）| 9 | **1** | **刻意不同**，見下 |
| **hrDevice**（設備頁）| 68 | 9 | **刻意不同**，見下 |

## 為什麼 ports 與 hrDevice 少那麼多

內建 SNMP 把每一個 NDIS 過濾驅動都當成獨立介面輸出。實測 `192.0.2.208`
的 ports 內容：

```
ethernet_32777    ethernet_32770    ppp_32768 (down)
ethernet_8   ethernet_9   ethernet_11   ethernet_12   ethernet_13   ethernet_15
```

全部是自動命名、看不出用途的項目，`ppp_32768` 還是一個 down 的 WAN Miniport。
`hrDevice` 的 68 筆中有 **51 筆是 `hrDeviceNetwork`**，同樣包含 WFP 過濾驅動、
QoS 排程器與隧道介面。

在一台 Hyper-V host 上這個數字會膨脹到 40～80 個介面。每一個都會在 LibreNMS
產生一個 port 與一組 RRD，而虛擬介面時有時無，離開時 RRD 就變成孤兒。

jt-snmpd 只輸出 `HardwareInterface = TRUE` 且非 `FilterInterface` 的介面，
並排除 loopback 與 NIC team 成員。同一台機器上 11 個介面裡準確挑出 1 張實體網路卡，
正確排除了 WFP 過濾驅動 ×3、VPN 虛擬卡 ×2（PANGP / F5）、Kernel Debug、
Loopback、Teredo / IP-HTTPS / 6to4。

**這是設計決定，不是缺漏。** 需要完整清單時可將 `interface_filter.mode`
設為 `all`。

## OID 總數：6,457 vs 575

差距集中在幾張表，逐一說明。

### 刻意不提供（資訊揭露，spec §3.5）

| Subtree | 內建 | jt-snmpd | 為什麼不提供 |
|---|---:|---:|---|
| `hrSWInstalled` | 407 | 0 | 每個軟體的精確版本 = 現成的 CVE 清單 |
| `hrSWRun` | 1,394 | 0 | 哪套 EDR 在跑、裝在哪 = 客製化規避 |
| `hrSWRunPerf` | 398 | 0 | 同上 |
| `tcpConnTable` | 460 | 0 | 完整連線清單 |
| `udpTable` | 68 | 0 | 服務清單 |
| `ipNetToMedia`（ARP）| 448 | 0 | 內網 ARP 表 = 橫向移動的目標清單 |

合計 **3,175 個 OID**，佔兩者差距的絕大部分。

這些**都已實作或可實作**，但預設關閉。威脅模型（spec §3.1）認定主要對手是
已在內網的攻擊者：一次未認證的唯讀 walk 就能取得完整的弱點評估報告與內網拓撲，
而 agent 以 LocalSystem 執行。ARP 表已實作，設定中開啟即可。

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
| `entPhySensorTable` | 0 | 24 | 磁碟溫度、ACPI 熱區、CPU 頻率 |
| `diskIOEntry`（UCD）| 0 | 20 | 讀寫位元組與次數，含 64-bit 版本 |
| JT 自我健康 OID | 0 | 65 | 版本、RSS、快照年齡、collector 健康表 |
| `ipAddressTable`（IPv6）| 0 | 24 | 內建只有 IPv4 的 `ipAddrTable` |

### 為什麼 SMART 不放在 entPhySensorTable

第一版把 NVMe 耐用度與可用備援空間送成 `entPhySensorType = other(1)`，
在 LibreNMS 上完全看不到。`includes/discovery/sensors/entity-sensor.inc.php`
的對照表只認 9 種型別：

    voltsDC voltsAC amperes watts hertz percentRH rpm celsius dBm

`other` 不在裡面，整筆被無聲丟棄。agent 端一切正常、`snmpwalk` 也查得到值，
只是 LibreNMS 不收——這種「兩邊都沒錯但接不起來」的落差最難查。

計數型的 SMART 指標因此改走 **NET-SNMP-EXTEND-MIB**，那是 LibreNMS 讀 SMART 的
正規路徑，而且完全走 SNMP（被監控端不需要 LibreNMS agent 或 smartctl）。

順帶記錄一個 LibreNMS 的上游缺陷：`entity-sensor.inc.php:47` 把 `hertz` 對應到
類別 `freq`，但 `LibreNMS/Enum/Sensor.php:24` 定義的合法類別是 `frequency`，
因此所有以 `hertz` 回報的感測器都會被丟棄。同樣的寫法也出現在
`cisco-entity-sensor.inc.php:56` 與 `openbsd.inc.php:28`，影響範圍不只本專案。

## 實體機上的 Inventory 實例

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

> 序號在本文件中以 `****` 取代。agent 本身**會**回報真實序號——現場要換哪一顆
> 磁碟、哪一條記憶體時，序號才是找得到的依據，那些資料停留在客戶自己的
> 監控系統內。這裡遮蔽只是因為這份文件會公開。

## 行為差異摘要

| 面向 | 內建 SNMP | jt-snmpd |
|---|---|---|
| 支援狀態 | Microsoft 已標記停止支援 | 持續維護 |
| SNMP 版本 | v1 / v2c / v3 | v2c（v3 開發中）|
| 寫入 | 支援 SET | **唯讀** |
| Trap | 支援 | 不支援（v1.0 非目標）|
| 來源存取控制 | `PermittedManagers`，在解析之後生效 | 前置解析閘門，在 BER decoder **之前** |
| 速率限制 | 無 | 每來源 token bucket |
| 回應大小控制 | 無 | 上限 1400 bytes，不分片 |
| 介面過濾 | 無，全部輸出 | 只輸出實體網路卡 |
| ifIndex 穩定性 | Windows 原生索引，**不保證跨重開機穩定** | 以 NET_LUID 持久化 |
| 自我健康監控 | 無 | 私有 OID 子樹 |
| 部署 | Windows 功能（DISM / Add-WindowsCapability）| MSI（GPO / Intune / SCCM）|

`ifIndex` 那一列值得特別注意：更換驅動、拔插網路卡、重建 vSwitch 都可能讓
Windows 重新編號，而 LibreNMS 以 ifIndex 對應 port。編號一變，舊 port 被標記
刪除、新 port 重新建立，歷史 RRD 全部變成孤兒。jt-snmpd 以 NET_LUID 為主鍵
持久化配發，首次見到某介面時給一個 ifIndex，之後永不變更。

## 移轉行為

安裝時自動從內建 SNMP 的登錄檔沿用：

| 來源 | 目標 | 處理 |
|---|---|---|
| `ValidCommunities` 權限 4（唯讀）| community | 直接匯入 |
| `ValidCommunities` 權限 8 / 16（可寫）| community | **降級為唯讀**並警告 |
| `ValidCommunities` 權限 1 / 2 | — | 不匯入（對唯讀 agent 無意義）|
| `PermittedManagers` | 來源 ACL + 防火牆範圍 | 主機名稱解析為 IP，失敗則列出警告 |
| `PermittedManagers` 為空 | — | **安裝中止**，絕不移轉為 Any/Any |
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

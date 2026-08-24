# 攻擊面分析：裝了 jt-snmpd 之後，這台 Windows 多了什麼風險

> 量測日期：2026-08-24　jt-snmpd 0.3.0
> 量測環境：192.0.2.63（Dell Latitude E5270 / Windows 10 22H2）
> 量測端：192.0.2.10（LibreNMS 26.8.1）

監控代理程式最不能接受的失敗，是**它自己成為被攻擊的入口**。
這份文件把「多開了什麼、能被利用到什麼程度、擋住的是哪一層」逐項量測並記錄，
數字都可以自行複製。

---

## 1. 新增的網路暴露面：UDP/161

裝了之後多開一個 UDP 監聽埠。SNMP 是**反射式 DDoS 的經典協定**——
攻擊者偽造來源 IP 送一個小請求，讓被害者收到大回應。

### 實測放大倍率

```
16:51:53.019679 192.0.2.10.58590 > 192.0.2.63.161: UDP, length 39
16:51:53.037594 192.0.2.63.161 > 192.0.2.10.58590: UDP, length 774
```

**39 → 774 位元組 = 19.8×**，而且這是攻擊者刻意把 GETBULK 的
`max-repetitions` 拉到 **1000** 的結果。

沒有上限的 agent 在同樣請求下會回傳到訊息大小上限為止；以本專案 6,000 筆
OID 的規模推算，回應會膨脹到數萬位元組，放大倍率進入 1000× 等級。
壓住它的是兩個機制：

| 機制 | 值 | 效果 |
|---|---|---|
| `MAXREP_CAP` | 25 | 無論請求要多少，最多處理 25 次重複 |
| 回應位元組上限 | 1400 | 不分片，超過即截斷回應 |

**放大倍率的理論上限因此是 1400/39 ≈ 36×，實測 19.8×。**

### 反射攻擊實際上打不出去

放大倍率只有在「能讓回應送到被害者」時才有意義。要做到這件事，
攻擊者必須偽造一個**在管理網段內**的來源位址，而這被兩層擋住：

```
JT SNMP Agent (UDP 161): Allow proto=UDP port=161 from=192.168.1.0/255.255.255.0
JT SNMP Agent (ICMPv4):  Allow proto=ICMPv4         from=192.168.1.0/255.255.255.0
```

- **Windows 防火牆**：安裝時強制輸入管理網段，預設拒絕其餘來源。
  封包在到達我們的程序之前就被作業系統丟掉。
- **前置解析閘門**（`preauth.py`）：來源 IP 白名單 → 封包大小上限 →
  每來源 token bucket → 外層 TLV 健全性檢查，
  **全部在 pysnmp 的 BER 解碼器之前**執行。

第二層的存在理由是第一層可能被設寬（客戶自行改防火牆），
以及**深度防禦**：BER 解碼器是攻擊面最大的一塊，能不讓未授權封包碰到它最好。

---

## 2. 程式碼執行風險：服務以 LocalSystem 執行

這是最需要誠實面對的一項。**解析器的漏洞就是 SYSTEM 權限的遠端執行。**

### 為什麼一定要 LocalSystem

磁碟 SMART 需要 `SMART_RCV_DRIVE_DATA` 這類 IOCTL，而
`\\.\PhysicalDriveN` 必須以 `GENERIC_READ | GENERIC_WRITE` 開啟——
那需要系統管理權限。虛擬服務帳戶做不到。

### 緩解：權杖權限已剝到最小

```
SERVICE_NAME: jt-snmpd
        PRIVILEGES : SeChangeNotifyPrivilege
                   : SeSystemProfilePrivilege
                   : SeIncreaseQuotaPrivilege
```

LocalSystem 預設帶約 30 項權限，這裡只留 3 項。**已移除的包括**
`SeDebugPrivilege`（讀寫任意程序記憶體）、`SeTcbPrivilege`、
`SeImpersonatePrivilege`（權杖竊取，提權慣用路徑）、
`SeLoadDriverPrivilege`（載入核心驅動）、`SeBackupPrivilege` / `SeRestorePrivilege`
（繞過 ACL 讀寫任意檔案）。

即使解析器被攻破，攻擊者拿到的是一個無法偵錯其他程序、無法模擬其他使用者、
無法載入驅動的 SYSTEM 內容。

### 緩解：唯讀，且沒有 oracle

```
$ snmpset -v2c -c mon2 192.0.2.63 .1.3.6.1.2.1.1.6.0 s "PWNED"
Timeout: No Response from 192.0.2.63
$ snmpget -v2c -c mon2 -Oqv 192.0.2.63 .1.3.6.1.2.1.1.6.0
"LAB"
```

SET 不是「回錯誤」而是**直接丟棄**——不回應就不提供任何可探測的訊息。
實作上是不覆寫 `write_variables()`，所以唯讀不是靠檢查，是靠**沒有那條路徑**。

### 緩解：不引入核心驅動

CPU 溫度需要讀 MSR，那需要核心驅動。業界慣用的 WinRing0 已列入
Microsoft 易受攻擊驅動封鎖清單——**為了一個溫度值，在數百台政府與醫院主機上
安裝一個能任意讀寫 MSR 與實體記憶體的驅動，是把監控工具變成提權管道。**
本專案因此放棄 CPU 溫度（見 `CLAUDE.md` 鐵則 8）。

### 緩解：解析器面對敵意輸入

所有「長度／偏移量取自緩衝區自身」的解析都當成敵意輸入處理，
且與採集分離成純函式，用惡意位元組做 property test
（`tests/test_sensors_parsing.py`，34 項）。

ctypes 是 Python 記憶體安全性失效之處：緩衝區配小了，核心會直接寫出界。
已修正的實例——`CallNtPowerInformation` 原型用 `os.cpu_count()` 決定緩衝區大小，
在超過 64 個邏輯處理器的機器上會少報，導致核心寫出界。
現改用 `GetActiveProcessorCount(ALL_PROCESSOR_GROUPS)`。

---

## 3. 自我阻斷服務

一個亂寫的長度欄位不會讓 Python 越界，但會讓迴圈跑四十億次——
在「絕不能拖慢 host」的硬性要求下，這是一次自我 DoS。所有解析都有上限常數
（`MAX_INSTANCES`、`MAX_WMI_BUFFER`、`MAX_PROCESSORS`、`MAX_NAME_CHARS`）。

輪詢造成的負載已量測（`CLAUDE.md`）：7,000 倍真實輪詢速率下，
固定基準工作負載退化 **0.41%**（程序設為 `BELOW_NORMAL_PRIORITY_CLASS` 之後）。
單次完整 walk 12.5 ms CPU；LibreNMS 每 5 分鐘一次 → 實際約 0.004% CPU。

每來源 token bucket 限制請求速率，`prune()` 定期清除過期項目，
避免來源表本身成為記憶體耗盡的途徑。

---

## 4. 資訊揭露：刻意不提供的資料

威脅模型（spec §3.1）認定主要對手是**已經在內網的攻擊者**。
一次未認證的唯讀 walk 若能取得完整弱點評估報告與內網拓撲，
這個 agent 就是攻擊者的資產。因此以下**預設關閉**（合計 3,175 個 OID）：

| Subtree | Windows 內建 SNMP | jt-snmpd | 不提供的理由 |
|---|---:|---:|---|
| `hrSWInstalled` | 407 | 0 | 每個軟體的精確版本 = 現成的 CVE 清單 |
| `hrSWRun` / `hrSWRunPerf` | 1,792 | 0 | 哪套 EDR 在跑、裝在哪 = 客製化規避 |
| `tcpConnTable` | 460 | 0 | 完整連線清單 |
| `udpTable` | 68 | 0 | 服務清單 |
| `ipNetToMedia`（ARP）| 448 | 0 | 內網 ARP 表 = 橫向移動的目標清單 |

介面過濾也順帶減少了揭露：只輸出實體網卡，VPN 虛擬卡、WFP 過濾驅動、
隧道介面都不出現。

---

## 5. 尚未緩解的風險（誠實列出）

| 風險 | 現況 | 計畫 |
|---|---|---|
| **community 明文傳輸** | v2c 沒有加密或認證，網路上可嗅探 | SNMPv3（SHA-256 + AES-128，金鑰以 DPAPI 儲存）為 v1.0 需求 |
| **來源 IP 可偽造** | UDP 無連線，白名單擋得住反射但擋不住盲送 | v3 的認證可根治；目前靠速率限制與唯讀降低影響 |
| **執行檔未簽章** | Authenticode 簽章尚未取得 | 已規劃向 SignPath Foundation 申請 |
| **pysnmp BER 解碼器** | 前置閘門擋在它之前，但授權來源仍會走到它 | 專用小解析器（Phase 1），縮小這塊攻擊面 |
| **LocalSystem** | 權限已剝到 3 項 | 無法再降：SMART IOCTL 需要系統管理權限 |

---

## 6. 對照：不裝 jt-snmpd 的替代方案

| 方案 | UDP/161 | 執行身分 | 寫入 | 來源控制 | 速率限制 | 支援狀態 |
|---|---|---|---|---|---|---|
| **Windows 內建 SNMP** | 開 | LocalSystem（權限未剝除）| **支援 SET** | `PermittedManagers`，**在解析之後**生效 | 無 | Microsoft 已標記停止支援 |
| **jt-snmpd** | 開 | LocalSystem（僅 3 項權限）| 唯讀 | 前置閘門，**在 BER 解碼器之前** | 每來源 token bucket | 持續維護 |
| 不做 SNMP 監控 | 不開 | — | — | — | — | 沒有監控 |

**取代內建 SNMP 是攻擊面的淨減少**：少了 SET、少了 27 項權限、
少了 3,175 個資訊揭露 OID，多了速率限制與前置來源檢查。

---

## 7. 如何自行複製這些量測

```bash
# 放大倍率
tcpdump -i any -n -q "udp port 161 and host <目標>" &
snmpbulkget -v2c -c <community> -Cr1000 -Cn0 <目標> .1.3.6.1.2.1

# 唯讀驗證
snmpset -v2c -c <community> <目標> .1.3.6.1.2.1.1.6.0 s "TEST"   # 應逾時
snmpget -v2c -c <community> -Oqv <目標> .1.3.6.1.2.1.1.6.0        # 應不變
```

```powershell
# 權杖權限
sc qprivs jt-snmpd
# 防火牆範圍
Get-NetFirewallRule -DisplayName 'JT SNMP Agent*' | Get-NetFirewallAddressFilter
```

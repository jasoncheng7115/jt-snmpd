# Phase 0 決策閘門 — 查證結果

> 對應規格：`spec.md` §1
> 規則：本文件記錄**實際量測與查證**的結果。未驗證的項目一律列入 §9「未驗證項目與風險」，
> 不得以推測填入。

| 閘門 | 狀態 |
|---|---|
| A — 多網路卡 UDP 來源位址 | ⛔ 無法驗（Win11 測試機為單網路卡；需多網路卡環境） |
| B — sysDescr / sysObjectID 與 LibreNMS OS 偵測 | ✅ **GO**（已用真實 LibreNMS 26.8.1 + Win11 內建 SNMP 驗證，見 §1.4） |
| C — PySNMP 可行性與效能 | ✅ **GO**（架構達標，BER 瓶頸有對策，見 §2.6） |
| D — 打包、EDR 與程式碼簽章 | ✅ **GO**（PyInstaller exe 服務已重開機驗證，見 §4.1）；HVCI/WDAC 待驗，簽章確定不做 |
| E — Net-SNMP 對照評估（ADR-0001） | ⛔ 未開始 |

> 環境：開發機 Ubuntu 24.04（Python 3.12.3、pysnmp 7.1.29）；
> 測試機 Win11 Pro build 26200 @ 192.0.2.54（單網路卡，admin）；
> LibreNMS 26.8.1 @ 192.0.2.10（正式機，唯讀操作）。

---

## 1. 閘門 B — LibreNMS 端查證（已完成）

查證對象：LibreNMS `master`，查證日期 2026-08-23。

### 1.1 `windows.yaml` 的實際路徑與旗標

spec §11 列了兩個候選路徑。實際為：

```
resources/definitions/os_detection/windows.yaml     ← 存在
includes/definitions/discovery/windows.yaml         ← 404，spec 的另一個猜測已不適用
```

完整內容：

```yaml
os: windows
type: server
text: 'Microsoft Windows'
ifname: true
processor_stacked: true
bad_hrSystemUptime: true
mib_dir: dell
group: microsoft
over:
    - { graph: device_processor, text: 'Processor Usage' }
    - { graph: device_mempool,   text: 'Memory Usage' }
    - { graph: device_storage,   text: 'Storage Usage' }
discovery:
    - sysObjectID: .1.3.6.1.4.1.311.1.1.3
    - sysDescr: Windows
```

spec §1.2 要求確認的四個旗標，實際值：

| 旗標 | 實際值 | 對本專案的影響 |
|---|---|---|
| `bad_hrSystemUptime` | **`true`** | LibreNMS **忽略 hrSystemUptime，只用 sysUpTime**。§2.6 的 497 天 TimeTicks 回捲風險因此全部集中在 sysUpTime 一個 OID 上，`jtAgentUptimeWrapCount` 的重要性提高 |
| `processor_stacked` | **`true`** | CPU 圖表為堆疊呈現，每核心一列 |
| `ifname` | **`true`** | **port 標籤使用 ifName**。ifName 必須是有意義的名稱，直接綁住 §2.4 介面篩選的設計——篩選掉的介面不會有 port，留下的介面 ifName 必須可讀 |
| `bad_ifXEntry` | **檔案中未出現** → 預設 `false` | LibreNMS 會正常使用 ifXTable 的 64-bit counters，v1.0 必須提供 |

### 1.2 OS 偵測的實際條件

`discovery` 有兩條 OR 條件：`sysObjectID` 前置碼 `.1.3.6.1.4.1.311.1.1.3`，**或** `sysDescr` 含 `Windows`。

**對 §1.2 長期規劃的影響（spec 未預見的有利發現）**：取得自有 PEN 之後改用自家
sysObjectID，裝置**不會**掉出 `windows` 這個 OS——第二條 `sysDescr: Windows` 會接住。
但 `LibreNMS/OS/Windows.php` 的 `discoverOS()` 用來抓 Hardware / Version / Build 的
regex 仍要求 Microsoft SNMP Service 的字串格式，所以 §1.2「短期完全模仿 MS 格式」
的結論**不變**。

### 1.3 待驗（需其他機型）

- [x] 實際 sysDescr 能被 `Windows.php` regex 正確 match（見 §1.4）
- [x] Device 頁面 Hardware / Version / Features 三欄位皆有值（見 §1.4）
- [ ] `GetProductInfo()` + `DsRoleGetPrimaryDomainInformation()` 的 server / DC 分支（僅有工作站）
- [ ] `Windows.php` 的 `ServerHardware` trait 會嘗試哪些 MIB（需 server）

### 1.4 閘門 B — Windows 端實測（GO）

在 WIN11-PRO-1（build 26200）上以 localhost 查詢其 **Microsoft 內建 SNMP Service**
（唯讀，未改任何設定），取得真實的 system group：

```
sysDescr.0    = Hardware: AMD64 Family 25 Model 80 Stepping 0 AT/AT COMPATIBLE
                - Software: Windows Version 6.3 (Build 26200 Multiprocessor Free)
sysObjectID.0 = 1.3.6.1.4.1.311.1.1.3.1.1     → client（工作站）✓
sysServices.0 = 79
```

以 `Windows.php` 的 `discoverOS()` regex 實測：**match 成功**，抓出
`hardware='...AT/AT COMPATIBLE'`、`nt='6.3'`、`build='26200'`、`smp='Multiprocessor'`。

**同一台已被正式 LibreNMS 26.8.1 納管（device_id 106），DB 中三欄位實際值：**

| 欄位 | 值 |
|---|---|
| os | `windows` |
| version | `11 Insider (NT 6.3)` |
| hardware | `AMD x64` |
| features | `Multiprocessor` |

**三欄位皆有值、皆不空白 → 閘門 B 驗收標準達成。** 我方 agent 只需複製此 sysDescr
格式即可得到相同結果。fixture：`docs/fixtures/win11-pro-26200-msft-snmp.json`。

#### ⚠ 對 spec §1.2 的兩處修正（實測發現）

1. **NT 版本不是 `10.0`。** spec §1.2 範例寫 `Windows Version 10.0`，但真實
   Microsoft SNMP Service 在 Win11 build 26200 上回報 **`6.3`**（`snmp.exe` 無
   version manifest，`GetVersionEx` 受版本謊報限制停在 6.3）。我方 agent 的
   sysDescr 應**照抄 `6.3`** 以求與 MS 完全一致。

2. **LibreNMS 主要吃 build number，不是 NT 版本。** `getClientVersion($build, $nt)`
   以 **build 當主鍵**查表；`$nt` 只在 build 9200 用來區分 Win8/8.1。因此：
   - build **26100** → `11 (24H2)`（乾淨）
   - build **26200** → 不在表中，落到 default `build > 22000 → '11 Insider (NT 6.3)'`

   結論：我方 sysDescr 的 **build number 必須正確**，NT 版本欄位照抄 MS 的 6.3 即可。

#### §11 待確認清單，本次一併查證

| 項目 | 結果 |
|---|---|
| LibreNMS os_detection 檔案路徑 | `resources/definitions/os_detection/windows.yaml`（見 §1.1） |
| LibreNMS 當前版本 | **26.8.1**（`master`），`lnms` CLI 在 `/opt/librenms/lnms` |
| windows.yaml 四旗標 | 見 §1.1 |

---

## 2. 閘門 C — PySNMP（API 與正確性已完成，效能進行中）

版本：**pysnmp 7.1.29**（PyPI 當前最新，`requires-python >=3.10`）。
相依只有 `pyasn1`，執行時**不需要 PySMI 或任何編譯過的 MIB module**——
這一點證實了 §4.3 對啟動時間與 RSS 的預期。

### 2.1 `AbstractMibInstrumController` 的實際 API

spec §11 問「實際 API 形式（snake_case）」。答案：是 snake_case，而且介面極小：

```python
class AbstractMibInstrumController:
    def read_variables(self, *varBinds, **context): ...       # GET
    def read_next_variables(self, *varBinds, **context): ...  # GETNEXT / GETBULK
    def write_variables(self, *varBinds, **context): ...      # SET
```

**結論：§4.3 的「換掉 pysnmp MIB 物件模型」成本極低**——實作兩個方法即可，
`write_variables` 不覆寫就自動成為唯讀 agent（符合 §2.12）。

自訂 controller 的掛載方式：`SnmpContext.context_names[b""] = <controller>`。

### 2.2 VACM 的實際掛點（§3.5 的實作陷阱在此）

`verify_access` 是由 command responder 以 `acFun` 放進 context dict 傳給
instrum controller 的。**代表 VACM 要不要在 walk 路徑上生效，完全取決於
自訂 controller 有沒有呼叫它。**

§3.5 警告的「典型漏洞是 GET 有篩選但 walk 直接跨過去」在這個架構下
就是一行程式碼的差距。而且被 VACM 拒絕的項目必須**繼續往下找**而非回傳錯誤，
否則 walk 會在該處中斷。已在原型中實作，L4-5.4 需專項測試。

### 2.3 原生 `BulkCommandResponder` 的三個問題（spec 未預見）

pysnmp 的 GETBULK 實作（`entity/rfc3413/cmdrsp.py`）是：

```python
while M and R:
    rspVarBinds.extend(mgmtFun(*varBinds, **context))   # 每個 repetition 一次呼叫
    varBinds = rspVarBinds[-R:]
    M -= 1
```

原始碼中還留著 `# TODO(etingof): manage all PDU var-binds in a single call`。

| # | 問題 | 後果 |
|---|---|---|
| 1 | 每個 repetition 都是獨立的 `read_next_variables` 呼叫 | **§4.3「GETBULK 退化為陣列切片」在原生實作下不成立**。max-repetitions=25 就是 25 次 bisect，而正確答案是 1 次 bisect + 1 次切片 |
| 2 | 只有 varbind **筆數**上限（`max_varbinds = 64`），**沒有任何位元組上限** | **§4.4 的 1400 bytes 截斷無法靠設定達成，必須覆寫這個 responder** |
| 3 | 走到 MIB 結尾時用 endOfMibView 把回應**塞滿**到 max-repetitions 筆 | 實測 200 筆的樹回 **225 行**（200 + 25 筆 endOfMibView）。每個 subtree 的最後一個封包都白費頻寬，§36 列為不可接受 |

**結論：必須自訂 `BulkCommandResponder`。** 已實作為
`bench/gate_c/agent.py::BatchedBulkCommandResponder`，同時解決上述三項。
單一 repeater（bulkwalk 常態）走切片快速路徑，多 repeater 交回原生語意。

### 2.4 BER 大小預算：解析式計算取代實際編碼

§4.4 需要在編碼時邊算大小邊截斷。若在請求路徑上反覆試編碼再回退，成本很高，
因此改為**在 snapshot 建立時預先算好每筆 varbind 的編碼大小**，請求路徑只剩累加比較。

但實測顯示，用真實 BER 編碼器來預算大小**本身就撞破預算**：

| 快照建立成本 | µs/varbind | 佔比 |
|---|---|---|
| 值物件建構 | 7.4 | 6% |
| OID 產生 + 排序 | 0.7 | 1% |
| **BER 大小預算（實際編碼）** | **115.5** | **93%** |

10,000 個 varbind 需 1.63 秒，直接超過 §4.2「快照重建 < 500 ms」。

改用**解析式長度計算**（不實際編碼）後：**2.55 µs/varbind，加速 45×**。

⚠ **一個必須記住的陷阱**：pyasn1 對負整數邊界**不使用最短編碼**，會多送一個
多餘的前導位元組（`-128` 編成 `ff 80` 而非 `80`；`-2147483648` 編成 5 bytes 而非 4）。
解析式計算的用途是**預測 pyasn1 會吐出多少位元組**，因此必須跟著 pyasn1 走，
而不是跟著 DER 規範走。正負號最終共用同一條公式：`v.bit_length() // 8 + 1`。

pyasn1 一旦改變編碼方式，大小預測就會無聲漂掉，回應可能超過 1400 bytes 而被
防火牆分片丟棄——症狀是「LibreNMS 間歇性抓不到資料」，極難查。
因此 `tests/test_ber_size.py` 以 property test 對照真實編碼器，必須進 CI。
**540 例通過（含 4,000 組隨機）。**

### 2.5 正確性：§4.3 的「結構保證」已實證

§4.3 聲稱排序陣列讓 §36 的正確性要求「全部成為結構保證，不需人工維護」。
`tests/test_walk_correctness.py` 驗證此聲稱，**20 例全數通過**：

- 全樹 walk 回傳每個 OID 恰好一次，內容與 snapshot 完全一致
- 嚴格字典序，無 duplicate OID
- walk 必定終止（無 GETNEXT loop）
- **max-repetitions = 1 / 2 / 10 / 25 / 100 / 1000 的結果集完全相同**
- **回應永不超過 1400 bytes，即使客戶端要求 10000 筆**
- 過大的 max-repetitions 被截斷為有效回應，而非錯誤或空回應
- 走到 MIB 結尾時不以 endOfMibView 填充
- GET 命中 / noSuchInstance / noSuchObject 語意正確
- GETNEXT 在首筆之前、末筆之後的邊界行為正確

### 2.6 效能量測（Ubuntu 24.04 開發機，10k / 50k varbind）

門檻見 §1.3 / §4.2：每 varbind < 80 µs、全樹 walk < 10 秒、GETBULK（max-rep 25）< 30 ms。

#### 端對端（經 socket + asyncio，snmpbulkwalk 量測）

| varbinds | 模式 | 全樹 walk | µs/vb（wall） | agent CPU µs/vb | GETBULK p50 | p95 |
|---|---|---|---|---|---|---|
| 10,000 | batched | 2.66 s | 266 | 252 | 14.9 ms | 18.6 ms |
| 50,000 | batched | 13.2 s | 264 | 252 | 14.6 ms | 17.3 ms |

初步端對端 CPU 為 **~252 µs/vb，超出 80 µs 門檻 3.1 倍**。µs/vb 從 1k 到 50k 幾乎持平，
證明這不是固定成本攤提問題，而是真實的每 varbind 成本。**GETBULK 延遲 <30 ms 達標。**

#### 瓶頸拆解（決定性）

同行程 profile（`bench/gate_c/profile_path.py`，排除 socket/asyncio 雜訊）拆出兩層瓶頸：

| 處理階段 | 成本 | 說明 |
|---|---|---|
| snapshot + bisect controller（GET/GETNEXT/切片） | **8.0 µs/vb** | **架構層完全達標** |
| 回應組裝：pysnmp PDU 物件模型（`apiPDU.set_varbinds` + `ber.encode`） | 164 µs/vb | pyasn1 物件建構 84 + 編碼 49 |
| 回應組裝：**wire 預編碼**（snapshot 建立時預編，請求路徑僅切片+串接） | **0.35 µs/vb** | **快 360×** |
| 請求解碼：pyasn1 純 Python BER decoder | 主成本 | 見下 |

換上 wire 預編碼後，**整條請求路徑降到 18.3 µs/vb**（cProfile 顯示其中 86% 已是
「解碼進來的請求」，編碼/bisect/組裝僅佔 14%）。

#### 結論

- **§4.3 的 snapshot + bisect 架構驗證通過**：MIB 層 8 µs/vb，且 §36 的正確性
  要求成為結構保證（20 例測試全過）。**閘門 C 判定：GO。**
- **§1.3 預先點名的風險②「純 Python BER 效能」屬實**，且是換掉 pysnmp 物件模型後
  的下一個瓶頸。兩項對策：
  1. **回應編碼 → wire 預編碼**（已實作 `bench/gate_c/wire.py`，與 pyasn1
     位元組完全一致，6,294 組對照零不符）。這是 v1.0 必用路徑。
  2. **請求解碼**：一個唯讀 SNMP agent 只需解析固定形狀的 GET/GETNEXT/GETBULK
     PDU，可寫一個極小的專用 BER 解析器取代 pyasn1 通用 decoder。列為
     **Phase 1 的效能項**，非架構阻塞——因為即使不做，換 wire 後 18 µs/vb
     已在門檻內。

#### 建立時成本（§4.2 快照重建 < 500 ms）

| 項目 | 成本 | 50k varbind 總計 |
|---|---|---|
| wire 預編碼（含值物件與 OID） | 7.9 µs/vb | 395 ms ✓ |
| wire 記憶體佔用 | — | 1.38 MB（可忽略） |

⚠ **平台落差必須註記**：本量測在 Linux 上進行。SNMP 訊息層、BER、
snapshot/bisect 皆與平台無關，故**架構可行性的結論有效**；
Windows 上須覆測絕對數字後才能作為正式驗收依據（Windows 的 Python 通常較慢）。

---

## 3. 未驗證項目與風險

| 項目 | 阻塞原因 | 風險 |
|---|---|---|
| 閘門 A 全部 | 無多網路卡（≥3 IP、跨網段）主機 | **高**——多網路卡回應來源 IP 錯誤會造成間歇性 timeout 且極難除錯，且客戶端幾乎都是管理/業務網段分離 |
| 閘門 D 全部 | 無 Windows 存取；無 HVCI/WDAC 端點 | **高**——未簽章時 WDAC 需改用雜湊規則放行 |
| 閘門 B 的 Windows 端 | Win11 SSH 未授權 | 中 |
| sysObjectID 的 server / DC 分支 | 僅有 Win11 工作站 | 中——兩個分支無法驗 |
| Server Core、Server 2016/2019/2022/2025 | 無環境 | **高**——§9.3 的平台 DoD 無法達成 |
| 效能絕對數字 | 僅有 Linux 量測 | 中——架構結論有效，驗收數字待補 |

---

## 4.1 閘門 D — 服務機制實測（Win11 build 26200，已通過）

在 WIN11-PRO-1 上以正規 python.org Python 3.12.10 + pywin32 + PyInstaller 6.22.2
（安裝於 `C:\jtdev\Python312`，不碰系統 Store Python）驗證**服務化的核心機制**。
測試服務用非標準埠 16161，**全程未動內建 SNMP（161）與正式監控**。

#### 已驗證（GO）

| 項目 | 結果 |
|---|---|
| pywin32 `ServiceFramework` 服務控制處理常式 | ✅ 可註冊、可 Start/Stop |
| 執行身分 | ✅ **LocalSystem**（log 記錄 `WIN11-PRO-1$` 電腦帳號）|
| 啟動類型 | ✅ **Automatic** |
| snapshot+bisect responder 在 session 0 綁定 UDP | ✅（修正一個 bug，見下）|
| loopback SNMP GET 回應 | ✅ 正確回傳 sysDescr |
| **重開機後不需登入自啟** | ✅ **開機後 16 秒自啟**（開機 21:40:13 → 服務 21:40:29）|
| 重開後無互動登入 | ✅ `quser` 確認 console 無人登入，服務仍 Running 並回應 |

**結論：使用者要求的「開機後自動跑、不需任何人登入」已在真機證實可達成。**

#### 修正的 bug（§6.5「假活著」的縮影）

第一次測試時服務 **Status=Running 但 UDP 埠沒有聽取者**——log 停在 `SvcDoRun start`、
沒有 `agent LISTENING`。根因：pysnmp 的 `udp.open_server_mode()` 需要在
**running event loop 內**建立，而初版把 transport 建立放在 `loop.run_until_complete()`
**之前**，`open_server_mode` 拿不到 running loop，socket 從未真正綁定。

這正是 §6.5 指出的最危險故障型態：**服務顯示 Running 不等於能回應**。
→ 強化了「loopback 自我測試是唯一能偵測假活著的機制」這個規格的必要性，
且 §6.7「首次快照完成前只回應 system group」與此類綁定時序問題同源。

修正：transport 建立與 event loop 必須在同一個 async 進入點內（`run_agent` 已改）。

#### PyInstaller exe 作為服務主程式（已完成，2026-08-24）

spec §1.4 硬性規則「服務主程式必須是自己的 exe」已達成。

建置：`PyInstaller --onedir --console --name jt-snmpd`
（**一律 one-folder，禁用 one-file**——one-file 會解壓到 `%TEMP%` 執行，
是已知 DLL 劫持路徑）。hidden imports 需含 `win32timezone`。

產出：`jt-snmpd.exe` 3.3 MB，one-folder 共 110 檔 / 22.9 MB。

**frozen 模式的關鍵差異**：未打包時走 `win32serviceutil.HandleCommandLine`
（由 `pythonservice.exe` 代 host）；打包後**必須**改走
`servicemanager.PrepareToHostSingle()` + `StartServiceCtrlDispatcher()`，
因為此時沒有 `pythonservice.exe` 可代跑。以 `sys.frozen` 與 argv 長度分派。

重開機存活測試（無人登入）：

| 證據 | 值 |
|---|---|
| 開機時間 | 08-24 08:40:53 |
| 互動登入 | **無人登入**（quser 確認）|
| 服務 | Running / Automatic |
| 行程 | **jt-snmpd**，08:41:07 啟動（開機後 **14 秒**）|
| BINPATH | `"C:\jtdev\dist\jt-snmpd\jt-snmpd.exe"`（**加引號**）|
| UDP/161 | 由 `jt-snmpd.exe` 佔用 |
| log | `frozen=True` / `LISTENING` / 131 varbinds |

**零 Python 依賴**：目標機不需安裝任何 Python，exe 自帶執行環境。

#### 待驗（需其他環境）

- [ ] Defender + HVCI/WDAC 端點上 exe 不被隔離、可存活（無此環境）
- [ ] WDAC 強制模式下以雜湊規則放行的行為

## 4. 已送件事項（附錄 B）

| 項目 | 狀態 |
|---|---|
| IANA Private Enterprise Number | ⛔ 未送件 |
| 程式碼簽章 | ⛔ 不申請憑證；改以公布 SHA-256 建立完整性 |

兩者皆有外部審核等待期，spec 附錄 B 要求 Phase 0 第一天送出。

# 命名與路徑定案


## 命名

| 項目 | 名稱 | 理由 |
|---|---|---|
| 專案 / repo | **jt-snmpd** | `d` 結尾符合 daemon 慣例（對照 `snmpd`/`sshd`），一看即知是常駐服務 |
| Windows 服務名稱 | **jt-snmpd** | 與專案同名，`sc query jt-snmpd` 直覺 |
| 服務顯示名稱 | **JT SNMP Agent** | services.msc 中可讀 |
| 服務描述 | 以標準 MIB 提供 Windows 主機監控資料的 SNMP Agent | |
| 主執行檔 | **jt-snmpd.exe** | 服務主程式|
| 管理 CLI | **jt-snmpdctl.exe** | 與服務主程式分離，避免混淆（對照 systemctl）|
| 安裝目錄名 | **JT SNMP Agent** | |
| 資料目錄名 | **JT-SNMP** | |
| GPO 原則路徑 | `HKLM\SOFTWARE\Policies\JasonTools\JTSNMPD` | |

## 安裝路徑（程式與資料嚴格分離）

```
%ProgramFiles%\JT SNMP Agent\          ← 程式本體，唯讀。不放 ProgramData
    jt-snmpd.exe
    jt-snmpdctl.exe
    _internal\                          PyInstaller one-folder 執行環境
    mibs\                               MIB 檔（供 LibreNMS / snmpwalk）

%ProgramData%\JT-SNMP\                 ← 設定與狀態，可寫
    config.yaml
    config.example.yaml
    secrets\usm.dat                     SNMPv3 localized key（DPAPI machine scope）
    state\index-map.json                ifIndex 保存（弄丟它，LibreNMS 會重建全部 port）
    state\engine-state.json             engineID / engineBoots
    state\ms-snmp-migration.json        Windows SNMP 移轉與還原資訊
    logs\jt-snmpd.log                   輪替：5 MB × 5 份
    logs\ms-snmp-migration-report.txt
```

## 硬性路徑規則

1. **ImagePath 必須加引號**。實測過：`C:\程式集測試\JT SNMP 代理程式\jt_snmpd.py`
   未加引號時被空白截斷成 `C:\程式集測試\JT`，處理程序直接死掉且無任何 log。
   這就是資安稽核最常抓到的 *unquoted service path*。
   預設安裝路徑 `%ProgramFiles%\JT SNMP Agent\` **本身就含空白**，所以這不是邊緣案例。

2. **ProgramData ACL 必須驗證擁有者並重設**。`C:\ProgramData` 預設 ACL 允許 Users
   建立子資料夾，攻擊者可搶先建立 `JT-SNMP` 並保留寫入權。安裝程式不能只做
   create-if-not-exists。目標 ACL：`SYSTEM: Full`、`Administrators: Full`、其他無。

3. **非 ASCII 路徑必須支援**。客戶可能裝在中文路徑。已實測通過（見下）。

## 已實測驗證（2026-08-24，Win11 build 26200 正體中文）

從 `C:\程式集測試\JT SNMP 代理程式\`（**中文 + 空白**）執行 agent：

```
LISTENING 0.0.0.0:16162 varbinds=131
fs_encoding=utf-8
sysDescr     = Hardware: AMD64 Family 25 Model 80 Stepping 0 AT/AT COMPATIBLE
               - Software: Windows Version 6.3 (Build 26200 Multiprocessor Free)
sysServices  = 76                        ← 我方 agent（內建 MS SNMP 為 79）
ifName       = 乙太網路                   ← 中文 UTF-8 正確
ifDescr      = Red Hat VirtIO Ethernet Adapter
hrProcLoad   = 8                         ← 真實 CPU 取樣
diskIODevice = PhysicalDrive0            ← UCD-DISKIO
```

### 編碼鐵則（從實測 bug 得出）

**SNMP OCTET STRING 是位元組串，不是文字。** pyasn1 預設以 latin-1 編碼 `str`，
遇到非 ASCII 直接丟 `PyAsn1UnicodeEncodeError`。正體中文 Windows 的網路卡別名就是
中文（「乙太網路」），所以這在台灣環境是**必踩**的，不是邊緣案例。

一律經過 `octet()` 包裝明確編成 UTF-8，禁止裸用 `rfc1902.OctetString(str)`。
同理，所有檔案 I/O 一律明確 `encoding="utf-8"`，Windows 的 `open()` 預設是系統
ANSI 代碼頁（正體中文為 cp950），寫入非 cp950 字元會丟 `UnicodeEncodeError`。

---
layout: default
title: 命名與路徑（繁體中文）
description: Naming and paths, and the encoding rules that came out of real bugs
---

[← 回到說明文件首頁](https://jasoncheng7115.github.io/jt-snmpd/) ·
[English](https://jasoncheng7115.github.io/jt-snmpd/naming-and-paths.html) | **繁體中文**

# 命名與路徑定案


## 命名

**一個名字，到處都一樣：`jt-snmpd`。**

原本的做法是分成兩種寫法：技術識別用 `jt-snmpd`，人看的顯示名稱用
「JT SNMP Agent」，資料目錄又是第三種寫法 `JT-SNMP`。這在 Windows 上是常見慣例
（顯示「Windows Defender 防火牆」而服務名是 `mpssvc`），但實際造成的是誤會：
使用者在 GitHub 上找到的是 jt-snmpd，在「應用程式與功能」看到的卻是另一個名字，
而磁碟上還有第三種。0.9.6 起全部統一。

| 項目 | 名稱 | 理由 |
|---|---|---|
| 專案 / repo | **jt-snmpd** | `d` 結尾符合 daemon 慣例（對照 `snmpd`/`sshd`），一看即知是常駐服務 |
| Windows 服務名稱 | **jt-snmpd** | `sc query jt-snmpd` 直覺 |
| 服務顯示名稱 | **jt-snmpd** | 與服務名稱一致，services.msc 裡不會出現第二個名字 |
| 產品名稱（MSI、應用程式與功能）| **jt-snmpd** | 使用者在 GitHub 找到什麼，在控制台就看到什麼 |
| 安裝精靈標題 | **jt-snmpd Setup** | |
| 服務描述 | 以標準 MIB 提供 Windows 主機監控資料的 SNMP Agent | |
| 主執行檔 | **jt-snmpd.exe** | 服務主程式 |
| 管理 CLI | **jt-snmpdctl.exe** | 與服務主程式分離，避免混淆（對照 systemctl）|
| 安裝目錄名 | **jt-snmpd** | 順帶消除路徑中的空白，也就消除了 unquoted service path 這一整類問題 |
| 資料目錄名 | **jt-snmpd** | 0.9.5 以前是 `JT-SNMP`；升級時由安裝程式搬移，見下 |
| 防火牆規則 | **jt-snmpd (UDP 161)**、**jt-snmpd (ICMPv4)** | |
| GPO 原則路徑 | `HKLM\SOFTWARE\Policies\JasonTools\JTSNMPD` | 登錄機碼保持不變，改它會讓既有的 GPO 失效 |

### 從 0.9.5 以前升級

安裝程式會把 `%ProgramData%\JT-SNMP` **搬移**到 `%ProgramData%\jt-snmpd`。
這一步不能省：`state\index-map.json` 裡是 ifIndex 的配發結果，弄丟它，
LibreNMS 會刪掉每一個 port 重新探索，歷史 RRD 一起失去對應；
`state\ms-snmp-restore.json` 則是「內建 SNMP 服務原本長什麼樣」的唯一紀錄。
搬移失敗時會退而複製並在記錄檔中明白說出來，因為多一份目錄可以救，少一份不行。
`tests/test_data_dir_migration.py` 守著這件事。

## 安裝路徑（程式與資料嚴格分離）

以下是實機上的實際內容，不是規劃。標「（規劃）」的目前不存在。

```
%ProgramFiles%\jt-snmpd\          ← 程式本體，唯讀。不放 ProgramData
    jt-snmpd.exe
    msi-configure.ps1                   安裝與解除安裝時由 MSI 呼叫
    _internal\                          PyInstaller one-folder 執行環境
    jt-snmpdctl.exe                     （規劃）管理 CLI
    mibs\                               （規劃）MIB 檔，供 LibreNMS / snmpwalk

%ProgramData%\jt-snmpd\                 ← 設定與狀態，可寫
    config.json                         安裝程式寫入，agent 在進入點讀取
    state\index-map.json                ifIndex 保存（弄丟它，LibreNMS 會重建全部 port）
    state\engine.json                   engineID / engineBoots
    state\ms-snmp-restore.json          內建 SNMP 的原始啟動類型與狀態，供解除安裝還原
    state\disk-maxtemp.json             實際觀測到的磁碟最高溫，跨重新啟動保存
    logs\jt-snmpd.log                   輪替：每檔 5 MB，保留 3 份（上界 4 檔約 20 MB）
    logs\msi-configure.log              安裝程式自己的記錄
    secrets\usm.dat                     SNMPv3 的 localized key，DPAPI machine scope
```

> 這份清單曾經寫著 `config.yaml`、`engine-state.json`、`ms-snmp-migration.json`，
> 三個名字都與實際不符。文件裡的路徑要跟實機核對過再寫。

## 硬性路徑規則

1. **ImagePath 必須加引號**。實測過：`C:\程式集測試\JT SNMP 代理程式\jt_snmpd.py`
   未加引號時被空白截斷成 `C:\程式集測試\JT`，處理程序直接死掉且無任何 log。
   這就是資安稽核最常抓到的 *unquoted service path*。
   預設安裝路徑 `%ProgramFiles%\jt-snmpd\` **本身就含空白**，所以這不是邊緣案例。

2. **ProgramData ACL 必須驗證擁有者並重設**。`C:\ProgramData` 預設 ACL 允許 Users
   建立子資料夾，攻擊者可搶先建立 `jt-snmpd` 並保留寫入權。安裝程式不能只做
   create-if-not-exists。目標 ACL：`SYSTEM: Full`、`Administrators: Full`、其他無。

3. **非 ASCII 路徑必須支援**。客戶可能裝在中文路徑。已實測通過（見下）。

## 已實測驗證（2026-08-24，Win11 build 26200 台灣繁體中文）

從 `C:\程式集測試\JT SNMP 代理程式\`（**中文 + 空白**）執行 agent：

```
LISTENING 0.0.0.0:16162 varbinds=131
fs_encoding=utf-8
sysDescr     = Hardware: AMD64 Family 25 Model 80 Stepping 0 AT/AT COMPATIBLE
               - Software: Windows Version 6.3 (Build 26200 Multiprocessor Free)
sysServices  = 76                        ← 見下方說明
ifName       = 乙太網路                   ← 中文 UTF-8 正確
ifDescr      = Red Hat VirtIO Ethernet Adapter
hrProcLoad   = 8                         ← 真實 CPU 取樣
diskIODevice = PhysicalDrive0            ← UCD-DISKIO
```

> **`sysServices` 不能用來分辨是哪一個 agent 在回答**，雖然這裡曾經這樣用過。
> 它來自登錄檔的 `RFC1156Agent\sysServices`，管理員可以從服務內容的「代理程式」
> 分頁勾選，所以它描述的是這台機器，不是這個軟體。在一台 Windows Server 2016
> 網域控制站上實測，未經調整的內建服務回報的就是 **76**，與本 agent 相同。
> 要分辨請看 `sysDescr`，或私有子樹底下的 `jtAgentVersion` —— 內建服務生不出那個。

### 編碼規則（從實測 bug 得出）

**SNMP OCTET STRING 是位元組串，不是文字。** pyasn1 預設以 latin-1 編碼 `str`，
遇到非 ASCII 直接丟 `PyAsn1UnicodeEncodeError`。台灣繁體中文 Windows 的網路卡別名就是
中文（「乙太網路」），所以這在台灣環境是**必踩**的，不是邊緣案例。

一律經過 `octet()` 包裝明確編成 UTF-8，禁止裸用 `rfc1902.OctetString(str)`。
同理，所有檔案 I/O 一律明確 `encoding="utf-8"`，Windows 的 `open()` 預設是系統
ANSI 代碼頁（台灣繁體中文為 cp950），寫入非 cp950 字元會丟 `UnicodeEncodeError`。

---

## 相關文件

- [說明文件首頁](https://jasoncheng7115.github.io/jt-snmpd/)
- [手動移除](https://jasoncheng7115.github.io/jt-snmpd/manual-removal_zh-TW.html)
- [安全性評估](https://jasoncheng7115.github.io/jt-snmpd/attack-surface_zh-TW.html)

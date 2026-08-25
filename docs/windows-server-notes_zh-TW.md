---
layout: default
title: 部署到 Windows Server
description: Server 2016 / 2019 / 2022 / 2025 之間的差異，以及哪些差異會影響到這個 agent
---

[← 回說明文件首頁](https://jasoncheng7115.github.io/jt-snmpd/) ·
[English](https://jasoncheng7115.github.io/jt-snmpd/windows-server-notes.html) | **繁體中文**

# 部署到 Windows Server

已在 **Windows Server 2016 Standard（build 14393）的網域控制站**上實測：
40 項安裝生命週期檢查全過、從內建 SNMP Service 的移轉正常、
agent 回報的是網域控制站那一支 `sysObjectID`。
以下標示**實測**的都來自那台機器；標示**文件**的來自 Microsoft，
**尚未在此重現** —— 2019、2022、2025 都還沒有測過。

---

## 1. 先講結論

| 版本 | 預期可用 | 要注意的 |
|---|---|---|
| 2016 | **已實測**：40/40、移轉、DC 分支 | — |
| 2019 | 可 | 若要移轉內建服務的設定，安裝方式不同 |
| 2022 | 可 | 同上，另有一個 Microsoft 記載的問題：服務清單只看得到 SNMP Trap |
| 2025 | 可 | Credential Guard 與 VBS 預設開啟；SMB 簽章預設為必要 |

2019、2022、2025 都沒有移除這個 agent 依賴的任何介面。差異在它周圍的環境。

---

## 2. 內建 SNMP Service 各版本的安裝方式不同

這件事**只有在你想讓安裝程式沿用既有設定時才有關係**。jt-snmpd 不需要內建服務存在。

| 版本 | 內建服務怎麼裝 |
|---|---|
| Server 2016 | `Install-WindowsFeature SNMP-Service`，是 Windows 功能。*實測：`Get-WindowsFeature SNMP-Service` 回報 `Installed`。* |
| Server 2019 / 2022 / 2025 | 隨選功能：`Add-WindowsCapability -Online -Name "SNMP.Client~~~~0.0.1.0"` |
| Windows 10 / 11 | 同上。`dism /online /enable-feature /featureName:SNMP` 會以 `0x800f080c` 失敗，**因為這個功能已棄用** |

對部署有兩個影響：

- **隨選功能需要來源。** 在沒有對外網路的機器上（這正是本專案設定的環境），
  `Add-WindowsCapability` 會失敗，除非指到 FoD 的 ISO 或 WSUS／本機來源。
  如果你原本打算「先裝內建服務，好讓 jt-snmpd 去移轉它的 community」，
  **不要這樣做，直接把 `COMMUNITY=` 傳給安裝程式就好** ——
  那是一個屬性，不需要網路上的任何東西。
- **Server 2022 的服務清單可能看起來不對。** Microsoft 記載了一個問題：
  加入 SNMP 與 WMI SNMP Provider 之後，`services.msc` 裡只出現 *SNMP Trap*。
  如果安裝程式說找不到可移轉的內建服務，請用 `Get-Service SNMP` 確認，不要只看主控台。

沒有內建服務、也沒給 `COMMUNITY=` 時，安裝程式會**停下來並說明原因**，不會自己編一個 ——
這一點在一台內建服務執行中、但沒有設定任何 community 的 Server 2016 網域控制站上驗過：
msiexec 回 1603、交易回滾、沒有任何殘留，內建服務也沒有被動過。

---

## 3. Windows Server 2025：預設值變了

**Credential Guard 預設開啟**，對象是符合硬體需求的已加入網域機器，
開啟它會連帶開啟以虛擬化為基礎的安全性（VBS）。Microsoft 說明這適用於
**非網域控制站**的系統；升級到 2025 的機器除非明確關閉，否則會維持啟用。

對多數監控 agent 來說麻煩從這裡開始，因為要讀 CPU 核心溫度或電壓就得載入核心驅動程式，
而常被拿來做這件事的驅動程式已經在 Microsoft 的易受攻擊驅動程式封鎖清單上。
**這個 agent 完全沒有核心模式元件** —— 每一個 collector 都是以 ctypes 直呼 Win32 ——
所以 VBS、HVCI、Credential Guard 沒有我們的東西可以擋。
那是一開始就訂下的規則，不是事後反應，而 2025 是它發揮作用的地方。

這也是為什麼這個 agent 不回報 CPU 核心溫度。誠實的說法寫在
[安全性評估](https://jasoncheng7115.github.io/jt-snmpd/attack-surface_zh-TW.html)：
沒有驅動程式就讀不到的值，就完全不回報。

**SMB 簽章對所有對外連線預設為必要。** 這個 agent 完全不碰 SMB。
它只會在你**從檔案共享派送 MSI** 時碰到你 —— 群組原則軟體安裝，或手動複製 ——
而所有受支援的 Windows 版本都會簽章，所以現代的共享不受影響。
要留意的是放 MSI 的 NAS 或設備如果不支援簽章。

**2025 移除、而這裡一項都沒用到的**：Windows PowerShell 2.0
（安裝程式用的是 5.1，那個留著）、SMTP Server 功能、WordPad。
**WMIC** 在 2025 變成隨選功能，Microsoft 說未來會移除。
這個 agent 從來沒有呼叫過 `wmic.exe`，資料路徑上也不開任何子處理程序，
所以那次移除對使用它的人不構成任何移轉工作。

---

## 4. Server Core

安裝程式沒有任何互動提示：它需要的每一個值都以 MSI 屬性傳入，
少了就直接失敗，不會停在那裡等人打字。`msiexec /qn` 就是全部的程序。

尚未在真正的 Server Core 安裝上驗證過。圖形精靈在那裡本來就用不到；
該用的是無訊息路徑，而那正是 40 項生命週期檢查走的路徑。

---

## 5. 網域控制站

**已在正在運作的 DC 上實測。** agent 透過 `DsRoleGetPrimaryDomainInformation`
判斷角色，回報網域控制站那一支 `sysObjectID`：

```
sysObjectID = 1.3.6.1.4.1.311.1.1.3.1.3
```

這在 LibreNMS 端有意義：`Windows.php` 靠這一支去選資料中心版本字串，
所以一台被當成普通伺服器回報的 DC，版本會是另一個、錯的字串。
用戶端、伺服器、網域控制站分成三支就是為了這個。

唯讀網域控制站（RODC）尚未測試。

---

## 6. `sysServices` 分辨不出是哪一個 agent 在回答

在 Server 2016 網域控制站上兩者都是 **76**：內建服務的值來自登錄檔的
`RFC1156Agent\sysServices`，管理員可以從服務內容的「代理程式」分頁勾選。
它描述的是這台機器，不是這個軟體。要分辨請看 `sysDescr`，
或查私有子樹底下的 `jtAgentVersion` —— 內建服務生不出那個。

這一節之所以存在，是因為本專案內部用「76 對 79」這條規則用了好幾個月，
直到在 Server 上量了一次才發現它不是一條規則。

---

## 相關文件

- [說明文件首頁](https://jasoncheng7115.github.io/jt-snmpd/)
- [與內建 SNMP 的對照](https://jasoncheng7115.github.io/jt-snmpd/comparison-vs-builtin-snmp_zh-TW.html)
- [安全性評估](https://jasoncheng7115.github.io/jt-snmpd/attack-surface_zh-TW.html)
- [自行編譯打包與簽章](https://jasoncheng7115.github.io/jt-snmpd/build-and-sign_zh-TW.html)

**Microsoft 來源：**
[Windows Server 已移除或不再開發的功能](https://learn.microsoft.com/en-us/windows-server/get-started/removed-deprecated-features-windows-server) ·
[無法安裝 SNMP 與 WMI SNMP Provider 功能](https://learn.microsoft.com/en-us/troubleshoot/windows-client/networking/cannot-install-snmp-wmisnmpprovider) ·
[Credential Guard 概觀](https://learn.microsoft.com/en-us/windows/security/identity-protection/credential-guard/) ·
[Windows Server 2025 的 SMB 安全性強化](https://learn.microsoft.com/en-us/windows-server/storage/file-server/smb-security-hardening)

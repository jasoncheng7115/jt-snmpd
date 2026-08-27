---
layout: default
title: ADR-0001 為何自建（繁體中文）
description: Why the agent was written from scratch in Python rather than rebuilding Net-SNMP
---

[← 回到說明文件首頁](https://jasoncheng7115.github.io/jt-snmpd/) ·
[English](https://jasoncheng7115.github.io/jt-snmpd/adr/0001-why-not-net-snmp.html) | **繁體中文**

# ADR-0001：為何不重建 Net-SNMP for Windows，改以 Python 自建

> 狀態：已決策（Accepted）
> 日期：2026-08-24

## 背景

原計畫前提是「Net-SNMP 缺乏現代官方 Windows binary」。這是事實，但不等於不能重建，
Net-SNMP 的 win32 mibgroup 已有相當完整的 HOST-RESOURCES / IF-MIB 實作。因此
本閘門要求以書面比較後再定案，避免日後重複討論。

## 選項比較

| 方案 | 初期成本 | 長期維護 | 擴充 JT 私有 MIB | 團隊技能是否相符 | 能否完整打包 |
|---|---|---|---|---|---|
| **A. 從零以 Python 實作（採用）** | 高 | 中 | 容易 | 高 | 容易（PyInstaller）|
| B. vcpkg + MSVC 重建 Net-SNMP + 補 mibgroup | 中 | 高（要跟上游）| 需寫 C | 低 | 需處理 C runtime |
| C. Telegraf / windows_exporter | 極低 | 低 | — | — | — |

## 決策：選 A（Python 自建）

### 為何不選 C（Telegraf / windows_exporter）

**LibreNMS 消費的是 SNMP，不是 Prometheus / InfluxDB。** Telegraf 輸出 line protocol、
windows_exporter 輸出 Prometheus metrics，都不是 SNMP，LibreNMS 無法直接以標準 SNMP
poller 納管。且客戶環境普遍要求「不得有主動對外連線的代理程式」，而這兩者的典型部署
都是 agent 主動推送或被 Prometheus 拉取，與 LibreNMS 的 SNMP 拉取模型不符。

### 為何不選 B（重建 Net-SNMP）

1. **長期維護成本高**：需持續跟上游 Net-SNMP 版本，且 win32 mibgroup 的建置鏈
   （vcpkg + MSVC）在 CI 上脆弱。
2. **擴充 JT 私有 MIB 需寫 C**：自我健康 OID、與 LibreNMS 的相容性微調，
   全部要改 C 程式碼並重新編譯，改一輪就慢一輪。
3. **需要另一條建置與發版鏈**：以 C 維護一個長期分支，要 autotools 與 MSVC 的
   工具鏈，與本專案其餘部分（Python + PyInstaller + WiX）沒有任何共用之處。
   那是第二套要跟著版本走、跟著出問題的東西。
4. **不容易整包帶走**：C binary 需處理 MSVC runtime 相依；Python + PyInstaller
   one-folder 產出的東西本來就是完整一包，此點已於閘門 D 以實機驗證。

**上游自己的故障史也指向同一個方向。** 這一點不是推測，是 Net-SNMP 公開的問題與
安全通報，而且**每一項都落在本專案沒有的那些部分**：

| Net-SNMP 的問題 | 對本專案是否適用 |
|---|---|
| AgentX 子代理逾時導致 snmpd 當掉或 100% CPU 空轉（bug 2411） | 不適用，本專案沒有 AgentX，也不接受外掛擴充 |
| `ipNetToMediaTable` 與 `table_iterator` 的多次記憶體洩漏 | 不適用，本專案是排序陣列加 bisect，每次重建整份快照，沒有走訪器狀態 |
| USM 重複使用者造成記憶體洩漏（bug 2942） | 不適用，使用者在啟動時一次載入，執行期不變動 |
| snmptrapd 特製封包緩衝區溢位（CVE-2025-68615） | 不適用，本專案不實作 trap 接收 |
| ICMP-MIB 表格物件的阻斷服務向量 | 不適用，本專案不提供該表格 |

**要說明的是這張表的性質。** 它不是在說 Net-SNMP 寫得不好 —— 那是一個經過數十年、
支撐無數環境的實作。它說的是:**那些問題大多來自它的擴充機制（AgentX 子代理、
動態模組）與手動記憶體管理**，而本專案兩者都沒有，代價是功能範圍小得多。
選 A 換到的不是「更安全的程式碼」，是**小得多的攻擊面與更少的活動零件**。

### 上游來源

上表引用的每一項都是公開可查的，列在這裡是為了讓讀者自己判斷，而不是要人相信本文件：

- [Net-SNMP bug 2411 — AgentX 子代理逾時](https://sourceforge.net/p/net-snmp/bugs/2411/)
- [Net-SNMP bug 2942 — USM 重複使用者記憶體洩漏](https://sourceforge.net/p/net-snmp/bugs/2942/)
- [GHSA-4389-rwqf-q9gq — snmptrapd 緩衝區溢位（CVE-2025-68615）](https://github.com/net-snmp/net-snmp/security/advisories/GHSA-4389-rwqf-q9gq)
- [Net-SNMP NEWS](https://www.net-snmp.org/docs/NEWS.html)

### 為何選 A（Python 自建），且已有實證

初期成本雖高，但：

- **snapshot + bisect 架構讓 SNMP 協定正確性成為結構保證**（閘門 C 驗證，20 例測試通過），
  不需人工維護 GETNEXT ordering / 無重複 OID / endOfMibView。
- **擴充私有 MIB 只是往排序陣列加項目**，無需編譯。
- **與既有 jt-* 專案技能一致**（Python）。
- **PyInstaller one-folder 產出的是完整一包**，符合客戶「不上網、零外部相依」要求（閘門 D 驗證）。
- **Phase 0.5 已用實機證明可行**：Python 自建 agent 已部署到 Win11、正式 LibreNMS
  透過它成功偵測 OS 並取得 ports / storage / processor / diskio（見 deploy/README.md）。

## 後果

- 承擔純 Python BER 的效能挑戰（已知，對策見閘門 C：wire 預編碼 + 專用解析器）。
- 需自行維護與 LibreNMS 的相容性，且以修 agent 為優先，而非改 LibreNMS。
- 換得：改版快、整包帶得走、與團隊既有技能相符，正確性由架構本身保證。

這份 ADR 的目的不是重新開會，而是讓未來的自己與外部審閱者不必重問。

---

## 相關文件

- [說明文件首頁](https://jasoncheng7115.github.io/jt-snmpd/)
- [與內建 SNMP Service 對照](https://jasoncheng7115.github.io/jt-snmpd/comparison-vs-builtin-snmp_zh-TW.html)
- [安全性評估](https://jasoncheng7115.github.io/jt-snmpd/attack-surface_zh-TW.html)

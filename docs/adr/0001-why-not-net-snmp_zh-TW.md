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

| 方案 | 初期成本 | 長期維護 | 擴充 JT 私有 MIB | 團隊技能匹配 | 自包含部署 |
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
   全部要改 C 程式碼並重新編譯，迭代慢。
3. **團隊技能不匹配**：既有 jt-* 專案（jt-doc-tools）皆為 Python，重建 Net-SNMP
   需 C / autotools / win32 建置專長。
4. **自包含部署較難**：C binary 需處理 MSVC runtime 相依；Python + PyInstaller
   one-folder 天生自包含，此點已於閘門 D 以實機驗證。

### 為何選 A（Python 自建），且已有實證

初期成本雖高，但：

- **snapshot + bisect 架構讓 SNMP 協定正確性成為結構保證**（閘門 C 驗證，20 例測試通過），
  不需人工維護 GETNEXT ordering / 無重複 OID / endOfMibView。
- **擴充私有 MIB 只是往排序陣列加項目**，無需編譯。
- **與既有 jt-* 專案技能一致**（Python）。
- **PyInstaller one-folder 自包含**，符合客戶「不上網、零外部相依」要求（閘門 D 驗證）。
- **Phase 0.5 已用真機證明可行**：Python 自建 agent 已部署到 Win11、正式 LibreNMS
  透過它成功偵測 OS 並取得 ports / storage / processor / diskio（見 deploy/README.md）。

## 後果

- 承擔純 Python BER 的效能挑戰（已知，對策見閘門 C：wire 預編碼 + 專用解析器）。
- 需自行維護與 LibreNMS 的相容性，且以修 agent 為優先，而非改 LibreNMS。
- 換得：迭代快、自包含、技能匹配、正確性由架構保證。

這份 ADR 的目的不是重新開會，而是讓未來的自己與外部審閱者不必重問。

---

## 相關文件

- [說明文件首頁](https://jasoncheng7115.github.io/jt-snmpd/)
- [與內建 SNMP Service 對照](https://jasoncheng7115.github.io/jt-snmpd/comparison-vs-builtin-snmp_zh-TW.html)
- [安全性評估](https://jasoncheng7115.github.io/jt-snmpd/attack-surface_zh-TW.html)

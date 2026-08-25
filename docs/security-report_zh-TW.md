---
layout: default
title: 資安檢測結果（繁體中文）
description: The current scan baseline, with a verdict on every finding
---

[← 回到說明文件首頁](https://jasoncheng7115.github.io/jt-snmpd/) ·
[English](https://jasoncheng7115.github.io/jt-snmpd/security-report.html) | **繁體中文**

# 資安檢測結果

[工具鏈文件](https://jasoncheng7115.github.io/jt-snmpd/security-scanning_zh-TW.html)
寫的是「應該跑什麼」。這一頁是**實際跑了什麼、跑出什麼**。

只有數量的報告不值得提交。下面每一條發現都附上判定與理由，
因為審查者真正想知道的不是「幾個」，而是「每一個為什麼可以接受」。

| | |
|---|---|
| 日期 | 2026-08-25 |
| 版本 | jt-snmpd 1.0.0 |
| 掃描範圍 | `deploy/`、`tools/`、`packaging/`，共 13 個檔案、4,279 行 |

---

## 摘要

| 檢查項目 | 工具 | 結果 |
|---|---|---|
| 原始碼靜態分析（SAST） | Bandit 1.9.4 | **HIGH 0**、MEDIUM 3、LOW 11，全部逐條交代於下 |
| 相依弱點（SCA） | pip-audit 2.10.1 | **70 個套件，0 個已知弱點** |
| 個資與機密 | `tools/check-privacy.py` | **HIGH 0**，每次推送都跑 |
| 測試套件 | pytest | 830 通過、1 略過，每次推送都跑 |
| 安裝檔產物檢查 | 直接讀 Windows Installer 表格 | 5 項，每次推送都跑 |

**執行時期的相依只有兩個套件。** `pysnmp 7.1.29` 依賴 `pyasn1 0.6.4`，
而 `pyasn1` 不依賴任何東西。70 個裡的其餘全是建置與測試工具，
不會進到客戶的機器。這是刻意的：相依愈少，這一節就愈短。

---

## 靜態分析：逐條交代

Bandit 沒有 HIGH。以下十四條為 MEDIUM 與 LOW，每一條不是誤判就是有紀錄的決定。

### B104，「可能綁定到所有介面」（MEDIUM ×3）

| 位置 | 判定 |
|---|---|
| `deploy/jt_agent.py:2938`、`:3069` | **接受，這是設計。** agent 刻意綁 `0.0.0.0`。綁定位址不會篩選發送端，它只決定哪些本機位址收得到封包。來源限制由另外兩處把關，而且是把在對的地方：Windows 防火牆規則只開放管理網段，前置解析閘門在 pysnmp 讀到任何一個位元組之前就檢查來源位址。綁單一位址會讓多網路卡主機失效，而且不會增加任何安全性。見[安全性評估 §1](https://jasoncheng7115.github.io/jt-snmpd/attack-surface_zh-TW.html)。 |
| `tools/check-privacy.py:151` | **誤判。** `"0.0.0.0"` 出現在 IP 規則的**排除**清單裡，用意正是不要把萬用位址報成外洩。 |

### B105，「可能寫死的密碼」（LOW ×2）

`deploy/diskhealth.py:314` 與 `:317`。**誤判。** 那兩個值是
`{"health_pass": True}` 裡的布林值。Bandit 的啟發式規則會比對任何含 `pass`
的名稱，而 `health_pass` 是 SMART 的判定結果，不是憑證。

### B110 / B112，try/except pass 與 continue（LOW ×2）

| 位置 | 判定 |
|---|---|
| `deploy/jt_agent.py:190` | **接受。** 寫入 Windows 事件記錄可能因權限或事件來源未註冊而失敗。一個因為「記不了錯誤」就掛掉的監控 agent，比繼續跑的更糟；同一則訊息已經寫進記錄檔了。 |
| `deploy/diskhealth.py:413` | **接受。** 不回應 SMART 指令的磁碟會被略過，而不是捏造數值。一個沒反應的 USB 橋接器不該讓其餘所有磁碟從快照裡消失。 |

兩處都是窄範圍、都只攔一種預期得到的失敗，而且都有註解說明原因。

### B404 / B603 / B607，使用 subprocess（LOW ×7）

`tools/check-privacy.py`、`tools/prepare-public-repo.py` 與
`tools/check-terminology.py` 呼叫 `git` 取得已追蹤的檔案清單。
**接受。** 三者都以固定的參數陣列呼叫，不經 shell、不帶使用者輸入，
而且都不會出貨到客戶機器，它們是 repo 的工具。B607 是同一個呼叫再被挑一次，
理由是寫 `git` 而不是絕對路徑。
agent 本身完全不開子處理程序，那是專案的硬性規則。

---

## 相依弱點

pip-audit 對照 PyPI Advisory Database 與 OSV，在 70 個已安裝套件中
未發現任何已知弱點。

70 這個數字會高估暴露面，值得分開看：

| | 套件 | 會不會進到客戶機器 |
|---|---|---|
| 執行時期 | `pysnmp`、`pyasn1` | 會，包在 MSI 裡 |
| 打包 | `pyinstaller`、`pywin32` | 它們的產物會，它們本身不會 |
| 測試與工具 | 其餘 | 不會 |

---

## 還沒跑的項目

把這件事寫清楚也是報告的一部分。工具鏈文件列的比實際跑過的多：

| 項目 | 狀態 |
|---|---|
| Semgrep，含本專案自訂規則 | 未跑 |
| gitleaks 掃完整 git 歷史 | 未跑 |
| CycloneDX SBOM | 產生過一次，未定期更新 |
| boofuzz 對 UDP/161 跑 24 小時 | 未跑 |
| PROTOS c06-snmpv1 | 未跑 |
| Windows 平台檢查（`signtool`、`accesschk`、PrivescCheck） | 未成批執行；`sc qprivs` 已人工驗過 |
| HVCI / WDAC 端點存活 | 受阻，沒有這種端點可用 |

除了最後一項，其餘都不是被什麼卡住，只是還沒做。列在這裡是為了避免有人讀了
工具鏈文件就以為那些全都做過了。

---

## 如何自行重跑

```bash
pip install bandit pip-audit
bandit -r deploy/ tools/ packaging/ -f json -o reports/bandit.json
pip-audit --format json -o reports/pip-audit.json
python3 tools/check-privacy.py
python3 -m pytest tests/ -q
```

`reports/` 不公開：原始 JSON 含本機檔案系統路徑。這一頁才是可提交的形式。

---

## 相關文件

- [說明文件首頁](https://jasoncheng7115.github.io/jt-snmpd/)
- [資安檢測工具鏈](https://jasoncheng7115.github.io/jt-snmpd/security-scanning_zh-TW.html)
- [安全性評估](https://jasoncheng7115.github.io/jt-snmpd/attack-surface_zh-TW.html)

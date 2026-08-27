---
layout: default
title: 資安檢測工具鏈（繁體中文）
description: Security scanning toolchain and the report a review will accept
---

[← 回到說明文件首頁](https://jasoncheng7115.github.io/jt-snmpd/) ·
[English](https://jasoncheng7115.github.io/jt-snmpd/security-scanning.html) | **繁體中文**

# 資安檢測工具鏈與報告

| 項目 | 內容 |
|---|---|
| 用途 | 政府 / 醫院標案的資安審查需要**可提交的報告**，不是口頭保證 |

## 為什麼 ZAP 不適用

OWASP ZAP 是 **web 應用程式**的動態掃描器（DAST），它爬 HTTP 端點、注入
payload、看回應。jt-snmpd **沒有 HTTP 介面**，攻擊面是 UDP/161 上的 BER 編碼
封包。ZAP 對它一無所用。

正確的組合是三層：**原始碼靜態分析（SAST）＋ 相依弱點掃描（SCA）＋
協定層模糊測試（DAST，但要 SNMP 專用的）**。

---

## 1. SAST，原始碼靜態分析

| 工具 | 用途 | 為什麼需要 |
|---|---|---|
| **Bandit** | Python 安全反模式 | 抓 `eval`、`subprocess(shell=True)`、寫死密碼、弱雜湊、不安全的 tempfile、`assert` 用於驗證 |
| **Semgrep** | 規則式 SAST（含 `p/security-audit`、`p/secrets`） | 比 Bandit 更深的資料流；可自訂規則抓本專案特有問題 |
| **Ruff**（`S` 規則集） | flake8-bandit 移植，速度極快 | 適合每次 commit 就跑；與 Bandit 規則重疊但更快 |
| **mypy** | 靜態型別檢查 | 型別錯誤在 ctypes 邊界特別危險，傳錯型別會讀寫錯誤記憶體 |
| **CodeQL** | GitHub 的資料流分析 | 跨函式追蹤「未認證輸入 → 危險操作」，最接近人工審查 |

### 本專案特別要檢查的模式

一般規則集抓不到的，需自訂 Semgrep 規則：

```yaml
# 所有 ctypes 外部函式必須宣告 argtypes/restype
# 缺少宣告會讓 64 位回傳值被截斷，實測造成 C: 磁碟顯示 0 GB
- id: ctypes-missing-argtypes
  pattern: $LIB.$FUNC(...)
  pattern-not-inside: |
      $LIB.$FUNC.argtypes = ...
      ...

# OCTET STRING 必須經 octet() 包裝
# 裸用 rfc1902.OctetString(str) 遇非 ASCII 會拋 PyAsn1UnicodeEncodeError
- id: bare-octetstring
  pattern: rfc1902.OctetString($X)
  pattern-not: rfc1902.OctetString($X.encode(...))

# 內部計時不得用 wall clock
- id: wall-clock-timing
  patterns:
    - pattern-either:
        - pattern: datetime.now()
        - pattern: time.time()
```

---

## 2. SCA，相依弱點掃描與 SBOM

| 工具 | 用途 |
|---|---|
| **pip-audit** | 對照 PyPI Advisory DB 與 OSV，掃 Python 相依的 CVE |
| **OSV-Scanner** | Google 的跨生態系掃描器，涵蓋面比 pip-audit 廣 |
| **CycloneDX-python** | 產生 **SBOM**|
| **Trivy** | 也能掃檔案系統與 SBOM，一併驗 secrets |

jt-snmpd 的相依極少（`pysnmp` → `pyasn1`，加打包期的 `pywin32`、`pyinstaller`），
這是刻意的，**相依愈少，SCA 報告愈乾淨，審查愈好過**。

---

## 3. Secret 掃描

| 工具 | 用途 |
|---|---|
| **gitleaks** | 掃整個 git 歷史，不只當前工作區 |
| **detect-secrets** | 可建立 baseline，避免誤報疲勞 |

專案規則：金鑰明文不得出現在 config、log、Event Log 或 MSI 屬性中。
Secret 掃描是這條的自動化守門。

---

## 4. DAST，協定層模糊測試（取代 ZAP 的角色）

以下兩項為必做：

| 項目 | 工具 | 通過條件 |
|---|---|---|
| 24 小時 fuzzing | **boofuzz**（對 UDP/161 送畸形 BER） | 零 crash、零 hang、RSS 不成長 |
| PROTOS c06-snmpv1 | Oulu 大學的 SNMP 語料庫 | 同上 |

補充項目（本文件新增）：

| 項目 | 方法 |
|---|---|
| 前置解析閘門有效性 | 白名單外來源必須零回應；`tests/test_preauth_gate.py` 27 例對抗式測試 |
| 未認證封包風暴 | CPU 不超標、RSS 不成長、正常 manager 仍在 SLA 內 |
| 回應大小 | 所有回應 < 1400 bytes，無 IP 分片 |

---

## 5. Windows 平台專屬檢查

這些是 SAST 工具看不到、但資安稽核一定會問的：

| 項目 | 工具 / 方法 | 為什麼 |
|---|---|---|
| **Authenticode 簽章** | `signtool verify /pa /v`、Sysinternals `sigcheck` | 憑證到位前，此項用於確認自行簽章後的結果；見[程式碼簽章](https://jasoncheng7115.github.io/jt-snmpd/code-signing_zh-TW.html) |
| **unquoted service path** | `sc qc jt-snmpd` 檢查 binPath 有引號 | 最常被稽核抓到的 finding。預設安裝路徑本身含空白 |
| **服務帳號與特權** | `sc qprivs jt-snmpd` | 驗證特權縮減生效|
| **檔案 / 目錄 ACL** | Sysinternals `accesschk -d` | `C:\ProgramData` 預設允許 Users 建子目錄，攻擊者可搶先建立 |
| **弱 ACL 提權路徑** | **PowerUp** / **PrivescCheck** | 專門掃 Windows 服務提權路徑，會同時檢查上面三項 |
| **DLL 劫持** | 確認 one-folder（非 one-file）；`Process Monitor` 觀察載入路徑 | one-file 會解壓到 `%TEMP%` 執行 |
| **Defender / EDR 誤判** | 在啟用 Defender + HVCI 的機器上實裝並觀察隔離 | PyInstaller 產物有誤判史 |
| **記憶體完整性相容** | 啟用 HVCI 後重開機並確認服務仍運作 | 客戶端點普遍啟用 |

---

## 6. 建議的 CI 組合

```
每次 commit（快）
  ruff check --select S       # 安全規則
  mypy deploy/
  pytest tests/ -q

每次 PR（中）
  bandit -r deploy/ -f json -o reports/bandit.json
  semgrep --config p/security-audit --config p/secrets --json -o reports/semgrep.json
  pip-audit --format json -o reports/pip-audit.json
  gitleaks detect --report-format json --report-path reports/gitleaks.json
  cyclonedx-py env -o reports/sbom.json

每次 release（慢，發版前必做）
  上述全部 +
  boofuzz 24 小時對 UDP/161
  PROTOS c06-snmpv1
  Windows 平台檢查（signtool / accesschk / PrivescCheck）
  安裝測試矩陣
  30 天穩定性（major 版本）
```

## 7. 報告產出

所有工具都能輸出 JSON / SARIF。建議收斂成一份可提交的摘要：

```
reports/
├── sbom.json                 CycloneDX SBOM（相依清單，審查必附）
├── bandit.json               SAST
├── semgrep.json              SAST（含自訂規則）
├── pip-audit.json            相依 CVE
├── gitleaks.json             secret 掃描
├── fuzzing-summary.txt       boofuzz 24h 結果
├── windows-checks.txt        簽章 / ACL / 特權 / unquoted path
└── SECURITY-REPORT.md        以上的人類可讀摘要，附通過 / 未通過判定
```

**判定原則**：SAST 的 High/Critical 必須為零或有書面例外說明；
相依 CVE 的 High 以上必須為零；fuzzing 必須零 crash。
未達標即不得出貨（`TEST_PLAN.md` §10 Release Gate）。

## 8. 目前狀態，以及結果放在哪裡

**[資安檢測結果](https://jasoncheng7115.github.io/jt-snmpd/security-report_zh-TW.html)**
是當前的基線，每一條發現都附判定。摘要：Bandit HIGH 0、pip-audit 在 62 個套件中
零弱點、執行時期的相依只有兩個套件。

每次推送都會在 GitHub Actions 上跑的：

| 檢查 | 位置 |
|---|---|
| 完整測試套件 | `tests.yml`，Linux |
| 個資與機密掃描 | `tests.yml`，Linux |
| 執行檔與 MSI 建置 | `tests.yml`，Windows |
| 安裝檔產物檢查（直接讀 MSI 自己的表格） | `tests.yml`，Windows |

人工跑、尚未進 CI 的：Bandit 與 pip-audit。完全還沒跑的：Semgrep、gitleaks、
模糊測試各項，以及 Windows 平台那一批。結果頁面把這些明白列出，
而不是讓這份文件暗示它們都做過了。

---

## 相關文件

- [說明文件首頁](https://jasoncheng7115.github.io/jt-snmpd/)
- [安全性評估](https://jasoncheng7115.github.io/jt-snmpd/attack-surface_zh-TW.html)
- [程式碼簽章](https://jasoncheng7115.github.io/jt-snmpd/code-signing_zh-TW.html)
- [發版檢查清單](https://jasoncheng7115.github.io/jt-snmpd/release-checklist_zh-TW.html)

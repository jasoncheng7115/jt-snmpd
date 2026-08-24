# dist/ — 發佈成品（安裝檔）

English: [README.md](README.md)

對外交付的成品。**不進版本控制**（見 `.gitignore`），由 release 流程產生。

```
dist/
├── jt-snmpd-<版本>-x64.msi           主要交付。GPO / Intune / SCCM / msiexec
├── jt-snmpd-<版本>-x64.msi.sha256
├── jt-snmpd-<版本>.intunewin          Intune 現成包
└── jt-snmpd-<版本>-admx.zip           ADMX / ADML 原則範本
```

## 為什麼是 MSI

spec §5.4：**GPO 的軟體安裝原則上只接受 MSI**，這一條即決定選型。
MSI 另外免費提供 UpgradeCode 升級處理、安裝失敗自動回滾，
以及出現在「加入或移除程式」（客戶資產盤點看得到，`hrSWInstalledTable` 亦看得到）。

Inno Setup / NSIS 產生的 EXE 不支援 GPO 軟體安裝、無交易式 rollback，
不是同一個層級的選項。

## 交付原則

- **完全自包含**：安裝時不上網抓任何東西，所有內容（含任何第三方 binary）
  直接打進 MSI（CLAUDE.md 鐵則 6）
- **需自行處理簽章**：未簽章的檔案在 WDAC 環境無法以發行者規則放行，
  且政府標案資安審查會退件（spec §1.4）
- **Release Gate 全綠才可產出**：見 `TEST_PLAN.md` §10

## 狀態

MSI 已實作並在實機驗證：安裝、升級、移除、重裝、PURGE 移除共 40 項
生命週期檢查全綠（`tests/lifecycle.ps1`）。

每次發版會歸檔到 `dist/releases/<版本>/`，內含 MSI、`.sha256` 與
`BUILDINFO.txt`。BUILDINFO 記錄 configure / wxs / agent 三份來源的 SHA-256——
曾經發生同一台機器上有兩份 `msi-configure.ps1`、改到不被用的那份，
建置照樣成功但修正沒進 MSI，留指紋才回答得出「客戶手上那顆裡的腳本是哪一版」。

**本安裝檔未經 Authenticode 簽章**，目前沒有申請憑證的計畫。
請改以隨附的 `.sha256` 驗證完整性；WDAC / AppLocker 環境的處理方式見
https://jasoncheng7115.github.io/jt-snmpd/code-signing_zh-TW.html 。

安裝檔本身**不進 git**，改由 GitHub Release 附加。

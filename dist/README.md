# dist/ — 發佈成品（安裝檔）

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
- **必須簽章**：無 Authenticode 簽章在 WDAC 環境完全無法部署，
  且政府標案資安審查會退件（spec §1.4）
- **Release Gate 全綠才可產出**：見 `TEST_PLAN.md` §10

## 狀態

MSI 打包尚未實作（Phase 3.5）。目前僅有 `build/` 的 one-folder 產物，
以手動方式部署至測試機。

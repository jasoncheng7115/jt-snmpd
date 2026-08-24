# build/ — 建置產物（執行檔）

English: [README.md](README.md)

PyInstaller one-folder 的輸出。**不進版本控制**（見 `.gitignore`）。

```
build/
└── jt-snmpd/
    ├── jt-snmpd.exe        服務主程式（同時是 CLI 進入點）
    └── _internal/          自帶的 Python runtime、pysnmp、pywin32 等
```

## 產生方式

在 Windows 目標機上執行：

```powershell
powershell -ExecutionPolicy Bypass -File packaging\build-exe.ps1 `
    -Python C:\jtdev\Python312\python.exe `
    -Source deploy\jt_agent.py `
    -OutDir build
```

建置腳本內含三道閘門，任一失敗即 exit 1：

1. **停服務並等待行程消失** —— `Stop-Service` 回來不代表檔案句柄已釋放
2. **產物必須比來源新** —— 只驗「exe 存在」會在建置失敗時取到殘留的舊版本
3. **`--selftest`** —— 實際初始化 SNMP engine 並建立快照，攔截「exe 產出但缺資料檔」

## 為什麼是 one-folder 而非 one-file

spec §1.4 硬性規則。one-file 會把內容解壓到 `%TEMP%`（服務身分下是
`C:\Windows\Temp`）再執行，那是已知的 DLL 劫持路徑，在 WDAC / HVCI
環境也更容易被擋。

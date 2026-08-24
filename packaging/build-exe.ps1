# jt-snmpd — PyInstaller 建置腳本
#
# 注意：本檔以 **UTF-8 with BOM** 儲存。
# Windows PowerShell 5.1 在沒有 BOM 時會以系統 ANSI 代碼頁（正體中文為 cp950）
# 讀取 .ps1，UTF-8 中文註解會變亂碼並打斷語法（ParserError: UnexpectedToken）。
# 實測踩過。所有含中文的 .ps1 一律加 BOM。
#
# 為什麼要有這個檔案：建置參數手打過兩次，第二次漏掉 pysnmp 的 MIB 資料檔，
# 產出的 exe 啟動即拋 MibNotFoundError——而 **服務狀態仍顯示 Running**
# （spec §6.5 的「假活著」）。建置參數只能有一份來源。
#
# 用法：
#   powershell -ExecutionPolicy Bypass -File build-exe.ps1 -Python C:\jtdev\Python312\python.exe -Source C:\jtdev\jt_snmpd.py

param(
    [Parameter(Mandatory=$true)][string]$Python,
    [Parameter(Mandatory=$true)][string]$Source,
    [string]$OutDir = "build",
    [string]$WorkDir = "build\.pyinstaller"
)

$ErrorActionPreference = 'Continue'   # native 工具寫 stderr 不應中斷建置

$name = "jt-snmpd"

# --- 建置前：確保沒有行程佔用輸出目錄 ---------------------------------------
# jt-doc-tools v1.1.66~69 踩過同一個坑：Stop-Service 回來了不代表檔案控制代碼已釋放。
# PyInstaller 會先 rmtree 舊的 dist 目錄，控制代碼未釋放時丟
#   PermissionError: [WinError 5] ... _internal\win32\servicemanager.pyd
# 建置因此失敗，但**舊 exe 仍留在原地**——若只用 Test-Path 判定成功，
# 會誤以為建置成功而實際部署了舊版本。實測踩過。
function Wait-ForProcessGone {
    param([string]$ProcName, [int]$TimeoutSec = 30)
    $deadline = (Get-Date).AddSeconds($TimeoutSec)
    while ((Get-Date) -lt $deadline) {
        $p = Get-Process -Name $ProcName -ErrorAction SilentlyContinue
        if (-not $p) { return $true }
        Start-Sleep -Milliseconds 500
    }
    return $false
}

$svc = Get-Service -Name $name -ErrorAction SilentlyContinue
if ($svc -and $svc.Status -ne 'Stopped') {
    Write-Host "[build] 停止服務 $name ..."
    Stop-Service -Name $name -Force -ErrorAction SilentlyContinue
}
Get-Process -Name $name -ErrorAction SilentlyContinue | Stop-Process -Force -ErrorAction SilentlyContinue
if (-not (Wait-ForProcessGone -ProcName $name)) {
    Write-Host "[build] FAILED: $name 行程仍在執行，檔案控制代碼未釋放"
    exit 1
}

# 主動清掉輸出目錄，讓失敗就是失敗，不會留下舊 exe 混淆判定。
#
# 為什麼用「改名」而不是「刪除」：Windows 對**已載入為映像**的 .pyd/.dll
# 回傳 ERROR_ACCESS_DENIED（訊息是「拒絕存取路徑」而非「檔案使用中」），
# 即使服務已停止、行程已結束、服務註冊也已刪除，核心層的映像區段仍可能未回收
# ——實測時 Get-Process 列不出任何持有者，但檔案就是刪不掉。
# Windows 允許**改名**被鎖住的檔案，這也是 MSI 換檔的標準做法
# （搭配 MOVEFILE_DELAY_UNTIL_REBOOT 在重開機時清掉）。
$target = Join-Path $OutDir $name
if (Test-Path $target) {
    try {
        Remove-Item -Path $target -Recurse -Force -ErrorAction Stop
    } catch {
        $stamp = Get-Date -Format 'yyyyMMddHHmmss'
        $old = "$target.old.$stamp"
        Write-Host "[build] 舊目錄無法刪除（映像區段未回收），改名為 $old"
        Rename-Item -Path $target -NewName (Split-Path $old -Leaf) -ErrorAction Stop
    }
}
Remove-Item -Path "$name.spec" -Force -ErrorAction SilentlyContinue

# 清掉先前留下的 .old 目錄（此時通常已可刪）
Get-ChildItem -Path $OutDir -Directory -Filter "$name.old.*" -ErrorAction SilentlyContinue |
    ForEach-Object { Remove-Item $_.FullName -Recurse -Force -ErrorAction SilentlyContinue }

# spec §1.4：一律 one-folder，**禁用 one-file**。
# one-file 會把內容解壓到 %TEMP%（服務身分下是 C:\Windows\Temp）再執行，
# 那是已知的 DLL 劫持路徑，且在 WDAC/HVCI 環境更容易被擋。
$args = @(
    "-m", "PyInstaller",
    "--noconfirm",
    "--onedir",
    "--console",
    "--name", $name,
    "--distpath", $OutDir,
    "--workpath", $WorkDir,

    # pywin32 服務相依。win32timezone 是 pywin32 的已知隱藏相依，
    # 少了它服務會在啟動時 ImportError。
    "--hidden-import", "win32timezone",
    "--hidden-import", "win32serviceutil",
    "--hidden-import", "win32service",
    "--hidden-import", "win32event",
    "--hidden-import", "servicemanager",

    # pysnmp 在執行期以 **檔案** 形式載入 MIB 模組（DirMibSource 掃 .py/.pyc），
    # 不是靠 import。因此必須用 collect-all 把資料檔一併打包，
    # 只給 collect-submodules 會漏掉，症狀是啟動即 MibNotFoundError。
    "--collect-all", "pysnmp",
    "--collect-all", "pyasn1",

    # version.py / preauth.py / smbios.py / diskhealth.py 與主程式同目錄，
    # PyInstaller 的模組搜尋需要明確加入該路徑，否則 import 失敗。
    "--paths", (Split-Path -Parent $Source),

    $Source
)

Write-Host "[build] $Python $($args -join ' ')"
& $Python @args
$code = $LASTEXITCODE

$exe = Join-Path $OutDir "$name\$name.exe"
if (-not (Test-Path $exe)) {
    Write-Host "[build] FAILED: $exe 不存在 (exit=$code)"
    exit 1
}

# 只驗「exe 存在」不夠：建置失敗時舊 exe 可能還在原地。
# 必須確認產物比來源新，否則就是拿到了殘留的舊版本。
$srcTime = (Get-Item $Source).LastWriteTime
$exeTime = (Get-Item $exe).LastWriteTime
if ($exeTime -lt $srcTime) {
    Write-Host "[build] FAILED: exe ($exeTime) 比來源 ($srcTime) 舊 —— 建置未實際執行"
    exit 1
}

$files = (Get-ChildItem (Join-Path $OutDir $name) -Recurse -File | Measure-Object).Count
$mb = [math]::Round(((Get-ChildItem (Join-Path $OutDir $name) -Recurse -File |
        Measure-Object -Property Length -Sum).Sum / 1MB), 1)

Write-Host "[build] OK exe=$exe files=$files size=${mb}MB"

# 建置後煙霧測試：直接跑一次 snapshot 建立，確認打包完整。
# 只驗 exe 存在是不夠的——MIB 資料檔漏打包時 exe 照樣產出。
Write-Host "[build] 煙霧測試..."
$smoke = & $exe --selftest 2>&1 | Out-String
if ($smoke -match "SELFTEST_OK") {
    Write-Host "[build] 煙霧測試通過"
} else {
    Write-Host "[build] 煙霧測試失敗："
    Write-Host $smoke
    exit 1
}

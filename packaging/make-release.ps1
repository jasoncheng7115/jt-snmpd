# jt-snmpd 發佈包建置腳本
#
# 注意：本檔以 UTF-8 with BOM 儲存（PowerShell 5.1 無 BOM 時以 cp950 讀取）。
#
# 產生 dist/jt-snmpd-<版本>-x64/ 與同名 .zip，內容：
#   install.ps1        安裝程式
#   jt-snmpd/          PyInstaller one-folder 產物
#   README.txt         給管理員的簡要說明
#   VERSION            版本資訊
#
# 這是 MSI 之前的過渡發佈格式。完全自包含，安裝時不上網抓任何東西。

param(
    [string]$Version = "0.1.0",
    [string]$BuildDir = "build\jt-snmpd",
    [string]$OutDir = "dist"
)

$ErrorActionPreference = 'Continue'

if (-not (Test-Path (Join-Path $BuildDir 'jt-snmpd.exe'))) {
    Write-Host "[FAIL] 找不到 $BuildDir\jt-snmpd.exe，請先執行 build-exe.ps1" -ForegroundColor Red
    exit 1
}

$name = "jt-snmpd-$Version-x64"
$stage = Join-Path $OutDir $name
if (Test-Path $stage) { Remove-Item $stage -Recurse -Force -ErrorAction SilentlyContinue }
New-Item -ItemType Directory -Force $stage | Out-Null

Copy-Item $BuildDir (Join-Path $stage 'jt-snmpd') -Recurse -Force
Copy-Item (Join-Path $PSScriptRoot 'install.ps1') $stage -Force

$commit = (& git rev-parse --short HEAD 2>$null)
@(
    "jt-snmpd $Version"
    "built    $(Get-Date -Format 'yyyy-MM-dd HH:mm:ss')"
    "commit   $commit"
    "host     $env:COMPUTERNAME"
) | Set-Content (Join-Path $stage 'VERSION') -Encoding UTF8

@(
    "JT SNMP Agent $Version"
    ""
    "安裝（需系統管理員權限）："
    "  powershell -ExecutionPolicy Bypass -File install.ps1 -ManagementNetworks 192.168.1.0/24"
    ""
    "解除安裝："
    "  powershell -ExecutionPolicy Bypass -File install.ps1 -Uninstall"
    "  加上 -Purge 可一併清除設定與狀態檔"
    ""
    "安裝程式會："
    "  - 偵測 Windows 內建 SNMP Service，沿用其 community / 管理主機 / sysContact / sysLocation"
    "  - 停用內建 SNMP Service（不移除功能，解除安裝時自動還原）"
    "  - 建立僅限管理網段的防火牆規則（預設 deny，不允許 Any/Any）"
    "  - 啟動後做 loopback SNMP 自我測試，確認不是「服務 Running 但不回應」"
    ""
    "本程式安裝後不會主動對外連線：不檢查更新、不回報遙測、不下載任何內容。"
) | Set-Content (Join-Path $stage 'README.txt') -Encoding UTF8

$zip = Join-Path $OutDir "$name.zip"
if (Test-Path $zip) { Remove-Item $zip -Force }
Compress-Archive -Path "$stage\*" -DestinationPath $zip -Force

$sha = (Get-FileHash $zip -Algorithm SHA256).Hash.ToLower()
"$sha  $name.zip" | Set-Content "$zip.sha256" -Encoding ASCII

$mb = [math]::Round((Get-Item $zip).Length / 1MB, 1)
Write-Host "[OK] $zip (${mb} MB)" -ForegroundColor Green
Write-Host "[OK] SHA256 $sha"

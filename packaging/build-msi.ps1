# jt-snmpd MSI 建置腳本（WiX v5）
#
# 注意：本檔以 UTF-8 with BOM 儲存（PowerShell 5.1 無 BOM 時以 cp950 讀取）。
#
# 為什麼要 MSI（spec §5.4）：GPO 的軟體安裝原則上只接受 MSI。
# 這是「客戶能不能用 AD 派送」的唯一決定因素。
#
# 前置需求（建置機，不是目標機）：
#   .NET SDK 8+          C:\jtdev\dotnet 或標準路徑
#   wix 5.x              dotnet tool install --global wix --version 5.*
#
# 用法：
#   powershell -ExecutionPolicy Bypass -File build-msi.ps1 -Version 0.1.0

param(
    # 不給則從 deploy/version.py 讀取——版本只有一個來源（見該檔說明）
    [string]$Version  = "",
    [string]$BuildDir = "build\jt-snmpd",
    [string]$OutDir   = "dist",
    [string]$DotnetRoot = "C:\jtdev\dotnet"
)

$ErrorActionPreference = 'Continue'

# --- 原始碼位置：無條件解析（BUILDINFO 的來源指紋也要用）-------------------
$verFile = Join-Path (Split-Path -Parent $PSScriptRoot) 'deploy\version.py'
if (-not (Test-Path $verFile)) { $verFile = Join-Path $PSScriptRoot 'version.py' }
if (-not (Test-Path $verFile)) { $verFile = 'C:\jtdev\version.py' }
$SrcDir = if (Test-Path $verFile) { Split-Path -Parent $verFile } else { $PSScriptRoot }

# --- 版本：單一來源 ---------------------------------------------------------
if (-not $Version) {
    if (Test-Path $verFile) {
        $m = Select-String -Path $verFile -Pattern '^VERSION\s*=\s*"([^"]+)"'
        if ($m) { $Version = $m.Matches[0].Groups[1].Value }
    }
    if (-not $Version) {
        Write-Host "[FAIL] 無法從 version.py 取得版本，且未以 -Version 指定" -ForegroundColor Red
        exit 1
    }
    Write-Host "[*] 版本取自 version.py: $Version"
}

# --- 前置檢查 ---------------------------------------------------------------
if (-not (Test-Path (Join-Path $BuildDir 'jt-snmpd.exe'))) {
    Write-Host "[FAIL] 找不到 $BuildDir\jt-snmpd.exe，請先執行 build-exe.ps1" -ForegroundColor Red
    exit 1
}

if (Test-Path (Join-Path $DotnetRoot 'dotnet.exe')) {
    $env:DOTNET_ROOT = $DotnetRoot
    $env:PATH = "$DotnetRoot;$env:PATH"
}
$wix = Join-Path $env:USERPROFILE '.dotnet\tools\wix.exe'
if (-not (Test-Path $wix)) {
    $c = Get-Command wix -ErrorAction SilentlyContinue
    if ($c) { $wix = $c.Source } else {
        Write-Host "[FAIL] 找不到 wix.exe。請執行：dotnet tool install --global wix --version 5.*" -ForegroundColor Red
        exit 1
    }
}
$env:DOTNET_CLI_TELEMETRY_OPTOUT = '1'
$env:DOTNET_NOLOGO = '1'
Write-Host "[*] WiX $(& $wix --version)"

New-Item -ItemType Directory -Force $OutDir | Out-Null
$work = Join-Path $OutDir '.wix'
Remove-Item $work -Recurse -Force -ErrorAction SilentlyContinue
New-Item -ItemType Directory -Force $work | Out-Null

# --- 產生檔案清單 -----------------------------------------------------------
# PyInstaller one-folder 有兩百多個檔案，手寫元件清單不可能維護。
# wix 的 harvest 在 v5 已移除，改為自行產生 .wxs 片段。
# 每個檔案一個 Component（MSI 的最佳實務：一檔一元件，
# 讓修補與升級能以檔案為單位處理）。
Write-Host "[*] 產生檔案清單..."
$root = (Resolve-Path $BuildDir).Path
$files = Get-ChildItem $root -Recurse -File
$dirNodes = @{}
$components = New-Object System.Collections.Generic.List[string]
$dirDefs = New-Object System.Collections.Generic.List[string]

function Get-SafeId {
    param($s, $prefix)
    # MSI 的 Id 只能是 A-Za-z0-9._，且不能以數字開頭
    $clean = ($s -replace '[^A-Za-z0-9._]', '_')
    if ($clean.Length -gt 60) {
        $hash = [BitConverter]::ToString(
            [Security.Cryptography.MD5]::Create().ComputeHash(
                [Text.Encoding]::UTF8.GetBytes($s))).Replace('-','').Substring(0,8)
        $clean = $clean.Substring(0, 50) + '_' + $hash
    }
    return "$prefix$clean"
}

# 先建立目錄結構
$dirs = $files | ForEach-Object { $_.DirectoryName } | Sort-Object -Unique
foreach ($d in $dirs) {
    $rel = $d.Substring($root.Length).TrimStart('\')
    if (-not $rel) { $dirNodes[''] = 'INSTALLFOLDER'; continue }
    $parts = $rel -split '\\'
    $acc = ''
    $parentId = 'INSTALLFOLDER'
    foreach ($p in $parts) {
        $acc = if ($acc) { "$acc\$p" } else { $p }
        if ($dirNodes.ContainsKey($acc)) { $parentId = $dirNodes[$acc]; continue }
        $id = Get-SafeId $acc 'dir_'
        $dirNodes[$acc] = $id
        $dirDefs.Add("    <DirectoryRef Id=""$parentId""><Directory Id=""$id"" Name=""$p"" /></DirectoryRef>")
        $parentId = $id
    }
}

foreach ($f in $files) {
    $relDir = $f.DirectoryName.Substring($root.Length).TrimStart('\')
    $dirId = if ($relDir) { $dirNodes[$relDir] } else { 'INSTALLFOLDER' }
    $relPath = $f.FullName.Substring($root.Length).TrimStart('\')
    $cid = Get-SafeId $relPath 'c_'
    $fid = Get-SafeId $relPath 'f_'
    $src = $f.FullName.Replace('&', '&amp;')
    $components.Add(@"
      <Component Id="$cid" Directory="$dirId" Guid="*">
        <File Id="$fid" Source="$src" KeyPath="yes" />
      </Component>
"@)
}

# MSI 自訂動作腳本本身也要進安裝目錄
$cfgScript = Join-Path $PSScriptRoot 'msi-configure.ps1'
if (-not (Test-Path $cfgScript)) {
    Write-Host "[FAIL] 找不到 msi-configure.ps1" -ForegroundColor Red
    exit 1
}
$components.Add(@"
      <Component Id="c_msi_configure" Directory="INSTALLFOLDER" Guid="*">
        <File Id="f_msi_configure" Source="$($cfgScript.Replace('&','&amp;'))" KeyPath="yes" />
      </Component>
"@)

$frag = @"
<?xml version="1.0" encoding="UTF-8"?>
<Wix xmlns="http://wixtoolset.org/schemas/v4/wxs">
  <Fragment>
$($dirDefs -join "`n")
    <ComponentGroup Id="AgentFiles">
$($components -join "`n")
    </ComponentGroup>
  </Fragment>
</Wix>
"@
$fragPath = Join-Path $work 'files.wxs'
[IO.File]::WriteAllText($fragPath, $frag, (New-Object Text.UTF8Encoding $false))
Write-Host "[*] $($files.Count + 1) 個檔案，$($dirDefs.Count) 個目錄"

# --- 圖示 -------------------------------------------------------------------
# 這裡曾經產生一個 16x16 的空白 ICO 佔位，於是「加入或移除程式」裡的項目
# 是一片空白——在客戶的資產盤點畫面上，那看起來像是安裝到一半的東西。
$icon = Join-Path $work 'app.ico'
$brandIcon = Join-Path (Split-Path -Parent $PSScriptRoot) 'docs\brand\jt-snmpd.ico'
if (Test-Path $brandIcon) {
    Copy-Item $brandIcon $icon -Force
    Write-Host "[*] 圖示：$brandIcon"
} else {
    # 找不到時仍要產出可用的 MSI，但要講出來，不要無聲地回到空白圖示
    Write-Host "[!] 找不到 $brandIcon，改用空白佔位圖示" -ForegroundColor Yellow
    $ico = [byte[]](0,0,1,0,1,0,16,16,0,0,1,0,32,0,64,0,0,0,22,0,0,0)
    $ico += ,0 * 64
    [IO.File]::WriteAllBytes($icon, $ico)
}

# --- 建置 -------------------------------------------------------------------
$msi = Join-Path $OutDir "jt-snmpd-$Version-x64.msi"
Write-Host "[*] wix build ..."
& $wix build `
    -arch x64 `
    -d "ProductVersion=$Version" `
    -d "IconFile=$icon" `
    -ext WixToolset.Util.wixext `
    -o $msi `
    (Join-Path $PSScriptRoot 'wix\jt-snmpd.wxs') `
    $fragPath
$code = $LASTEXITCODE

if (-not (Test-Path $msi)) {
    Write-Host "[FAIL] MSI 未產出 (exit=$code)" -ForegroundColor Red
    exit 1
}

$sha = (Get-FileHash $msi -Algorithm SHA256).Hash.ToLower()
"$sha  $(Split-Path $msi -Leaf)" | Set-Content "$msi.sha256" -Encoding ASCII

# --- 版本歸檔 ---------------------------------------------------------------
# 每個發佈版本的安裝檔都要留存：客戶回報問題時必須能取得「他手上那一版」，
# 而不是只有最新版。倒回、重現、資安稽核都需要。
$archive = Join-Path $OutDir "releases\$Version"
New-Item -ItemType Directory -Force $archive | Out-Null
Copy-Item $msi $archive -Force
Copy-Item "$msi.sha256" $archive -Force
$commit = (& git rev-parse --short HEAD 2>$null)
$mb = [math]::Round((Get-Item $msi).Length / 1MB, 1)

# 來源指紋：曾經在同一台機器上出現兩份 msi-configure.ps1（根目錄與 packaging\），
# 而 $PSScriptRoot 決定用哪份——改到不被用的那份，修正完全不會進 MSI，
# 但建置照樣成功、版本號照樣更新。留下雜湊，事後才回答得出
# 「客戶手上那顆 MSI 裡的設定腳本到底是哪一版」。
function SrcHash { param($p)
    if (Test-Path $p) { (Get-FileHash $p -Algorithm SHA256).Hash.Substring(0,16) } else { 'absent' } }
@(
    "product   jt-snmpd"
    "version   $Version"
    "built     $(Get-Date -Format 'yyyy-MM-dd HH:mm:ss')"
    "builder   $env:COMPUTERNAME / $env:USERNAME"
    "commit    $commit"
    "sha256    $sha"
    "files     $(@($files).Count) 個檔案"
    "size      ${mb} MB"
    ""
    "-- 來源指紋 (SHA256 前 16 碼) --"
    "configure $(SrcHash $cfgScript)"
    "wxs       $(SrcHash (Join-Path $PSScriptRoot 'wix\jt-snmpd.wxs'))"
    "agent     $(SrcHash (Join-Path $SrcDir 'jt_agent.py'))"
) | Set-Content (Join-Path $archive 'BUILDINFO.txt') -Encoding UTF8
Write-Host "[OK] 已歸檔至 $archive"
Write-Host "[OK] $msi (${mb} MB)" -ForegroundColor Green
Write-Host "[OK] SHA256 $sha"
Write-Host ""
Write-Host "GPO / 手動安裝："
Write-Host "  msiexec /i `"$(Split-Path $msi -Leaf)`" /qn MANAGEMENTNETWORKS=192.168.1.0/24"
Write-Host "解除安裝："
Write-Host "  msiexec /x `"$(Split-Path $msi -Leaf)`" /qn"

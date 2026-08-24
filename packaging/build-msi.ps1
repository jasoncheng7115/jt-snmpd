# jt-snmpd MSI build script (WiX v5)
#
# Note: saved as UTF-8 with BOM; without one PowerShell 5.1 reads the file using
# the system ANSI code page.
#
# Why MSI (spec §5.4): Group Policy software installation accepts MSI and
# nothing else. That single fact decides whether a customer can deploy this
# through Active Directory at all.
#
# Prerequisites (on the build machine, not the target):
#   .NET SDK 8+          at C:\jtdev\dotnet or on the standard path
#   wix 5.x              dotnet tool install --global wix --version 5.*
#
# Usage:
#   powershell -ExecutionPolicy Bypass -File build-msi.ps1 -Version 0.1.0

param(
    # Omit to read deploy/version.py — there is one source for the version
    # (see that file)
    [string]$Version  = "",
    [string]$BuildDir = "build\jt-snmpd",
    [string]$OutDir   = "dist",
    [string]$DotnetRoot = "C:\jtdev\dotnet"
)

$ErrorActionPreference = 'Continue'

# --- Source location: resolved unconditionally (BUILDINFO fingerprints need it)
$verFile = Join-Path (Split-Path -Parent $PSScriptRoot) 'deploy\version.py'
if (-not (Test-Path $verFile)) { $verFile = Join-Path $PSScriptRoot 'version.py' }
if (-not (Test-Path $verFile)) { $verFile = 'C:\jtdev\version.py' }
$SrcDir = if (Test-Path $verFile) { Split-Path -Parent $verFile } else { $PSScriptRoot }

# --- Version: single source --------------------------------------------------
if (-not $Version) {
    if (Test-Path $verFile) {
        $m = Select-String -Path $verFile -Pattern '^VERSION\s*=\s*"([^"]+)"'
        if ($m) { $Version = $m.Matches[0].Groups[1].Value }
    }
    if (-not $Version) {
        Write-Host "[FAIL] could not read the version from version.py and none was given with -Version" -ForegroundColor Red
        exit 1
    }
    Write-Host "[*] version from version.py: $Version"
}

# --- Pre-checks --------------------------------------------------------------
if (-not (Test-Path (Join-Path $BuildDir 'jt-snmpd.exe'))) {
    Write-Host "[FAIL] $BuildDir\jt-snmpd.exe not found; run build-exe.ps1 first" -ForegroundColor Red
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
        Write-Host "[FAIL] wix.exe not found. Run:" -ForegroundColor Red
        Write-Host "         dotnet tool install --global wix --version 5.*"
        Write-Host "         wix extension add -g WixToolset.Util.wixext"
        Write-Host "         wix extension add -g WixToolset.UI.wixext"
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

# --- Generate the file list --------------------------------------------------
# A PyInstaller one-folder output has over two hundred files, and a hand-written
# component list is not maintainable. WiX v5 removed harvesting, so the .wxs
# fragment is generated here instead.
#
# One Component per file, which is MSI best practice: patching and upgrading can
# then work file by file.
Write-Host "[*] generating the file list ..."
$root = (Resolve-Path $BuildDir).Path
$files = Get-ChildItem $root -Recurse -File
$dirNodes = @{}
$components = New-Object System.Collections.Generic.List[string]
$dirDefs = New-Object System.Collections.Generic.List[string]

function Get-SafeId {
    param($s, $prefix)
    # An MSI Id may only contain A-Za-z0-9._ and cannot start with a digit
    $clean = ($s -replace '[^A-Za-z0-9._]', '_')
    if ($clean.Length -gt 60) {
        $hash = [BitConverter]::ToString(
            [Security.Cryptography.MD5]::Create().ComputeHash(
                [Text.Encoding]::UTF8.GetBytes($s))).Replace('-','').Substring(0,8)
        $clean = $clean.Substring(0, 50) + '_' + $hash
    }
    return "$prefix$clean"
}

# Build the directory structure first
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

# The custom action script itself also goes into the installation directory
$cfgScript = Join-Path $PSScriptRoot 'msi-configure.ps1'
if (-not (Test-Path $cfgScript)) {
    Write-Host "[FAIL] msi-configure.ps1 not found" -ForegroundColor Red
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
Write-Host "[*] $($files.Count + 1) files, $($dirDefs.Count) directories"

# --- Artefact freshness gate -------------------------------------------------
# build-msi packages whatever is already in build\; it does not build anything
# itself. Forget to run build-exe first, or have build-exe fail with its output
# swallowed, and this packages **the old exe** into an MSI carrying the new
# version number — an installer that looks new with old code inside. This
# happened: a fix to the idempotent config load never reached the MSI, while the
# version number, SHA-256 and archive directory all said it had.
#
# build-exe.ps1 has had this gate for a while, but it protects the exe. The MSI
# layer was a separate gap.
$exePath = Join-Path $BuildDir 'jt-snmpd.exe'
if (-not (Test-Path $exePath)) {
    Write-Host "[FAIL] $exePath not found; run build-exe.ps1 first" -ForegroundColor Red
    exit 1
}
$exeTime = (Get-Item $exePath).LastWriteTime
$newestSrc = Get-ChildItem $SrcDir -Filter *.py -ErrorAction SilentlyContinue |
             Sort-Object LastWriteTime -Descending | Select-Object -First 1
if ($newestSrc -and $newestSrc.LastWriteTime -gt $exeTime) {
    Write-Host "[FAIL] the executable is older than the source, so this would package stale code:" -ForegroundColor Red
    Write-Host "         exe  $($exeTime.ToString('yyyy-MM-dd HH:mm:ss'))  $exePath"
    Write-Host "         source $($newestSrc.LastWriteTime.ToString('yyyy-MM-dd HH:mm:ss'))  $($newestSrc.Name)"
    Write-Host "       Run build-exe.ps1 again first."
    exit 1
}

# --- Icon --------------------------------------------------------------------
# This used to generate a blank 16x16 placeholder ICO, which left the Add/Remove
# Programs entry with no icon at all — on a customer's asset inventory screen
# that looks like a half-finished installation.
$icon = Join-Path $work 'app.ico'
$brandIcon = Join-Path (Split-Path -Parent $PSScriptRoot) 'docs\brand\jt-snmpd.ico'
if (Test-Path $brandIcon) {
    Copy-Item $brandIcon $icon -Force
    Write-Host "[*] icon: $brandIcon"
} else {
    # Still produce a usable MSI if it is missing, but say so rather than
    # quietly falling back to a blank icon
    Write-Host "[!] $brandIcon not found; using a blank placeholder icon" -ForegroundColor Yellow
    $ico = [byte[]](0,0,1,0,1,0,16,16,0,0,1,0,32,0,64,0,0,0,22,0,0,0)
    $ico += ,0 * 64
    [IO.File]::WriteAllBytes($icon, $ico)
}

# --- Build -------------------------------------------------------------------
$msi = Join-Path $OutDir "jt-snmpd-$Version-x64.msi"
Write-Host "[*] wix build ..."
& $wix build `
    -arch x64 `
    -d "ProductVersion=$Version" `
    -d "IconFile=$icon" `
    -ext WixToolset.Util.wixext `
    -ext WixToolset.UI.wixext `
    -o $msi `
    (Join-Path $PSScriptRoot 'wix\jt-snmpd.wxs') `
    $fragPath
$code = $LASTEXITCODE

# A failure is a failure. This used to check only that the file existed, so a
# failed wix build picked up **the previous** MSI and printed [OK] with the old
# version number. That happened: a missing WiX extension failed the build, and
# the script reported success and archived an old installer. Same class of
# problem as the artefact freshness issue build-exe.ps1 already fixed.
if ($code -ne 0) {
    Write-Host "[FAIL] wix build failed (exit=$code)" -ForegroundColor Red
    exit 1
}
if (-not (Test-Path $msi)) {
    Write-Host "[FAIL] no MSI was produced (exit=$code)" -ForegroundColor Red
    exit 1
}
# Even with exit code 0, confirm this MSI came from this build and is not a
# leftover
$age = (Get-Date) - (Get-Item $msi).LastWriteTime
if ($age.TotalMinutes -gt 10) {
    Write-Host "[FAIL] $msi is not from this build (last written $([int]$age.TotalMinutes) minutes ago)" -ForegroundColor Red
    exit 1
}

$sha = (Get-FileHash $msi -Algorithm SHA256).Hash.ToLower()
"$sha  $(Split-Path $msi -Leaf)" | Set-Content "$msi.sha256" -Encoding ASCII

# --- Per-version archive -----------------------------------------------------
# Every released installer is kept: when a customer reports a problem, the exact
# build they are running has to be obtainable, not just the latest one. Rolling
# back, reproducing and security audits all need it.
$archive = Join-Path $OutDir "releases\$Version"
New-Item -ItemType Directory -Force $archive | Out-Null
Copy-Item $msi $archive -Force
Copy-Item "$msi.sha256" $archive -Force
$commit = (& git rev-parse --short HEAD 2>$null)
$mb = [math]::Round((Get-Item $msi).Length / 1MB, 1)

# Source fingerprints: one machine once held two copies of msi-configure.ps1
# (the repository root and packaging\), and $PSScriptRoot decided which was used.
# Editing the one that was not used meant the fix never reached the MSI, while
# the build succeeded and the version number advanced as usual. The hashes are
# how "which version of the configure script is inside the package the customer
# is holding?" becomes answerable.
function SrcHash { param($p)
    if (Test-Path $p) { (Get-FileHash $p -Algorithm SHA256).Hash.Substring(0,16) } else { 'absent' } }
@(
    "product   jt-snmpd"
    "version   $Version"
    "built     $(Get-Date -Format 'yyyy-MM-dd HH:mm:ss')"
    "builder   $env:COMPUTERNAME / $env:USERNAME"
    "commit    $commit"
    "sha256    $sha"
    "files     $(@($files).Count) files"
    "size      ${mb} MB"
    ""
    "-- source fingerprints (first 16 hex of SHA-256) --"
    "configure $(SrcHash $cfgScript)"
    "wxs       $(SrcHash (Join-Path $PSScriptRoot 'wix\jt-snmpd.wxs'))"
    "agent     $(SrcHash (Join-Path $SrcDir 'jt_agent.py'))"
) | Set-Content (Join-Path $archive 'BUILDINFO.txt') -Encoding UTF8
Write-Host "[OK] archived to $archive"
Write-Host "[OK] $msi (${mb} MB)" -ForegroundColor Green
Write-Host "[OK] SHA256 $sha"
Write-Host ""
Write-Host "GPO or manual installation:"
Write-Host "  msiexec /i `"$(Split-Path $msi -Leaf)`" /qn MANAGEMENTNETWORKS=192.168.1.0/24"
Write-Host "Uninstall:"
Write-Host "  msiexec /x `"$(Split-Path $msi -Leaf)`" /qn"

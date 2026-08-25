# jt-snmpd release package builder
#
# Note: saved as UTF-8 with BOM. Without it PowerShell 5.1 reads the file using
# the system ANSI code page.
#
# Produces dist/jt-snmpd-<version>-x64/ and a .zip of the same name, containing:
#   install.ps1        the installer
#   jt-snmpd/          the PyInstaller one-folder output
#   README.txt         a short note for the administrator
#   VERSION            version information
#
# This is the interim format that predates the MSI. Fully self-contained: the
# installer downloads nothing.

param(
    [string]$Version = "0.1.0",
    [string]$BuildDir = "build\jt-snmpd",
    [string]$OutDir = "dist"
)

$ErrorActionPreference = 'Continue'

if (-not (Test-Path (Join-Path $BuildDir 'jt-snmpd.exe'))) {
    Write-Host "[FAIL] $BuildDir\jt-snmpd.exe not found; run build-exe.ps1 first" -ForegroundColor Red
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
    "jt-snmpd $Version"
    ""
    "Install (requires administrator rights):"
    "  powershell -ExecutionPolicy Bypass -File install.ps1 -ManagementNetworks 192.168.1.0/24"
    ""
    "Uninstall:"
    "  powershell -ExecutionPolicy Bypass -File install.ps1 -Uninstall"
    "  Add -Purge to remove the configuration and state files as well"
    ""
    "The installer will:"
    "  - detect the built-in Windows SNMP Service and carry over its community,"
    "    permitted managers, sysContact and sysLocation"
    "  - disable the built-in SNMP Service (disabled, not removed; restored on uninstall)"
    "  - create a firewall rule scoped to the management networks (deny by default, never Any/Any)"
    "  - run a loopback SNMP self-test after starting, to confirm the service is"
    "    answering rather than merely reporting Running"
    ""
    "Once installed this program makes no outbound connections: no update checks,"
    "no telemetry, no downloads."
) | Set-Content (Join-Path $stage 'README.txt') -Encoding UTF8

$zip = Join-Path $OutDir "$name.zip"
if (Test-Path $zip) { Remove-Item $zip -Force }
Compress-Archive -Path "$stage\*" -DestinationPath $zip -Force

$sha = (Get-FileHash $zip -Algorithm SHA256).Hash.ToLower()
"$sha  $name.zip" | Set-Content "$zip.sha256" -Encoding ASCII

$mb = [math]::Round((Get-Item $zip).Length / 1MB, 1)
Write-Host "[OK] $zip (${mb} MB)" -ForegroundColor Green
Write-Host "[OK] SHA256 $sha"

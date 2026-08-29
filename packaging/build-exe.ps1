# jt-snmpd — PyInstaller build script
#
# Note: saved as **UTF-8 with BOM**. Without a BOM, Windows PowerShell 5.1 reads
# a .ps1 using the system ANSI code page, which mangles any non-ASCII content and
# breaks parsing (ParserError: UnexpectedToken). This happened.
#
# Why this file exists: the build arguments were typed by hand twice, and the
# second time pysnmp's MIB data files were left out. The resulting exe raised
# MibNotFoundError on startup while **the service still reported Running** (the
# "alive but dead" case). There can be only one source for the build
# arguments.
#
# Usage:
#   powershell -ExecutionPolicy Bypass -File build-exe.ps1 -Python C:\jtdev\Python312\python.exe -Source C:\jtdev\jt_snmpd.py

param(
    [Parameter(Mandatory=$true)][string]$Python,
    [Parameter(Mandatory=$true)][string]$Source,
    [string]$OutDir = "build",
    [string]$WorkDir = "build\.pyinstaller"
)

$ErrorActionPreference = 'Continue'   # native tools write to stderr; that must
                                      # not abort the build

$name = "jt-snmpd"

# --- Before building: make sure nothing holds the output directory ----------
# The same trap caught jt-doc-tools v1.1.66-69: Stop-Service returning does not
# mean the file handles have been released. PyInstaller rmtree's the old dist
# directory first, and with handles still open that raises
#   PermissionError: [WinError 5] ... _internal\win32\servicemanager.pyd
# The build then fails, but **the old exe is still there** — and judging success
# by Test-Path alone reports a successful build while deploying the previous
# version. This happened.
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
    Write-Host "[build] stopping service $name ..."
    Stop-Service -Name $name -Force -ErrorAction SilentlyContinue
}
Get-Process -Name $name -ErrorAction SilentlyContinue | Stop-Process -Force -ErrorAction SilentlyContinue
if (-not (Wait-ForProcessGone -ProcName $name)) {
    Write-Host "[build] FAILED: the $name process is still running; file handles not released"
    exit 1
}

# Clear the output directory deliberately, so a failure is a failure and no old
# exe is left behind to confuse the verdict.
#
# Why rename rather than delete: for a .pyd or .dll **already loaded as an
# image**, Windows returns ERROR_ACCESS_DENIED — reported as "access to the path
# is denied" rather than "the file is in use". Even with the service stopped, the
# process gone and the service registration deleted, the kernel image section may
# not have been reclaimed; measured, Get-Process listed no holder at all and the
# file still could not be deleted.
#
# Windows does allow a locked file to be **renamed**, which is also how MSI
# replaces files (with MOVEFILE_DELAY_UNTIL_REBOOT to clean up at the next boot).
$target = Join-Path $OutDir $name
if (Test-Path $target) {
    try {
        Remove-Item -Path $target -Recurse -Force -ErrorAction Stop
    } catch {
        $stamp = Get-Date -Format 'yyyyMMddHHmmss'
        $old = "$target.old.$stamp"
        Write-Host "[build] old directory could not be deleted (image section still held); renamed to $old"
        Rename-Item -Path $target -NewName (Split-Path $old -Leaf) -ErrorAction Stop
    }
}
Remove-Item -Path "$name.spec" -Force -ErrorAction SilentlyContinue

# Clean up .old directories left by earlier builds (usually deletable by now)
Get-ChildItem -Path $OutDir -Directory -Filter "$name.old.*" -ErrorAction SilentlyContinue |
    ForEach-Object { Remove-Item $_.FullName -Recurse -Force -ErrorAction SilentlyContinue }

# always one-folder, **never one-file**. one-file extracts itself into
# %TEMP% (C:\Windows\Temp under the service account) before executing, which is a
# known DLL hijacking path and more likely to be blocked under WDAC and HVCI.
$args = @(
    "-m", "PyInstaller",
    "--noconfirm",
    "--onedir",
    "--console",
    "--name", $name,
    "--distpath", $OutDir,
    "--workpath", $WorkDir,

    # pywin32 service dependencies. win32timezone is a known hidden dependency of
    # pywin32; without it the service raises ImportError at startup.
    "--hidden-import", "win32timezone",
    "--hidden-import", "win32serviceutil",
    "--hidden-import", "win32service",
    "--hidden-import", "win32event",
    "--hidden-import", "servicemanager",
    # Reached only through _write_event's lazy import, so PyInstaller's static
    # analysis does not see it. Without these the Event Log write fails on every
    # start: harmless by design, but it would silently remove the copy of our
    # errors that field staff and Get-WinEvent actually look at.
    "--hidden-import", "win32evtlog",
    "--hidden-import", "win32evtlogutil",

    # pysnmp loads MIB modules as **files** at runtime (DirMibSource scans for
    # .py/.pyc), not through import. So collect-all is required to bundle the
    # data files; collect-submodules alone misses them, and the symptom is
    # MibNotFoundError on startup.
    "--collect-all", "pysnmp",
    # Embed the icon in the exe: the service list, Task Manager and Explorer all
    # show it, and a service without one is unrecognisable in a long list of
    # system services
    "--icon", (Join-Path (Split-Path -Parent $PSScriptRoot) "docs\brand\jt-snmpd.ico"),
    "--collect-all", "pyasn1",

    # version.py, preauth.py, smbios.py and diskhealth.py sit beside the main
    # script, and PyInstaller's module search needs that path added explicitly or
    # the imports fail.
    "--paths", (Split-Path -Parent $Source),

    $Source
)

Write-Host "[build] $Python $($args -join ' ')"
& $Python @args
$code = $LASTEXITCODE

$exe = Join-Path $OutDir "$name\$name.exe"
if (-not (Test-Path $exe)) {
    Write-Host "[build] FAILED: $exe does not exist (exit=$code)"
    exit 1
}

# "The exe exists" is not enough: after a failed build the old one may still be
# there. The artefact has to be newer than the source, or this is a leftover.
$srcTime = (Get-Item $Source).LastWriteTime
$exeTime = (Get-Item $exe).LastWriteTime
if ($exeTime -lt $srcTime) {
    Write-Host "[build] FAILED: the exe ($exeTime) is older than the source ($srcTime) - the build did not run"
    exit 1
}

$files = (Get-ChildItem (Join-Path $OutDir $name) -Recurse -File | Measure-Object).Count
$mb = [math]::Round(((Get-ChildItem (Join-Path $OutDir $name) -Recurse -File |
        Measure-Object -Property Length -Sum).Sum / 1MB), 1)

Write-Host "[build] OK exe=$exe files=$files size=${mb}MB"

# Post-build smoke test: build a snapshot for real, to confirm the package is
# complete. Checking the exe exists is not enough — it is produced just the same
# when the MIB data files are missing.
Write-Host "[build] smoke test ..."
$smoke = & $exe --selftest 2>&1 | Out-String
if ($smoke -match "SELFTEST_OK") {
    Write-Host "[build] smoke test passed"
} else {
    Write-Host "[build] smoke test FAILED:"
    Write-Host $smoke
    exit 1
}

<#
    install-update-matrix.ps1 -- the paths lifecycle.ps1 does not walk.

    lifecycle.ps1 drives install, upgrade, uninstall, reinstall and purge, all
    through /qn, and it has forty assertions. Everything 1.1.3 fixed was outside
    it, and not by a little: a repair deleted the service and returned 0, a
    repair replaced the community and the management networks with whatever the
    built-in SNMP service happened to hold, and a repair with no properties
    aborted with 1603. All three were green in every existing check, because no
    check had ever run a repair.

    This file covers what an administrator actually does after the first
    install: upgrade a machine that has been configured, and repair one that is
    misbehaving. The property under test throughout is the same one: **an
    installer must not silently change a setting it did not ask about.**

    Two things mislead when testing this, both of which produced a wrong
    conclusion before they were understood:

      * A repair does not replace msi-configure.ps1. It is unversioned, so
        Windows Installer keeps the copy already on disk, even under
        REINSTALLMODE=vamus. A repair therefore runs the script installed last
        time. To test a change to that script, install first.
      * The MSI has to be the one just built. Copying it off a build machine
        before the build finished tests the previous artefact, which is why
        -ExpectMsiSha256 is mandatory here rather than advisory.

    Usage:
      install-update-matrix.ps1 -OldMsi <path> -NewMsi <path>
                                -ExpectMsiSha256 <hex> -ExpectVersion 1.1.3
                                [-Community mon] [-Networks 192.0.2.0/24]
#>
[CmdletBinding()]
param(
    [Parameter(Mandatory)][string]$OldMsi,
    [Parameter(Mandatory)][string]$NewMsi,
    [Parameter(Mandatory)][string]$ExpectMsiSha256,
    [Parameter(Mandatory)][string]$ExpectVersion,
    [string]$Community = 'matrix-community',
    [string]$Networks  = '192.0.2.0/24,127.0.0.1',
    [string]$V3User    = 'matrixv3',
    [string]$V3Auth    = 'matrix-auth-passphrase-2026',
    [string]$V3Priv    = 'matrix-priv-passphrase-2026'
)

$ErrorActionPreference = 'Stop'
$SVC   = 'jt-snmpd'
$DATA  = Join-Path $env:ProgramData 'jt-snmpd'
$CFG   = Join-Path $DATA 'config.json'
$IDX   = Join-Path $DATA 'state\index-map.json'
$EVT   = "HKLM:\SYSTEM\CurrentControlSet\Services\EventLog\Application\$SVC"
$pass = 0; $fail = 0

function Check($what, $ok, $detail = '') {
    if ($ok) { $script:pass++; "  PASS  $what" }
    else     { $script:fail++; "  FAIL  $what $detail" }
}
function Sec($t) { ""; "== $t =="; }
function SvcState { $s = Get-Service $SVC -ErrorAction SilentlyContinue
                    if ($s) { $s.Status.ToString() } else { 'absent' } }
function Cfg { if (Test-Path $CFG) { Get-Content $CFG -Raw -Encoding UTF8 | ConvertFrom-Json } }
function Run([string]$msi, [string[]]$extra) {
    $args = @('/i', "`"$msi`"", '/qn') + $extra
    (Start-Process msiexec.exe -ArgumentList $args -Wait -PassThru).ExitCode
}
function Get-InstalledCodes {
    # Uninstall keys rather than Win32_Product: querying that class makes
    # Windows Installer reconfigure every installed product, which is slow and
    # has been known to restart services.
    $roots = @('HKLM:\SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall',
               'HKLM:\SOFTWARE\WOW6432Node\Microsoft\Windows\CurrentVersion\Uninstall')
    foreach ($r in $roots) {
        Get-ChildItem $r -ErrorAction SilentlyContinue | ForEach-Object {
            $d = Get-ItemProperty $_.PSPath -ErrorAction SilentlyContinue
            if ($d.DisplayName -like 'jt-snmpd*') { $_.PSChildName }
        }
    }
}
function Remove-Product([string]$msi, [string[]]$extra = @()) {
    (Start-Process msiexec.exe -ArgumentList (@('/x', "`"$msi`"", '/qn') + $extra) -Wait -PassThru).ExitCode
}

Sec 'A. the artefact is the one that was just built'
$h = (Get-FileHash $NewMsi -Algorithm SHA256).Hash.ToLower()
Check 'the new MSI matches the expected SHA-256' ($h -eq $ExpectMsiSha256.ToLower()) "(got $h)"
if ($h -ne $ExpectMsiSha256.ToLower()) {
    "STOPPING: testing the wrong file proves nothing."; exit 1
}

Sec 'B. clean the machine so the first install is a first install'
# By ProductCode, not by MSI path.
#
# The package has no fixed ProductCode, so WiX generates a new one for every
# build. `msiexec /x <a freshly built MSI>` therefore does not match a product
# installed from an earlier build of the same version: it returns 1605 and does
# nothing, and the run that follows starts on a machine that was never cleaned.
# That is exactly what happened, and the check below is what caught it.
#
# Released MSIs do not have this problem -- there is one artefact per version --
# so this is a property of testing against local builds, not of the product.
foreach ($code in (Get-InstalledCodes)) {
    [void](Start-Process msiexec.exe -ArgumentList @('/x', $code, '/qn', 'PURGE=1') -Wait -PassThru)
}
Start-Sleep 6
$clean = ((SvcState) -eq 'absent') -and (-not (Test-Path $DATA))
Check 'the machine starts clean' $clean "(service=$(SvcState), data dir present=$(Test-Path $DATA))"
if (-not $clean) {
    # Everything below assumes a machine with nothing installed. Running on with
    # a half-removed product produces a page of failures that describe the
    # leftovers rather than the software, which is worse than not running: the
    # first attempt at this file did exactly that, and the cascade hid whether
    # anything real was wrong.
    ""
    "STOPPING: the machine was not clean and nothing after this would mean anything."
    "Remove the product by hand, then run again:"
    "  sc stop jt-snmpd; sc delete jt-snmpd"
    "  Remove-Item 'C:\Program Files\jt-snmpd' -Recurse -Force"
    "  Remove-Item '$DATA' -Recurse -Force"
    exit 1
}

Sec 'C. a first silent install still has to be told where the pollers are'
# Deny by default. This is the one case the launch condition must still refuse,
# and 1.1.3 widened that condition, so it is worth an assertion of its own.
$code = Run $NewMsi @("COMMUNITY=$Community")
Check 'installing with no MANAGEMENTNETWORKS is refused' ($code -ne 0) "(got $code)"
Start-Sleep 4
Check 'and nothing was left behind' ((SvcState) -eq 'absent') "(got $(SvcState))"

Sec 'D. first install of the OLD version, configured'
$code = Run $OldMsi @("MANAGEMENTNETWORKS=$Networks", "COMMUNITY=$Community")
Check 'install exits 0' ($code -eq 0) "(got $code)"
Start-Sleep 10
Check 'the service is running' ((SvcState) -eq 'Running') "(got $(SvcState))"
$c = Cfg
Check 'the community is the one supplied' ($c.community -eq $Community) "(got $($c.community))"
Check 'the networks are the ones supplied' ((@($c.allowed_networks) -join ',') -eq $Networks) "(got $(@($c.allowed_networks) -join ','))"
$idxHash = if (Test-Path $IDX) { (Get-FileHash $IDX -Algorithm SHA256).Hash } else { $null }

Sec 'E. the operator changes things by hand, as they are documented to'
$c = Cfg
$c.rate_burst = 777
$c.rate_pps   = 42
$c.enable_arp_table = $true
$c | ConvertTo-Json -Depth 5 | Set-Content $CFG -Encoding UTF8
Restart-Service $SVC -Force -WarningAction SilentlyContinue
Start-Sleep 8
Check 'the service came back after the edit' ((SvcState) -eq 'Running') "(got $(SvcState))"

Sec 'F. UPDATE: upgrade with properties, the GPO redeployment case'
$code = Run $NewMsi @("MANAGEMENTNETWORKS=$Networks", "COMMUNITY=$Community")
Check 'the upgrade exits 0' ($code -eq 0) "(got $code)"
Start-Sleep 12
Check 'the service is running after the upgrade' ((SvcState) -eq 'Running') "(got $(SvcState))"
Check 'the version is the new one' (((Get-CimInstance Win32_Product -Filter "Name like 'jt-snmpd%'" | Select-Object -First 1).Version) -eq $ExpectVersion)
$c = Cfg
Check 'rate_burst survived the upgrade'      ($c.rate_burst -eq 777)  "(got $($c.rate_burst))"
Check 'rate_pps survived the upgrade'        ($c.rate_pps -eq 42)     "(got $($c.rate_pps))"
Check 'enable_arp_table survived the upgrade' ($c.enable_arp_table -eq $true) "(got $($c.enable_arp_table))"
$missing = @('schema_version','community','allowed_networks','port',
             'enable_arp_table','rate_pps','rate_burst','v3_only') |
           Where-Object { $null -eq $c.$_ }
Check 'every key the agent reads is present' ($missing.Count -eq 0) "(missing: $($missing -join ', '))"
if ($idxHash) {
    Check 'index-map was not rebuilt, so ifIndex is stable' ((Get-FileHash $IDX -Algorithm SHA256).Hash -eq $idxHash)
}

Sec 'G. the event source points outside anything we install'
# Otherwise the Event Log service holds a handle on a file the next upgrade has
# to replace, and the graphical upgrade grows a page asking the operator to stop
# Windows Event Log.
$emf = (Get-ItemProperty $EVT -ErrorAction SilentlyContinue).EventMessageFile
Check 'the event source is registered' ($null -ne $emf)
Check 'its message file is not in the install folder' ($emf -notmatch 'jt-snmpd') "(got $emf)"
Check 'its message file is under System32' ($emf -match 'System32') "(got $emf)"

Sec 'H. UPDATE: a v3-only host, with an account, can still be upgraded'
# 1.1.1 made these hosts unupgradable: the installer proved the service answered
# with an SNMPv2c GET, which such a host deliberately refuses. 1.1.2 sends an
# SNMPv3 engine discovery instead, so this section also exercises that probe end
# to end -- it is the only place the v3 branch of the health check runs.
# v3_only with no SNMPv3 account is a service that refuses to start, on purpose.
# So this section has to put it back whatever happens: an earlier run died
# between setting it and clearing it, and left a domain controller with a
# service that would not start and an installer that rolled back with 1603 every
# time. The defect was the test's, not the product's, and the product was
# behaving exactly as documented.
try {
    # An account has to exist first. v3_only with no account is a service that
    # refuses to start, which is the documented behaviour and the right one, so
    # skipping this step tests the refusal rather than the upgrade. The first
    # version of this file did exactly that and reported a defect that was not
    # there.
    #
    # The passphrases come from standard input because an argument is visible in
    # the process list to every user on the machine while the command runs.
    $exe = Join-Path $env:ProgramFiles 'jt-snmpd\jt-snmpd.exe'
    & cmd /c "(echo $V3Auth& echo $V3Priv) | `"$exe`" user add $V3User" | Out-Null
    $users = & $exe user list 2>&1
    Check 'an SNMPv3 account can be provisioned' (($users -join ' ') -match $V3User) "($users)"

    $c = Cfg; $c.v3_only = $true
    $c | ConvertTo-Json -Depth 5 | Set-Content $CFG -Encoding UTF8
    Restart-Service $SVC -Force -WarningAction SilentlyContinue
    Start-Sleep 8
    Check 'the service runs with v3_only and an account' ((SvcState) -eq 'Running') "(got $(SvcState))"
    $code = Run $NewMsi @('REINSTALL=ALL', 'REINSTALLMODE=vomus', "MANAGEMENTNETWORKS=$Networks", "COMMUNITY=$Community")
    Check 'a v3_only host upgrades cleanly' ($code -eq 0) "(got $code)"
    Start-Sleep 12
    Check 'v3_only was preserved' ((Cfg).v3_only -eq $true) "(got $((Cfg).v3_only))"
} finally {
    $c = Cfg
    if ($c) {
        $c.v3_only = $false
        $c | ConvertTo-Json -Depth 5 | Set-Content $CFG -Encoding UTF8
    }
    if ((SvcState) -ne 'absent') { Restart-Service $SVC -Force -WarningAction SilentlyContinue }
    Start-Sleep 8
}
Check 'the service is running once v3_only is cleared' ((SvcState) -eq 'Running') "(got $(SvcState))"

Sec 'I. REPAIR with no properties at all'
# How a repair is actually run. Before 1.1.3 this aborted with 1603; once that
# was fixed it deleted the service and returned 0; once that was fixed it
# replaced the community and the networks from the built-in SNMP service.
$before = Cfg
$code = Run $NewMsi @('REINSTALL=ALL', 'REINSTALLMODE=vomus')
Check 'the repair exits 0' ($code -eq 0) "(got $code)"
Start-Sleep 12
Check 'the service still exists and is running' ((SvcState) -eq 'Running') "(got $(SvcState))"
Check 'the firewall rules are still there' ((@(Get-NetFirewallRule -DisplayName 'jt-snmpd*' -ErrorAction SilentlyContinue).Count) -ge 2)
$after = Cfg
Check 'the community was not changed'  ($after.community -eq $before.community) "(was $($before.community), now $($after.community))"
Check 'the networks were not changed'  ((@($after.allowed_networks) -join ',') -eq (@($before.allowed_networks) -join ',')) "(was $(@($before.allowed_networks) -join ','), now $(@($after.allowed_networks) -join ','))"
Check 'rate_burst was not changed'     ($after.rate_burst -eq $before.rate_burst) "(was $($before.rate_burst), now $($after.rate_burst))"
Check 'the built-in SNMP service was not restored behind our back' (
    (-not (Get-Service SNMP -ErrorAction SilentlyContinue)) -or
    ((Get-Service SNMP).StartType -ne 'Automatic'))

Sec 'J. uninstall keeps the data, reinstall keeps the ports'
$idxHash2 = if (Test-Path $IDX) { (Get-FileHash $IDX -Algorithm SHA256).Hash } else { $null }
$code = Remove-Product $NewMsi
Check 'uninstall exits 0' ($code -eq 0) "(got $code)"
Check 'the service is gone' ((SvcState) -eq 'absent') "(got $(SvcState))"
Check 'the data directory is kept' (Test-Path $DATA)
Check 'the event source registration is gone' (-not (Test-Path $EVT))
$code = Run $NewMsi @("MANAGEMENTNETWORKS=$Networks", "COMMUNITY=$Community")
Check 'reinstall exits 0' ($code -eq 0) "(got $code)"
Start-Sleep 12
Check 'the service is running again' ((SvcState) -eq 'Running') "(got $(SvcState))"
if ($idxHash2) {
    Check 'index-map is byte-identical, so LibreNMS keeps its ports' ((Get-FileHash $IDX -Algorithm SHA256).Hash -eq $idxHash2)
}

Sec 'K. PURGE removes everything'
$code = Remove-Product $NewMsi @('PURGE=1')
Check 'the purge exits 0' ($code -eq 0) "(got $code)"
Check 'the service is gone' ((SvcState) -eq 'absent')
Check 'the data directory is gone' (-not (Test-Path $DATA))

""
"==== $pass passed, $fail failed ===="
if ($fail -gt 0) { exit 1 } else { exit 0 }

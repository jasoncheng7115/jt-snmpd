#Requires -Version 5.1
<#
    jt-snmpd MSI custom action script

    Note: saved as UTF-8 with BOM; without one PowerShell 5.1 reads the file
    using the system ANSI code page.

    Called from a deferred custom action in the MSI, this performs everything
    install.ps1 does after the files are copied. Both share the same logic so
    that "installed by MSI" and "installed by script" cannot drift into two
    different states (a key design point).

    The MSI handles: pre-checks, file copying, removing the old version on
    upgrade, and rollback on failure.
    This script handles: migrating and disabling the built-in SNMP service, the
    config file, ACLs, service registration, firewall rules and the health check.
#>
[CmdletBinding()]
param(
    [string]$ManagementNetworks = "",
    [string]$Community = "",
    [string]$KeepMsSnmp = "0",

    [switch]$Uninstall,
    [string]$Purge = "0"
)

$ErrorActionPreference = 'Continue'

$SERVICE_NAME  = 'jt-snmpd'
$DATA_DIR      = Join-Path $env:ProgramData 'jt-snmpd'
# Where the data directory lived up to 0.9.5, before everything was renamed to
# match the project. Upgrades have to bring it across: it holds index-map.json,
# and losing that makes LibreNMS rediscover every port and orphan the history.
$DATA_DIR_OLD  = Join-Path $env:ProgramData 'JT-SNMP'
$STATE_DIR     = Join-Path $DATA_DIR 'state'
$LOG_DIR       = Join-Path $DATA_DIR 'logs'
$SECRETS_DIR   = Join-Path $DATA_DIR 'secrets'
$EXE_NAME      = 'jt-snmpd.exe'
$FW_RULE       = 'jt-snmpd (UDP 161)'
$FW_RULE_ICMP  = 'jt-snmpd (ICMPv4)'
$MSSNMP_PARAMS = 'HKLM:\SYSTEM\CurrentControlSet\Services\SNMP\Parameters'

# A custom action has no console, so everything is written to a log file for
# later diagnosis
$MSI_LOG = Join-Path $LOG_DIR 'msi-configure.log'
# File logging has to stop before a PURGE: this log lives inside the directory
# being removed, and one more line recreates logs\ — leaving debris behind after
# what claimed to be a complete removal. This happened.
$script:LogToFile = $true
function Log {
    param($m)
    $line = "{0} {1}" -f (Get-Date -Format 'yyyy-MM-dd HH:mm:ss'), $m
    if ($script:LogToFile) {
        try {
            if (-not (Test-Path $LOG_DIR)) { New-Item -ItemType Directory -Force $LOG_DIR | Out-Null }
            Add-Content -Path $MSI_LOG -Value $line -Encoding UTF8
        } catch { }
    }
    Write-Host $line
}

function Stop-AgentService {
    $svc = Get-Service -Name $SERVICE_NAME -ErrorAction SilentlyContinue
    if (-not $svc) { return }
    if ($svc.Status -ne 'Stopped') { Stop-Service -Name $SERVICE_NAME -Force -ErrorAction SilentlyContinue }
    # Stopping the service does not mean its file handles are released (the
    # actual bug in jt-doc-tools v1.1.66-69)
    $deadline = (Get-Date).AddSeconds(30)
    while ((Get-Date) -lt $deadline) {
        if (-not (Get-Process -Name $SERVICE_NAME -ErrorAction SilentlyContinue)) { break }
        Start-Sleep -Milliseconds 500
    }
    Get-Process -Name $SERVICE_NAME -ErrorAction SilentlyContinue |
        Stop-Process -Force -ErrorAction SilentlyContinue
    & sc.exe delete $SERVICE_NAME | Out-Null
    Start-Sleep -Seconds 2
    Log "stopped and removed the previous service"
}

# ---------------- Uninstall ----------------
if ($Uninstall) {
    Log "=== uninstall starting ==="
    Stop-AgentService
    Remove-NetFirewallRule -DisplayName "$FW_RULE*" -ErrorAction SilentlyContinue
    Remove-NetFirewallRule -DisplayName "$FW_RULE_ICMP*" -ErrorAction SilentlyContinue
    Log "firewall rules removed"

    # Restore the built-in SNMP service
    $restorePath = Join-Path $STATE_DIR 'ms-snmp-restore.json'
    if (Test-Path $restorePath) {
        try {
            $r = Get-Content $restorePath -Raw -Encoding UTF8 | ConvertFrom-Json
            if ($r.ms_snmp.disabled_by_us -and $r.ms_snmp.service_existed) {
                $orig = $r.ms_snmp.original_start_type
                if ($orig -and $orig -ne 'Disabled') {
                    Set-Service -Name SNMP -StartupType $orig -ErrorAction SilentlyContinue
                    if ($r.ms_snmp.original_status -eq 'Running') {
                        Start-Service -Name SNMP -ErrorAction SilentlyContinue
                    }
                    Log "built-in Windows SNMP Service restored to $orig"
                }
            }
        } catch { Log "failed to restore the built-in SNMP service: $_" }
    }

    if ($Purge -eq '1') {
        # Turn off file logging first, or every Log line below recreates logs\.
        Log "removing the data directory (PURGE=1): $DATA_DIR"
        $script:LogToFile = $false
        # The service has just stopped, so a DPAPI blob or the log file may still
        # be held briefly. Retry rather than skipping quietly.
        # Both locations: on a machine upgraded from 0.9.5 or earlier the old
        # directory may still be present, and a purge that leaves it behind is
        # not a purge. The next installation would inherit it through the
        # migration step.
        $purged = $false
        foreach ($attempt in 1..5) {
            Remove-Item $DATA_DIR -Recurse -Force -ErrorAction SilentlyContinue
            Remove-Item $DATA_DIR_OLD -Recurse -Force -ErrorAction SilentlyContinue
            if (-not (Test-Path $DATA_DIR) -and -not (Test-Path $DATA_DIR_OLD)) { $purged = $true; break }
            Start-Sleep -Milliseconds 400
        }
        if ($purged) {
            Log "data directory completely removed (PURGE=1)"
        } else {
            # Do not claim success falsely: anything left behind is inherited by
            # the next installation.
            $left = @(Get-ChildItem $DATA_DIR -Recurse -Force -ErrorAction SilentlyContinue).Count +
                    @(Get-ChildItem $DATA_DIR_OLD -Recurse -Force -ErrorAction SilentlyContinue).Count
            Log "WARN data directory not fully removed; $left items remain under $DATA_DIR or $DATA_DIR_OLD"
        }
    } else {
        # keeping it by default is deliberate. Customers commonly
        # uninstall and reinstall to troubleshoot, and clearing the index map
        # makes LibreNMS rediscover everything, orphaning the existing RRDs.
        Log "data directory kept: $DATA_DIR"
    }
    Log "=== uninstall complete ==="
    exit 0
}

# ---------------- Install / upgrade ----------------
# This script lives inside the installation directory, so the path is derived
# from its own location rather than passed in by the MSI — which avoids the trap
# where [INSTALLFOLDER]'s trailing backslash escapes the closing quote.
$InstallDir = Split-Path -Parent $PSCommandPath
Log "=== configuration starting, InstallDir=$InstallDir ==="
$exe = Join-Path $InstallDir $EXE_NAME
if (-not (Test-Path $exe)) { Log "FAIL $exe not found"; exit 1 }

Stop-AgentService

# --- Read the built-in SNMP configuration  ---
$msCfg = [ordered]@{
    service_exists = $false; status = $null; start_type = $null
    communities = @{}; permitted_managers = @()
    sys_contact = $null; sys_location = $null; sys_services = $null
    trap_destinations = @(); extension_agents = @()
}
$svc = Get-Service -Name SNMP -ErrorAction SilentlyContinue
if ($svc) {
    $msCfg.service_exists = $true
    $msCfg.status = "$($svc.Status)"
    $msCfg.start_type = "$($svc.StartType)"
    Log "built-in SNMP Service detected: $($svc.Status) / $($svc.StartType)"
}
if (Test-Path $MSSNMP_PARAMS) {
    $vc = Join-Path $MSSNMP_PARAMS 'ValidCommunities'
    if (Test-Path $vc) {
        $props = Get-ItemProperty $vc
        foreach ($n in (Get-Item $vc).Property) { $msCfg.communities[$n] = [int]$props.$n }
    }
    $pm = Join-Path $MSSNMP_PARAMS 'PermittedManagers'
    if (Test-Path $pm) {
        $props = Get-ItemProperty $pm
        foreach ($n in (Get-Item $pm).Property) { $msCfg.permitted_managers += "$($props.$n)" }
    }
    $ra = Join-Path $MSSNMP_PARAMS 'RFC1156Agent'
    if (Test-Path $ra) {
        $p = Get-ItemProperty $ra
        $msCfg.sys_contact = $p.sysContact; $msCfg.sys_location = $p.sysLocation
        $msCfg.sys_services = $p.sysServices
    }
    $tc = Join-Path $MSSNMP_PARAMS 'TrapConfiguration'
    if (Test-Path $tc) {
        foreach ($k in Get-ChildItem $tc) {
            $p = Get-ItemProperty $k.PSPath
            foreach ($n in (Get-Item $k.PSPath).Property) {
                $msCfg.trap_destinations += "$($k.PSChildName) -> $($p.$n)"
            }
        }
    }
    $ea = Join-Path $MSSNMP_PARAMS 'ExtensionAgents'
    if (Test-Path $ea) {
        $p = Get-ItemProperty $ea
        foreach ($n in (Get-Item $ea).Property) { $msCfg.extension_agents += $n }
    }
}

# --- Decide the community  ---
$comm = $Community
if (-not $comm) {
    foreach ($name in $msCfg.communities.Keys) {
        $access = $msCfg.communities[$name]
        if ($access -eq 4) { if (-not $comm) { $comm = $name }; Log "imported a read-only community" }
        elseif ($access -in @(8,16)) {
            if (-not $comm) { $comm = $name }
            Log "[!] community was writable (access=$access); downgraded to read-only"
        } else { Log "community access=$access (NONE/NOTIFY); not imported" }
    }
}
if (-not $comm) {
    # Reaching here means no COMMUNITY was supplied and the built-in service has
    # no read-only community to migrate. Without one the agent refuses to serve,
    # the health check then times out and the MSI rolls back with 1603 — an error
    # code that says nothing about the cause. Say it here instead.
    Log "FAIL cannot determine a community: COMMUNITY was not supplied and the"
    Log "     built-in SNMP service has no read-only community to migrate."
    Log "     Reinstall with msiexec /i jt-snmpd.msi /qn COMMUNITY=<your community> ..."
    Log "     or install through the UI and fill it in on the settings page."
    exit 1
}
if ($comm -in @('public','private')) {
    Log "[!] this is a well-known default community; SNMPv3 is strongly recommended"
}

# --- Decide the management networks ---
$nets = @()
if ($ManagementNetworks) {
    $nets = $ManagementNetworks -split '[,;\s]+' | Where-Object { $_ }
} elseif ($msCfg.permitted_managers.Count -gt 0) {
    foreach ($m in $msCfg.permitted_managers) {
        if ($m -match '^\d{1,3}(\.\d{1,3}){3}$') { $nets += $m; Log "PermittedManagers $m" }
        else {
            try {
                $ip = ([System.Net.Dns]::GetHostAddresses($m) |
                       Where-Object AddressFamily -eq 'InterNetwork' |
                       Select-Object -First 1).IPAddressToString
                if ($ip) { $nets += $ip; Log "PermittedManagers '$m' resolved to $ip" }
            } catch { Log "[!] PermittedManagers '$m' could not be resolved; not added to the ACL" }
        }
    }
}
if ($nets.Count -eq 0) {
    # Never migrate to Any/Any
    Log "FAIL no management networks were supplied and none could be taken from the existing configuration. Deny by default; Any/Any is not allowed."
    exit 1
}
Log "management networks: $($nets -join ', ')"

# --- Carry the data directory across from the pre-rename location ---
# Everything was renamed to jt-snmpd in 0.9.6 so the product, the service, the
# paths and the repository finally agree. The data directory is the one that
# cannot simply be recreated: state\index-map.json holds the ifIndex
# assignments, and losing it makes LibreNMS delete every port and rediscover,
# taking the historical RRDs with it. state\ms-snmp-restore.json is the only
# record of what the built-in SNMP service looked like before we disabled it.
#
# **This is tested per file, not by asking whether the new directory exists.**
# The first version asked exactly that, and could never fire: this script writes
# its own log to $LOG_DIR, so by the time the check ran the destination had
# already been created by the logging. The migration was skipped on every
# upgrade, the old data was left behind, and the agent started with a fresh
# index map -- the precise failure the migration exists to prevent. It was found
# on the first real upgrade, not by reading the code.
#
# Moving item by item is also idempotent: a partially completed migration
# finishes on the next run instead of being skipped for looking done.
if (Test-Path $DATA_DIR_OLD) {
    Log "carrying data across from $DATA_DIR_OLD"
    $carried = 0; $skipped = 0; $failed = 0
    foreach ($rel in @('config.json', 'state', 'secrets')) {
        $src = Join-Path $DATA_DIR_OLD $rel
        $dst = Join-Path $DATA_DIR $rel
        if (-not (Test-Path $src)) { continue }
        if (Test-Path $dst) {
            # Never overwrite: the destination is live data on a reinstall.
            Log "  $rel already present at the new location; leaving it alone"
            $skipped++
            continue
        }
        try {
            Move-Item -Path $src -Destination $dst -Force -ErrorAction Stop
            Log "  moved $rel"
            $carried++
        } catch {
            try {
                Copy-Item -Path $src -Destination $dst -Recurse -Force -ErrorAction Stop
                Log "  [!] move failed, copied $rel instead: $_"
                $carried++
            } catch {
                Log "  FAIL could not carry $rel across: $_"
                $failed++
            }
        }
    }
    # Keep the old logs as history rather than deleting them, but out of the way.
    $oldLogs = Join-Path $DATA_DIR_OLD 'logs'
    if (Test-Path $oldLogs) {
        $archive = Join-Path $LOG_DIR 'pre-0.9.6'
        try {
            if (-not (Test-Path $archive)) { New-Item -ItemType Directory -Force $archive | Out-Null }
            Get-ChildItem $oldLogs -File -ErrorAction Stop | ForEach-Object {
                Move-Item $_.FullName (Join-Path $archive $_.Name) -Force -ErrorAction SilentlyContinue
            }
            Log "  earlier logs kept in $archive"
        } catch { Log "  [!] could not archive the earlier logs: $_" }
    }
    if ($failed -gt 0) {
        Log "FAIL the data directory could not be carried across; refusing to continue with a partial state"
        exit 1
    }
    # Only remove the old directory once everything worth keeping is out of it.
    try {
        $left = @(Get-ChildItem $DATA_DIR_OLD -Recurse -File -ErrorAction SilentlyContinue)
        if ($left.Count -eq 0) {
            Remove-Item $DATA_DIR_OLD -Recurse -Force -ErrorAction Stop
            Log "  removed $DATA_DIR_OLD ($carried carried, $skipped already present)"
        } else {
            Log "  [!] $DATA_DIR_OLD still holds $($left.Count) file(s) and was left in place"
        }
    } catch { Log "  [!] could not remove $DATA_DIR_OLD`: $_" }
}

# --- Data directory and ACL  ---
foreach ($d in @($DATA_DIR, $STATE_DIR, $LOG_DIR, $SECRETS_DIR)) {
    if (-not (Test-Path $d)) { New-Item -ItemType Directory -Force $d | Out-Null }
}
try {
    # The default ACL on C:\ProgramData lets Users create subdirectories, so an
    # attacker can create ours first and keep write access to it. Creating it
    # only when absent is not enough; the ACL has to be reset.
    $acl = New-Object System.Security.AccessControl.DirectorySecurity
    $acl.SetAccessRuleProtection($true, $false)
    foreach ($sid in @('S-1-5-18', 'S-1-5-32-544')) {
        $account = New-Object System.Security.Principal.SecurityIdentifier $sid
        $acl.AddAccessRule((New-Object System.Security.AccessControl.FileSystemAccessRule(
            $account, 'FullControl', 'ContainerInherit,ObjectInherit', 'None', 'Allow')))
    }
    $acl.SetOwner((New-Object System.Security.Principal.SecurityIdentifier 'S-1-5-32-544'))
    Set-Acl -Path $DATA_DIR -AclObject $acl
    Log "data directory ACL set to SYSTEM and Administrators only"
} catch { Log "[!] failed to set the ACL: $_" }

# --- Write the config and the restore record ---
$cfg = [ordered]@{
    schema_version = 1; community = $comm; allowed_networks = @($nets)
    port = 161; enable_arp_table = $false; installed_at = (Get-Date).ToString('s')
    installed_by = 'msi'
}
# No BOM: Windows PowerShell 5.1's -Encoding UTF8 adds one, and most JSON
# parsers — Python's json.load included — fail on it. The agent now reads with
# utf-8-sig and tolerates either form, but this still writes the clean version.
[IO.File]::WriteAllText((Join-Path $DATA_DIR 'config.json'),
    ($cfg | ConvertTo-Json -Depth 5), (New-Object Text.UTF8Encoding $false))

# On upgrade the restore record must **not** be overwritten with the current
# state: the built-in service was already disabled by the previous install, so
# re-reading it only yields Disabled/Stopped. Writing that back makes the
# uninstall guard `if ($orig -ne 'Disabled')` permanently false, and after
# install -> upgrade -> uninstall the built-in service never comes back.
#
# What has to be recorded is how things looked **before we first touched them**,
# so an existing record always wins and only the first install writes one.
$RESTORE_FILE = Join-Path $STATE_DIR 'ms-snmp-restore.json'
$msSnmpBlock = $null
if (Test-Path $RESTORE_FILE) {
    try {
        $prev = Get-Content $RESTORE_FILE -Raw -Encoding UTF8 | ConvertFrom-Json
        if ($prev.ms_snmp) {
            $msSnmpBlock = [ordered]@{
                service_existed     = [bool]$prev.ms_snmp.service_existed
                original_start_type = $prev.ms_snmp.original_start_type
                original_status     = $prev.ms_snmp.original_status
                disabled_by_us      = [bool]$prev.ms_snmp.disabled_by_us
            }
            Log ("reusing the existing restore record: built-in SNMP was " +
                 "$($msSnmpBlock.original_start_type) / $($msSnmpBlock.original_status)")
        }
    } catch { Log "WARN the existing restore record could not be parsed; rebuilding from the current state: $_" }
}
if (-not $msSnmpBlock) {
    $msSnmpBlock = [ordered]@{
        service_existed = $msCfg.service_exists
        original_start_type = $msCfg.start_type
        original_status = $msCfg.status
        disabled_by_us = ($KeepMsSnmp -ne '1') -and $msCfg.service_exists
    }
}

$restore = [ordered]@{
    schema_version = 1; migrated_at = (Get-Date).ToString('s')
    ms_snmp = $msSnmpBlock
    imported = [ordered]@{
        communities = @($msCfg.communities.Keys | ForEach-Object {
            if ($_.Length -gt 4) { $_.Substring(0,4) + '***' } else { '***' } })
        permitted_managers = @($msCfg.permitted_managers)
        sys_contact = $msCfg.sys_contact; sys_location = $msCfg.sys_location
    }
    not_imported = [ordered]@{
        trap_destinations = @($msCfg.trap_destinations)
        extension_agents = @($msCfg.extension_agents)
        sys_services = $msCfg.sys_services
    }
}
$restore | ConvertTo-Json -Depth 6 | Set-Content $RESTORE_FILE -Encoding UTF8

# --- Disable the built-in SNMP service (disabled, not removed) ---
if ($msCfg.service_exists -and $KeepMsSnmp -ne '1') {
    Stop-Service -Name SNMP -Force -ErrorAction SilentlyContinue
    Set-Service -Name SNMP -StartupType Disabled -ErrorAction SilentlyContinue
    Start-Sleep -Seconds 2
    # Verify rather than assume: Group Policy or third-party management can block
    # either action. If it did not actually stop, the built-in service still holds
    # UDP/161 and our bind fails — better to say who holds the port here than to
    # let it surface as an unexplained health-check timeout later.
    $after = Get-Service -Name SNMP -ErrorAction SilentlyContinue
    if ($after -and ($after.Status -ne 'Stopped' -or $after.StartType -ne 'Disabled')) {
        Log ("FAIL could not disable the built-in SNMP Service; it is currently " +
             "$($after.Status) / $($after.StartType)。" +
             "It may be under Group Policy control. Disable it manually and retry, or install with KEEPMSSNMP=1 on a different port.")
        exit 1
    }
    Log "built-in SNMP Service disabled (was $($msCfg.start_type) / $($msCfg.status))"
}

# --- Register the service ---
& $exe --startup auto install 2>&1 | Out-Null
if (-not (Get-Service -Name $SERVICE_NAME -ErrorAction SilentlyContinue)) {
    Log "FAIL service registration failed"; exit 1
}
& sc.exe description $SERVICE_NAME 'SNMP agent serving Windows host monitoring data over standard MIBs' | Out-Null
# Three-stage automatic recovery; failureflag 1 makes a non-zero exit code
# trigger it too
& sc.exe failure $SERVICE_NAME reset= 86400 actions= restart/60000/restart/60000/restart/300000 | Out-Null
& sc.exe failureflag $SERVICE_NAME 1 | Out-Null
# Privilege reduction
& sc.exe privs $SERVICE_NAME SeChangeNotifyPrivilege/SeSystemProfilePrivilege/SeIncreaseQuotaPrivilege | Out-Null
$s = Get-CimInstance Win32_Service -Filter "Name='$SERVICE_NAME'"
Log "service registered: $($s.StartName) / $($s.StartMode)"
if ($s.PathName -notmatch '^"') { Log "[!] ImagePath is not quoted: $($s.PathName)" }

# --- Firewall  ---
Remove-NetFirewallRule -DisplayName "$FW_RULE*" -ErrorAction SilentlyContinue
New-NetFirewallRule -DisplayName $FW_RULE -Direction Inbound -Protocol UDP `
    -LocalPort 161 -RemoteAddress $nets -Action Allow -Profile Any `
    -Description 'jt-snmpd inbound SNMP' | Out-Null
# Disabling the built-in service also disables its ICMP rule, and LibreNMS uses
# ping to decide whether a device is up (measured)
Remove-NetFirewallRule -DisplayName "$FW_RULE_ICMP*" -ErrorAction SilentlyContinue
New-NetFirewallRule -DisplayName $FW_RULE_ICMP -Direction Inbound -Protocol ICMPv4 `
    -IcmpType 8 -RemoteAddress $nets -Action Allow -Profile Any `
    -Description 'jt-snmpd ICMP echo for NMS availability' | Out-Null
Log "firewall rules created (UDP/161 and ICMPv4, sources limited to $($nets -join ', '))"

# --- Start the service and run the loopback health check  ---
function Test-SnmpLoopback {
    param($CommunityName)
    $c2 = [Text.Encoding]::ASCII.GetBytes($CommunityName)
    $oid = [byte[]](0x2B,0x06,0x01,0x02,0x01,0x01,0x03,0x00)
    $vb = [byte[]](0x30,(2+$oid.Length+2)) + [byte[]](0x06,$oid.Length) + $oid + [byte[]](0x05,0x00)
    $vbl = [byte[]](0x30,$vb.Length) + $vb
    $pdu = [byte[]](0xA0,(3+3+3+$vbl.Length)) + [byte[]](0x02,0x01,0x01) +
           [byte[]](0x02,0x01,0x00) + [byte[]](0x02,0x01,0x00) + $vbl
    $body = [byte[]](0x02,0x01,0x01) + [byte[]](0x04,$c2.Length) + $c2 + $pdu
    $msg = [byte[]](0x30,$body.Length) + $body
    try {
        $u = New-Object System.Net.Sockets.UdpClient
        $u.Client.ReceiveTimeout = 3000
        $u.Connect('127.0.0.1', 161)
        [void]$u.Send($msg, $msg.Length)
        $ep = New-Object System.Net.IPEndPoint([System.Net.IPAddress]::Any, 0)
        $resp = $u.Receive([ref]$ep); $u.Close()
        return ($resp.Length -gt 0)
    } catch { return $false }
}

Start-Service -Name $SERVICE_NAME
$deadline = (Get-Date).AddSeconds(30)
$healthy = $false
while ((Get-Date) -lt $deadline) {
    Start-Sleep -Seconds 2
    if ((Get-Service -Name $SERVICE_NAME).Status -ne 'Running') { continue }
    if (Test-SnmpLoopback -CommunityName $comm) { $healthy = $true; break }
}
if (-not $healthy) {
    # By default an MSI only confirms the service started, and a service that
    # started is not the same as one that answers SNMP: the "alive but dead"
    # case. A failed health check rolls the whole MSI transaction back.
    Log "FAIL the service started but did not answer a loopback SNMP query within 30 seconds"
    exit 1
}
Log "service started and passed the loopback self-test"
Log "=== configuration complete ==="
exit 0
